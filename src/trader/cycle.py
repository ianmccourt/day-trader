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

from datetime import datetime

from trader.agent import Agent, AgentResult
from trader.alerts import AlertSink, update_heartbeat
from trader.broker import Broker, BrokerError
from trader.constants import MARKET_TZ, NO_NEW_ENTRIES_AFTER_ET
from trader.db import (
    close_cycle,
    close_orphan_theses,
    kill_switch_engaged,
    open_cycle,
    record_account,
    record_decision,
    record_positions,
    utcnow,
)
from trader.execution import ExecutionResult, build_proposal, place_order, reconcile_fills
from trader.risk.config import RiskConfig
from trader.state import build_cycle_context, trading_day_for

log = logging.getLogger("trader.cycle")

# Terminal statuses written to cycles.status.
STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_SKIPPED_CLOSED = "skipped_market_closed"
STATUS_SKIPPED_NO_ENTRY = "skipped_no_entry_window"
STATUS_HALTED_KILL_SWITCH = "halted_kill_switch"


def _past_entry_window(clock_now: datetime) -> bool:
    """True once the exchange-local clock passes NO_NEW_ENTRIES_AFTER_ET.

    Takes the *broker's* clock timestamp, not the host's: the broker clock is
    already the authority for the market-open gate, and it is the injectable
    one in tests.
    """
    local = clock_now.astimezone(MARKET_TZ)
    return (local.hour, local.minute) >= NO_NEW_ENTRIES_AFTER_ET


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
    alert_sink: AlertSink | None = None,
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
        
        # Update heartbeat and check for alerts
        if alert_sink:
            update_heartbeat()
            if status == "error":
                alert_sink.check_consecutive_errors(conn)
        
        return CycleOutcome(cycle_id, status, duration_ms, result, execution, error)

    # Kill switch is checked at the top of every cycle (SPEC.md risk layer).
    # Re-checked before any order in Phase 2.
    if kill_switch_engaged(conn):
        log.warning("kill_switch_engaged", extra={"cycle_id": cycle_id})
        return finish(STATUS_HALTED_KILL_SWITCH)

    # Read yesterday's (and this morning's) fills back before doing anything
    # else, so the evaluation data stays complete without a manual
    # `trader reconcile`. Advisory: a failed read is logged per order and the
    # cycle proceeds.
    reconciled = [
        r for r in reconcile_fills(conn, broker, limit=50) if r["status"] == "reconciled"
    ]
    if reconciled:
        log.info(
            "auto_reconciled_fills", extra={"cycle_id": cycle_id, "count": len(reconciled)}
        )

    try:
        ctx = build_cycle_context(
            conn,
            broker,
            cycle_id=cycle_id,
            now=started,
            scan_symbols=sorted(risk_config.symbol_allowlist) if risk_config else None,
        )
    except BrokerError as exc:
        # A broker timeout fails the cycle loudly rather than silently skipping.
        log.error("broker_error", extra={"cycle_id": cycle_id}, exc_info=True)
        return finish(STATUS_ERROR, error=f"{type(exc).__name__}: {exc}")

    record_account(conn, cycle_id, ctx.account)
    # Empty books write no rows. Status and the dashboard read positions for
    # the latest account_snapshot cycle — not MAX(positions_snapshot.cycle_id)
    # — so a flatten does not leave yesterday's lot on the panel.
    if ctx.positions:
        record_positions(conn, cycle_id, ctx.positions)

    # A broker-side stop or take-profit may have flattened a position since
    # the last cycle; its thesis would otherwise stay open forever.
    orphans = close_orphan_theses(conn, [p["symbol"] for p in ctx.positions])
    if orphans:
        log.info("closed_orphan_theses", extra={"cycle_id": cycle_id, "count": orphans})

    if not ctx.clock.is_open and not force:
        log.info(
            "market_closed", extra={"cycle_id": cycle_id, "next_open": str(ctx.clock.next_open)}
        )
        return finish(STATUS_SKIPPED_CLOSED)

    # After the playbook's last entry window, a flat book with no resting
    # orders has exactly one legal decision: no_action. Skip the LLM call and
    # say so, instead of paying a full cycle to hear it. Requires the
    # open-orders read to have *succeeded* (an empty-because-errored list is
    # not evidence of flatness), and `force` bypasses it like the market gate.
    if (
        not force
        and _past_entry_window(ctx.clock.timestamp)
        and not ctx.positions
        and not ctx.open_orders
        and ctx.open_orders_note is None
    ):
        log.info("no_entry_window_flat", extra={"cycle_id": cycle_id})
        return finish(STATUS_SKIPPED_NO_ENTRY)

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

    execution = place_order(conn, broker, ctx, proposal, risk_config, alert_sink=alert_sink)
    return finish(STATUS_OK, result=result, execution=execution)
