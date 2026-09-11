"""The code-side scanner: pure arithmetic over plain dicts, no broker needed.

The scan is what lets the agent see the whole allowlist without spending its
tool budget, so its numbers have to be right — a wrong opening range would put
a wrong stop on a real (paper) order.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from tests.fakes import FakeBroker
from trader.constants import MARKET_TZ
from trader.scanner import (
    MAX_RENDERED_SCAN_ROWS,
    ScanRow,
    classify_regime,
    compute_row,
    render_scan,
    scan_universe,
)

SESSION_OPEN = datetime(2026, 9, 11, 9, 30, tzinfo=MARKET_TZ)


def bar(minutes_after_open: int, o: float, h: float, low: float, c: float, v: float = 1000.0):
    t = SESSION_OPEN + timedelta(minutes=minutes_after_open)
    return {"t": t.isoformat(), "o": o, "h": h, "l": low, "c": c, "v": v}


def at(minutes_after_open: int) -> datetime:
    return SESSION_OPEN + timedelta(minutes=minutes_after_open)


def payload(bars, last=101.0, prev_close=100.0, today_volume=6e6, avg_daily_volume=3e6):
    return {
        "last": last,
        "prev_close": prev_close,
        "today_volume": today_volume,
        "avg_daily_volume": avg_daily_volume,
        "bars_5min": bars,
    }


OR_BARS = [
    bar(0, 100.0, 101.0, 99.5, 100.5),
    bar(5, 100.5, 101.5, 100.0, 101.0),
    bar(10, 101.0, 102.0, 100.5, 101.5),
]  # opening range: high 102.0, low 99.5


def test_percent_change_and_volume_ratio() -> None:
    # Halfway through the session (12:45 ET): 6M traded vs an expected
    # 3M-average x 0.5 elapsed = 1.5M, so volume runs at 4x its usual pace.
    row = compute_row("AAPL", payload(OR_BARS), now=at(195))
    assert row.pct_change == 1.0  # 101 vs 100
    assert row.vol_ratio == pytest.approx(4.0)


def test_volume_ratio_is_prorated_by_time_of_day() -> None:
    # Same cumulative volume reads as a higher ratio earlier in the session.
    early = compute_row("AAPL", payload(OR_BARS), now=at(39))  # 10% elapsed
    late = compute_row("AAPL", payload(OR_BARS), now=at(390))  # full session
    assert early.vol_ratio == pytest.approx(20.0)
    assert late.vol_ratio == pytest.approx(2.0)


def test_opening_range_needs_all_three_bars() -> None:
    row = compute_row("AAPL", payload(OR_BARS[:2]), now=at(20))
    assert row.or_high is None and row.or_low is None and row.or_pos is None


def test_opening_range_bounds() -> None:
    row = compute_row("AAPL", payload(OR_BARS), now=at(20))
    assert row.or_high == 102.0
    assert row.or_low == 99.5


def test_or_position_uses_the_last_closed_bar_not_the_wick() -> None:
    # A bar that wicked above the range but closed inside it is "inside".
    bars = [*OR_BARS, bar(15, 101.5, 103.0, 101.0, 101.8)]
    row = compute_row("AAPL", payload(bars), now=at(25))
    assert row.or_pos == "inside"

    # A closed bar above the range high is "above".
    bars = [*OR_BARS, bar(15, 101.5, 103.0, 101.4, 102.5)]
    row = compute_row("AAPL", payload(bars), now=at(25))
    assert row.or_pos == "above"

    # Below the range low is "below".
    bars = [*OR_BARS, bar(15, 100.0, 100.2, 98.0, 99.0)]
    row = compute_row("AAPL", payload(bars), now=at(25))
    assert row.or_pos == "below"


def test_a_bar_still_forming_does_not_count() -> None:
    # The 09:45 bar closed above the range, but "now" is 09:47 — it has not
    # closed yet, so the symbol is still judged from inside the range.
    bars = [*OR_BARS, bar(15, 101.5, 103.0, 101.4, 102.5)]
    row = compute_row("AAPL", payload(bars), now=at(17))
    assert row.or_pos is None  # no *closed* bar after the range yet


def test_vwap_delta_is_computed_from_closed_bars() -> None:
    # Uniform bars: VWAP equals the typical price, so last=101 sits above it.
    row = compute_row("AAPL", payload(OR_BARS), now=at(20))
    assert row.vwap_delta_pct is not None
    assert row.vwap_delta_pct > 0


def test_missing_inputs_degrade_to_none_not_a_crash() -> None:
    row = compute_row("AAPL", {"last": None, "prev_close": None, "bars_5min": []}, now=at(20))
    assert row == ScanRow("AAPL", None, None, None, None, None, None, None)


# --- regime ------------------------------------------------------------------


def _period(start_min: int, base: float, step: float) -> list[dict]:
    """Three 5-minute bars forming one rising (or falling) 15-minute period."""
    return [
        bar(start_min + i * 5, base + i * step, base + i * step + 0.5,
            base + i * step - 0.5, base + (i + 1) * step)
        for i in range(3)
    ]


def test_regime_up_when_highs_rise_and_close_is_above_midpoint() -> None:
    bars = _period(0, 100.0, 0.5) + _period(15, 101.5, 0.5)
    assert classify_regime(bars, now=at(30)) == "up"


def test_regime_down_on_the_mirror() -> None:
    bars = _period(0, 100.0, -0.5) + _period(15, 98.5, -0.5)
    assert classify_regime(bars, now=at(30)) == "down"


def test_regime_needs_two_complete_periods() -> None:
    assert classify_regime(_period(0, 100.0, 0.5), now=at(15)) is None
    assert classify_regime([], now=at(30)) is None


# --- orchestration and rendering ---------------------------------------------


def test_scan_universe_computes_rows_and_regime() -> None:
    broker = FakeBroker(prices={"AAPL": 100.0, "QQQ": 500.0})
    rows, regime = scan_universe(broker, ["AAPL", "QQQ"], now=at(60))
    assert {r.symbol for r in rows} == {"AAPL", "QQQ"}
    assert regime in ("up", "down", "chop")  # flat synthetic bars: some verdict


def test_render_is_capped_and_discloses_truncation() -> None:
    rows = [
        compute_row(f"S{i:03d}", payload(OR_BARS), now=at(20))
        for i in range(MAX_RENDERED_SCAN_ROWS + 5)
    ]
    lines = render_scan(rows, regime="chop", held=set())
    # header + column line + capped rows + truncation note
    assert len(lines) == 2 + MAX_RENDERED_SCAN_ROWS + 1
    assert "5 quieter names not shown" in lines[-1]


def test_held_names_sort_first_and_are_marked() -> None:
    quiet = compute_row("QUIET", payload(OR_BARS, last=100.0, prev_close=100.0), now=at(20))
    mover = compute_row("MOVER", payload(OR_BARS, last=105.0, prev_close=100.0), now=at(20))
    held = compute_row("HELD", payload(OR_BARS, last=100.0, prev_close=100.0), now=at(20))
    lines = render_scan([quiet, mover, held], regime="up", held={"HELD"})
    body = lines[2:]
    assert body[0].startswith("HELD") and "*held*" in body[0]
    assert body[1].startswith("MOVER")
