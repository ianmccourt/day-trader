"""The only path from a proposal to the broker. It runs the risk layer first.

`place_order` is the sole caller of `Broker.submit_order` in this repo. There is
no flag, argument, or code path that skips `evaluate` (SPEC.md constraint #3),
and tests/test_no_bypass.py fails the build if a second caller appears.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from trader.broker import Broker, BrokerError, OrderReceipt
from trader.db import (
    get_flag,
    kill_switch_engaged,
    record_decision,
    record_risk_event,
    set_flag,
)
from trader.risk.config import RiskConfig
from trader.risk.engine import Verdict, evaluate
from trader.risk.models import Proposal, RiskState
from trader.state import CycleContext

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
        return (
            f"Order accepted: {self.proposal.action} {self.proposal.qty:g} "
            f"{self.proposal.symbol} (broker id {self.receipt.order_id}, "
            f"status {self.receipt.status})."
        )


def build_proposal(
    broker: Broker, *, action: str, symbol: str, qty: float, reasoning: str = ""
) -> Proposal:
    """Price the proposal from the broker, never from the model."""
    symbol = symbol.strip().upper()
    return Proposal(
        action=action,  # type: ignore[arg-type]  # validated by proposal_sanity
        symbol=symbol,
        qty=qty,
        reference_price=broker.get_latest_price(symbol),
        reasoning=reasoning,
    )


def place_order(
    conn: sqlite3.Connection,
    broker: Broker,
    ctx: CycleContext,
    proposal: Proposal,
    config: RiskConfig,
) -> ExecutionResult:
    """Risk-check a proposal and, only if it passes every check, submit it."""
    # Re-read both from the DB rather than trusting ctx: the context was built
    # at the top of the cycle and the kill switch may have been thrown since
    # (SPEC.md: checked again before every order).
    state = RiskState.from_context(
        ctx,
        config,
        kill_switch=kill_switch_engaged(conn),
        daily_loss_halted=(get_flag(conn, daily_halt_flag(ctx.trading_day)) or "0") == "1",
    )

    verdict = evaluate(proposal, state)

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
            symbol=proposal.symbol, qty=proposal.qty, side=proposal.action
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
