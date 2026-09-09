"""The engine runs every check, collects every failure, and fails closed."""

from __future__ import annotations

from datetime import datetime

from trader.constants import MARKET_TZ
from trader.risk.checks import CHECKS
from trader.risk.config import RiskConfig
from trader.risk.engine import evaluate
from trader.risk.models import Proposal, RiskState

CONFIG = RiskConfig(
    max_position_notional=5_000.0,
    max_total_exposure=25_000.0,
    max_daily_loss=2_000.0,
    max_orders_per_hour=6,
    max_orders_per_day=20,
    symbol_allowlist=frozenset({"AAPL"}),
)


def state(**overrides: object) -> RiskState:
    base: dict[str, object] = {
        "config": CONFIG,
        "now": datetime(2026, 9, 9, 11, 0, tzinfo=MARKET_TZ),
        "market_open": True,
        "kill_switch": False,
        "equity": 100_000.0,
        "start_of_day_equity": 100_000.0,
    }
    base.update(overrides)
    return RiskState(**base)  # type: ignore[arg-type]


GOOD = Proposal(action="buy", symbol="AAPL", qty=10, reference_price=100.0)


def test_a_clean_proposal_is_approved() -> None:
    verdict = evaluate(GOOD, state())
    assert verdict.approved
    assert verdict.failures == ()
    assert verdict.summary == "approved"


def test_all_failures_are_reported_not_just_the_first() -> None:
    """A rejection should tell the model everything wrong, in one round trip."""
    awful = Proposal(action="buy", symbol="DOGE", qty=1e6, reference_price=100.0)
    verdict = evaluate(awful, state(kill_switch=True, market_open=False, orders_today=999))
    fired = {f.check for f in verdict.failures}
    assert fired >= {
        "kill_switch",
        "symbol_allowlist",
        "regular_trading_hours_only",
        "max_position_notional",
        "max_total_exposure",
        "max_orders_per_day",
    }
    assert verdict.summary.startswith("rejected:")
    assert verdict.summary.count(",") == len(verdict.failures) - 1


def test_every_check_actually_runs() -> None:
    calls: list[str] = []

    def spy(name: str):
        def check(proposal, s):
            calls.append(name)
            return True, ""

        check.__name__ = name
        return check

    evaluate(GOOD, state(), checks=tuple(spy(f"c{i}") for i in range(5)))
    assert calls == ["c0", "c1", "c2", "c3", "c4"]


def test_a_raising_check_fails_closed(caplog) -> None:
    def exploding(proposal, s):
        raise ZeroDivisionError("bad limit arithmetic")

    exploding.__name__ = "exploding"
    verdict = evaluate(GOOD, state(), checks=(*CHECKS, exploding))
    assert not verdict.approved
    failure = next(f for f in verdict.failures if f.check == "exploding")
    assert "ZeroDivisionError" in failure.reason


def test_rejection_message_is_legible_to_the_model() -> None:
    verdict = evaluate(GOOD, state(kill_switch=True))
    message = verdict.as_model_message()
    assert "REJECTED" in message
    assert "Nothing was sent to the broker" in message
    assert "kill_switch: kill switch is engaged" in message


def test_evaluation_does_not_mutate_its_inputs() -> None:
    s = state()
    before = (s.equity, dict(s.positions), s.orders_today)
    evaluate(GOOD, s)
    assert (s.equity, dict(s.positions), s.orders_today) == before
