"""Phase 1's real deliverable: the loop runs, logs, and survives a restart."""

from __future__ import annotations

import sqlite3
import threading

import pytest

from tests.fakes import FakeBroker, position
from trader.agent import AgentResult, StubAgent
from trader.cycle import (
    STATUS_ERROR,
    STATUS_HALTED_KILL_SWITCH,
    STATUS_OK,
    STATUS_SKIPPED_CLOSED,
    run_cycle,
)
from trader.db import KILL_SWITCH, connect, cycles_on, reconcile_orphan_cycles, set_flag, utcnow
from trader.risk.config import RiskConfig
from trader.state import trading_day_for


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path / "t.sqlite3")


@pytest.fixture
def broker():
    return FakeBroker(positions=[position("AAPL", 10, 200.0, 205.0)])


def test_a_cycle_can_run_on_a_worker_thread(tmp_path) -> None:
    """Regression: APScheduler's default pool is a different thread than connect()."""
    conn = connect(tmp_path / "t.sqlite3")
    broker = FakeBroker()
    err: list[sqlite3.Error] = []
    outcome: list[object] = []

    def job() -> None:
        try:
            outcome.append(run_cycle(conn, broker, StubAgent()))
        except sqlite3.Error as exc:
            err.append(exc)

    t = threading.Thread(target=job)
    t.start()
    t.join()
    assert err == [], err
    assert outcome and outcome[0].status == STATUS_OK


def test_open_market_cycle_logs_everything(conn, broker) -> None:
    outcome = run_cycle(conn, broker, StubAgent())
    assert outcome.status == STATUS_OK

    row = conn.execute("SELECT * FROM cycles WHERE cycle_id = ?", (outcome.cycle_id,)).fetchone()
    assert row["ended_at"] and row["duration_ms"] is not None
    assert row["model"] == "stub"
    assert "## Positions" in row["full_prompt"]  # full prompt persisted, per spec
    assert row["full_response"] == "no_action"
    assert row["tool_calls"] == "[]"

    account = conn.execute("SELECT * FROM account_snapshot").fetchone()
    assert account["equity"] == pytest.approx(100_000.0)
    snap = conn.execute("SELECT * FROM positions_snapshot").fetchone()
    assert snap["symbol"] == "AAPL" and snap["cycle_id"] == outcome.cycle_id

    decision = conn.execute("SELECT * FROM decisions").fetchone()
    assert decision["action"] == "no_action" and decision["broker_order_id"] is None


def test_closed_market_is_skipped_but_still_snapshots(conn, broker) -> None:
    broker.is_open = False
    outcome = run_cycle(conn, broker, StubAgent())
    assert outcome.status == STATUS_SKIPPED_CLOSED
    # Snapshots still land: knowing what we held overnight matters.
    assert conn.execute("SELECT COUNT(*) FROM positions_snapshot").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0


def test_force_runs_the_agent_outside_rth(conn, broker) -> None:
    broker.is_open = False
    outcome = run_cycle(conn, broker, StubAgent(), force=True)
    assert outcome.status == STATUS_OK


def test_kill_switch_halts_before_any_broker_call(conn, broker) -> None:
    set_flag(conn, KILL_SWITCH, "1", note="test")
    outcome = run_cycle(conn, broker, StubAgent())
    assert outcome.status == STATUS_HALTED_KILL_SWITCH
    assert broker.calls == []  # nothing was even read


def test_kill_switch_is_not_overridable_by_force(conn, broker) -> None:
    set_flag(conn, KILL_SWITCH, "1")
    assert run_cycle(conn, broker, StubAgent(), force=True).status == STATUS_HALTED_KILL_SWITCH


def test_broker_failure_fails_the_cycle_loudly(conn, broker) -> None:
    broker.fail_on = {"get_account"}
    outcome = run_cycle(conn, broker, StubAgent())
    assert outcome.status == STATUS_ERROR
    row = conn.execute(
        "SELECT error FROM cycles WHERE cycle_id = ?", (outcome.cycle_id,)
    ).fetchone()
    assert "simulated get_account failure" in row["error"]


def test_agent_exception_fails_the_cycle_without_killing_the_loop(conn, broker) -> None:
    class Exploding:
        def run(self, ctx):
            raise ValueError("boom")

    outcome = run_cycle(conn, broker, Exploding())
    assert outcome.status == STATUS_ERROR
    assert "boom" in (outcome.error or "")
    # The next cycle must still work.
    assert run_cycle(conn, broker, StubAgent()).status == STATUS_OK


def test_restart_midday_reconstructs_state(tmp_path, broker) -> None:
    """Simulate a crash mid-cycle, then a fresh process opening the same DB."""
    db = tmp_path / "t.sqlite3"

    first = connect(db)
    run_cycle(first, broker, StubAgent())
    run_cycle(first, broker, StubAgent())
    # Crash: a cycle row exists but never got closed.
    first.execute(
        "INSERT INTO cycles(started_at, trading_day, status) VALUES (?, ?, 'running')",
        (utcnow().isoformat(), trading_day_for(utcnow())),
    )
    first.close()

    second = connect(db)
    assert reconcile_orphan_cycles(second) == 1
    outcome = run_cycle(second, broker, StubAgent())
    assert outcome.status == STATUS_OK

    day = trading_day_for(utcnow())
    rows = cycles_on(second, day)
    assert [r["status"] for r in rows] == [STATUS_OK, STATUS_OK, "interrupted", STATUS_OK]
    # Cycle ids continue rather than restarting, so the history is contiguous.
    assert [r["cycle_id"] for r in rows] == [1, 2, 3, 4]
    # Positions were re-read from the broker on every cycle that actually ran
    # (three: the interrupted row was inserted directly, never executed).
    assert broker.calls.count("get_positions") == 3


def test_context_sees_prior_decisions_after_restart(tmp_path, broker) -> None:
    db = tmp_path / "t.sqlite3"
    first = connect(db)
    run_cycle(first, broker, StubAgent())
    first.close()

    captured: list[str] = []

    class Recorder:
        def run(self, ctx):
            captured.append(ctx.recent_decisions[-1]["action"])
            return AgentResult(action="no_action", reasoning="r", model="rec")

    run_cycle(connect(db), broker, Recorder())
    assert captured == ["no_action"]


# --- Phase 2: proposals route through the risk layer ------------------------


class ScriptedAgent:
    """Returns a fixed proposal. Stands in for the Phase 3 model."""

    def __init__(self, action: str, symbol: str | None = None, qty: float | None = None) -> None:
        self.action, self.symbol, self.qty = action, symbol, qty

    def run(self, ctx):
        return AgentResult(
            action=self.action,
            reasoning="scripted",
            symbol=self.symbol,
            qty=self.qty,
            model="scripted",
        )


RISK = RiskConfig(
    max_position_notional=5_000.0,
    max_total_exposure=25_000.0,
    max_daily_loss=2_000.0,
    max_orders_per_hour=6,
    max_orders_per_day=20,
    symbol_allowlist=frozenset({"AAPL"}),
)


def test_an_approved_proposal_reaches_the_broker(conn, broker) -> None:
    broker.prices["AAPL"] = 100.0
    outcome = run_cycle(conn, broker, ScriptedAgent("buy", "AAPL", 10), risk_config=RISK)
    assert outcome.status == STATUS_OK
    assert outcome.execution is not None and outcome.execution.executed
    assert broker.submitted == [{"symbol": "AAPL", "qty": 10, "side": "buy"}]


def test_a_rejected_proposal_never_reaches_the_broker(conn, broker) -> None:
    outcome = run_cycle(conn, broker, ScriptedAgent("buy", "DOGE", 10), risk_config=RISK)
    assert outcome.status == STATUS_OK  # the cycle succeeded; the order did not
    assert outcome.execution is not None and not outcome.execution.executed
    assert broker.submitted == []
    assert conn.execute("SELECT COUNT(*) FROM risk_events").fetchone()[0] >= 1


def test_a_proposal_without_a_risk_config_fails_the_cycle(conn, broker) -> None:
    """Refuse rather than fall back to defaults nobody chose."""
    outcome = run_cycle(conn, broker, ScriptedAgent("buy", "AAPL", 10))
    assert outcome.status == STATUS_ERROR
    assert "no risk config" in (outcome.error or "")
    assert broker.submitted == []


def test_force_does_not_let_an_order_past_the_rth_check(conn, broker) -> None:
    """--force is a loop-level dry run, not a risk override."""
    broker.is_open = False
    outcome = run_cycle(
        conn, broker, ScriptedAgent("buy", "AAPL", 10), risk_config=RISK, force=True
    )
    assert outcome.execution is not None
    assert "regular_trading_hours_only" in {f.check for f in outcome.execution.verdict.failures}
    assert broker.submitted == []


def test_pricing_failure_fails_the_cycle(conn, broker) -> None:
    broker.fail_on = {"get_latest_price"}
    outcome = run_cycle(conn, broker, ScriptedAgent("buy", "AAPL", 10), risk_config=RISK)
    assert outcome.status == STATUS_ERROR
    assert "get_latest_price" in (outcome.error or "")
    assert broker.submitted == []


def test_a_proposal_with_no_qty_is_rejected_not_crashed(conn, broker) -> None:
    outcome = run_cycle(conn, broker, ScriptedAgent("buy", "AAPL", None), risk_config=RISK)
    assert outcome.status == STATUS_OK
    assert outcome.execution is not None
    assert "proposal_sanity" in {f.check for f in outcome.execution.verdict.failures}


# --- housekeeping that now runs inside every cycle ---------------------------


def test_fills_are_reconciled_automatically_at_cycle_start(conn, broker) -> None:
    """`trader reconcile` still exists, but forgetting it no longer loses data."""
    broker.prices["AAPL"] = 100.0
    run_cycle(conn, broker, ScriptedAgent("buy", "AAPL", 10), risk_config=RISK)
    assert conn.execute(
        "SELECT final_status FROM decisions WHERE broker_order_id IS NOT NULL"
    ).fetchone()["final_status"] is None  # not yet read back

    run_cycle(conn, broker, StubAgent())
    assert conn.execute(
        "SELECT final_status FROM decisions WHERE broker_order_id IS NOT NULL"
    ).fetchone()["final_status"] == "filled"


def test_a_stopped_out_positions_thesis_is_closed(conn) -> None:
    """A broker-side stop can flatten a position between cycles; the next
    cycle must close the thesis instead of letting it squat in state forever."""
    empty_broker = FakeBroker()  # the position is gone
    conn.execute(
        "INSERT INTO theses(symbol, opened_at, rationale, invalidation_condition, status) "
        "VALUES ('AAPL', datetime('now', '-2 hours'), 'why', 'when', 'open')"
    )
    run_cycle(conn, empty_broker, StubAgent())
    assert conn.execute("SELECT status FROM theses").fetchone()["status"] == "closed"


def test_a_freshly_written_thesis_survives_the_orphan_sweep(conn) -> None:
    """Grace window: an order submitted moments ago may not show a position yet."""
    empty_broker = FakeBroker()
    conn.execute(
        "INSERT INTO theses(symbol, opened_at, rationale, invalidation_condition, status) "
        "VALUES ('AAPL', datetime('now'), 'why', 'when', 'open')"
    )
    run_cycle(conn, empty_broker, StubAgent())
    assert conn.execute("SELECT status FROM theses").fetchone()["status"] == "open"


def test_a_held_positions_thesis_is_not_touched(conn, broker) -> None:
    conn.execute(
        "INSERT INTO theses(symbol, opened_at, rationale, invalidation_condition, status) "
        "VALUES ('AAPL', datetime('now', '-2 hours'), 'why', 'when', 'open')"
    )
    run_cycle(conn, broker, StubAgent())  # broker holds AAPL
    assert conn.execute("SELECT status FROM theses").fetchone()["status"] == "open"


def test_the_scan_reaches_the_prompt_when_a_risk_config_is_supplied(conn, broker) -> None:
    outcome = run_cycle(conn, broker, StubAgent(), risk_config=RISK)
    row = conn.execute(
        "SELECT full_prompt FROM cycles WHERE cycle_id = ?", (outcome.cycle_id,)
    ).fetchone()
    assert "## Market scan" in row["full_prompt"]
    assert "regime(QQQ):" in row["full_prompt"]


def test_a_scan_failure_degrades_to_a_note_not_a_dead_cycle(conn, broker) -> None:
    broker.fail_on = {"get_scan_data"}
    outcome = run_cycle(conn, broker, StubAgent(), risk_config=RISK)
    assert outcome.status == STATUS_OK
    row = conn.execute(
        "SELECT full_prompt FROM cycles WHERE cycle_id = ?", (outcome.cycle_id,)
    ).fetchone()
    assert "scan unavailable" in row["full_prompt"]
