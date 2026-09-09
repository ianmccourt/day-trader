"""Drive a battery of hostile proposals through the real execution path.

Phase 2's acceptance criterion, as something you can read rather than a test
summary. Uses the fake broker and a scratch DB, so it needs no credentials and
touches no account.

    uv run python scripts/adversarial_run.py
"""

from __future__ import annotations

import logging
import math
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.fakes import FakeBroker, position

from trader import logging_setup
from trader.db import KILL_SWITCH, connect, open_cycle, set_flag, utcnow
from trader.execution import place_order
from trader.risk.config import load_risk_config
from trader.risk.models import Proposal
from trader.state import build_cycle_context, trading_day_for

DB = Path("data/adversarial.sqlite3")


def prop(action: str, symbol: str, qty: float, price: float = 100.0) -> Proposal:
    return Proposal(action=action, symbol=symbol, qty=qty, reference_price=price)  # type: ignore[arg-type]


def main() -> int:
    logging_setup.configure("WARNING", json_lines=False)
    # Every rejection logs a warning by design; here the table below is the
    # readable record, so quiet the duplicate stream.
    logging.getLogger("trader.execution").setLevel(logging.ERROR)
    for suffix in ("", "-wal", "-shm"):
        Path(str(DB) + suffix).unlink(missing_ok=True)

    config = load_risk_config()
    broker = FakeBroker(
        prices={s: 100.0 for s in config.symbol_allowlist} | {"DOGE": 1.0},
        positions=[position("AAPL", 10, 100.0, 100.0)],
    )
    conn = connect(DB)

    attacks: list[tuple[str, Proposal]] = [
        ("oversized position", prop("buy", "AAPL", 10_000)),
        ("absurd size", prop("buy", "AAPL", 1e12)),
        ("off-allowlist symbol", prop("buy", "DOGE", 100, 1.0)),
        ("off-allowlist, lowercased", prop("buy", "doge", 100, 1.0)),
        ("negative quantity", prop("buy", "AAPL", -100)),
        ("zero quantity", prop("buy", "AAPL", 0)),
        ("NaN quantity", prop("buy", "AAPL", math.nan)),
        ("infinite quantity", prop("buy", "AAPL", math.inf)),
        ("zero reference price", prop("buy", "AAPL", 1e6, 0.0)),
        ("naked short", prop("sell", "AAPL", 5_000)),
        ("empty symbol", prop("buy", "", 1)),
        ("legitimate order", prop("buy", "AAPL", 5)),
    ]

    print(f"risk config: {config.source}")
    print(f"allowlist: {', '.join(sorted(config.symbol_allowlist))}\n")
    print(f"{'attack':<28} {'result':<10} checks that fired")
    print("-" * 96)

    executed = 0
    for label, proposal in attacks:
        cycle_id = open_cycle(conn, started_at=utcnow(), trading_day=trading_day_for(utcnow()))
        ctx = build_cycle_context(conn, broker, cycle_id=cycle_id)
        result = place_order(conn, broker, ctx, proposal, config)
        executed += int(result.executed)
        fired = ",".join(f.check for f in result.verdict.failures) or "-"
        print(f"{label:<28} {'EXECUTED' if result.executed else 'rejected':<10} {fired}")

    print("\n--- after hours ---")
    broker.is_open = False
    cycle_id = open_cycle(conn, started_at=utcnow(), trading_day=trading_day_for(utcnow()))
    ctx = build_cycle_context(conn, broker, cycle_id=cycle_id)
    result = place_order(conn, broker, ctx, prop("buy", "AAPL", 1), config)
    print(
        f"{'closed-market order':<28} {'EXECUTED' if result.executed else 'rejected':<10} "
        f"{','.join(f.check for f in result.verdict.failures)}"
    )
    broker.is_open = True

    print("\n--- rapid fire (30 attempts) ---")
    generous = replace(config, max_position_notional=1e9, max_total_exposure=1e9)
    before = len(broker.submitted)
    for _ in range(30):
        cycle_id = open_cycle(conn, started_at=utcnow(), trading_day=trading_day_for(utcnow()))
        ctx = build_cycle_context(conn, broker, cycle_id=cycle_id)
        place_order(conn, broker, ctx, prop("buy", "AAPL", 1), generous)
    print(
        f"30 attempts -> {len(broker.submitted) - before} submitted "
        f"(max_orders_per_hour={config.max_orders_per_hour})"
    )

    print("\n--- kill switch thrown after context was built ---")
    cycle_id = open_cycle(conn, started_at=utcnow(), trading_day=trading_day_for(utcnow()))
    ctx = build_cycle_context(conn, broker, cycle_id=cycle_id)
    set_flag(conn, KILL_SWITCH, "1", note="adversarial run")
    result = place_order(conn, broker, ctx, prop("buy", "AAPL", 1), config)
    print(
        f"stale context said kill_switch={ctx.kill_switch}; order was "
        f"{'EXECUTED' if result.executed else 'rejected'} "
        f"({','.join(f.check for f in result.verdict.failures)})"
    )
    set_flag(conn, KILL_SWITCH, "0")

    print("\n--- totals ---")
    print(f"orders that reached the broker: {len(broker.submitted)}")
    print(f"  {executed} from the attack table (expected 1: the legitimate order)")
    rows = conn.execute(
        "SELECT check_name, COUNT(*) n FROM risk_events GROUP BY check_name ORDER BY n DESC"
    ).fetchall()
    print("rejections by check:")
    for row in rows:
        print(f"  {row['n']:>4}  {row['check_name']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
