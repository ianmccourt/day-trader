"""The cycle loop: short, independent invocations on a wall-clock schedule.

Deliberately not one long-running conversation. The scheduler owns nothing but
timing; all state lives in SQLite, so killing this process mid-session and
restarting it loses nothing but the in-flight cycle.
"""

from __future__ import annotations

import logging
import signal
import sqlite3
from collections.abc import Callable
from types import FrameType

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from trader.agent import Agent
from trader.broker import Broker
from trader.constants import MARKET_TZ, RTH_CLOSE, RTH_OPEN
from trader.cycle import CycleOutcome, run_cycle
from trader.db import reconcile_orphan_cycles
from trader.risk.config import RiskConfig

log = logging.getLogger("trader.scheduler")


def rth_trigger(cycle_minutes: int) -> CronTrigger:
    """Wake every `cycle_minutes` on weekday RTH hours, exchange-local.

    The trigger is deliberately a little wider than the session (it fires from
    09:00 and during the 15:xx hour). The broker clock inside run_cycle is the
    authority on whether the market is actually open, which is what makes
    holidays and early closes correct without a local calendar.
    """
    return CronTrigger(
        day_of_week="mon-fri",
        hour=f"{RTH_OPEN[0]}-{RTH_CLOSE[0] - 1}",
        minute=f"*/{cycle_minutes}",
        timezone=MARKET_TZ,
    )


def run_scheduler(
    conn: sqlite3.Connection,
    broker: Broker,
    agent: Agent,
    *,
    cycle_minutes: int,
    risk_config: RiskConfig,
    on_cycle: Callable[[CycleOutcome], None] | None = None,
) -> None:
    orphans = reconcile_orphan_cycles(conn)
    if orphans:
        # Expected after any unclean shutdown; noisy on purpose so I notice
        # if it starts happening every restart.
        log.warning("reconciled_orphan_cycles", extra={"count": orphans})

    scheduler = BlockingScheduler(timezone=MARKET_TZ)

    def job() -> None:
        outcome = run_cycle(conn, broker, agent, risk_config=risk_config)
        if on_cycle:
            on_cycle(outcome)

    scheduler.add_job(
        job,
        trigger=rth_trigger(cycle_minutes),
        id="trading_cycle",
        name="trading_cycle",
        # A cycle must never overlap itself, and a machine that was asleep
        # should run one catch-up cycle, not fifty.
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )

    def shutdown(signum: int, _frame: FrameType | None) -> None:
        log.warning("shutdown_signal", extra={"signal": signal.Signals(signum).name})
        scheduler.shutdown(wait=False)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    log.info(
        "scheduler_start",
        extra={
            "cycle_minutes": cycle_minutes,
            "risk_config": risk_config.source,
            "window": (
                f"{RTH_OPEN[0]:02d}:{RTH_OPEN[1]:02d}-"
                f"{RTH_CLOSE[0]:02d}:{RTH_CLOSE[1]:02d} ET"
            ),
        },
    )
    scheduler.start()
    log.info("scheduler_stopped")
