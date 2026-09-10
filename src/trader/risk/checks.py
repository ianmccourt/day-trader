"""The risk checks. Pure functions over (proposal, state) -> (bool, reason).

Every check is independent and total: it reads only its two arguments, never
short-circuits another check, and returns a reason string on failure that is
shown verbatim to the model. Adding a check means adding it to CHECKS below;
the engine runs whatever is in that tuple.

These functions are the most important code in the repo. They are tested in
tests/test_risk_checks.py with a passing, failing, and boundary case each.
"""

from __future__ import annotations

import math
from collections.abc import Callable

from trader.risk.models import Proposal, RiskState

#: (ok, reason). `reason` is empty when ok.
CheckResult = tuple[bool, str]
Check = Callable[[Proposal, RiskState], CheckResult]

OK: CheckResult = (True, "")


def _money(value: float) -> str:
    return f"${value:,.2f}"


def kill_switch(proposal: Proposal, state: RiskState) -> CheckResult:
    """Hard stop. Deliberately has no override anywhere in the codebase."""
    if state.kill_switch:
        return False, "kill switch is engaged; no orders will be placed"
    return OK


def proposal_sanity(proposal: Proposal, state: RiskState) -> CheckResult:
    """Reject structurally invalid proposals before any limit is computed.

    Not in SPEC.md's list. Added because every downstream check does arithmetic
    on qty and reference_price: a NaN qty would otherwise pass every numeric
    comparison silently and reach the broker.
    """
    if proposal.action not in ("buy", "sell"):
        return False, f"action must be 'buy' or 'sell', got {proposal.action!r}"
    if not proposal.is_finite:
        return False, (
            f"qty and reference_price must be finite, got qty={proposal.qty!r} "
            f"reference_price={proposal.reference_price!r}"
        )
    if proposal.qty <= 0:
        return False, f"qty must be positive, got {proposal.qty!r}"
    if proposal.reference_price <= 0:
        return False, f"reference_price must be positive, got {proposal.reference_price!r}"
    if not proposal.symbol or proposal.symbol != proposal.symbol.strip():
        return False, f"symbol must be a non-empty bare ticker, got {proposal.symbol!r}"
    return OK


def symbol_allowlist(proposal: Proposal, state: RiskState) -> CheckResult:
    """Explicit list; nothing else is tradeable."""
    allowed = state.config.symbol_allowlist
    if proposal.symbol.upper() not in allowed:
        return False, (
            f"{proposal.symbol!r} is not on the symbol allowlist "
            f"({', '.join(sorted(allowed))})"
        )
    return OK


def regular_trading_hours_only(proposal: Proposal, state: RiskState) -> CheckResult:
    """The broker clock is the authority — this check just reads its verdict."""
    if state.config.regular_trading_hours_only and not state.market_open:
        return False, f"market is closed at {state.now.isoformat()}; RTH-only is enabled"
    return OK


def max_position_notional(proposal: Proposal, state: RiskState) -> CheckResult:
    """Cap the *resulting* per-symbol position, not the order.

    Sizing on the order alone would let the agent build an unbounded position
    out of individually-legal slices.
    """
    limit = state.config.max_position_notional
    current = state.position(proposal.symbol)
    resulting_qty = current.qty + proposal.signed_qty
    resulting_notional = abs(resulting_qty) * proposal.reference_price
    if resulting_notional > limit:
        return False, (
            f"{proposal.symbol}: resulting position {resulting_qty:g} sh "
            f"= {_money(resulting_notional)} exceeds max_position_notional "
            f"{_money(limit)} (currently {current.qty:g} sh)"
        )
    return OK


def max_total_exposure(proposal: Proposal, state: RiskState) -> CheckResult:
    """Cap portfolio-wide absolute notional after the order fills.

    The symbol's existing notional is recomputed at the reference price rather
    than reused from the snapshot, so a stale market_value cannot understate
    the result.
    """
    limit = state.config.max_total_exposure
    current = state.position(proposal.symbol)
    others = state.total_exposure - abs(current.market_value)
    resulting_symbol_notional = abs(current.qty + proposal.signed_qty) * proposal.reference_price
    resulting_total = others + resulting_symbol_notional
    if resulting_total > limit:
        return False, (
            f"resulting total exposure {_money(resulting_total)} exceeds "
            f"max_total_exposure {_money(limit)} "
            f"(other positions {_money(others)}, {proposal.symbol} "
            f"{_money(resulting_symbol_notional)})"
        )
    return OK


def max_daily_loss(proposal: Proposal, state: RiskState) -> CheckResult:
    """Halt for the rest of the day once equity has fallen past the limit.

    The halt latches: `state.daily_loss_halted` is set from a DB flag by the
    execution layer, so an intraday recovery does not quietly re-enable trading.
    """
    limit = state.config.max_daily_loss
    if state.daily_loss_halted:
        return False, (
            f"trading halted for the day: max_daily_loss {_money(limit)} was breached earlier"
        )
    pl = state.daily_pl
    if pl is None:
        return False, "cannot evaluate max_daily_loss: prior close equity is unknown"
    if -pl > limit:
        return False, (
            f"daily loss {_money(-pl)} exceeds max_daily_loss {_money(limit)}; "
            f"halting trading for the day"
        )
    return OK


def max_orders_per_hour(proposal: Proposal, state: RiskState) -> CheckResult:
    limit = state.config.max_orders_per_hour
    if state.orders_last_hour >= limit:
        return False, (
            f"{state.orders_last_hour} orders in the last hour reaches "
            f"max_orders_per_hour {limit}"
        )
    return OK


def max_orders_per_day(proposal: Proposal, state: RiskState) -> CheckResult:
    limit = state.config.max_orders_per_day
    if state.orders_today >= limit:
        return False, (
            f"{state.orders_today} orders today reaches max_orders_per_day {limit}"
        )
    return OK


def max_orders_per_cycle(proposal: Proposal, state: RiskState) -> CheckResult:
    """Cap proposals that already reached the broker this cycle.

    The tool layer also refuses extra *attempts*, including rejections. This
    check is the backstop for anything that calls place_order directly.
    """
    limit = state.config.max_orders_per_cycle
    if state.orders_this_cycle >= limit:
        return False, (
            f"{state.orders_this_cycle} orders this cycle reaches "
            f"max_orders_per_cycle {limit}"
        )
    return OK


def protective_exits(proposal: Proposal, state: RiskState) -> CheckResult:
    """Stop and take-profit must be on the correct side of the entry.

    A buy-stop above a long, or a take-profit below it, would fire immediately
    or protect nothing.     Missing exits are allowed by this check (the playbook may still require
    them) — but a present one has to be geometrically valid.
    """
    stop = proposal.stop_price
    take_profit = proposal.take_profit_price
    if stop is None and take_profit is None:
        return OK
    for name, value in (("stop_price", stop), ("take_profit_price", take_profit)):
        if value is None:
            continue
        if not math.isfinite(value) or value <= 0:
            return False, f"{name} must be a positive finite price, got {value!r}"
    ref = proposal.reference_price
    if not math.isfinite(ref) or ref <= 0:
        return OK  # proposal_sanity reports the real problem
    if proposal.action == "buy":
        if stop is not None and stop >= ref:
            return False, (
                f"stop_price {_money(stop)} must be below entry {_money(ref)} for a long"
            )
        if take_profit is not None and take_profit <= ref:
            return False, (
                f"take_profit_price {_money(take_profit)} must be above entry "
                f"{_money(ref)} for a long"
            )
    else:
        if stop is not None and stop <= ref:
            return False, (
                f"stop_price {_money(stop)} must be above entry {_money(ref)} for a short"
            )
        if take_profit is not None and take_profit >= ref:
            return False, (
                f"take_profit_price {_money(take_profit)} must be below entry "
                f"{_money(ref)} for a short"
            )
    return OK


def stop_loss_budget(proposal: Proposal, state: RiskState) -> CheckResult:
    """A stop that would lose more than the remaining daily budget is rejected.

    Without this, a $25k 3x-ETF position with a 50% stop could blow past
    max_daily_loss between cycles while the protective order sat at the broker.
    No stop means this check does not fire; max_daily_loss still latches later.
    """
    if proposal.stop_price is None:
        return OK
    if not proposal.is_finite or not math.isfinite(proposal.stop_price):
        return OK
    potential = abs(proposal.reference_price - proposal.stop_price) * proposal.qty
    limit = state.config.max_daily_loss
    pl = state.daily_pl
    if pl is None:
        return OK  # max_daily_loss already fails closed
    remaining = limit + min(pl, 0.0)
    if remaining <= 0:
        return False, (
            f"no remaining daily-loss budget to underwrite a stop "
            f"(max_daily_loss {_money(limit)}, daily P/L {_money(pl)})"
        )
    if potential > remaining:
        return False, (
            f"stop-loss risk {_money(potential)} exceeds remaining daily-loss "
            f"budget {_money(remaining)} (max_daily_loss {_money(limit)}, "
            f"daily P/L {_money(pl)})"
        )
    return OK


def no_unintended_short(proposal: Proposal, state: RiskState) -> CheckResult:
    """Reject any order that opens or increases a short position.

    Not in SPEC.md's list. Added because 'sell 1000' against a 10-share holding
    is a 990-share naked short that only max_position_notional would catch, and
    only by accident. Disable with [session].allow_shorts if that is wanted.
    """
    if state.config.allow_shorts:
        return OK
    current = state.position(proposal.symbol)
    resulting_qty = current.qty + proposal.signed_qty
    if resulting_qty < 0:
        return False, (
            f"{proposal.symbol}: selling {proposal.qty:g} against {current.qty:g} held "
            f"would leave a short position of {resulting_qty:g} sh; shorts are disabled"
        )
    return OK


#: Evaluation order. The engine runs all of them regardless of earlier failures,
#: so a rejection tells the model everything that is wrong, not just the first
#: thing. Order matters only for how the reasons are presented.
CHECKS: tuple[Check, ...] = (
    kill_switch,
    proposal_sanity,
    symbol_allowlist,
    regular_trading_hours_only,
    no_unintended_short,
    max_position_notional,
    max_total_exposure,
    max_daily_loss,
    max_orders_per_hour,
    max_orders_per_day,
    max_orders_per_cycle,
    protective_exits,
    stop_loss_budget,
)

CHECK_NAMES: tuple[str, ...] = tuple(c.__name__ for c in CHECKS)
