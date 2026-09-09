"""Phase 4: did the agent beat doing nothing?

Reads only logged history plus one benchmark series. Everything here is derived
from `cycles`, `account_snapshot`, `decisions` and `risk_events` — the eval never
re-runs a cycle, and running it twice over the same window gives the same answer.

Two comparisons, because "beat doing nothing" has two honest readings:
  * vs. cash — the account did nothing at all, 0% return
  * vs. buy-and-hold SPY over the same window
"""

from __future__ import annotations

import sqlite3
from collections import Counter, deque
from dataclasses import asdict, dataclass, field
from datetime import datetime

from trader.broker import Broker, BrokerError

#: The benchmark. Fixed, not configurable — spec.MD names SPY.
BENCHMARK_SYMBOL = "SPY"


class EvaluationError(RuntimeError):
    """Not enough logged history to evaluate the requested window."""


@dataclass(frozen=True, slots=True)
class RoundTrip:
    """One closed position: a buy matched to the sell that unwound it."""

    symbol: str
    qty: float
    entry_at: str
    exit_at: str
    entry_price: float
    exit_price: float
    holding_hours: float

    @property
    def realized_pl(self) -> float:
        return (self.exit_price - self.entry_price) * self.qty

    @property
    def is_win(self) -> bool:
        return self.realized_pl > 0


@dataclass
class Report:
    start: str
    end: str

    cycles: int = 0
    cycles_by_status: dict[str, int] = field(default_factory=dict)
    error_cycles: int = 0

    start_equity: float | None = None
    end_equity: float | None = None
    strategy_return_pct: float | None = None

    benchmark_symbol: str = BENCHMARK_SYMBOL
    benchmark_start_price: float | None = None
    benchmark_end_price: float | None = None
    benchmark_return_pct: float | None = None
    benchmark_note: str | None = None

    orders_submitted: int = 0
    orders_filled: int = 0
    orders_unreconciled: int = 0
    #: Orders whose price came from the risk layer's reference price rather
    #: than a confirmed fill. Run `trader reconcile` to drive this to zero.
    prices_estimated: int = 0
    round_trips: list[RoundTrip] = field(default_factory=list)
    open_at_end: dict[str, float] = field(default_factory=dict)
    realized_pl: float = 0.0

    proposals: int = 0
    rejections: int = 0
    rejections_by_check: dict[str, int] = field(default_factory=dict)

    prompt_tokens: int = 0
    completion_tokens: int = 0

    # --- derived -----------------------------------------------------------

    @property
    def wins(self) -> int:
        return sum(1 for t in self.round_trips if t.is_win)

    @property
    def win_rate_pct(self) -> float | None:
        if not self.round_trips:
            return None
        return 100.0 * self.wins / len(self.round_trips)

    @property
    def avg_holding_hours(self) -> float | None:
        if not self.round_trips:
            return None
        return sum(t.holding_hours for t in self.round_trips) / len(self.round_trips)

    @property
    def excess_vs_cash_pct(self) -> float | None:
        """Doing nothing earns 0%. This is simply the strategy's return."""
        return self.strategy_return_pct

    @property
    def excess_vs_benchmark_pct(self) -> float | None:
        if self.strategy_return_pct is None or self.benchmark_return_pct is None:
            return None
        return self.strategy_return_pct - self.benchmark_return_pct

    @property
    def verdict(self) -> str:
        """The one line spec.MD actually asked for."""
        if self.strategy_return_pct is None:
            return "no equity history in this window — cannot evaluate"
        if not self.orders_filled:
            return "the agent placed no orders in this window; there is nothing to judge"
        parts = [f"{_beat(self.excess_vs_cash_pct)} cash ({self.strategy_return_pct:+.2f}%)"]
        if self.excess_vs_benchmark_pct is not None:
            parts.append(
                f"{_beat(self.excess_vs_benchmark_pct)} buy-and-hold "
                f"{self.benchmark_symbol} ({self.excess_vs_benchmark_pct:+.2f}% excess)"
            )
        return "; ".join(parts)

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["round_trips"] = [asdict(t) | {"realized_pl": t.realized_pl} for t in self.round_trips]
        data.update(
            {
                "wins": self.wins,
                "losses": len(self.round_trips) - self.wins,
                "win_rate_pct": self.win_rate_pct,
                "avg_holding_hours": self.avg_holding_hours,
                "excess_vs_cash_pct": self.excess_vs_cash_pct,
                "excess_vs_benchmark_pct": self.excess_vs_benchmark_pct,
                "verdict": self.verdict,
            }
        )
        return data


# --- helpers ---------------------------------------------------------------


def _beat(excess: float) -> str:
    """A flat result matched the alternative; it did not lose to it."""
    if abs(excess) < 0.005:  # below the precision we print
        return "matched"
    return "beat" if excess > 0 else "lost to"


def _pct(start: float, end: float) -> float | None:
    return None if not start else 100.0 * (end / start - 1.0)


def _hours_between(a: str, b: str) -> float:
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 3600.0


def _fill_price(row: sqlite3.Row) -> float | None:
    """Actual fill if reconciled, else the price the risk layer sized on."""
    price = row["filled_avg_price"]
    return float(price) if price is not None else (
        float(row["reference_price"]) if row["reference_price"] is not None else None
    )


def _match_round_trips(fills: list[sqlite3.Row]) -> tuple[list[RoundTrip], dict[str, float]]:
    """FIFO-match sells against earlier buys, per symbol.

    FIFO because it is the convention and because it needs no configuration.
    A sell with no matching buy inside the window is skipped rather than
    guessed at — the position was opened before the window started, and
    inventing an entry price would put a fake number in the win rate.
    """
    lots: dict[str, deque[tuple[float, float, str]]] = {}
    trips: list[RoundTrip] = []

    for row in fills:
        symbol = row["symbol"]
        qty = float(row["filled_qty"] if row["filled_qty"] else row["qty"] or 0.0)
        price = _fill_price(row)
        when = row["filled_at"] or row["timestamp"]
        if qty <= 0 or price is None:
            continue

        if row["action"] == "buy":
            lots.setdefault(symbol, deque()).append((qty, price, when))
            continue

        remaining = qty
        queue = lots.get(symbol) or deque()
        while remaining > 0 and queue:
            lot_qty, lot_price, lot_when = queue[0]
            matched = min(remaining, lot_qty)
            trips.append(
                RoundTrip(
                    symbol=symbol,
                    qty=matched,
                    entry_at=lot_when,
                    exit_at=when,
                    entry_price=lot_price,
                    exit_price=price,
                    holding_hours=_hours_between(lot_when, when),
                )
            )
            remaining -= matched
            if matched >= lot_qty:
                queue.popleft()
            else:
                queue[0] = (lot_qty - matched, lot_price, lot_when)

    open_at_end = {
        symbol: sum(q for q, _, _ in queue) for symbol, queue in lots.items() if queue
    }
    return trips, open_at_end


# --- the report ------------------------------------------------------------


def evaluate(
    conn: sqlite3.Connection,
    start: str,
    end: str,
    *,
    broker: Broker | None = None,
) -> Report:
    """Build a report over trading days [start, end], inclusive."""
    if start > end:
        raise EvaluationError(f"start {start} is after end {end}")
    report = Report(start=start, end=end)

    cycles = conn.execute(
        "SELECT status, prompt_tokens, completion_tokens FROM cycles "
        "WHERE trading_day BETWEEN ? AND ?",
        (start, end),
    ).fetchall()
    if not cycles:
        raise EvaluationError(f"no cycles logged between {start} and {end}")
    report.cycles = len(cycles)
    report.cycles_by_status = dict(Counter(c["status"] for c in cycles))
    report.error_cycles = report.cycles_by_status.get("error", 0)
    report.prompt_tokens = sum(c["prompt_tokens"] or 0 for c in cycles)
    report.completion_tokens = sum(c["completion_tokens"] or 0 for c in cycles)

    # Equity: first and last snapshot inside the window. Snapshots are written
    # every cycle including market-closed ones, so this brackets the window
    # tightly even on a day the agent never traded.
    equity = conn.execute(
        "SELECT a.equity, a.captured_at FROM account_snapshot a "
        "JOIN cycles c USING (cycle_id) WHERE c.trading_day BETWEEN ? AND ? "
        "ORDER BY a.id",
        (start, end),
    ).fetchall()
    if equity:
        report.start_equity = float(equity[0]["equity"])
        report.end_equity = float(equity[-1]["equity"])
        report.strategy_return_pct = _pct(report.start_equity, report.end_equity)

    decisions = conn.execute(
        "SELECT d.* FROM decisions d JOIN cycles c USING (cycle_id) "
        "WHERE c.trading_day BETWEEN ? AND ? ORDER BY d.id",
        (start, end),
    ).fetchall()
    report.proposals = sum(1 for d in decisions if d["action"] in ("buy", "sell"))
    submitted = [d for d in decisions if d["broker_order_id"]]
    report.orders_submitted = len(submitted)
    report.orders_unreconciled = sum(1 for d in submitted if d["final_status"] is None)
    report.orders_filled = sum(1 for d in submitted if d["final_status"] == "filled")
    # Match over orders that filled *or* have not been reconciled yet, and
    # exclude ones the broker is known to have canceled/rejected/expired. An
    # unreconciled order falls back to the risk layer's reference price, so
    # count those separately — the win rate carries that caveat.
    usable = [d for d in submitted if d["final_status"] in ("filled", None)]
    report.prices_estimated = sum(
        1 for d in usable if d["filled_avg_price"] is None and d["reference_price"] is not None
    )

    report.round_trips, report.open_at_end = _match_round_trips(usable)
    report.realized_pl = sum(t.realized_pl for t in report.round_trips)

    risk_events = conn.execute(
        "SELECT r.check_name, COUNT(*) AS n FROM risk_events r JOIN cycles c USING (cycle_id) "
        "WHERE c.trading_day BETWEEN ? AND ? GROUP BY r.check_name ORDER BY n DESC",
        (start, end),
    ).fetchall()
    report.rejections_by_check = {r["check_name"]: r["n"] for r in risk_events}
    report.rejections = sum(1 for d in decisions if (d["risk_result"] or "").startswith("rejected"))

    _attach_benchmark(report, broker, len(equity))
    return report


def _attach_benchmark(report: Report, broker: Broker | None, sessions: int) -> None:
    """Buy-and-hold SPY over the same window, priced from daily closes."""
    if broker is None:
        report.benchmark_note = "no broker supplied; benchmark unavailable"
        return
    # Reach back far enough to cover the window plus weekends, then keep only
    # the bars inside it.
    span_days = _hours_between(f"{report.start}T00:00:00+00:00", f"{report.end}T23:59:59+00:00")
    wanted = max(2, min(30, int(span_days / 24) + 2))
    try:
        bars = broker.get_bars(BENCHMARK_SYMBOL, timeframe="1Day", limit=wanted)
    except BrokerError as exc:
        report.benchmark_note = f"benchmark unavailable: {exc}"
        return

    inside = [b for b in bars if report.start <= b["t"][:10] <= report.end]
    if len(inside) < 2:
        # A single session has no daily-close span to measure. Say so rather
        # than reporting a 0% benchmark, which would read as a real result.
        report.benchmark_note = (
            f"window covers {len(inside)} {BENCHMARK_SYMBOL} daily bar(s); "
            f"a buy-and-hold return needs at least 2. "
            f"{'Intraday window.' if sessions else ''}".strip()
        )
        if inside:
            report.benchmark_start_price = inside[0]["o"]
            report.benchmark_end_price = inside[-1]["c"]
            report.benchmark_return_pct = _pct(inside[0]["o"], inside[-1]["c"])
            report.benchmark_note += " Using open-to-close over the single session."
        return

    report.benchmark_start_price = inside[0]["o"]
    report.benchmark_end_price = inside[-1]["c"]
    report.benchmark_return_pct = _pct(inside[0]["o"], inside[-1]["c"])


def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.2f}%"


def format_report(r: Report) -> str:
    """The version I actually read. --json is for anything programmatic."""
    lines = [
        f"Evaluation  {r.start} .. {r.end}",
        "=" * 62,
        "",
        f"VERDICT: {r.verdict}",
        "",
        "Return",
        f"  strategy          {_fmt_pct(r.strategy_return_pct):>10}"
        f"   (${r.start_equity or 0:,.2f} -> ${r.end_equity or 0:,.2f})",
        f"  doing nothing         {'+0.00%':>6}   (cash)",
        f"  buy-and-hold {r.benchmark_symbol:<4} {_fmt_pct(r.benchmark_return_pct):>10}"
        + (
            f"   (${r.benchmark_start_price:,.2f} -> ${r.benchmark_end_price:,.2f})"
            if r.benchmark_start_price and r.benchmark_end_price
            else ""
        ),
        f"  excess vs cash    {_fmt_pct(r.excess_vs_cash_pct):>10}",
        f"  excess vs {r.benchmark_symbol:<7} {_fmt_pct(r.excess_vs_benchmark_pct):>10}",
    ]
    if r.benchmark_note:
        lines.append(f"  note: {r.benchmark_note}")

    lines += [
        "",
        "Trading",
        f"  orders submitted  {r.orders_submitted:>10}",
        f"  confirmed filled  {r.orders_filled:>10}",
        f"  unreconciled      {r.orders_unreconciled:>10}"
        + ("   (run `trader reconcile`)" if r.orders_unreconciled else ""),
        f"  round trips       {len(r.round_trips):>10}",
        f"  win rate          {'n/a' if r.win_rate_pct is None else f'{r.win_rate_pct:.1f}%':>10}"
        + (f"   ({r.wins}W / {len(r.round_trips) - r.wins}L)" if r.round_trips else ""),
        f"  avg holding       "
        f"{'n/a' if r.avg_holding_hours is None else f'{r.avg_holding_hours:.1f}h':>10}",
        f"  realized P&L      {f'${r.realized_pl:,.2f}':>10}",
    ]
    if r.prices_estimated:
        lines.append(
            f"  note: {r.prices_estimated} order(s) priced from the risk layer's "
            "reference price, not a confirmed fill"
        )
    if r.open_at_end:
        held = ", ".join(f"{sym} {qty:g}" for sym, qty in sorted(r.open_at_end.items()))
        lines.append(f"  still open        {held:>10}   (not counted in realized P&L)")

    lines += ["", "Risk rejections", f"  total             {r.rejections:>10}"]
    if r.rejections_by_check:
        lines += [
            f"    {n:>5}  {check}" for check, n in r.rejections_by_check.items()
        ]
    else:
        lines.append("    (none)")

    lines += [
        "",
        "Cycles",
        f"  total             {r.cycles:>10}",
        *[f"    {n:>5}  {status}" for status, n in sorted(r.cycles_by_status.items())],
        f"  tokens            {r.prompt_tokens:,} in / {r.completion_tokens:,} out",
    ]
    if r.round_trips:
        lines += ["", "Round trips"]
        for t in r.round_trips:
            lines.append(
                f"  {t.symbol:<6} {t.qty:>8g} @ ${t.entry_price:,.2f} -> ${t.exit_price:,.2f}  "
                f"{t.holding_hours:>7.1f}h  ${t.realized_pl:>+10,.2f}"
            )
    return "\n".join(lines)
