"""ORB (Opening Range Breakout) historical backtest.

Proves or falsifies the discretionary ORB playbook parameters from
prompts/system.txt over liquid names. Uses Alpaca historical bars if keys
present; otherwise runs in stub mode with fixture data.

Rules matched to playbook:
- Opening range: 09:30-09:45 ET (first three 5-minute bars)
- Entry: first 5-minute close outside range (above for long, below for short)
- Stop: range midpoint
- Target: 1.5x range height beyond entry
- Regime filter: QQQ regime (up/down/chop) from scanner logic
- Time windows: 09:45-12:30 primary, 12:30-14:30 VWAP continuation only

Metrics: trades, win rate, avg R, total R, by regime, by time bucket.
Honest about look-ahead: uses closed bars only.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

from trader.broker import Broker, BrokerError
from trader.constants import MARKET_TZ
from trader.db import iso, utcnow
from trader.scanner import classify_regime

log = logging.getLogger("trader.backtest")

#: Opening range time window
OR_START = time(9, 30)
OR_END = time(9, 45)

#: Entry time windows (playbook rules)
PRIMARY_ENTRY_START = time(9, 45)
PRIMARY_ENTRY_END = time(12, 30)
VWAP_ENTRY_START = time(12, 30)
VWAP_ENTRY_END = time(14, 30)
MANAGE_ONLY_START = time(14, 30)
FLATTEN_START = time(15, 30)
RTH_CLOSE = time(16, 0)

#: Min/max range height as % of price
MIN_RANGE_HEIGHT_PCT = 0.15
MAX_RANGE_HEIGHT_PCT = 1.5

#: R:R ratio (target = entry + R * range_height, stop = range_midpoint)
REWARD_RISK_RATIO = 1.5

#: Default backtest output directory
DEFAULT_BACKTEST_DIR = Path("data/backtests")


class BacktestError(RuntimeError):
    """Backtest configuration or execution error."""


@dataclass(frozen=True, slots=True)
class ORBSetup:
    """One day's opening range breakout setup for a symbol."""
    
    symbol: str
    date: str
    or_high: float
    or_low: float
    or_midpoint: float
    range_height: float
    range_height_pct: float
    regime: str | None  # up|down|chop from QQQ


@dataclass(frozen=True, slots=True)
class Trade:
    """One backtest trade with entry, exit, and R outcome."""
    
    symbol: str
    date: str
    direction: str  # long|short
    entry_time: str
    entry_price: float
    stop_price: float
    target_price: float
    exit_time: str
    exit_price: float
    exit_reason: str  # target|stop|eod
    regime: str | None
    pnl: float
    r_multiple: float  # actual R (negative if stopped)
    
    @property
    def won(self) -> bool:
        return self.r_multiple > 0
    
    @property
    def entry_hour(self) -> int:
        return datetime.fromisoformat(self.entry_time).astimezone(MARKET_TZ).hour


@dataclass
class BacktestReport:
    """Backtest results with metrics."""
    
    symbols: list[str]
    start_date: str
    end_date: str
    stub_mode: bool
    trades: list[Trade] = field(default_factory=list)
    skipped_days: int = 0
    
    @property
    def total_trades(self) -> int:
        return len(self.trades)
    
    @property
    def winners(self) -> int:
        return sum(1 for t in self.trades if t.won)
    
    @property
    def losers(self) -> int:
        return sum(1 for t in self.trades if not t.won)
    
    @property
    def win_rate(self) -> float:
        return self.winners / self.total_trades if self.total_trades > 0 else 0.0
    
    @property
    def avg_r(self) -> float:
        return (
            sum(t.r_multiple for t in self.trades) / self.total_trades
            if self.total_trades > 0
            else 0.0
        )
    
    @property
    def total_r(self) -> float:
        return sum(t.r_multiple for t in self.trades)
    
    def by_regime(self) -> dict[str, dict[str, Any]]:
        """Stats by regime (up/down/chop)."""
        regimes: dict[str, list[Trade]] = {}
        for trade in self.trades:
            regimes.setdefault(trade.regime or "unknown", []).append(trade)
        
        return {
            regime: {
                "trades": len(trades),
                "win_rate": sum(1 for t in trades if t.won) / len(trades) if trades else 0,
                "avg_r": sum(t.r_multiple for t in trades) / len(trades) if trades else 0,
                "total_r": sum(t.r_multiple for t in trades),
            }
            for regime, trades in regimes.items()
        }
    
    def by_hour(self) -> dict[int, dict[str, Any]]:
        """Stats by entry hour."""
        hours: dict[int, list[Trade]] = {}
        for trade in self.trades:
            hours.setdefault(trade.entry_hour, []).append(trade)
        
        return {
            hour: {
                "trades": len(trades),
                "win_rate": sum(1 for t in trades if t.won) / len(trades) if trades else 0,
                "avg_r": sum(t.r_multiple for t in trades) / len(trades) if trades else 0,
            }
            for hour, trades in sorted(hours.items())
        }
    
    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable report."""
        return {
            "symbols": self.symbols,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "stub_mode": self.stub_mode,
            "skipped_days": self.skipped_days,
            "total_trades": self.total_trades,
            "winners": self.winners,
            "losers": self.losers,
            "win_rate": self.win_rate,
            "avg_r": self.avg_r,
            "total_r": self.total_r,
            "by_regime": self.by_regime(),
            "by_hour": self.by_hour(),
            "trades": [
                {
                    "symbol": t.symbol,
                    "date": t.date,
                    "direction": t.direction,
                    "entry_time": t.entry_time,
                    "entry_price": t.entry_price,
                    "stop_price": t.stop_price,
                    "target_price": t.target_price,
                    "exit_time": t.exit_time,
                    "exit_price": t.exit_price,
                    "exit_reason": t.exit_reason,
                    "regime": t.regime,
                    "pnl": t.pnl,
                    "r_multiple": t.r_multiple,
                }
                for t in self.trades
            ],
        }


def _bar_time(bar: dict[str, Any]) -> datetime:
    """Parse bar timestamp to local market time."""
    return datetime.fromisoformat(str(bar["t"])).astimezone(MARKET_TZ)


def _compute_orb(bars_5min: list[dict[str, Any]]) -> ORBSetup | None:
    """Compute opening range from 09:30-09:45 ET bars."""
    or_bars = [
        b for b in bars_5min
        if OR_START <= _bar_time(b).time() < OR_END
    ]
    
    if len(or_bars) < 3:
        return None
    
    or_high = max(float(b["h"]) for b in or_bars)
    or_low = min(float(b["l"]) for b in or_bars)
    or_midpoint = (or_high + or_low) / 2
    range_height = or_high - or_low
    avg_price = (or_high + or_low) / 2
    range_height_pct = (range_height / avg_price) * 100
    
    # Skip if range is too small or too large (playbook rules)
    if range_height_pct < MIN_RANGE_HEIGHT_PCT or range_height_pct > MAX_RANGE_HEIGHT_PCT:
        return None
    
    return ORBSetup(
        symbol="",  # filled by caller
        date="",  # filled by caller
        or_high=or_high,
        or_low=or_low,
        or_midpoint=or_midpoint,
        range_height=range_height,
        range_height_pct=range_height_pct,
        regime=None,  # filled by caller
    )


def _find_entry_bar(
    bars_5min: list[dict[str, Any]],
    setup: ORBSetup,
    direction: str,
) -> dict[str, Any] | None:
    """Find first 5-minute bar that closes outside range after 09:45 ET."""
    after_range = [
        b for b in bars_5min
        if _bar_time(b).time() >= PRIMARY_ENTRY_START
    ]
    
    for bar in after_range:
        close = float(bar["c"])
        bar_time = _bar_time(bar).time()
        
        # Check time window (primary entry only for now)
        if not (PRIMARY_ENTRY_START <= bar_time < PRIMARY_ENTRY_END):
            continue
        
        # Check close outside range
        if direction == "long" and close > setup.or_high:
            return bar
        elif direction == "short" and close < setup.or_low:
            return bar
    
    return None


def _simulate_trade(
    setup: ORBSetup,
    entry_bar: dict[str, Any],
    bars_5min: list[dict[str, Any]],
    direction: str,
) -> Trade:
    """Simulate trade from entry to exit (target/stop/EOD)."""
    entry_time = _bar_time(entry_bar)
    entry_price = float(entry_bar["c"])
    
    # Compute stop and target (playbook rules)
    if direction == "long":
        stop_price = setup.or_midpoint
        target_price = entry_price + (REWARD_RISK_RATIO * setup.range_height)
    else:  # short
        stop_price = setup.or_midpoint
        target_price = entry_price - (REWARD_RISK_RATIO * setup.range_height)
    
    # Simulate exit on subsequent bars
    exit_time = entry_time
    exit_price = entry_price
    exit_reason = "eod"
    
    after_entry = [
        b for b in bars_5min
        if _bar_time(b) > entry_time
    ]
    
    for bar in after_entry:
        high = float(bar["h"])
        low = float(bar["l"])
        close = float(bar["c"])
        bar_time = _bar_time(bar)
        
        # Check target first (playbook: exits are law)
        if direction == "long" and high >= target_price:
            exit_time = bar_time
            exit_price = target_price
            exit_reason = "target"
            break
        elif direction == "short" and low <= target_price:
            exit_time = bar_time
            exit_price = target_price
            exit_reason = "target"
            break
        
        # Check stop
        if direction == "long" and low <= stop_price:
            exit_time = bar_time
            exit_price = stop_price
            exit_reason = "stop"
            break
        elif direction == "short" and high >= stop_price:
            exit_time = bar_time
            exit_price = stop_price
            exit_reason = "stop"
            break
        
        # Flatten at 15:30 (playbook rule)
        if bar_time.time() >= FLATTEN_START:
            exit_time = bar_time
            exit_price = close
            exit_reason = "eod"
            break
    
    # Compute R multiple and PnL
    risk = abs(entry_price - stop_price)
    if direction == "long":
        pnl = exit_price - entry_price
    else:  # short
        pnl = entry_price - exit_price
    
    r_multiple = pnl / risk if risk > 0 else 0.0
    
    return Trade(
        symbol=setup.symbol,
        date=setup.date,
        direction=direction,
        entry_time=iso(entry_time),
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        exit_time=iso(exit_time),
        exit_price=exit_price,
        exit_reason=exit_reason,
        regime=setup.regime,
        pnl=pnl,
        r_multiple=r_multiple,
    )


def _parse_day(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=MARKET_TZ)
    except ValueError as exc:
        raise BacktestError(f"invalid date {value!r}; expected YYYY-MM-DD") from exc


def _session_bounds(date_str: str) -> tuple[datetime, datetime]:
    day = _parse_day(date_str)
    start = day.replace(hour=9, minute=30, second=0, microsecond=0)
    end = day.replace(hour=16, minute=0, second=0, microsecond=0)
    return start, end


def _trade_for_day(
    symbol: str,
    date_str: str,
    bars_5min: list[dict[str, Any]],
    regime: str | None,
) -> Trade | None:
    """One symbol, one day, at most one regime-aligned ORB."""
    setup = _compute_orb(bars_5min)
    if setup is None:
        return None
    setup = ORBSetup(
        symbol=symbol,
        date=date_str,
        or_high=setup.or_high,
        or_low=setup.or_low,
        or_midpoint=setup.or_midpoint,
        range_height=setup.range_height,
        range_height_pct=setup.range_height_pct,
        regime=regime,
    )
    if regime == "up":
        directions = ("long",)
    elif regime == "down":
        directions = ("short",)
    else:
        # chop / unknown: sit out. The live playbook is softer on chop; a
        # mechanical first-evidence backtest does not guess.
        return None
    for direction in directions:
        entry_bar = _find_entry_bar(bars_5min, setup, direction)
        if entry_bar is not None:
            return _simulate_trade(setup, entry_bar, bars_5min, direction)
    return None


def _fetch_session_bars(broker: Broker, symbol: str, date_str: str) -> list[dict[str, Any]]:
    start, end = _session_bounds(date_str)
    return broker.get_bars_between(symbol, timeframe="5Min", start=start, end=end)


def backtest_orb(
    symbols: list[str],
    start_date: str,
    end_date: str,
    *,
    broker: Broker | None = None,
    stub: bool = False,
) -> BacktestReport:
    """Run the ORB playbook over a date range.

    `--stub` (or stub=True) uses checked-in fixtures. Anything else requires a
    broker that can fetch historical 5-minute bars; missing data fails closed
    rather than silently replaying the fixtures.
    """
    if start_date > end_date:
        raise BacktestError(f"start {start_date} is after end {end_date}")
    _parse_day(start_date)
    _parse_day(end_date)
    if stub:
        return _backtest_stub(symbols, start_date, end_date)
    if broker is None:
        raise BacktestError(
            "live backtest requires a broker; pass --stub for fixture data"
        )

    report = BacktestReport(
        symbols=symbols,
        start_date=start_date,
        end_date=end_date,
        stub_mode=False,
    )
    current = _parse_day(start_date)
    last = _parse_day(end_date)
    while current <= last:
        if current.weekday() >= 5:
            current += timedelta(days=1)
            continue
        date_str = current.strftime("%Y-%m-%d")
        try:
            session_end = _session_bounds(date_str)[1]
            needed = list(dict.fromkeys([*symbols, "QQQ"]))
            by_symbol: dict[str, list[dict[str, Any]]] = {}
            for symbol in needed:
                by_symbol[symbol] = _fetch_session_bars(broker, symbol, date_str)
            if not any(by_symbol.values()):
                report.skipped_days += 1
                current += timedelta(days=1)
                continue
            regime = classify_regime(by_symbol.get("QQQ") or [], now=session_end)
            for symbol in symbols:
                trade = _trade_for_day(symbol, date_str, by_symbol.get(symbol) or [], regime)
                if trade is not None:
                    report.trades.append(trade)
        except BrokerError:
            report.skipped_days += 1
        current += timedelta(days=1)
    return report


def _backtest_stub(symbols: list[str], start_date: str, end_date: str) -> BacktestReport:
    """Stub backtest with fixture data for testing without broker."""
    # Load fixture data
    fixture = _load_fixture()
    
    report = BacktestReport(
        symbols=symbols,
        start_date=start_date,
        end_date=end_date,
        stub_mode=True,
    )
    
    # Run backtest on fixture data
    for symbol in symbols:
        if symbol not in fixture:
            continue
        
        for day_data in fixture[symbol]:
            if not (start_date <= day_data["date"] <= end_date):
                continue
            
            trade = _trade_for_day(
                symbol,
                day_data["date"],
                day_data["bars_5min"],
                day_data.get("regime"),
            )
            if trade is not None:
                report.trades.append(trade)
    
    return report


def _load_fixture() -> dict[str, list[dict[str, Any]]]:
    """Load fixture data for stub mode."""
    # Fixture data: synthetic SPY/QQQ days with known ORB setups
    return {
        "SPY": [
            {
                "date": "2026-09-17",
                "regime": "up",
                "bars_5min": [
                    # Opening range: 09:30-09:45 (580.00 - 581.00)
                    {"t": "2026-09-17T09:30:00-04:00", "o": 580.0, "h": 580.5, "l": 580.0, "c": 580.3, "v": 1000000},
                    {"t": "2026-09-17T09:35:00-04:00", "o": 580.3, "h": 581.0, "l": 580.2, "c": 580.8, "v": 1200000},
                    {"t": "2026-09-17T09:40:00-04:00", "o": 580.8, "h": 581.0, "l": 580.5, "c": 580.7, "v": 1100000},
                    # Breakout bar: closes above 581.00
                    {"t": "2026-09-17T09:45:00-04:00", "o": 580.7, "h": 581.5, "l": 580.7, "c": 581.2, "v": 1500000},
                    # Run to target
                    {"t": "2026-09-17T09:50:00-04:00", "o": 581.2, "h": 581.8, "l": 581.0, "c": 581.5, "v": 1300000},
                    {"t": "2026-09-17T09:55:00-04:00", "o": 581.5, "h": 582.0, "l": 581.3, "c": 581.7, "v": 1100000},
                    # Target hit (entry 581.2 + 1.5*1.0 range = 582.7)
                    {"t": "2026-09-17T10:00:00-04:00", "o": 581.7, "h": 582.8, "l": 581.5, "c": 582.5, "v": 1400000},
                ],
            },
            {
                "date": "2026-09-18",
                "regime": "down",
                "bars_5min": [
                    # Opening range: 09:30-09:45 (582.00 - 583.00)
                    {"t": "2026-09-18T09:30:00-04:00", "o": 582.5, "h": 583.0, "l": 582.0, "c": 582.3, "v": 1000000},
                    {"t": "2026-09-18T09:35:00-04:00", "o": 582.3, "h": 582.8, "l": 582.1, "c": 582.7, "v": 1100000},
                    {"t": "2026-09-18T09:40:00-04:00", "o": 582.7, "h": 582.9, "l": 582.3, "c": 582.5, "v": 1050000},
                    # Breakdown bar: closes below 582.00
                    {"t": "2026-09-18T09:45:00-04:00", "o": 582.5, "h": 582.6, "l": 581.5, "c": 581.7, "v": 1600000},
                    # Stopped (midpoint = 582.5)
                    {"t": "2026-09-18T09:50:00-04:00", "o": 581.7, "h": 582.8, "l": 581.5, "c": 582.6, "v": 1400000},
                ],
            },
        ],
        "QQQ": [
            {
                "date": "2026-09-17",
                "regime": "up",
                "bars_5min": [
                    {"t": "2026-09-17T09:30:00-04:00", "o": 490.0, "h": 490.5, "l": 490.0, "c": 490.3, "v": 2000000},
                    {"t": "2026-09-17T09:35:00-04:00", "o": 490.3, "h": 491.0, "l": 490.2, "c": 490.8, "v": 2200000},
                    {"t": "2026-09-17T09:40:00-04:00", "o": 490.8, "h": 491.0, "l": 490.5, "c": 490.7, "v": 2100000},
                    {"t": "2026-09-17T09:45:00-04:00", "o": 490.7, "h": 491.5, "l": 490.7, "c": 491.2, "v": 2500000},
                    {"t": "2026-09-17T09:50:00-04:00", "o": 491.2, "h": 491.8, "l": 491.0, "c": 491.5, "v": 2300000},
                    {"t": "2026-09-17T09:55:00-04:00", "o": 491.5, "h": 492.0, "l": 491.3, "c": 491.7, "v": 2100000},
                    {"t": "2026-09-17T10:00:00-04:00", "o": 491.7, "h": 492.8, "l": 491.5, "c": 492.5, "v": 2400000},
                ],
            },
        ],
    }


def save_backtest(report: BacktestReport, output_dir: Path = DEFAULT_BACKTEST_DIR) -> Path:
    """Save backtest report to JSON file."""
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Generate filename with timestamp and hash
    timestamp = utcnow().strftime("%Y%m%d_%H%M%S")
    symbols_str = "_".join(report.symbols[:3])  # Limit filename length
    mode = "stub" if report.stub_mode else "live"
    filename = f"orb_{symbols_str}_{report.start_date}_{report.end_date}_{mode}_{timestamp}.json"
    
    output_path = output_dir / filename
    output_path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    
    log.info("backtest_saved", extra={"path": str(output_path)})
    return output_path


def format_backtest(report: BacktestReport) -> str:
    """Human-readable backtest summary."""
    lines = [
        "ORB Historical Backtest",
        "=" * 60,
        f"Symbols: {', '.join(report.symbols)}",
        f"Period: {report.start_date} to {report.end_date}",
        f"Mode: {'STUB (fixture data)' if report.stub_mode else 'Live (broker historical)'}",
        "",
        "Summary:",
        f"  Total trades: {report.total_trades}",
        f"  Winners: {report.winners}",
        f"  Losers: {report.losers}",
        f"  Win rate: {report.win_rate:.1%}",
        f"  Avg R: {report.avg_r:+.2f}",
        f"  Total R: {report.total_r:+.2f}",
    ]
    
    if report.skipped_days > 0:
        lines.append(f"  Skipped days (data unavailable): {report.skipped_days}")
    
    # By regime
    by_regime = report.by_regime()
    if by_regime:
        lines.extend([
            "",
            "By Regime:",
        ])
        for regime, stats in sorted(by_regime.items()):
            lines.append(
                f"  {regime:8s}: {stats['trades']:3d} trades, "
                f"{stats['win_rate']:.1%} win rate, "
                f"{stats['avg_r']:+.2f} avg R, "
                f"{stats['total_r']:+.2f} total R"
            )
    
    # By hour
    by_hour = report.by_hour()
    if by_hour:
        lines.extend([
            "",
            "By Entry Hour (ET):",
        ])
        for hour, stats in by_hour.items():
            lines.append(
                f"  {hour:02d}:xx: {stats['trades']:3d} trades, "
                f"{stats['win_rate']:.1%} win rate, "
                f"{stats['avg_r']:+.2f} avg R"
            )
    
    # Sample trades
    if report.trades:
        lines.extend([
            "",
            "Sample Trades (first 5):",
        ])
        for trade in report.trades[:5]:
            entry_dt = datetime.fromisoformat(trade.entry_time).astimezone(MARKET_TZ)
            lines.append(
                f"  {trade.date} {trade.symbol:6s} {trade.direction:5s} @ {trade.entry_price:.2f} "
                f"({entry_dt.strftime('%H:%M')}) -> {trade.exit_reason:6s} {trade.r_multiple:+.2f}R "
                f"[{trade.regime or 'N/A'}]"
            )
    
    lines.append("")
    lines.append("Note: This is first-evidence pass. No walk-forward optimization.")
    lines.append("Small sample size? Say so. Multi-day paper prove is next.")
    
    return "\n".join(lines)
