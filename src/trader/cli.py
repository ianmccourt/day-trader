"""Operator entrypoints. Everything here is either read-only or flips a flag."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import Any

from trader import logging_setup
from trader.agent import StubAgent
from trader.broker import AlpacaBroker, Broker
from trader.config import MissingCredential, Settings, load_settings
from trader.constants import ALPACA_PAPER_BASE_URL
from trader.cycle import run_cycle
from trader.db import (
    KILL_SWITCH,
    connect,
    cycles_on,
    kill_switch_engaged,
    last_cycle,
    reconcile_orphan_cycles,
    set_flag,
    utcnow,
)
from trader.scheduler import run_scheduler
from trader.state import trading_day_for


def _open(settings: Settings) -> tuple[sqlite3.Connection, Broker]:
    conn = connect(settings.db_path)
    broker = AlpacaBroker(settings.alpaca_api_key, settings.alpaca_secret_key)
    return conn, broker


def cmd_run(settings: Settings, _args: argparse.Namespace) -> int:
    conn, broker = _open(settings)
    run_scheduler(conn, broker, StubAgent(), cycle_minutes=settings.cycle_minutes)
    return 0


def cmd_cycle(settings: Settings, args: argparse.Namespace) -> int:
    conn, broker = _open(settings)
    reconcile_orphan_cycles(conn)
    outcome = run_cycle(conn, broker, StubAgent(), force=args.force)
    print(json.dumps({"cycle_id": outcome.cycle_id, "status": outcome.status}, indent=2))
    return 0 if outcome.status != "error" else 1


def cmd_status(settings: Settings, _args: argparse.Namespace) -> int:
    """Reconstruct today's session purely from the DB. No broker call."""
    conn = connect(settings.db_path)
    day = trading_day_for(utcnow())
    rows = cycles_on(conn, day)
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    last = last_cycle(conn)
    decisions = conn.execute(
        "SELECT action, COUNT(*) AS n FROM decisions d "
        "JOIN cycles c USING (cycle_id) WHERE c.trading_day = ? GROUP BY action",
        (day,),
    ).fetchall()
    positions = conn.execute(
        "SELECT symbol, qty, market_value, unrealized_pl FROM positions_snapshot "
        "WHERE cycle_id = (SELECT MAX(cycle_id) FROM positions_snapshot)"
    ).fetchall()
    out: dict[str, Any] = {
        "db": str(settings.db_path),
        "broker_base_url": ALPACA_PAPER_BASE_URL,
        "trading_day": day,
        "kill_switch": kill_switch_engaged(conn),
        "cycles_today": len(rows),
        "cycles_by_status": by_status,
        "decisions_today": {d["action"]: d["n"] for d in decisions},
        "last_cycle": dict(last) | {"full_prompt": "<omitted>", "full_response": "<omitted>"}
        if last
        else None,
        "last_known_positions": [dict(p) for p in positions],
    }
    print(json.dumps(out, indent=2, default=str))
    return 0


def cmd_cycles(settings: Settings, args: argparse.Namespace) -> int:
    conn = connect(settings.db_path)
    rows = conn.execute(
        "SELECT cycle_id, started_at, trading_day, status, duration_ms, error "
        "FROM cycles ORDER BY cycle_id DESC LIMIT ?",
        (args.limit,),
    ).fetchall()
    for r in reversed(rows):
        err = f"  {r['error'].splitlines()[0]}" if r["error"] else ""
        print(
            f"{r['cycle_id']:>5}  {r['started_at']}  {r['trading_day']}  "
            f"{r['status']:<22} {r['duration_ms'] or '-'!s:>6}ms{err}"
        )
    return 0


def cmd_show(settings: Settings, args: argparse.Namespace) -> int:
    """Dump one cycle's full prompt and response — the thing I actually read."""
    conn = connect(settings.db_path)
    row = conn.execute("SELECT * FROM cycles WHERE cycle_id = ?", (args.cycle_id,)).fetchone()
    if row is None:
        print(f"no such cycle: {args.cycle_id}", file=sys.stderr)
        return 1
    d = dict(row)
    print(f"=== cycle {d['cycle_id']} [{d['status']}] {d['started_at']} ===")
    for key in ("model", "prompt_tokens", "completion_tokens", "duration_ms", "error"):
        print(f"{key}: {d[key]}")
    print("\n--- PROMPT ---\n" + (d["full_prompt"] or "(none)"))
    print("\n--- RESPONSE ---\n" + (d["full_response"] or "(none)"))
    print("\n--- TOOL CALLS ---\n" + (d["tool_calls"] or "[]"))
    return 0


def cmd_kill(settings: Settings, args: argparse.Namespace) -> int:
    conn = connect(settings.db_path)
    if args.state == "status":
        print("ENGAGED" if kill_switch_engaged(conn) else "off")
        return 0
    set_flag(conn, KILL_SWITCH, "1" if args.state == "on" else "0", note=args.note)
    print(f"kill_switch -> {'ENGAGED' if args.state == 'on' else 'off'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="trader", description=__doc__)
    p.add_argument("--text-logs", action="store_true", help="human-readable logs, not JSON lines")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="start the scheduled cycle loop (blocking)").set_defaults(
        func=cmd_run
    )

    one = sub.add_parser("cycle", help="run a single cycle now")
    one.add_argument(
        "--force", action="store_true", help="run even when the market is closed (dry run)"
    )
    one.set_defaults(func=cmd_cycle)

    sub.add_parser("status", help="reconstruct today's session from the DB").set_defaults(
        func=cmd_status
    )

    lst = sub.add_parser("cycles", help="list recent cycles")
    lst.add_argument("--limit", type=int, default=25)
    lst.set_defaults(func=cmd_cycles)

    show = sub.add_parser("show", help="dump one cycle's full prompt and response")
    show.add_argument("cycle_id", type=int)
    show.set_defaults(func=cmd_show)

    kill = sub.add_parser("kill", help="engage/release the kill switch")
    kill.add_argument("state", choices=["on", "off", "status"])
    kill.add_argument("--note", default=None)
    kill.set_defaults(func=cmd_kill)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    needs_broker = args.command in {"run", "cycle"}
    try:
        settings = load_settings(require_broker=needs_broker)
    except MissingCredential as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    logging_setup.configure(settings.log_level, json_lines=not args.text_logs)
    return int(args.func(settings, args))


if __name__ == "__main__":
    raise SystemExit(main())
