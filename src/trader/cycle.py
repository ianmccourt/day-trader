"""One cycle: gate, build context, invoke the agent, persist, tear down.

The cycle row is written *before* any work happens so that a crash leaves
evidence. Everything the agent saw and produced lands in the DB; nothing is
carried in memory to the next cycle (SPEC.md architecture).
"""

from __future__ import annotations

import logging
import sqlite3
import time
import traceback
from dataclasses import dataclass

from trader.agent import Agent, AgentResult
from trader.broker import Broker, BrokerError
from trader.db import (
    close_cycle,
    kill_switch_engaged,
    open_cycle,
    record_account,
    record_decision,
    record_positions,
    utcnow,
)
from trader.execution import ExecutionResult, build_proposal, place_order
from trader.risk.config import RiskConfig
from trader.state import build_cycle_context, trading_day_for

log = logging.getLogger("trader.cycle")

# Terminal statuses written to cycles.status.
STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_SKIPPED_CLOSED = "skipped_market_closed"
STATUS_HALTED_KILL_SWITCH = "halted_kill_switch"


@dataclass(frozen=True, slots=True)
class CycleOutcome:
    cycle_id: int
    status: str
    duration_ms: int
    result: AgentResult | None = None
    execution: ExecutionResult | None = None
    error: str | None = None


def run_cycle(
    conn: sqlite3.Connection,
    broker: Broker,
    agent: Agent,
    *,
    risk_config: RiskConfig | None = None,
    force: bool = False,
) -> CycleOutcome:
    """Run exactly one cycle. Never raises: failures are logged and persisted.

    `force` bypasses only the scheduler-level market-open gate, so a dry run can
    exercise the loop outside RTH. It does not bypass the kill switch and it
    does not bypass the risk layer: `regular_trading_hours_only` still rejects
    the order downstream.
    """
    started = utcnow()
    day = trading_day_for(started)
    t0 = time.perf_counter()
    cycle_id = open_cycle(conn, started_at=started, trading_day=day)
    log.info("cycle_start", extra={"cycle_id": cycle_id, "trading_day": day})

    def finish(
        status: str,
        *,
        result: AgentResult | None = None,
        execution: ExecutionResult | None = None,
        error: str | None = None,
    ) -> CycleOutcome:
        duration_ms = int((time.perf_counter() - t0) * 1000)
        close_cycle(
            conn,
            cycle_id,
            status=status,
            duration_ms=duration_ms,
            model=result.model if result else None,
            prompt_tokens=result.prompt_tokens if result else None,
            completion_tokens=result.completion_tokens if result else None,
            full_prompt=result.full_prompt if result else None,
            full_response=result.full_response if result else None,
            tool_calls=result.tool_calls if result else None,
            error=error,
        )
        log.info(
            "cycle_end",
            extra={
                "cycle_id": cycle_id,
                "status": status,
                "duration_ms": duration_ms,
                "action": result.action if result else None,
                "error": error,
            },
        )
        return CycleOutcome(cycle_id, status, duration_ms, result, execution, error)

    # Kill switch is checked at the top of every cycle (SPEC.md risk layer).
    # Re-checked before any order in Phase 2.
    if kill_switch_engaged(conn):
        log.warning("kill_switch_engaged", extra={"cycle_id": cycle_id})
        return finish(STATUS_HALTED_KILL_SWITCH)

    try:
        ctx = build_cycle_context(conn, broker, cycle_id=cycle_id, now=started)
    except BrokerError as exc:
        # A broker timeout fails the cycle loudly rather than silently skipping.
        log.error("broker_error", extra={"cycle_id": cycle_id}, exc_info=True)
        return finish(STATUS_ERROR, error=f"{type(exc).__name__}: {exc}")

    record_account(conn, cycle_id, ctx.account)
    if ctx.positions:
        record_positions(conn, cycle_id, ctx.positions)

    if not ctx.clock.is_open and not force:
        log.info(
            "market_closed", extra={"cycle_id": cycle_id, "next_open": str(ctx.clock.next_open)}
        )
        return finish(STATUS_SKIPPED_CLOSED)

    try:
        result = agent.run(ctx)
    except Exception as exc:
        log.error("agent_error", extra={"cycle_id": cycle_id}, exc_info=True)
        return finish(STATUS_ERROR, error=f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")

    if result.executions:
        # The agent already routed its proposal through execution.place_order
        # (the tool loop needs the risk verdict as a tool result). Everything is
        # already recorded in decisions and risk_events; do not re-run it.
        return finish(STATUS_OK, result=result, execution=result.executions[-1])

    if result.action not in ("buy", "sell"):
        # A cycle that chose to do nothing is still a decision worth querying.
        record_decision(
            conn,
            cycle_id=cycle_id,
            action=result.action,
            reasoning=result.reasoning,
            outcome="no_order_proposed",
        )
        return finish(STATUS_OK, result=result)

    if risk_config is None:
        # Refuse rather than fall back to defaults: an order sized against
        # limits nobody configured is exactly the failure this layer exists
        # to prevent.
        return finish(
            STATUS_ERROR,
            result=result,
            error="agent proposed an order but no risk config was supplied to run_cycle",
        )

    try:
        proposal = build_proposal(
            broker,
            action=result.action,
            symbol=result.symbol or "",
            qty=result.qty if result.qty is not None else float("nan"),
            reasoning=result.reasoning,
        )
    except BrokerError as exc:
        log.error("pricing_failed", extra={"cycle_id": cycle_id}, exc_info=True)
        return finish(STATUS_ERROR, result=result, error=f"{type(exc).__name__}: {exc}")

    execution = place_order(conn, broker, ctx, proposal, risk_config)
    return finish(STATUS_OK, result=result, execution=execution)
