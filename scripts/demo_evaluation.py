"""Build a synthetic multi-day history and print the Phase 4 report.

Real evaluation needs weeks of logged cycles. This fabricates a plausible five
days — wins, losses, an unclosed position, and a spread of risk rejections — so
the report format can be reviewed today. No credentials, no account touched.

    uv run python scripts/demo_evaluation.py
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datetime import UTC, datetime
from typing import ClassVar

from trader.db import close_cycle, connect, open_cycle
from trader.evaluate import evaluate, format_report

DB = Path("data/demo_evaluation.sqlite3")
DAYS = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-05"]


class SpyBars:
    """SPY closes over the same week: up 1.9% — the bar the agent has to clear."""

    SERIES: ClassVar[list[tuple[str, float, float]]] = [
        ("2026-09-01", 760.0, 762.0),
        ("2026-09-02", 762.0, 758.0),
        ("2026-09-03", 758.0, 765.0),
        ("2026-09-04", 765.0, 771.0),
        ("2026-09-05", 771.0, 774.5),
    ]

    def get_bars(self, symbol: str, *, timeframe: str, limit: int) -> list[dict[str, object]]:
        return [
            {"t": f"{d}T04:00:00+00:00", "o": o, "h": max(o, c), "l": min(o, c), "c": c, "v": 1e6}
            for d, o, c in self.SERIES
        ]


def ts(day: str, hour: int) -> datetime:
    return datetime.fromisoformat(f"{day}T{hour:02d}:00:00+00:00").astimezone(UTC)


def cycle(
    conn: sqlite3.Connection, day: str, hour: int, equity: float, status: str = "ok"
) -> int:
    cycle_id = open_cycle(conn, started_at=ts(day, hour), trading_day=day)
    conn.execute(
        "INSERT INTO account_snapshot(cycle_id, equity, last_equity, cash, captured_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (cycle_id, equity, equity, equity * 0.4, ts(day, hour).isoformat()),
    )
    close_cycle(
        conn,
        cycle_id,
        status=status,
        duration_ms=4200,
        model="claude-sonnet-5",
        prompt_tokens=2400,
        completion_tokens=180,
    )
    return cycle_id


def order(
    conn: sqlite3.Connection,
    cycle_id: int,
    day: str,
    hour: int,
    action: str,
    symbol: str,
    qty: float,
    price: float,
) -> None:
    when = ts(day, hour).isoformat()
    conn.execute(
        "INSERT INTO decisions(cycle_id, timestamp, action, symbol, qty, risk_result, "
        "broker_order_id, outcome, reference_price, filled_qty, filled_avg_price, "
        "filled_at, final_status) VALUES (?,?,?,?,?,'approved',?,'filled',?,?,?,?,'filled')",
        (cycle_id, when, action, symbol, qty, f"o-{symbol}-{day}-{hour}", price, qty, price, when),
    )


def rejection(
    conn: sqlite3.Connection, cycle_id: int, day: str, hour: int, check: str, reason: str
) -> None:
    when = ts(day, hour).isoformat()
    conn.execute(
        "INSERT INTO decisions(cycle_id, timestamp, action, symbol, qty, risk_result, outcome) "
        "VALUES (?,?,'buy','NVDA',40,?,'rejected')",
        (cycle_id, when, f"rejected:{check}"),
    )
    conn.execute(
        "INSERT INTO risk_events(cycle_id, timestamp, check_name, reason, proposal) "
        "VALUES (?,?,?,?,'{}')",
        (cycle_id, when, check, reason),
    )


def main() -> int:
    for suffix in ("", "-wal", "-shm"):
        Path(str(DB) + suffix).unlink(missing_ok=True)
    conn = connect(DB)

    # Mon: open AAPL, get an oversized proposal rejected.
    d = DAYS[0]
    c = cycle(conn, d, 14, 100_000.0)
    order(conn, c, d, 14, "buy", "AAPL", 15, 300.00)
    c = cycle(conn, d, 18, 100_180.0)
    rejection(conn, c, d, 18, "max_position_notional", "resulting position exceeds $5,000")

    # Tue: add MSFT, hit the hourly rate limit twice.
    d = DAYS[1]
    c = cycle(conn, d, 14, 100_050.0)
    order(conn, c, d, 14, "buy", "MSFT", 9, 520.00)
    c = cycle(conn, d, 15, 99_900.0)
    for hour in (15, 16):
        rejection(conn, c, d, hour, "max_orders_per_hour", "6 orders in the last hour")
    cycle(conn, d, 19, 99_820.0)

    # Wed: close AAPL at a profit; a symbol outside the allowlist is refused.
    d = DAYS[2]
    c = cycle(conn, d, 15, 100_400.0)
    order(conn, c, d, 15, "sell", "AAPL", 15, 312.50)
    c = cycle(conn, d, 17, 100_600.0)
    rejection(conn, c, d, 17, "symbol_allowlist", "'TSLA' is not on the symbol allowlist")

    # Thu: close MSFT at a loss; one cycle fails on a broker timeout.
    d = DAYS[3]
    c = cycle(conn, d, 14, 100_500.0)
    order(conn, c, d, 14, "sell", "MSFT", 9, 511.00)
    cycle(conn, d, 16, 100_450.0, status="error")
    cycle(conn, d, 19, 100_520.0)

    # Fri: open QQQ and carry it into the weekend.
    d = DAYS[4]
    c = cycle(conn, d, 14, 100_600.0)
    order(conn, c, d, 14, "buy", "QQQ", 8, 600.00)
    cycle(conn, d, 19, 100_690.0)

    print(format_report(evaluate(conn, DAYS[0], DAYS[-1], broker=SpyBars())))
    print(
        f"\n(synthetic data in {DB}; `TRADER_DB_PATH={DB} uv run trader evaluate "
        f"--start {DAYS[0]} --end {DAYS[-1]} --json` for the machine-readable form)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
