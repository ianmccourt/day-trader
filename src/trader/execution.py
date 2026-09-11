"""The only path from a proposal to the broker. It runs the risk layer first.

`place_order` is the sole caller of `Broker.submit_order` in this repo. There is
no flag, argument, or code path that skips `evaluate` (SPEC.md constraint #3),
and tests/test_no_bypass.py fails the build if a second caller appears.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

from trader.broker import Broker, BrokerError, OrderReceipt
from trader.db import (
    get_flag,
    kill_switch_engaged,
    record_decision,
    record_fill,
    record_risk_event,
    set_flag,
    unreconciled_orders,
)
from trader.risk.config import RiskConfig
from trader.risk.engine import Verdict, evaluate
from trader.risk.models import Proposal, RiskState
from trader.state import CycleContext, refresh_cycle_context

log = logging.getLogger("trader.execution")


def daily_halt_flag(trading_day: str) -> str:
    """One latch per trading day, so it clears itself at the next session."""
    return f"daily_loss_halt:{trading_day}"


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """What happened to one proposal. Returned to the model as a tool result."""

    proposal: Proposal
    verdict: Verdict
    receipt: OrderReceipt | None = None
    broker_error: str | None = None
    decision_id: int | None = None

    @property
    def executed(self) -> bool:
        return self.receipt is not None

    def as_model_message(self) -> str:
        if self.broker_error:
            return (
                f"Order passed the risk layer but the broker rejected it: "
                f"{self.broker_error}"
            )
        if not self.verdict.approved:
            return self.verdict.as_model_message()
        assert self.receipt is not None
        extras: list[str] = []
        if self.proposal.stop_price is not None:
            extras.append(f"stop={self.proposal.stop_price:g}")
        if self.proposal.take_profit_price is not None:
            extras.append(f"take_profit={self.proposal.take_profit_price:g}")
        attached = f" ({', '.join(extras)})" if extras else ""
        return (
            f"Order accepted: {self.proposal.action} {self.proposal.qty:g} "
            f"{self.proposal.symbol}{attached} (broker id {self.receipt.order_id}, "
            f"status {self.receipt.status})."
        )


def build_proposal(
    broker: Broker,
    *,
    action: str,
    symbol: str,
    qty: float,
    reasoning: str = "",
    stop_price: float | None = None,
    take_profit_price: float | None = None,
) -> Proposal:
    """Price the proposal from the broker, never from the model."""
    symbol = symbol.strip().upper()
    return Proposal(
        action=action,  # type: ignore[arg-type]  # validated by proposal_sanity
        symbol=symbol,
        qty=qty,
        reference_price=broker.get_latest_price(symbol),
        reasoning=reasoning,
        stop_price=stop_price,
        take_profit_price=take_profit_price,
    )


def _reduces_position(*, ctx: CycleContext, proposal: Proposal) -> bool:
    """True when the order shrinks, flattens, or reverses an existing position."""
    held = ctx.position_for(proposal.symbol)
    if held is None:
        return False
    held_qty = float(held.get("qty") or 0.0)
    signed = proposal.qty if proposal.action == "buy" else -proposal.qty
    return held_qty * signed < 0


def reconcile_fills(
    conn: sqlite3.Connection, broker: Broker, *, limit: int = 200
) -> list[dict[str, Any]]:
    """Read submitted orders back from the broker and record terminal fills.

    Returns one entry per pending order: status "reconciled" for a recorded
    terminal fill, "open" for an order still working (left NULL for a later
    run), "error" if the broker read failed. Called automatically at the top
    of every cycle and by `trader reconcile`.
    """
    results: list[dict[str, Any]] = []
    for row in unreconciled_orders(conn, limit=limit):
        entry: dict[str, Any] = {"symbol": row["symbol"], "order_id": row["broker_order_id"]}
        try:
            fill = broker.get_order(row["broker_order_id"])
        except BrokerError as exc:
            log.warning(
                "reconcile_read_failed",
                extra={"order_id": row["broker_order_id"]},
                exc_info=True,
            )
            results.append(entry | {"status": "error", "error": str(exc)})
            continue
        if not fill.is_terminal:
            results.append(entry | {"status": "open"})
            continue
        record_fill(
            conn,
            row["id"],
            final_status=fill.status,
            filled_qty=fill.filled_qty,
            filled_avg_price=fill.filled_avg_price,
            filled_at=fill.filled_at.isoformat() if fill.filled_at else None,
        )
        results.append(
            entry
            | {
                "status": "reconciled",
                "final_status": fill.status,
                "filled_qty": fill.filled_qty,
                "filled_avg_price": fill.filled_avg_price,
            }
        )
    return results


def place_order(
    conn: sqlite3.Connection,
    broker: Broker,
    ctx: CycleContext,
    proposal: Proposal,
    config: RiskConfig,
) -> ExecutionResult:
    """Risk-check a proposal and, only if it passes every check, submit it."""
    # Re-read from the DB and the broker rather than trusting ctx: the context
    # was built at the top of the cycle, the kill switch may have been thrown
    # since (SPEC.md: checked again before every order), and a prior order in
    # this cycle may have changed the book.
    live = refresh_cycle_context(conn, broker, ctx)
    state = RiskState.from_context(
        live,
        config,
        kill_switch=kill_switch_engaged(conn),
        daily_loss_halted=(get_flag(conn, daily_halt_flag(ctx.trading_day)) or "0") == "1",
    )

    verdict = evaluate(proposal, state)

    # An approved order that reduces or flattens a position must first cancel
    # that symbol's resting exits: the broker reserves shares held by bracket
    # legs, so the reducing order would otherwise bounce with "insufficient
    # qty available". Never done for orders that open or add — their exits
    # should keep resting. Runs only after the risk verdict, so a rejected
    # proposal cannot strip a position of its protection.
    if verdict.approved and _reduces_position(ctx=live, proposal=proposal):
        try:
            canceled = broker.cancel_open_orders(proposal.symbol)
            if canceled:
                log.info(
                    "canceled_resting_orders",
                    extra={
                        "cycle_id": ctx.cycle_id,
                        "symbol": proposal.symbol,
                        "count": canceled,
                    },
                )
        except BrokerError:
            # Proceed: the broker may still accept the order, and if it
            # rejects it the position keeps its resting exits.
            log.warning(
                "cancel_resting_orders_failed",
                extra={"cycle_id": ctx.cycle_id, "symbol": proposal.symbol},
                exc_info=True,
            )

    for failure in verdict.failures:
        record_risk_event(
            conn,
            cycle_id=ctx.cycle_id,
            check_name=failure.check,
            reason=failure.reason,
            proposal=proposal.as_dict(),
        )
        # Latch the daily halt the moment the loss check fires, so a later
        # intraday recovery cannot silently re-enable trading.
        if failure.check == "max_daily_loss" and not state.daily_loss_halted:
            set_flag(conn, daily_halt_flag(ctx.trading_day), "1", note=failure.reason)

    if not verdict.approved:
        log.warning(
            "order_rejected",
            extra={
                "cycle_id": ctx.cycle_id,
                "symbol": proposal.symbol,
                "checks": [f.check for f in verdict.failures],
            },
        )
        decision_id = record_decision(
            conn,
            cycle_id=ctx.cycle_id,
            action=proposal.action,
            symbol=proposal.symbol,
            qty=proposal.qty,
            reasoning=proposal.reasoning,
            risk_result=verdict.summary,
            broker_order_id=None,
            outcome="rejected",
            reference_price=proposal.reference_price,
        )
        return ExecutionResult(proposal, verdict, decision_id=decision_id)

    try:
        receipt = broker.submit_order(
            symbol=proposal.symbol,
            qty=proposal.qty,
            side=proposal.action,
            stop_price=proposal.stop_price,
            take_profit_price=proposal.take_profit_price,
        )
    except BrokerError as exc:
        # Approved but undeliverable. Recorded without an order id so the rate
        # limiters do not count it as a submitted order.
        log.error("broker_submit_failed", extra={"cycle_id": ctx.cycle_id}, exc_info=True)
        decision_id = record_decision(
            conn,
            cycle_id=ctx.cycle_id,
            action=proposal.action,
            symbol=proposal.symbol,
            qty=proposal.qty,
            reasoning=proposal.reasoning,
            risk_result=verdict.summary,
            broker_order_id=None,
            outcome=f"broker_error: {exc}",
            reference_price=proposal.reference_price,
        )
        return ExecutionResult(proposal, verdict, broker_error=str(exc), decision_id=decision_id)

    log.info(
        "order_submitted",
        extra={
            "cycle_id": ctx.cycle_id,
            "symbol": proposal.symbol,
            "qty": proposal.qty,
            "side": proposal.action,
            "order_id": receipt.order_id,
        },
    )
    decision_id = record_decision(
        conn,
        cycle_id=ctx.cycle_id,
        action=proposal.action,
        symbol=proposal.symbol,
        qty=proposal.qty,
        reasoning=proposal.reasoning,
        risk_result=verdict.summary,
        broker_order_id=receipt.order_id,
        outcome=receipt.status,
        reference_price=proposal.reference_price,
    )
    return ExecutionResult(proposal, verdict, receipt=receipt, decision_id=decision_id)
