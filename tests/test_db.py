from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from trader.constants import MAX_THESIS_RATIONALE_CHARS
from trader.db import (
    KILL_SWITCH,
    close_cycle,
    connect,
    cycles_on,
    kill_switch_engaged,
    last_cycle,
    open_cycle,
    orders_for_cycle,
    orders_since,
    reconcile_orphan_cycles,
    record_decision,
    record_risk_event,
    set_flag,
    utcnow,
)


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path / "t.sqlite3")


def test_schema_creates_every_spec_table(conn: sqlite3.Connection) -> None:
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "cycles",
        "positions_snapshot",
        "theses",
        "decisions",
        "risk_events",
        "flags",
    } <= names


def test_connect_is_idempotent(tmp_path) -> None:
    path = tmp_path / "t.sqlite3"
    first = connect(path)
    open_cycle(first, started_at=utcnow(), trading_day="2026-09-09")
    first.close()
    second = connect(path)  # re-running must not wipe history
    assert len(cycles_on(second, "2026-09-09")) == 1


def test_thesis_rationale_is_hard_capped(conn: sqlite3.Connection) -> None:
    def insert(n: int) -> None:
        conn.execute(
            "INSERT INTO theses(symbol, opened_at, rationale, invalidation_condition, status) "
            "VALUES ('AAPL', ?, ?, 'x', 'open')",
            (utcnow().isoformat(), "a" * n),
        )

    insert(MAX_THESIS_RATIONALE_CHARS)  # boundary: exactly at the cap is allowed
    with pytest.raises(sqlite3.IntegrityError):
        insert(MAX_THESIS_RATIONALE_CHARS + 1)


def test_kill_switch_round_trips(conn: sqlite3.Connection) -> None:
    assert kill_switch_engaged(conn) is False  # default off
    set_flag(conn, KILL_SWITCH, "1", note="testing")
    assert kill_switch_engaged(conn) is True
    set_flag(conn, KILL_SWITCH, "0")
    assert kill_switch_engaged(conn) is False


def test_reconcile_orphan_cycles_marks_interrupted(conn: sqlite3.Connection) -> None:
    stuck = open_cycle(conn, started_at=utcnow(), trading_day="2026-09-09")
    done = open_cycle(conn, started_at=utcnow(), trading_day="2026-09-09")
    close_cycle(conn, done, status="ok", duration_ms=5)

    assert reconcile_orphan_cycles(conn) == 1
    rows = {r["cycle_id"]: r["status"] for r in cycles_on(conn, "2026-09-09")}
    assert rows[stuck] == "interrupted"
    assert rows[done] == "ok"
    assert reconcile_orphan_cycles(conn) == 0  # nothing left to reconcile


def test_last_cycle_ignores_running(conn: sqlite3.Connection) -> None:
    done = open_cycle(conn, started_at=utcnow(), trading_day="2026-09-09")
    close_cycle(conn, done, status="ok", duration_ms=1)
    open_cycle(conn, started_at=utcnow(), trading_day="2026-09-09")
    row = last_cycle(conn)
    assert row is not None and row["cycle_id"] == done


def test_orders_since_counts_only_submitted_orders(conn: sqlite3.Connection) -> None:
    cycle_id = open_cycle(conn, started_at=utcnow(), trading_day="2026-09-09")
    record_decision(conn, cycle_id=cycle_id, action="no_action")
    record_decision(conn, cycle_id=cycle_id, action="buy", symbol="AAPL", qty=1)  # rejected
    record_decision(
        conn, cycle_id=cycle_id, action="buy", symbol="AAPL", qty=1, broker_order_id="o-1"
    )
    assert orders_since(conn, utcnow() - timedelta(hours=1)) == 1
    assert orders_since(conn, utcnow() + timedelta(minutes=1)) == 0
    assert orders_for_cycle(conn, cycle_id) == 1


def test_risk_events_persist_the_proposal(conn: sqlite3.Connection) -> None:
    cycle_id = open_cycle(conn, started_at=utcnow(), trading_day="2026-09-09")
    record_risk_event(
        conn,
        cycle_id=cycle_id,
        check_name="symbol_allowlist",
        reason="DOGE not on allowlist",
        proposal={"symbol": "DOGE", "qty": 5},
    )
    row = conn.execute("SELECT * FROM risk_events").fetchone()
    assert row["check_name"] == "symbol_allowlist"
    assert '"symbol": "DOGE"' in row["proposal"]
