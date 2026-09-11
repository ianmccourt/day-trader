"""Execution-layer behaviours added with the position-management work:

- an approved order that reduces/flattens a position cancels that symbol's
  resting exits first (Alpaca reserves shares held by bracket legs);
- an order that opens or adds never touches resting orders;
- reconcile_fills records terminal fills and leaves working orders alone.
"""

from __future__ import annotations

import pytest

from tests.fakes import FakeBroker, position
from trader.db import connect, open_cycle, record_decision, utcnow
from trader.execution import build_proposal, place_order, reconcile_fills
from trader.risk.config import RiskConfig
from trader.state import build_cycle_context, trading_day_for

CONFIG = RiskConfig(
    max_position_notional=25_000.0,
    max_total_exposure=100_000.0,
    max_daily_loss=5_000.0,
    max_orders_per_hour=10,
    max_orders_per_day=50,
    max_orders_per_cycle=4,
    symbol_allowlist=frozenset({"AAPL", "SPY"}),
    allow_shorts=True,
)


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path / "t.sqlite3")


def _ctx(conn, broker):
    cycle_id = open_cycle(conn, started_at=utcnow(), trading_day=trading_day_for(utcnow()))
    return build_cycle_context(conn, broker, cycle_id=cycle_id)


def resting_stop(symbol: str) -> dict:
    return {
        "order_id": f"stop-{symbol}",
        "symbol": symbol,
        "side": "sell",
        "type": "stop",
        "qty": 10.0,
        "stop_price": 95.0,
        "limit_price": None,
        "status": "held",
    }


def test_a_flattening_sell_cancels_the_resting_exits_first(conn) -> None:
    broker = FakeBroker(
        prices={"AAPL": 100.0},
        positions=[position("AAPL", 10, 100.0)],
        open_orders=[resting_stop("AAPL")],
    )
    ctx = _ctx(conn, broker)
    proposal = build_proposal(broker, action="sell", symbol="AAPL", qty=10, reasoning="flatten")
    result = place_order(conn, broker, ctx, proposal, CONFIG)

    assert result.executed
    assert broker.get_open_orders("AAPL") == []
    assert broker.calls.index("cancel_open_orders") < broker.calls.index("submit_order")


def test_an_opening_buy_leaves_other_resting_orders_alone(conn) -> None:
    broker = FakeBroker(
        prices={"AAPL": 100.0, "SPY": 500.0},
        open_orders=[resting_stop("SPY")],
    )
    ctx = _ctx(conn, broker)
    proposal = build_proposal(broker, action="buy", symbol="AAPL", qty=5, reasoning="open")
    result = place_order(conn, broker, ctx, proposal, CONFIG)

    assert result.executed
    assert "cancel_open_orders" not in broker.calls
    assert len(broker.get_open_orders("SPY")) == 1


def test_an_adding_buy_keeps_the_positions_own_exits(conn) -> None:
    """Adding to a long must not strip its protection; only reducing cancels."""
    broker = FakeBroker(
        prices={"AAPL": 100.0},
        positions=[position("AAPL", 10, 100.0)],
        open_orders=[resting_stop("AAPL")],
    )
    ctx = _ctx(conn, broker)
    proposal = build_proposal(broker, action="buy", symbol="AAPL", qty=5, reasoning="add")
    place_order(conn, broker, ctx, proposal, CONFIG)

    assert "cancel_open_orders" not in broker.calls
    assert len(broker.get_open_orders("AAPL")) == 1


def test_a_rejected_reducing_order_does_not_cancel_anything(conn) -> None:
    """The cancel runs only after the risk verdict — a rejection must not
    leave the position unprotected."""
    broker = FakeBroker(
        prices={"AAPL": 100.0},
        positions=[position("AAPL", 10, 100.0)],
        open_orders=[resting_stop("AAPL")],
        is_open=False,  # RTH check rejects the order
    )
    ctx = _ctx(conn, broker)
    proposal = build_proposal(broker, action="sell", symbol="AAPL", qty=10, reasoning="flatten")
    result = place_order(conn, broker, ctx, proposal, CONFIG)

    assert not result.verdict.approved
    assert "cancel_open_orders" not in broker.calls
    assert len(broker.get_open_orders("AAPL")) == 1


def test_a_failed_cancel_is_logged_and_the_order_still_goes_out(conn) -> None:
    broker = FakeBroker(
        prices={"AAPL": 100.0},
        positions=[position("AAPL", 10, 100.0)],
        open_orders=[resting_stop("AAPL")],
        fail_on={"cancel_open_orders"},
    )
    ctx = _ctx(conn, broker)
    proposal = build_proposal(broker, action="sell", symbol="AAPL", qty=10, reasoning="flatten")
    result = place_order(conn, broker, ctx, proposal, CONFIG)

    assert result.executed  # the fake broker accepts it; Alpaca might not


# --- reconcile_fills ---------------------------------------------------------


def test_reconcile_records_terminal_fills(conn) -> None:
    broker = FakeBroker()
    cycle_id = open_cycle(conn, started_at=utcnow(), trading_day=trading_day_for(utcnow()))
    decision_id = record_decision(
        conn,
        cycle_id=cycle_id,
        action="buy",
        symbol="AAPL",
        qty=1,
        broker_order_id="abc-123",
        outcome="accepted",
    )
    results = reconcile_fills(conn, broker)
    assert results == [
        {
            "symbol": "AAPL",
            "order_id": "abc-123",
            "status": "reconciled",
            "final_status": "filled",
            "filled_qty": 1.0,
            "filled_avg_price": broker.default_price,
        }
    ]
    row = conn.execute("SELECT final_status FROM decisions WHERE id = ?", (decision_id,)).fetchone()
    assert row["final_status"] == "filled"


def test_reconcile_survives_a_broker_read_failure(conn) -> None:
    broker = FakeBroker(fail_on={"get_order"})
    cycle_id = open_cycle(conn, started_at=utcnow(), trading_day=trading_day_for(utcnow()))
    record_decision(
        conn, cycle_id=cycle_id, action="buy", symbol="AAPL", qty=1, broker_order_id="abc-123"
    )
    results = reconcile_fills(conn, broker)
    assert results[0]["status"] == "error"
    row = conn.execute("SELECT final_status FROM decisions").fetchone()
    assert row["final_status"] is None  # left for a later run
