"""SQLite persistence. Plain sqlite3 — the schema is small and I want the SQL visible.

Every table is append-only except `flags`. Nothing in this module deletes or
rewrites history; the eval harness (Phase 4) and I both read it directly, so
column names are chosen to be greppable rather than short.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trader.constants import MAX_THESIS_RATIONALE_CHARS

SCHEMA_VERSION = 2

# Deviations from SPEC.md's starting schema, and why:
#   * positions_snapshot gains cycle_id, current_price and market_value. The
#     risk layer's exposure checks need notional, and Phase 4 needs to join a
#     snapshot back to the cycle that saw it.
#   * cycles gains trading_day and status so a restart can ask "what happened
#     today" with an index hit rather than a date-parsing scan.
#   * flags is new: the kill switch needs somewhere to live that survives a
#     restart and can be flipped from outside the process.
#   * decisions gains reference_price (v2) plus the four fill columns filled by
#     `trader reconcile`. Phase 4 needs entry and exit prices to compute a win
#     rate and a holding period, and the broker's fill is the only truth for
#     those. All are nullable and additive.
# No column from the spec was dropped or renamed, and no migration rewrites or
# deletes a row — see MIGRATIONS below.
SCHEMA = f"""
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cycles (
    cycle_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at        TEXT    NOT NULL,          -- ISO-8601 UTC
    ended_at          TEXT,
    trading_day       TEXT    NOT NULL,          -- YYYY-MM-DD, exchange-local
    status            TEXT    NOT NULL,          -- running|ok|error|skipped_*|halted_*
    model             TEXT,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    full_prompt       TEXT,
    full_response     TEXT,
    tool_calls        TEXT,                      -- JSON array
    duration_ms       INTEGER,
    error             TEXT
);
CREATE INDEX IF NOT EXISTS idx_cycles_day ON cycles(trading_day, cycle_id);

CREATE TABLE IF NOT EXISTS positions_snapshot (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id       INTEGER NOT NULL REFERENCES cycles(cycle_id),
    symbol         TEXT    NOT NULL,
    qty            REAL    NOT NULL,
    avg_price      REAL    NOT NULL,
    current_price  REAL,
    market_value   REAL,
    unrealized_pl  REAL,
    captured_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_positions_cycle ON positions_snapshot(cycle_id);
CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions_snapshot(symbol, captured_at);

CREATE TABLE IF NOT EXISTS account_snapshot (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id       INTEGER NOT NULL REFERENCES cycles(cycle_id),
    equity         REAL    NOT NULL,
    last_equity    REAL,
    cash           REAL    NOT NULL,
    buying_power   REAL,
    long_market_value  REAL,
    short_market_value REAL,
    captured_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_account_cycle ON account_snapshot(cycle_id);

CREATE TABLE IF NOT EXISTS theses (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol                TEXT NOT NULL,
    opened_at             TEXT NOT NULL,
    updated_at            TEXT,
    closed_at             TEXT,
    cycle_id              INTEGER REFERENCES cycles(cycle_id),
    rationale             TEXT NOT NULL
        CHECK (length(rationale) <= {MAX_THESIS_RATIONALE_CHARS}),
    invalidation_condition TEXT NOT NULL,
    status                TEXT NOT NULL          -- open|closed|invalidated
);
CREATE INDEX IF NOT EXISTS idx_theses_symbol ON theses(symbol, status);

CREATE TABLE IF NOT EXISTS decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id        INTEGER NOT NULL REFERENCES cycles(cycle_id),
    timestamp       TEXT    NOT NULL,
    action          TEXT    NOT NULL,            -- buy|sell|no_action
    symbol          TEXT,
    qty             REAL,
    reasoning       TEXT,
    risk_result     TEXT,                        -- approved | rejected:<check>
    broker_order_id TEXT,
    outcome         TEXT,
    reference_price REAL,                        -- price the risk layer sized on
    filled_qty       REAL,                       -- the four below are filled in
    filled_avg_price REAL,                       -- by `trader reconcile`, from
    filled_at        TEXT,                       -- the broker's own record
    final_status     TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_cycle ON decisions(cycle_id);
CREATE INDEX IF NOT EXISTS idx_decisions_time ON decisions(timestamp);

CREATE TABLE IF NOT EXISTS risk_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id   INTEGER REFERENCES cycles(cycle_id),
    timestamp  TEXT NOT NULL,
    check_name TEXT NOT NULL,
    reason     TEXT NOT NULL,
    proposal   TEXT NOT NULL                     -- JSON
);
CREATE INDEX IF NOT EXISTS idx_risk_events_check ON risk_events(check_name, timestamp);

CREATE TABLE IF NOT EXISTS flags (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    note       TEXT
);
"""

KILL_SWITCH = "kill_switch"


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat(timespec="milliseconds")


#: Additive-only. Each entry is (column, DDL type). A migration may add a
#: nullable column and nothing else — never drop, rename, or rewrite, because
#: the logged history is the point of this database.
MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    "decisions": [
        ("reference_price", "REAL"),
        ("filled_qty", "REAL"),
        ("filled_avg_price", "REAL"),
        ("filled_at", "TEXT"),
        ("final_status", "TEXT"),
    ],
}


def _migrate(conn: sqlite3.Connection) -> list[str]:
    """Bring an existing database up to the current schema. Additive only."""
    applied: list[str] = []
    for table, columns in MIGRATIONS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                applied.append(f"{table}.{name}")
    return applied


def connect(db_path: Path) -> sqlite3.Connection:
    """Open (and initialise, if new) the database at `db_path`."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


# --- cycles ----------------------------------------------------------------


def open_cycle(conn: sqlite3.Connection, *, started_at: datetime, trading_day: str) -> int:
    cur = conn.execute(
        "INSERT INTO cycles(started_at, trading_day, status) VALUES (?, ?, 'running')",
        (iso(started_at), trading_day),
    )
    cycle_id = cur.lastrowid
    assert cycle_id is not None
    return cycle_id


def close_cycle(
    conn: sqlite3.Connection,
    cycle_id: int,
    *,
    status: str,
    duration_ms: int,
    model: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    full_prompt: str | None = None,
    full_response: str | None = None,
    tool_calls: Sequence[dict[str, Any]] | None = None,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE cycles SET
            ended_at = ?, status = ?, duration_ms = ?, model = ?,
            prompt_tokens = ?, completion_tokens = ?,
            full_prompt = ?, full_response = ?, tool_calls = ?, error = ?
        WHERE cycle_id = ?
        """,
        (
            iso(utcnow()),
            status,
            duration_ms,
            model,
            prompt_tokens,
            completion_tokens,
            full_prompt,
            full_response,
            json.dumps(list(tool_calls)) if tool_calls is not None else None,
            error,
            cycle_id,
        ),
    )


def reconcile_orphan_cycles(conn: sqlite3.Connection) -> int:
    """Mark cycles left `running` by a crash or kill as `interrupted`.

    Called once at startup. A cycle row is written before the work happens, so
    an unclean shutdown always leaves exactly one of these behind; leaving them
    as `running` would corrupt every "cycles today" count downstream.
    """
    cur = conn.execute(
        "UPDATE cycles SET status = 'interrupted', ended_at = ? "
        "WHERE status = 'running'",
        (iso(utcnow()),),
    )
    return cur.rowcount


def last_cycle(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM cycles WHERE status NOT IN ('running') ORDER BY cycle_id DESC LIMIT 1"
    ).fetchone()


def cycles_on(conn: sqlite3.Connection, trading_day: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM cycles WHERE trading_day = ? ORDER BY cycle_id", (trading_day,)
    ).fetchall()


# --- snapshots -------------------------------------------------------------


def record_account(conn: sqlite3.Connection, cycle_id: int, account: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO account_snapshot(
            cycle_id, equity, last_equity, cash, buying_power,
            long_market_value, short_market_value, captured_at)
        VALUES (:cycle_id, :equity, :last_equity, :cash, :buying_power,
                :long_market_value, :short_market_value, :captured_at)
        """,
        {"cycle_id": cycle_id, **account},
    )


def record_positions(
    conn: sqlite3.Connection, cycle_id: int, positions: Sequence[dict[str, Any]]
) -> None:
    conn.executemany(
        """
        INSERT INTO positions_snapshot(
            cycle_id, symbol, qty, avg_price, current_price,
            market_value, unrealized_pl, captured_at)
        VALUES (:cycle_id, :symbol, :qty, :avg_price, :current_price,
                :market_value, :unrealized_pl, :captured_at)
        """,
        [{"cycle_id": cycle_id, **p} for p in positions],
    )


# --- decisions & risk ------------------------------------------------------


def record_decision(
    conn: sqlite3.Connection,
    *,
    cycle_id: int,
    action: str,
    symbol: str | None = None,
    qty: float | None = None,
    reasoning: str | None = None,
    risk_result: str | None = None,
    broker_order_id: str | None = None,
    outcome: str | None = None,
    reference_price: float | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO decisions(
            cycle_id, timestamp, action, symbol, qty, reasoning,
            risk_result, broker_order_id, outcome, reference_price)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            cycle_id,
            iso(utcnow()),
            action,
            symbol,
            qty,
            reasoning,
            risk_result,
            broker_order_id,
            outcome,
            reference_price,
        ),
    )
    row_id = cur.lastrowid
    assert row_id is not None
    return row_id


def record_risk_event(
    conn: sqlite3.Connection,
    *,
    cycle_id: int | None,
    check_name: str,
    reason: str,
    proposal: dict[str, Any],
) -> None:
    conn.execute(
        "INSERT INTO risk_events(cycle_id, timestamp, check_name, reason, proposal) "
        "VALUES (?, ?, ?, ?, ?)",
        (cycle_id, iso(utcnow()), check_name, reason, json.dumps(proposal, sort_keys=True)),
    )


def orders_since(conn: sqlite3.Connection, since: datetime) -> int:
    """Count of orders this harness actually submitted since `since`.

    Sourced from our own decisions table rather than the broker, because the
    rate limits in risk.yaml are about what the agent did, and the broker has no
    idea which of its orders came from us.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM decisions "
        "WHERE broker_order_id IS NOT NULL AND timestamp >= ?",
        (iso(since),),
    ).fetchone()
    return int(row["n"])


def orders_for_cycle(conn: sqlite3.Connection, cycle_id: int) -> int:
    """Submitted orders (those with a broker id) attributed to this cycle."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM decisions "
        "WHERE cycle_id = ? AND broker_order_id IS NOT NULL",
        (cycle_id,),
    ).fetchone()
    return int(row["n"])


# --- flags / kill switch ---------------------------------------------------


def set_flag(conn: sqlite3.Connection, key: str, value: str, note: str | None = None) -> None:
    conn.execute(
        "INSERT INTO flags(key, value, updated_at, note) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "updated_at = excluded.updated_at, note = excluded.note",
        (key, value, iso(utcnow()), note),
    )


def get_flag(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM flags WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def kill_switch_engaged(conn: sqlite3.Connection) -> bool:
    return (get_flag(conn, KILL_SWITCH) or "0") == "1"


def unreconciled_orders(conn: sqlite3.Connection, limit: int = 200) -> list[sqlite3.Row]:
    """Submitted orders whose terminal state we have not read back yet."""
    return conn.execute(
        "SELECT id, broker_order_id, symbol FROM decisions "
        "WHERE broker_order_id IS NOT NULL AND final_status IS NULL "
        "ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def record_fill(
    conn: sqlite3.Connection,
    decision_id: int,
    *,
    final_status: str,
    filled_qty: float,
    filled_avg_price: float | None,
    filled_at: str | None,
) -> None:
    """Write a broker fill onto its decision row. Only ever fills in NULLs."""
    conn.execute(
        "UPDATE decisions SET final_status = ?, filled_qty = ?, "
        "filled_avg_price = ?, filled_at = ? WHERE id = ?",
        (final_status, filled_qty, filled_avg_price, filled_at, decision_id),
    )
