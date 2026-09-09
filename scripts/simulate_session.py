"""Run a compressed trading session against the fake broker, into a real DB.

This is the Phase 1 proof: a full session's worth of cycles, a hard kill
partway through, a fresh process reopening the same file, and a state summary
reconstructed entirely from SQLite. It needs no Alpaca credentials.

    uv run python scripts/simulate_session.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.fakes import FakeBroker, position

from trader import logging_setup
from trader.agent import StubAgent
from trader.cycle import run_cycle
from trader.db import connect, cycles_on, reconcile_orphan_cycles, utcnow
from trader.state import trading_day_for

DB = Path("data/simulated_session.sqlite3")
CYCLES_PER_SESSION = 26  # 09:30-16:00 at 15-minute cadence


def summarise(conn: sqlite3.Connection) -> dict[str, object]:
    day = trading_day_for(utcnow())
    rows = cycles_on(conn, day)
    statuses: dict[str, int] = {}
    for r in rows:
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1
    prompt_lengths = [len(r["full_prompt"]) for r in rows if r["full_prompt"]]
    return {
        "trading_day": day,
        "cycles": len(rows),
        "cycle_ids": [r["cycle_id"] for r in rows],
        "by_status": statuses,
        "decisions": conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0],
        "position_snapshots": conn.execute("SELECT COUNT(*) FROM positions_snapshot").fetchone()[0],
        "prompt_chars_min": min(prompt_lengths, default=0),
        "prompt_chars_max": max(prompt_lengths, default=0),
    }


def main() -> int:
    logging_setup.configure("INFO", json_lines=False)
    DB.unlink(missing_ok=True)
    Path(str(DB) + "-wal").unlink(missing_ok=True)
    Path(str(DB) + "-shm").unlink(missing_ok=True)

    broker = FakeBroker(
        positions=[position("AAPL", 10, 200.0, 205.5), position("SPY", 4, 560.0, 558.2)]
    )
    agent = StubAgent()

    print("\n=== process 1: first half of the session ===")
    conn = connect(DB)
    for _ in range(CYCLES_PER_SESSION // 2):
        run_cycle(conn, broker, agent, force=True)

    print("\n=== simulating a hard kill mid-cycle ===")
    conn.execute(
        "INSERT INTO cycles(started_at, trading_day, status) VALUES (?, ?, 'running')",
        (utcnow().isoformat(), trading_day_for(utcnow())),
    )
    conn.close()

    print("\n=== process 2: restart, reconcile, finish the session ===")
    conn = connect(DB)
    orphans = reconcile_orphan_cycles(conn)
    print(f"reconciled {orphans} interrupted cycle(s)")
    for _ in range(CYCLES_PER_SESSION - CYCLES_PER_SESSION // 2):
        run_cycle(conn, broker, agent, force=True)

    print("\n=== state reconstructed from the DB alone ===")
    print(json.dumps(summarise(conn), indent=2))
    print(f"\ninspect with:  uv run trader --text-logs cycles\n  TRADER_DB_PATH={DB}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
