"""Code-side market scan: breadth lives here, not in the model's tool budget.

The agent's context budget can afford roughly two `get_bars` calls per cycle,
which caps how much of the market it can *see* at two symbols. This module
inverts that: one batched broker call (`Broker.get_scan_data`) sweeps the whole
allowlist, the arithmetic the playbook needs (opening range, rough VWAP, volume
ratio, regime) happens in Python, and the result is rendered into the state
block as a compact table. Fifty symbols summarised here cost a few hundred
tokens; fifty symbols explored through tools is impossible.

Everything in this module is a pure function over plain dicts so it is unit
tested without a broker, and nothing here imports a vendor SDK — the fetch
stays in trader.broker where test_no_bypass.py confines it.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from trader.broker import Broker
from trader.constants import MARKET_TZ

log = logging.getLogger("trader.scanner")

#: The playbook's opening range: 09:30-09:45 ET, i.e. the first three 5-minute bars.
OR_START = (9, 30)
OR_END = (9, 45)

#: Rendered rows are capped so the scan section stays bounded in the prompt.
MAX_RENDERED_SCAN_ROWS = 20


@dataclass(frozen=True, slots=True)
class ScanRow:
    """One symbol's precomputed view. None means the input wasn't available."""

    symbol: str
    last: float | None
    pct_change: float | None  # vs previous close, in percent
    #: Today's cumulative volume vs the 10-day average *pro-rated by session
    #: time elapsed*, so ~1.0 means "normal pace for this time of day". Linear
    #: pro-rating understates the usual open/close volume U-shape slightly,
    #: which errs toward fewer, not more, "elevated volume" reads.
    vol_ratio: float | None
    or_low: float | None
    or_high: float | None
    or_pos: str | None  # above|inside|below, from the last *closed* 5m bar
    vwap_delta_pct: float | None  # last vs rough session VWAP, in percent


def _bar_local(bar: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(str(bar["t"])).astimezone(MARKET_TZ)


def _opening_range(bars: Sequence[dict[str, Any]]) -> tuple[float, float] | None:
    """High/low of 09:30-09:45 ET. None until all three bars exist."""
    window = [
        b
        for b in bars
        if OR_START <= (_bar_local(b).hour, _bar_local(b).minute) < OR_END
    ]
    if len(window) < 3:
        return None
    return max(float(b["h"]) for b in window), min(float(b["l"]) for b in window)


def _session_vwap(bars: Sequence[dict[str, Any]]) -> float | None:
    """Volume-weighted typical price over today's bars. The playbook's 'rough VWAP'."""
    volume = sum(float(b["v"]) for b in bars)
    if volume <= 0:
        return None
    weighted = sum(
        (float(b["h"]) + float(b["l"]) + float(b["c"])) / 3 * float(b["v"]) for b in bars
    )
    return weighted / volume


def _closed_bars(bars: Sequence[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Bars whose 5-minute window has fully elapsed. Wicks-in-progress don't count."""
    cutoff = now.astimezone(MARKET_TZ)
    return [b for b in bars if (_bar_local(b).hour * 60 + _bar_local(b).minute + 5)
            <= cutoff.hour * 60 + cutoff.minute]


def _session_elapsed_fraction(now: datetime) -> float:
    """Fraction of the 390-minute RTH session elapsed, clamped to [0.05, 1]."""
    local = now.astimezone(MARKET_TZ)
    minutes = (local.hour - OR_START[0]) * 60 + local.minute - OR_START[1]
    return min(1.0, max(minutes / 390.0, 0.05))


def compute_row(symbol: str, data: dict[str, Any], *, now: datetime) -> ScanRow:
    last = data.get("last")
    prev_close = data.get("prev_close")
    today_volume = data.get("today_volume")
    avg_volume = data.get("avg_daily_volume")
    bars = data.get("bars_5min") or []

    pct_change = (
        (last - prev_close) / prev_close * 100
        if last is not None and prev_close
        else None
    )
    vol_ratio = (
        today_volume / (avg_volume * _session_elapsed_fraction(now))
        if today_volume and avg_volume
        else None
    )

    opening_range = _opening_range(bars)
    or_high = or_low = None
    or_pos = None
    closed = _closed_bars(bars, now)
    if opening_range is not None:
        or_high, or_low = opening_range
        # Position is judged from the last closed bar's close, per the playbook
        # ("wicks do not count"), not from the latest trade.
        after_range = [b for b in closed if (_bar_local(b).hour, _bar_local(b).minute) >= OR_END]
        if after_range:
            last_close = float(after_range[-1]["c"])
            if last_close > or_high:
                or_pos = "above"
            elif last_close < or_low:
                or_pos = "below"
            else:
                or_pos = "inside"

    vwap = _session_vwap(closed)
    vwap_delta_pct = (
        (last - vwap) / vwap * 100 if last is not None and vwap else None
    )

    return ScanRow(
        symbol=symbol,
        last=last,
        pct_change=pct_change,
        vol_ratio=vol_ratio,
        or_low=or_low,
        or_high=or_high,
        or_pos=or_pos,
        vwap_delta_pct=vwap_delta_pct,
    )


def classify_regime(qqq_bars: Sequence[dict[str, Any]], *, now: datetime) -> str | None:
    """The playbook's regime filter, computed instead of asked of the model.

    `up` if the last closed 15-minute period closed above the session midpoint
    and 15-minute highs are rising; `down` on the mirror; `chop` otherwise.
    None until there are at least two full 15-minute periods to compare.
    """
    closed = _closed_bars(qqq_bars, now)
    if not closed:
        return None
    # Aggregate 5-minute bars into 15-minute periods, exchange-local.
    periods: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for bar in closed:
        local = _bar_local(bar)
        periods.setdefault((local.hour, local.minute - local.minute % 15), []).append(bar)
    complete = [bars for _, bars in sorted(periods.items()) if len(bars) == 3]
    if len(complete) < 2:
        return None
    highs = [max(float(b["h"]) for b in bars) for bars in complete]
    closes = [float(bars[-1]["c"]) for bars in complete]
    session_high = max(float(b["h"]) for b in closed)
    session_low = min(float(b["l"]) for b in closed)
    midpoint = (session_high + session_low) / 2
    if closes[-1] > midpoint and highs[-1] >= highs[-2]:
        return "up"
    lows = [min(float(b["l"]) for b in bars) for bars in complete]
    if closes[-1] < midpoint and lows[-1] <= lows[-2]:
        return "down"
    return "chop"


def scan_universe(
    broker: Broker, symbols: Sequence[str], *, now: datetime
) -> tuple[list[ScanRow], str | None]:
    """Fetch once, compute everything. Raises BrokerError; the caller degrades."""
    data = broker.get_scan_data(symbols)
    rows = [compute_row(sym, payload, now=now) for sym, payload in sorted(data.items())]
    qqq = data.get("QQQ", {}).get("bars_5min") or []
    regime = classify_regime(qqq, now=now)
    return rows, regime


def _fmt(value: float | None, spec: str = ".2f", suffix: str = "") -> str:
    return "-" if value is None else f"{value:{spec}}{suffix}"


def _rank(row: ScanRow, held: set[str]) -> tuple[int, float]:
    """Held names first, then names outside their range, then by |% change|."""
    if row.symbol in held:
        tier = 0
    elif row.or_pos in ("above", "below"):
        tier = 1
    else:
        tier = 2
    return tier, -abs(row.pct_change or 0.0)


def render_scan(
    rows: Sequence[ScanRow],
    *,
    regime: str | None,
    held: set[str],
    max_rows: int = MAX_RENDERED_SCAN_ROWS,
) -> list[str]:
    """Compact fixed-shape lines for the state block. Bounded by construction."""
    header = f"regime(QQQ): {regime or 'unknown'}"
    lines = [
        header,
        "symbol | last | chg% | vol_x | OR low-high | OR_pos | vwap_d%",
    ]
    ranked = sorted(rows, key=lambda r: _rank(r, held))
    for row in ranked[:max_rows]:
        or_range = (
            f"{row.or_low:.2f}-{row.or_high:.2f}"
            if row.or_low is not None and row.or_high is not None
            else "-"
        )
        marker = " *held*" if row.symbol in held else ""
        lines.append(
            f"{row.symbol} | {_fmt(row.last)} | {_fmt(row.pct_change, '+.1f')} | "
            f"{_fmt(row.vol_ratio, '.1f', 'x')} | {or_range} | {row.or_pos or '-'} | "
            f"{_fmt(row.vwap_delta_pct, '+.2f')}{marker}"
        )
    if len(ranked) > max_rows:
        lines.append(f"({len(ranked) - max_rows} quieter names not shown)")
    return lines
