"""Operator entrypoints. Everything here is either read-only or flips a flag."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from trader import logging_setup
from trader.agent import Agent, StubAgent
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
    record_fill,
    set_flag,
    unreconciled_orders,
    utcnow,
)
from trader.evaluate import EvaluationError, evaluate, format_report
from trader.llm import AnthropicAgent
from trader.prompts import PromptError
from trader.risk.checks import CHECK_NAMES
from trader.risk.config import (
    DEFAULT_RISK_CONFIG_PATH,
    RiskConfig,
    RiskConfigError,
    load_risk_config,
)
from trader.scheduler import run_scheduler
from trader.state import trading_day_for


def _open(settings: Settings) -> tuple[sqlite3.Connection, Broker]:
    conn = connect(settings.db_path)
    broker = AlpacaBroker(settings.alpaca_api_key, settings.alpaca_secret_key)
    return conn, broker


def _agent(
    settings: Settings, args: argparse.Namespace, conn: sqlite3.Connection,
    broker: Broker, config: RiskConfig
) -> Agent:
    """The Phase 1 stub, or the real model. Both satisfy the same protocol."""
    if args.stub:
        return StubAgent()
    return AnthropicAgent(
        conn,
        broker,
        config,
        api_key=settings.anthropic_api_key,
        thinking=not args.no_thinking,
        effort=args.effort,
    )


def _risk(args: argparse.Namespace) -> RiskConfig:
    """Load risk.toml. A bad config must stop the process, never be defaulted."""
    return load_risk_config(Path(args.risk_config))


def cmd_run(settings: Settings, args: argparse.Namespace) -> int:
    config = _risk(args)
    conn, broker = _open(settings)
    run_scheduler(
        conn,
        broker,
        _agent(settings, args, conn, broker, config),
        cycle_minutes=settings.cycle_minutes,
        risk_config=config,
    )
    return 0


def cmd_cycle(settings: Settings, args: argparse.Namespace) -> int:
    config = _risk(args)
    conn, broker = _open(settings)
    reconcile_orphan_cycles(conn)
    outcome = run_cycle(
        conn, broker, _agent(settings, args, conn, broker, config),
        risk_config=config, force=args.force
    )
    summary: dict[str, Any] = {
        "cycle_id": outcome.cycle_id,
        "status": outcome.status,
        "action": outcome.result.action if outcome.result else None,
        "prompt_tokens": outcome.result.prompt_tokens if outcome.result else None,
        "tool_calls": [t["name"] for t in outcome.result.tool_calls] if outcome.result else [],
        "error": outcome.error,
    }
    if outcome.execution is not None:
        summary["order"] = {
            "approved": outcome.execution.verdict.approved,
            "executed": outcome.execution.executed,
            "failed_checks": [f.check for f in outcome.execution.verdict.failures],
        }
    print(json.dumps(summary, indent=2))
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


def cmd_risk(settings: Settings, args: argparse.Namespace) -> int:
    """Print the loaded limits and the checks that will run against them."""
    config = _risk(args)
    print(
        json.dumps(
            {
                "source": config.source,
                "max_position_notional": config.max_position_notional,
                "max_total_exposure": config.max_total_exposure,
                "max_daily_loss": config.max_daily_loss,
                "max_orders_per_hour": config.max_orders_per_hour,
                "max_orders_per_day": config.max_orders_per_day,
                "symbol_allowlist": sorted(config.symbol_allowlist),
                "regular_trading_hours_only": config.regular_trading_hours_only,
                "allow_shorts": config.allow_shorts,
                "checks": list(CHECK_NAMES),
            },
            indent=2,
        )
    )
    return 0


def cmd_rejections(settings: Settings, args: argparse.Namespace) -> int:
    """Which checks are firing, and on what. The first thing to read after a session."""
    conn = connect(settings.db_path)
    breakdown = conn.execute(
        "SELECT check_name, COUNT(*) AS n FROM risk_events GROUP BY check_name ORDER BY n DESC"
    ).fetchall()
    print("rejections by check:")
    for row in breakdown:
        print(f"  {row['n']:>5}  {row['check_name']}")
    if not breakdown:
        print("  (none)")
    print(f"\nmost recent {args.limit}:")
    recent = conn.execute(
        "SELECT timestamp, cycle_id, check_name, reason FROM risk_events "
        "ORDER BY id DESC LIMIT ?",
        (args.limit,),
    ).fetchall()
    for row in reversed(recent):
        print(
            f"  [{row['timestamp']}] cycle {row['cycle_id']} "
            f"{row['check_name']}: {row['reason']}"
        )
    return 0


def cmd_reconcile(settings: Settings, args: argparse.Namespace) -> int:
    """Read submitted orders back from the broker and record their fills."""
    conn, broker = _open(settings)
    pending = unreconciled_orders(conn, limit=args.limit)
    if not pending:
        print("nothing to reconcile")
        return 0
    done = skipped = 0
    for row in pending:
        fill = broker.get_order(row["broker_order_id"])
        if not fill.is_terminal:
            # Still working. Leave it NULL so a later run picks it up.
            skipped += 1
            continue
        record_fill(
            conn,
            row["id"],
            final_status=fill.status,
            filled_qty=fill.filled_qty,
            filled_avg_price=fill.filled_avg_price,
            filled_at=fill.filled_at.isoformat() if fill.filled_at else None,
        )
        done += 1
        print(
            f"  {row['symbol']:<6} {fill.status:<12} "
            f"{fill.filled_qty:g} @ {fill.filled_avg_price or '-'}"
        )
    print(f"reconciled {done}, still open {skipped}")
    return 0


def cmd_evaluate(settings: Settings, args: argparse.Namespace) -> int:
    """Did the agent beat doing nothing, over a range of logged cycles?"""
    conn = connect(settings.db_path)
    broker: Broker | None = None
    # Without keys the report just says the benchmark is unavailable — the rest
    # of the evaluation is pure DB and still worth printing.
    if not args.no_benchmark and settings.alpaca_api_key and settings.alpaca_secret_key:
        broker = AlpacaBroker(settings.alpaca_api_key, settings.alpaca_secret_key)

    start = args.start or trading_day_for(utcnow())
    end = args.end or trading_day_for(utcnow())
    report = evaluate(conn, start, end, broker=broker)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, default=str))
    else:
        print(format_report(report))
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
    p.add_argument(
        "--stub", action="store_true", help="use the Phase 1 stub agent instead of the model"
    )
    p.add_argument(
        "--no-thinking", action="store_true", help="disable adaptive thinking on the model"
    )
    p.add_argument(
        "--effort",
        default="medium",
        choices=["low", "medium", "high", "xhigh", "max"],
        help="model effort level (default: medium)",
    )
    p.add_argument(
        "--risk-config",
        default=str(DEFAULT_RISK_CONFIG_PATH),
        help=f"path to the risk limits file (default: {DEFAULT_RISK_CONFIG_PATH})",
    )
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

    sub.add_parser("risk", help="print the loaded risk limits and checks").set_defaults(
        func=cmd_risk
    )

    rej = sub.add_parser("rejections", help="risk rejections, by check and most recent")
    rej.add_argument("--limit", type=int, default=20)
    rej.set_defaults(func=cmd_rejections)

    rec = sub.add_parser("reconcile", help="read submitted orders back from the broker")
    rec.add_argument("--limit", type=int, default=200)
    rec.set_defaults(func=cmd_reconcile)

    ev = sub.add_parser("evaluate", help="did the agent beat doing nothing?")
    ev.add_argument("--start", help="first trading day, YYYY-MM-DD (default: today)")
    ev.add_argument("--end", help="last trading day, YYYY-MM-DD (default: today)")
    ev.add_argument("--json", action="store_true", help="machine-readable output")
    ev.add_argument(
        "--no-benchmark", action="store_true", help="skip the SPY benchmark (no broker call)"
    )
    ev.set_defaults(func=cmd_evaluate)

    kill = sub.add_parser("kill", help="engage/release the kill switch")
    kill.add_argument("state", choices=["on", "off", "status"])
    kill.add_argument("--note", default=None)
    kill.set_defaults(func=cmd_kill)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    needs_broker = args.command in {"run", "cycle", "reconcile"}
    try:
        settings = load_settings(require_broker=needs_broker)
    except MissingCredential as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    logging_setup.configure(settings.log_level, json_lines=not args.text_logs)
    try:
        return int(args.func(settings, args))
    except RiskConfigError as exc:
        # A malformed risk file must stop the process, not start it unprotected.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except MissingCredential as exc:
        # The Anthropic key is read lazily, so it surfaces here rather than at
        # load_settings. --stub needs no key at all.
        print(f"error: {exc}\nUse --stub to run the loop without the model.", file=sys.stderr)
        return 2
    except PromptError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except EvaluationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
