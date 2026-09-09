"""Phase 1's real deliverable: the loop runs, logs, and survives a restart."""

from __future__ import annotations

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
from trader.state import trading_day_for


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path / "t.sqlite3")


@pytest.fixture
def broker():
    return FakeBroker(positions=[position("AAPL", 10, 200.0, 205.0)])


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
