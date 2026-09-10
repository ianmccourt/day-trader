"""A deliberately malicious set of proposals, run through the real execution path.

SPEC.md Phase 2's acceptance criterion: oversized, off-allowlist, after-hours and
rapid-fire proposals are rejected every time. These go through
`execution.place_order` against a fake broker and a real SQLite file, so they
exercise the persistence and latching too — not just the predicates.
"""

from __future__ import annotations

import math
from dataclasses import replace

import pytest

from tests.fakes import FakeBroker
from trader.db import KILL_SWITCH, connect, open_cycle, set_flag, utcnow
from trader.execution import build_proposal, daily_halt_flag, place_order
from trader.risk.config import RiskConfig
from trader.risk.models import Proposal
from trader.state import build_cycle_context, trading_day_for

CONFIG = RiskConfig(
    max_position_notional=5_000.0,
    max_total_exposure=25_000.0,
    max_daily_loss=2_000.0,
    max_orders_per_hour=6,
    max_orders_per_day=20,
    symbol_allowlist=frozenset({"AAPL", "MSFT", "SPY"}),
)


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path / "t.sqlite3")


@pytest.fixture
def broker():
    return FakeBroker(prices={"AAPL": 100.0, "MSFT": 100.0, "SPY": 100.0, "DOGE": 1.0})


def ctx_for(conn, broker, **_):
    cycle_id = open_cycle(conn, started_at=utcnow(), trading_day=trading_day_for(utcnow()))
    return build_cycle_context(conn, broker, cycle_id=cycle_id)


def p(
    action: str,
    symbol: str,
    qty: float,
    price: float = 100.0,
    stop_price: float | None = None,
) -> Proposal:
    return Proposal(
        action=action,  # type: ignore[arg-type]
        symbol=symbol,
        qty=qty,
        reference_price=price,
        stop_price=stop_price,
    )


# Each entry: (label, proposal, the check that must fire).
MALICIOUS: list[tuple[str, Proposal, str]] = [
    ("oversized single order", p("buy", "AAPL", 1_000), "max_position_notional"),
    ("absurdly oversized", p("buy", "AAPL", 1e9), "max_position_notional"),
    ("off-allowlist meme", p("buy", "DOGE", 1, 1.0), "symbol_allowlist"),
    ("off-allowlist via lowercase", p("buy", "doge", 1, 1.0), "symbol_allowlist"),
    ("negative quantity", p("buy", "AAPL", -50), "proposal_sanity"),
    ("zero quantity", p("buy", "AAPL", 0), "proposal_sanity"),
    ("NaN quantity", p("buy", "AAPL", math.nan), "proposal_sanity"),
    ("infinite quantity", p("buy", "AAPL", math.inf), "proposal_sanity"),
    ("free shares via zero price", p("buy", "AAPL", 1_000_000, 0.0), "proposal_sanity"),
    ("negative price", p("buy", "AAPL", 10, -100.0), "proposal_sanity"),
    ("naked short", p("sell", "AAPL", 500), "no_unintended_short"),
    ("empty symbol", p("buy", "", 1), "proposal_sanity"),
    ("padded symbol", p("buy", " AAPL ", 1), "proposal_sanity"),
    ("stop above a long", p("buy", "AAPL", 10, stop_price=110.0), "protective_exits"),
]


@pytest.mark.parametrize(
    ("label", "proposal", "expected_check"), MALICIOUS, ids=[m[0] for m in MALICIOUS]
)
def test_malicious_proposal_is_rejected(conn, broker, label, proposal, expected_check) -> None:
    ctx = ctx_for(conn, broker)
    result = place_order(conn, broker, ctx, proposal, CONFIG)

    assert not result.verdict.approved, f"{label} was approved"
    assert not result.executed
    assert broker.submitted == [], f"{label} reached the broker"
    assert expected_check in {f.check for f in result.verdict.failures}

    # The rejection is persisted with the check that fired and the proposal.
    events = conn.execute("SELECT check_name, proposal FROM risk_events").fetchall()
    assert expected_check in {e["check_name"] for e in events}
    decision = conn.execute("SELECT * FROM decisions").fetchone()
    assert decision["risk_result"].startswith("rejected:")
    assert decision["broker_order_id"] is None
    # And it is legible to the model.
    assert "REJECTED" in result.as_model_message()


def test_after_hours_orders_are_rejected(conn, broker) -> None:
    broker.is_open = False
    ctx = ctx_for(conn, broker)
    result = place_order(conn, broker, ctx, p("buy", "AAPL", 10), CONFIG)
    assert "regular_trading_hours_only" in {f.check for f in result.verdict.failures}
    assert broker.submitted == []


def test_rapid_fire_stops_at_the_hourly_limit(conn, broker) -> None:
    """Twenty attempts in a row must yield exactly max_orders_per_hour fills."""
    for _ in range(20):
        ctx = ctx_for(conn, broker)
        place_order(conn, broker, ctx, p("buy", "AAPL", 1), CONFIG)

    assert len(broker.submitted) == CONFIG.max_orders_per_hour
    rejected = conn.execute(
        "SELECT COUNT(*) FROM risk_events WHERE check_name = 'max_orders_per_hour'"
    ).fetchone()[0]
    assert rejected == 20 - CONFIG.max_orders_per_hour


def test_daily_order_cap_binds_when_the_hourly_cap_does_not(conn, broker) -> None:
    generous = replace(CONFIG, max_orders_per_hour=1_000, max_orders_per_day=3)
    for _ in range(10):
        ctx = ctx_for(conn, broker)
        place_order(conn, broker, ctx, p("buy", "AAPL", 1), generous)
    assert len(broker.submitted) == 3


def test_salami_slicing_cannot_exceed_the_position_cap(conn, broker) -> None:
    """Individually legal orders must not add up to an oversized position."""
    generous = replace(CONFIG, max_orders_per_hour=1_000, max_orders_per_day=1_000)
    filled = 0
    for _ in range(20):
        ctx = ctx_for(conn, broker)
        result = place_order(conn, broker, ctx, p("buy", "AAPL", 10), generous)
        if result.executed:
            filled += 10
    assert filled == 50  # $5,000 at $100, exactly max_position_notional
    assert len(broker.submitted) == 5


def test_salami_inside_one_cycle_is_still_capped(conn, broker) -> None:
    """A multi-order cycle must re-read the book between proposals."""
    generous = replace(
        CONFIG,
        max_orders_per_hour=1_000,
        max_orders_per_day=1_000,
        max_orders_per_cycle=20,
    )
    ctx = ctx_for(conn, broker)
    filled = 0
    for _ in range(20):
        result = place_order(conn, broker, ctx, p("buy", "AAPL", 10), generous)
        if result.executed:
            filled += 10
    assert filled == 50
    assert len(broker.submitted) == 5


def test_an_approved_order_can_carry_a_broker_stop(conn, broker) -> None:
    ctx = ctx_for(conn, broker)
    result = place_order(conn, broker, ctx, p("buy", "AAPL", 10, stop_price=95.0), CONFIG)
    assert result.executed
    assert broker.submitted == [
        {"symbol": "AAPL", "qty": 10.0, "side": "buy", "stop_price": 95.0}
    ]
    assert "stop=95" in result.as_model_message()


def test_kill_switch_thrown_mid_cycle_still_blocks_the_order(conn, broker) -> None:
    """The context was built before the switch was thrown; the order must still fail."""
    ctx = ctx_for(conn, broker)
    assert ctx.kill_switch is False
    set_flag(conn, KILL_SWITCH, "1", note="thrown between context build and order")
    result = place_order(conn, broker, ctx, p("buy", "AAPL", 1), CONFIG)
    assert "kill_switch" in {f.check for f in result.verdict.failures}
    assert broker.submitted == []


def test_daily_loss_halt_latches_for_the_rest_of_the_day(conn, broker) -> None:
    broker.equity, broker.last_equity = 97_000.0, 100_000.0
    ctx = ctx_for(conn, broker)
    first = place_order(conn, broker, ctx, p("buy", "AAPL", 1), CONFIG)
    assert "max_daily_loss" in {f.check for f in first.verdict.failures}

    day = trading_day_for(utcnow())
    from trader.db import get_flag

    assert get_flag(conn, daily_halt_flag(day)) == "1"

    # Equity fully recovers; trading must stay halted.
    broker.equity = 105_000.0
    ctx = ctx_for(conn, broker)
    second = place_order(conn, broker, ctx, p("buy", "AAPL", 1), CONFIG)
    assert "max_daily_loss" in {f.check for f in second.verdict.failures}
    assert broker.submitted == []


def test_a_legitimate_order_still_gets_through(conn, broker) -> None:
    """The battery above is worthless if the layer just rejects everything."""
    ctx = ctx_for(conn, broker)
    result = place_order(conn, broker, ctx, p("buy", "AAPL", 10), CONFIG)
    assert result.verdict.approved and result.executed
    assert broker.submitted == [{"symbol": "AAPL", "qty": 10, "side": "buy"}]
    decision = conn.execute("SELECT * FROM decisions").fetchone()
    assert decision["risk_result"] == "approved"
    assert decision["broker_order_id"] == "fake-order-1"
    assert conn.execute("SELECT COUNT(*) FROM risk_events").fetchone()[0] == 0
    assert "Order accepted" in result.as_model_message()


def test_price_comes_from_the_broker_not_the_proposal(conn, broker) -> None:
    """A model that lies about price must not be able to understate notional."""
    broker.prices["AAPL"] = 1_000.0
    proposal = build_proposal(broker, action="buy", symbol="AAPL", qty=100)
    assert proposal.reference_price == 1_000.0
    ctx = ctx_for(conn, broker)
    result = place_order(conn, broker, ctx, proposal, CONFIG)
    assert "max_position_notional" in {f.check for f in result.verdict.failures}


def test_broker_failure_after_approval_is_recorded_not_swallowed(conn, broker) -> None:
    ctx = ctx_for(conn, broker)
    broker.fail_on = {"submit_order"}
    result = place_order(conn, broker, ctx, p("buy", "AAPL", 10), CONFIG)
    assert result.verdict.approved and not result.executed
    assert "simulated submit_order failure" in (result.broker_error or "")
    decision = conn.execute("SELECT * FROM decisions").fetchone()
    assert decision["outcome"].startswith("broker_error:")
    # No order id, so it does not consume rate-limit budget.
    assert decision["broker_order_id"] is None
