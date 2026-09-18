"""Tests for ORB historical backtest."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from trader.backtest import (
    BacktestReport,
    Trade,
    _compute_orb,
    _find_entry_bar,
    _simulate_trade,
    backtest_orb,
    format_backtest,
    save_backtest,
)
from trader.constants import MARKET_TZ


def test_compute_orb_valid_range() -> None:
    """ORB is computed from three 5-minute bars in 09:30-09:45 ET window."""
    bars = [
        {"t": "2026-09-17T09:30:00-04:00", "h": 100.5, "l": 100.0, "c": 100.3, "v": 1000},
        {"t": "2026-09-17T09:35:00-04:00", "h": 101.0, "l": 100.2, "c": 100.8, "v": 1200},
        {"t": "2026-09-17T09:40:00-04:00", "h": 101.0, "l": 100.5, "c": 100.7, "v": 1100},
    ]
    
    setup = _compute_orb(bars)
    
    assert setup is not None
    assert setup.or_high == 101.0
    assert setup.or_low == 100.0
    assert setup.or_midpoint == 100.5
    assert setup.range_height == 1.0
    assert 0.99 < setup.range_height_pct < 1.0  # ~1% of ~100


def test_compute_orb_range_too_small() -> None:
    """ORB with range <0.15% is rejected."""
    bars = [
        {"t": "2026-09-17T09:30:00-04:00", "h": 100.05, "l": 100.0, "c": 100.03, "v": 1000},
        {"t": "2026-09-17T09:35:00-04:00", "h": 100.10, "l": 100.02, "c": 100.08, "v": 1200},
        {"t": "2026-09-17T09:40:00-04:00", "h": 100.10, "l": 100.05, "c": 100.07, "v": 1100},
    ]
    
    setup = _compute_orb(bars)
    
    assert setup is None  # Range ~0.1% is below MIN_RANGE_HEIGHT_PCT


def test_compute_orb_range_too_large() -> None:
    """ORB with range >1.5% is rejected (already the move)."""
    bars = [
        {"t": "2026-09-17T09:30:00-04:00", "h": 102.0, "l": 100.0, "c": 101.0, "v": 1000},
        {"t": "2026-09-17T09:35:00-04:00", "h": 102.0, "l": 100.0, "c": 101.5, "v": 1200},
        {"t": "2026-09-17T09:40:00-04:00", "h": 102.0, "l": 100.0, "c": 101.0, "v": 1100},
    ]
    
    setup = _compute_orb(bars)
    
    assert setup is None  # Range 2% is above MAX_RANGE_HEIGHT_PCT


def test_compute_orb_incomplete_range() -> None:
    """ORB requires all three 09:30-09:45 bars."""
    bars = [
        {"t": "2026-09-17T09:30:00-04:00", "h": 100.5, "l": 100.0, "c": 100.3, "v": 1000},
        {"t": "2026-09-17T09:35:00-04:00", "h": 101.0, "l": 100.2, "c": 100.8, "v": 1200},
        # Missing 09:40 bar
    ]
    
    setup = _compute_orb(bars)
    
    assert setup is None


def test_find_entry_bar_long_breakout() -> None:
    """Long entry: first 5-minute close above range high."""
    from trader.backtest import ORBSetup
    
    setup = ORBSetup(
        symbol="SPY",
        date="2026-09-17",
        or_high=101.0,
        or_low=100.0,
        or_midpoint=100.5,
        range_height=1.0,
        range_height_pct=1.0,
        regime="up",
    )
    
    bars = [
        {"t": "2026-09-17T09:30:00-04:00", "h": 100.5, "l": 100.0, "c": 100.3, "v": 1000},
        {"t": "2026-09-17T09:35:00-04:00", "h": 101.0, "l": 100.2, "c": 100.8, "v": 1200},
        {"t": "2026-09-17T09:40:00-04:00", "h": 101.0, "l": 100.5, "c": 100.7, "v": 1100},
        # Breakout bar: closes above 101.0
        {"t": "2026-09-17T09:45:00-04:00", "o": 100.7, "h": 101.5, "l": 100.7, "c": 101.2, "v": 1500},
    ]
    
    entry_bar = _find_entry_bar(bars, setup, "long")
    
    assert entry_bar is not None
    assert float(entry_bar["c"]) == 101.2


def test_find_entry_bar_short_breakout() -> None:
    """Short entry: first 5-minute close below range low."""
    from trader.backtest import ORBSetup
    
    setup = ORBSetup(
        symbol="SPY",
        date="2026-09-17",
        or_high=101.0,
        or_low=100.0,
        or_midpoint=100.5,
        range_height=1.0,
        range_height_pct=1.0,
        regime="down",
    )
    
    bars = [
        {"t": "2026-09-17T09:30:00-04:00", "h": 100.5, "l": 100.0, "c": 100.3, "v": 1000},
        {"t": "2026-09-17T09:35:00-04:00", "h": 101.0, "l": 100.2, "c": 100.8, "v": 1200},
        {"t": "2026-09-17T09:40:00-04:00", "h": 101.0, "l": 100.5, "c": 100.7, "v": 1100},
        # Breakdown bar: closes below 100.0
        {"t": "2026-09-17T09:45:00-04:00", "o": 100.7, "h": 100.7, "l": 99.5, "c": 99.7, "v": 1500},
    ]
    
    entry_bar = _find_entry_bar(bars, setup, "short")
    
    assert entry_bar is not None
    assert float(entry_bar["c"]) == 99.7


def test_find_entry_bar_no_breakout() -> None:
    """No entry if price stays inside range."""
    from trader.backtest import ORBSetup
    
    setup = ORBSetup(
        symbol="SPY",
        date="2026-09-17",
        or_high=101.0,
        or_low=100.0,
        or_midpoint=100.5,
        range_height=1.0,
        range_height_pct=1.0,
        regime="chop",
    )
    
    bars = [
        {"t": "2026-09-17T09:30:00-04:00", "h": 100.5, "l": 100.0, "c": 100.3, "v": 1000},
        {"t": "2026-09-17T09:35:00-04:00", "h": 101.0, "l": 100.2, "c": 100.8, "v": 1200},
        {"t": "2026-09-17T09:40:00-04:00", "h": 101.0, "l": 100.5, "c": 100.7, "v": 1100},
        # Stays inside range
        {"t": "2026-09-17T09:45:00-04:00", "o": 100.7, "h": 101.0, "l": 100.5, "c": 100.8, "v": 1500},
    ]
    
    assert _find_entry_bar(bars, setup, "long") is None
    assert _find_entry_bar(bars, setup, "short") is None


def test_simulate_trade_long_target_hit() -> None:
    """Long trade hits target (1.5R)."""
    from trader.backtest import ORBSetup
    
    setup = ORBSetup(
        symbol="SPY",
        date="2026-09-17",
        or_high=101.0,
        or_low=100.0,
        or_midpoint=100.5,
        range_height=1.0,
        range_height_pct=1.0,
        regime="up",
    )
    
    entry_bar = {"t": "2026-09-17T09:45:00-04:00", "c": 101.2, "v": 1500}
    
    bars = [
        entry_bar,
        # Run to target (entry 101.2 + 1.5 * 1.0 = 102.7)
        {"t": "2026-09-17T09:50:00-04:00", "h": 102.0, "l": 101.0, "c": 101.5, "v": 1300},
        {"t": "2026-09-17T09:55:00-04:00", "h": 102.8, "l": 101.5, "c": 102.5, "v": 1400},
    ]
    
    trade = _simulate_trade(setup, entry_bar, bars, "long")
    
    assert trade.entry_price == 101.2
    assert trade.stop_price == 100.5  # Midpoint
    assert trade.target_price == 102.7  # Entry + 1.5 * range
    assert trade.exit_reason == "target"
    assert trade.exit_price == 102.7
    assert trade.r_multiple == pytest.approx(1.5 / 0.7 * 0.7, abs=0.1)  # ~1.5R


def test_simulate_trade_long_stopped() -> None:
    """Long trade hits stop (range midpoint)."""
    from trader.backtest import ORBSetup
    
    setup = ORBSetup(
        symbol="SPY",
        date="2026-09-17",
        or_high=101.0,
        or_low=100.0,
        or_midpoint=100.5,
        range_height=1.0,
        range_height_pct=1.0,
        regime="up",
    )
    
    entry_bar = {"t": "2026-09-17T09:45:00-04:00", "c": 101.2, "v": 1500}
    
    bars = [
        entry_bar,
        # Reversal hits stop
        {"t": "2026-09-17T09:50:00-04:00", "h": 101.5, "l": 100.3, "c": 100.6, "v": 1300},
    ]
    
    trade = _simulate_trade(setup, entry_bar, bars, "long")
    
    assert trade.exit_reason == "stop"
    assert trade.exit_price == 100.5
    assert trade.r_multiple < 0  # Loss


def test_simulate_trade_eod_flatten() -> None:
    """Trade flattened at 15:30 (playbook rule)."""
    from trader.backtest import ORBSetup
    
    setup = ORBSetup(
        symbol="SPY",
        date="2026-09-17",
        or_high=101.0,
        or_low=100.0,
        or_midpoint=100.5,
        range_height=1.0,
        range_height_pct=1.0,
        regime="up",
    )
    
    entry_bar = {"t": "2026-09-17T09:45:00-04:00", "c": 101.2, "v": 1500}
    
    bars = [
        entry_bar,
        # Runs but doesn't hit target, flattened at 15:30
        {"t": "2026-09-17T15:30:00-04:00", "h": 102.0, "l": 101.5, "c": 101.8, "v": 1000},
    ]
    
    trade = _simulate_trade(setup, entry_bar, bars, "long")
    
    assert trade.exit_reason == "eod"
    assert trade.exit_price == 101.8


def test_backtest_stub_mode() -> None:
    """Stub backtest runs with fixture data."""
    report = backtest_orb(["SPY", "QQQ"], "2026-09-17", "2026-09-18", stub=True)
    
    assert report.stub_mode is True
    assert report.total_trades > 0  # Fixture has known setups
    assert all(t.symbol in ["SPY", "QQQ"] for t in report.trades)
    assert all("2026-09-17" <= t.date <= "2026-09-18" for t in report.trades)


def test_backtest_report_metrics() -> None:
    """Report computes win rate, avg R, total R correctly."""
    report = BacktestReport(
        symbols=["SPY"],
        start_date="2026-09-17",
        end_date="2026-09-18",
        stub_mode=True,
        trades=[
            Trade(
                symbol="SPY",
                date="2026-09-17",
                direction="long",
                entry_time="2026-09-17T09:45:00-04:00",
                entry_price=101.0,
                stop_price=100.5,
                target_price=102.25,
                exit_time="2026-09-17T10:00:00-04:00",
                exit_price=102.25,
                exit_reason="target",
                regime="up",
                pnl=1.25,
                r_multiple=1.5,
            ),
            Trade(
                symbol="SPY",
                date="2026-09-18",
                direction="short",
                entry_time="2026-09-18T09:45:00-04:00",
                entry_price=100.0,
                stop_price=100.5,
                target_price=99.25,
                exit_time="2026-09-18T09:50:00-04:00",
                exit_price=100.5,
                exit_reason="stop",
                regime="down",
                pnl=-0.5,
                r_multiple=-1.0,
            ),
        ],
    )
    
    assert report.total_trades == 2
    assert report.winners == 1
    assert report.losers == 1
    assert report.win_rate == 0.5
    assert report.avg_r == (1.5 - 1.0) / 2
    assert report.total_r == 0.5


def test_backtest_by_regime() -> None:
    """Report breaks down stats by regime."""
    report = backtest_orb(["SPY"], "2026-09-17", "2026-09-18", stub=True)
    
    by_regime = report.by_regime()
    
    assert "up" in by_regime
    assert "down" in by_regime
    assert all("trades" in stats for stats in by_regime.values())
    assert all("win_rate" in stats for stats in by_regime.values())
    assert all("avg_r" in stats for stats in by_regime.values())


def test_backtest_by_hour() -> None:
    """Report breaks down stats by entry hour."""
    report = backtest_orb(["SPY"], "2026-09-17", "2026-09-18", stub=True)
    
    by_hour = report.by_hour()
    
    assert len(by_hour) > 0
    # All entries should be in primary window (09:45-12:30)
    assert all(9 <= hour < 13 for hour in by_hour.keys())


def test_save_backtest(tmp_path: Path) -> None:
    """Backtest report is saved to JSON file."""
    report = backtest_orb(["SPY"], "2026-09-17", "2026-09-18", stub=True)
    
    output_path = save_backtest(report, output_dir=tmp_path)
    
    assert output_path.exists()
    assert output_path.suffix == ".json"
    assert "orb_SPY" in output_path.name
    
    # Round-trip
    import json
    data = json.loads(output_path.read_text())
    assert data["symbols"] == ["SPY"]
    assert data["stub_mode"] is True


def test_format_backtest_human_readable() -> None:
    """Format backtest produces human-readable summary."""
    report = backtest_orb(["SPY", "QQQ"], "2026-09-17", "2026-09-18", stub=True)
    
    output = format_backtest(report)
    
    assert "ORB Historical Backtest" in output
    assert "SPY, QQQ" in output
    assert "Total trades:" in output
    assert "Win rate:" in output
    assert "By Regime:" in output
    assert "By Entry Hour" in output
    assert "first-evidence pass" in output
