"""Unit tests for every risk check: a passing case, a failing case, a boundary.

SPEC.md calls these the most important code in the repo. Each check is exercised
in isolation over hand-built state — no DB, no broker, no LLM.
"""

from __future__ import annotations

import math
from datetime import datetime

import pytest

from trader.constants import MARKET_TZ
from trader.risk import checks
from trader.risk.config import RiskConfig
from trader.risk.models import PositionState, Proposal, RiskState

NOW = datetime(2026, 9, 9, 11, 0, tzinfo=MARKET_TZ)

CONFIG = RiskConfig(
    max_position_notional=5_000.0,
    max_total_exposure=25_000.0,
    max_daily_loss=2_000.0,
    max_orders_per_hour=6,
    max_orders_per_day=20,
    symbol_allowlist=frozenset({"AAPL", "MSFT", "SPY"}),
    regular_trading_hours_only=True,
    allow_shorts=False,
)


def state(**overrides: object) -> RiskState:
    base: dict[str, object] = {
        "config": CONFIG,
        "now": NOW,
        "market_open": True,
        "kill_switch": False,
        "equity": 100_000.0,
        "start_of_day_equity": 100_000.0,
        "positions": {},
        "orders_last_hour": 0,
        "orders_today": 0,
        "daily_loss_halted": False,
    }
    base.update(overrides)
    return RiskState(**base)  # type: ignore[arg-type]


def held(symbol: str, qty: float, price: float) -> dict[str, PositionState]:
    return {symbol: PositionState(symbol=symbol, qty=qty, market_value=qty * price)}


def buy(
    symbol: str = "AAPL",
    qty: float = 10,
    price: float = 100.0,
    stop_price: float | None = None,
    take_profit_price: float | None = None,
) -> Proposal:
    return Proposal(
        action="buy",
        symbol=symbol,
        qty=qty,
        reference_price=price,
        stop_price=stop_price,
        take_profit_price=take_profit_price,
    )


def sell(
    symbol: str = "AAPL",
    qty: float = 10,
    price: float = 100.0,
    stop_price: float | None = None,
    take_profit_price: float | None = None,
) -> Proposal:
    return Proposal(
        action="sell",
        symbol=symbol,
        qty=qty,
        reference_price=price,
        stop_price=stop_price,
        take_profit_price=take_profit_price,
    )


# --- kill_switch -----------------------------------------------------------


def test_kill_switch_passes_when_disengaged() -> None:
    assert checks.kill_switch(buy(), state()) == (True, "")


def test_kill_switch_rejects_when_engaged() -> None:
    ok, reason = checks.kill_switch(buy(), state(kill_switch=True))
    assert not ok
    assert "kill switch is engaged" in reason


def test_kill_switch_ignores_every_other_field() -> None:
    """Boundary: an otherwise perfect proposal is still rejected."""
    perfect = buy(qty=1, price=1.0)
    ok, _ = checks.kill_switch(perfect, state(kill_switch=True, orders_today=0))
    assert not ok


# --- proposal_sanity -------------------------------------------------------


def test_proposal_sanity_accepts_a_well_formed_proposal() -> None:
    assert checks.proposal_sanity(buy(), state()) == (True, "")


@pytest.mark.parametrize(
    ("proposal", "fragment"),
    [
        (Proposal(action="hold", symbol="AAPL", qty=1, reference_price=1.0), "action must be"),  # type: ignore[arg-type]
        (buy(qty=-1), "qty must be positive"),
        (buy(qty=math.nan), "must be finite"),
        (buy(qty=math.inf), "must be finite"),
        (buy(price=0.0), "reference_price must be positive"),
        (buy(price=-5.0), "reference_price must be positive"),
        (buy(price=math.nan), "must be finite"),
        (buy(symbol=""), "non-empty bare ticker"),
        (buy(symbol=" AAPL "), "non-empty bare ticker"),
    ],
)
def test_proposal_sanity_rejects_malformed_proposals(proposal: Proposal, fragment: str) -> None:
    ok, reason = checks.proposal_sanity(proposal, state())
    assert not ok
    assert fragment in reason


def test_proposal_sanity_boundary_zero_qty_and_smallest_positive() -> None:
    assert checks.proposal_sanity(buy(qty=0), state())[0] is False
    assert checks.proposal_sanity(buy(qty=1e-9), state())[0] is True


# --- symbol_allowlist ------------------------------------------------------


def test_symbol_allowlist_accepts_a_listed_symbol() -> None:
    assert checks.symbol_allowlist(buy("MSFT"), state()) == (True, "")


def test_symbol_allowlist_rejects_an_unlisted_symbol() -> None:
    ok, reason = checks.symbol_allowlist(buy("DOGE"), state())
    assert not ok
    assert "not on the symbol allowlist" in reason


def test_symbol_allowlist_boundary_case_is_normalised() -> None:
    """Boundary: casing must not be a way past the list, in either direction."""
    assert checks.symbol_allowlist(buy("aapl"), state())[0] is True
    assert checks.symbol_allowlist(buy("AaPl"), state())[0] is True
    assert checks.symbol_allowlist(buy("AAPL.X"), state())[0] is False


# --- regular_trading_hours_only --------------------------------------------


def test_rth_accepts_an_open_market() -> None:
    assert checks.regular_trading_hours_only(buy(), state(market_open=True)) == (True, "")


def test_rth_rejects_a_closed_market() -> None:
    ok, reason = checks.regular_trading_hours_only(buy(), state(market_open=False))
    assert not ok
    assert "market is closed" in reason


def test_rth_boundary_can_be_disabled_by_config() -> None:
    """Boundary: the only way to trade closed is to turn the limit off in config."""
    from dataclasses import replace

    relaxed = state(market_open=False, config=replace(CONFIG, regular_trading_hours_only=False))
    assert checks.regular_trading_hours_only(buy(), relaxed) == (True, "")


# --- max_position_notional -------------------------------------------------


def test_max_position_notional_accepts_a_small_order() -> None:
    assert checks.max_position_notional(buy(qty=10, price=100.0), state()) == (True, "")


def test_max_position_notional_rejects_an_oversized_order() -> None:
    ok, reason = checks.max_position_notional(buy(qty=100, price=100.0), state())
    assert not ok
    assert "exceeds max_position_notional" in reason


def test_max_position_notional_boundary_is_inclusive() -> None:
    """$5,000 exactly is allowed; one cent more is not."""
    assert checks.max_position_notional(buy(qty=50, price=100.0), state())[0] is True
    assert checks.max_position_notional(buy(qty=50.0001, price=100.0), state())[0] is False


def test_max_position_notional_counts_the_existing_position() -> None:
    """An agent must not build an oversized position out of legal slices."""
    s = state(positions=held("AAPL", 45, 100.0))
    assert checks.max_position_notional(buy(qty=5, price=100.0), s)[0] is True
    assert checks.max_position_notional(buy(qty=6, price=100.0), s)[0] is False


def test_max_position_notional_allows_a_sell_that_reduces_an_oversized_position() -> None:
    """A position over the cap must still be reducible, or we could not unwind it."""
    s = state(positions=held("AAPL", 80, 100.0))
    assert checks.max_position_notional(sell(qty=40, price=100.0), s)[0] is True


# --- max_total_exposure ----------------------------------------------------


def test_max_total_exposure_accepts_within_budget() -> None:
    s = state(positions=held("MSFT", 100, 100.0))
    assert checks.max_total_exposure(buy("AAPL", qty=10, price=100.0), s) == (True, "")


def test_max_total_exposure_rejects_beyond_budget() -> None:
    s = state(positions=held("MSFT", 240, 100.0))
    ok, reason = checks.max_total_exposure(buy("AAPL", qty=20, price=100.0), s)
    assert not ok
    assert "exceeds max_total_exposure" in reason


def test_max_total_exposure_boundary_is_inclusive() -> None:
    s = state(positions=held("MSFT", 200, 100.0))  # $20,000 already
    assert checks.max_total_exposure(buy("AAPL", qty=50, price=100.0), s)[0] is True
    assert checks.max_total_exposure(buy("AAPL", qty=50.01, price=100.0), s)[0] is False


def test_max_total_exposure_does_not_double_count_the_traded_symbol() -> None:
    """Adding to an existing position must replace its notional, not stack on it."""
    s = state(positions=held("AAPL", 100, 100.0))  # $10,000
    ok, _ = checks.max_total_exposure(buy("AAPL", qty=10, price=100.0), s)
    assert ok  # resulting total is $11,000, not $21,000


def test_max_total_exposure_uses_the_reference_price_not_a_stale_snapshot() -> None:
    """A stale market_value must not be able to understate the result."""
    stale = {"AAPL": PositionState("AAPL", qty=100, market_value=1.0)}  # nonsense snapshot
    ok, reason = checks.max_total_exposure(
        buy("AAPL", qty=200, price=100.0), state(positions=stale)
    )
    assert not ok
    assert "$30,000.00" in reason


# --- max_daily_loss --------------------------------------------------------


def test_max_daily_loss_accepts_a_small_drawdown() -> None:
    assert checks.max_daily_loss(buy(), state(equity=99_000.0)) == (True, "")


def test_max_daily_loss_rejects_a_large_drawdown() -> None:
    ok, reason = checks.max_daily_loss(buy(), state(equity=97_000.0))
    assert not ok
    assert "exceeds max_daily_loss" in reason


def test_max_daily_loss_boundary_is_inclusive() -> None:
    assert checks.max_daily_loss(buy(), state(equity=98_000.0))[0] is True  # exactly -$2,000
    assert checks.max_daily_loss(buy(), state(equity=97_999.99))[0] is False


def test_max_daily_loss_latch_survives_an_intraday_recovery() -> None:
    """Once halted, the day stays halted even at a profit."""
    ok, reason = checks.max_daily_loss(buy(), state(equity=101_000.0, daily_loss_halted=True))
    assert not ok
    assert "halted for the day" in reason


def test_max_daily_loss_fails_closed_without_a_prior_close() -> None:
    ok, reason = checks.max_daily_loss(buy(), state(start_of_day_equity=None))
    assert not ok
    assert "prior close equity is unknown" in reason


def test_max_daily_loss_ignores_gains() -> None:
    assert checks.max_daily_loss(buy(), state(equity=1_000_000.0))[0] is True


# --- order rate limits -----------------------------------------------------


def test_max_orders_per_hour_accepts_below_the_limit() -> None:
    assert checks.max_orders_per_hour(buy(), state(orders_last_hour=5)) == (True, "")


def test_max_orders_per_hour_rejects_at_and_above_the_limit() -> None:
    """Boundary: the limit is a cap on orders placed, so the 7th is the rejected one."""
    assert checks.max_orders_per_hour(buy(), state(orders_last_hour=6))[0] is False
    assert checks.max_orders_per_hour(buy(), state(orders_last_hour=99))[0] is False


def test_max_orders_per_day_accepts_below_the_limit() -> None:
    assert checks.max_orders_per_day(buy(), state(orders_today=19)) == (True, "")


def test_max_orders_per_day_rejects_at_and_above_the_limit() -> None:
    assert checks.max_orders_per_day(buy(), state(orders_today=20))[0] is False
    ok, reason = checks.max_orders_per_day(buy(), state(orders_today=21))
    assert not ok
    assert "max_orders_per_day 20" in reason


def test_max_orders_per_cycle_accepts_below_the_limit() -> None:
    assert checks.max_orders_per_cycle(buy(), state(orders_this_cycle=0)) == (True, "")


def test_max_orders_per_cycle_rejects_at_and_above_the_limit() -> None:
    assert checks.max_orders_per_cycle(buy(), state(orders_this_cycle=1))[0] is False
    ok, reason = checks.max_orders_per_cycle(buy(), state(orders_this_cycle=2))
    assert not ok
    assert "max_orders_per_cycle 1" in reason


# --- protective exits ------------------------------------------------------


def test_protective_exits_allows_a_plain_order() -> None:
    assert checks.protective_exits(buy(), state()) == (True, "")


def test_protective_exits_rejects_a_stop_above_a_long() -> None:
    ok, reason = checks.protective_exits(buy(stop_price=110.0), state())
    assert not ok
    assert "below entry" in reason


def test_protective_exits_boundary_stop_must_be_strictly_below() -> None:
    assert checks.protective_exits(buy(stop_price=99.99), state())[0] is True
    assert checks.protective_exits(buy(stop_price=100.0), state())[0] is False


def test_protective_exits_rejects_take_profit_below_a_long() -> None:
    ok, reason = checks.protective_exits(buy(take_profit_price=90.0), state())
    assert not ok
    assert "above entry" in reason


def test_protective_exits_short_geometry() -> None:
    assert checks.protective_exits(sell(stop_price=110.0, take_profit_price=90.0), state()) == (
        True,
        "",
    )
    assert checks.protective_exits(sell(stop_price=90.0), state())[0] is False
    assert checks.protective_exits(sell(take_profit_price=110.0), state())[0] is False


def test_protective_exits_rejects_non_finite_prices() -> None:
    assert checks.protective_exits(buy(stop_price=math.nan), state())[0] is False
    assert checks.protective_exits(buy(stop_price=-1.0), state())[0] is False


# --- stop-loss vs daily budget ---------------------------------------------


def test_stop_loss_budget_allows_a_tight_stop() -> None:
    # 10 sh * $5 = $50 of stop risk, well inside the $2,000 daily cap.
    assert checks.stop_loss_budget(buy(stop_price=95.0), state()) == (True, "")


def test_stop_loss_budget_rejects_a_stop_wider_than_the_day() -> None:
    # 10 sh * $250 = $2,500 > $2,000.
    ok, reason = checks.stop_loss_budget(buy(price=300.0, stop_price=50.0), state())
    assert not ok
    assert "exceeds remaining daily-loss" in reason


def test_stop_loss_budget_shrinks_when_the_day_is_already_red() -> None:
    # Remaining budget = 2000 - 1500 = 500. 10 sh * $10 = $100, still ok.
    assert checks.stop_loss_budget(
        buy(stop_price=90.0), state(equity=98_500.0)
    )[0] is True
    # 10 sh * $60 = $600 > $500 remaining.
    ok, reason = checks.stop_loss_budget(buy(stop_price=40.0), state(equity=98_500.0))
    assert not ok
    assert "remaining daily-loss budget" in reason


def test_stop_loss_budget_skips_when_no_stop_is_set() -> None:
    assert checks.stop_loss_budget(buy(), state()) == (True, "")


# --- no_unintended_short ---------------------------------------------------


def test_no_unintended_short_allows_selling_part_of_a_holding() -> None:
    s = state(positions=held("AAPL", 10, 100.0))
    assert checks.no_unintended_short(sell(qty=4), s) == (True, "")


def test_no_unintended_short_rejects_selling_more_than_held() -> None:
    s = state(positions=held("AAPL", 10, 100.0))
    ok, reason = checks.no_unintended_short(sell(qty=1000), s)
    assert not ok
    assert "short position of -990" in reason


def test_no_unintended_short_boundary_selling_exactly_flat() -> None:
    s = state(positions=held("AAPL", 10, 100.0))
    assert checks.no_unintended_short(sell(qty=10), s)[0] is True
    assert checks.no_unintended_short(sell(qty=10.0001), s)[0] is False


def test_no_unintended_short_can_be_enabled_by_config() -> None:
    from dataclasses import replace

    s = state(config=replace(CONFIG, allow_shorts=True))
    assert checks.no_unintended_short(sell(qty=1000), s) == (True, "")


# --- the registry itself ---------------------------------------------------


def test_every_check_is_registered() -> None:
    """A check that exists but is not in CHECKS would never run."""
    defined = {
        name
        for name, obj in vars(checks).items()
        if callable(obj)
        and not name.startswith("_")
        and getattr(obj, "__module__", None) == checks.__name__
    }
    assert defined == set(checks.CHECK_NAMES)


def test_every_check_has_the_same_signature() -> None:
    for check in checks.CHECKS:
        ok, reason = check(buy(), state())
        assert isinstance(ok, bool) and isinstance(reason, str)
