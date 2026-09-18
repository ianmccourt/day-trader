# ORB Historical Backtest Implementation

This document summarizes the ORB (Opening Range Breakout) historical backtest implementation in PR #5.

## Overview

First-evidence historical backtest to prove or falsify the discretionary ORB playbook parameters from `prompts/system.txt` over liquid names (SPY, QQQ, etc.). Implements ORB rules in code, provides fixture-driven testing, and reports metrics that matter before multi-day paper trading begins.

## Implementation

### Core Module: `src/trader/backtest.py` (~700 lines)

**ORB Rules** (matched to playbook):
- **Opening range**: 09:30-09:45 ET (first three 5-minute bars)
- **Entry**: First 5-minute bar that closes outside range
  - Long: close above `or_high`
  - Short: close below `or_low`
- **Stop**: Range midpoint (`(or_high + or_low) / 2`)
- **Target**: Entry + 1.5× range height (1.5:1 R:R)
- **Range validation**:
  - Min height: 0.15% of price (skip if too tight)
  - Max height: 1.5% of price (skip if already extended)
- **Regime filter**: QQQ regime (up/down/chop) from scanner logic
- **Time windows**:
  - 09:30-09:45: observe (no entries)
  - 09:45-12:30: primary ORB window
  - 12:30-14:30: VWAP continuation only (not yet implemented)
  - 14:30-15:30: manage only (no new entries)
  - 15:30-16:00: flatten day trades
- **Look-ahead**: Uses closed bars only (honest backtesting)

**Key Functions**:
- `_compute_orb(bars)`: Extract opening range from 09:30-09:45 bars
- `_find_entry_bar(bars, setup, direction)`: Find first close outside range
- `_simulate_trade(setup, entry_bar, bars, direction)`: Run to target/stop/EOD
- `backtest_orb(symbols, start, end, broker, stub)`: Main backtest loop
- `save_backtest(report)`: Write JSON report to `data/backtests/`
- `format_backtest(report)`: Human-readable summary

**Data Classes**:
- `ORBSetup`: One day's opening range (high, low, midpoint, range stats)
- `Trade`: One backtest trade (entry, exit, R multiple, regime)
- `BacktestReport`: Full results with metrics

### CLI Integration

**Command**: `trader backtest --start DATE --end DATE [--symbols SPY,QQQ] [--stub] [--json]`

**Arguments**:
- `--start`: First trading day (YYYY-MM-DD, default: 2026-09-17)
- `--end`: Last trading day (YYYY-MM-DD, default: 2026-09-18)
- `--symbols`: Comma-separated symbols (default: SPY,QQQ)
- `--stub`: Use fixture data instead of broker historical bars
- `--json`: Machine-readable output

**Output**:
- Human summary to stdout
- JSON report to `data/backtests/orb_*.json`
- Fallback to stub mode if broker unavailable

### Tests: `tests/test_backtest.py` (~350 lines, 16 tests)

**Rule Correctness**:
- `test_compute_orb_valid_range`: ORB from three 5-minute bars
- `test_compute_orb_range_too_small`: Reject <0.15% range
- `test_compute_orb_range_too_large`: Reject >1.5% range
- `test_compute_orb_incomplete_range`: Require all three bars

**Entry Detection**:
- `test_find_entry_bar_long_breakout`: Long entry (close above high)
- `test_find_entry_bar_short_breakout`: Short entry (close below low)
- `test_find_entry_bar_no_breakout`: No entry if inside range

**Trade Simulation**:
- `test_simulate_trade_long_target_hit`: Long trade hits 1.5R target
- `test_simulate_trade_long_stopped`: Long trade hits midpoint stop
- `test_simulate_trade_eod_flatten`: Trade flattened at 15:30 ET

**Backtest Integration**:
- `test_backtest_stub_mode`: Stub backtest with fixture data
- `test_backtest_report_metrics`: Win rate, avg R, total R computation
- `test_backtest_by_regime`: Regime breakdown (up/down/chop)
- `test_backtest_by_hour`: Time-of-day breakdown

**Persistence**:
- `test_save_backtest`: JSON round-trip
- `test_format_backtest_human_readable`: Human summary formatting

All tests pass without broker (synthetic bars and fixtures).

## Fixture Data

**Stub Mode Fixtures** (checked-in, always available):

### SPY 2026-09-17 (up regime)
- **Opening range**: 580.00 - 581.00 (1.00 range, ~0.17%)
- **Entry**: 09:45 ET, close @ 581.20 (above range high)
- **Direction**: Long
- **Target**: 582.70 (entry + 1.5 × 1.0)
- **Stop**: 580.50 (midpoint)
- **Exit**: 10:00 ET, target hit @ 582.70
- **Outcome**: +1.5R (winner)

### SPY 2026-09-18 (down regime)
- **Opening range**: 582.00 - 583.00 (1.00 range, ~0.17%)
- **Entry**: 09:45 ET, close @ 581.70 (below range low)
- **Direction**: Short
- **Target**: 580.20 (entry - 1.5 × 1.0)
- **Stop**: 582.50 (midpoint)
- **Exit**: 09:50 ET, stopped @ 582.50
- **Outcome**: -1.0R (loser)

### QQQ 2026-09-17 (up regime)
- **Opening range**: 490.00 - 491.00 (1.00 range, ~0.20%)
- **Entry**: 09:45 ET, close @ 491.20 (above range high)
- **Direction**: Long
- **Target**: 492.70 (entry + 1.5 × 1.0)
- **Exit**: 10:00 ET, target hit @ 492.70
- **Outcome**: +1.5R (winner)

## Sample Output

```
ORB Historical Backtest
============================================================
Symbols: SPY, QQQ
Period: 2026-09-17 to 2026-09-18
Mode: STUB (fixture data)

Summary:
  Total trades: 3
  Winners: 2
  Losers: 1
  Win rate: 66.7%
  Avg R: +0.67
  Total R: +2.00

By Regime:
  up      :   2 trades, 100.0% win rate, +1.50 avg R, +3.00 total R
  down    :   1 trades, 0.0% win rate, -1.00 avg R, -1.00 total R

By Entry Hour (ET):
  09:xx:   3 trades, 66.7% win rate, +0.67 avg R

Sample Trades (first 5):
  2026-09-17 SPY    long  @ 581.20 (09:45) -> target +1.50R [up]
  2026-09-17 QQQ    long  @ 491.20 (09:45) -> target +1.50R [up]
  2026-09-18 SPY    short @ 581.70 (09:45) -> stop   -1.00R [down]

Note: This is first-evidence pass. No walk-forward optimization.
Small sample size? Say so. Multi-day paper prove is next.
```

## Metrics Explained

### Core Metrics
- **Total trades**: Number of ORB entries taken
- **Winners**: Trades that hit target (exit_reason="target")
- **Losers**: Trades that hit stop (exit_reason="stop")
- **Win rate**: `winners / total_trades`
- **Avg R**: Average R multiple per trade (`sum(r_multiple) / total_trades`)
- **Total R**: Sum of all R multiples (`sum(r_multiple)`)

### By Regime
- Breakdown by QQQ regime (up/down/chop)
- Shows if regime filter works (do `up` trades outperform `chop`?)
- Each regime gets: trades, win rate, avg R, total R

### By Entry Hour
- Breakdown by hour of entry (9-12 ET typically)
- Shows time-of-day sensitivity
- Each hour gets: trades, win rate, avg R

## What It Proves (and Doesn't)

### ✅ Proves
1. **ORB rules work as coded** (matches playbook semantics from `prompts/system.txt`)
2. **Fixture path works** (tests always run, no broker dependency)
3. **First evidence** of ORB edge (or lack thereof) on historical liquid names
4. **Regime filter effectiveness** (do `up` trades outperform `chop`?)
5. **Time-of-day sensitivity** (morning breakouts vs afternoon)

### ❌ Doesn't Prove
1. **Not walk-forward optimized** (no parameter search, no in-sample/out-of-sample split)
2. **Small sample?** Fixture has 2 days; live backtest on larger range needed
3. **Multi-day stability** (scheduler, orphan reconciliation, daily loss tracking)
4. **Execution quality** (backtest assumes perfect fills at target/stop)
5. **Live edge translation** (does backtest R match actual live R?)

## Files Changed

- **New**: `src/trader/backtest.py` (~700 lines)
- **New**: `tests/test_backtest.py` (~350 lines, 16 tests)
- **Modified**: `src/trader/cli.py` (added `cmd_backtest`, backtest parser, `BacktestError` handling)
- **Modified**: `README.md` (new "ORB Historical Backtest" section)

**Total**: +1050 lines, 4 files changed

## Non-Goals

- ❌ No automatic parameter optimization (no grid search, no ML tuning)
- ❌ No changing `risk.toml` or live prompts based on results
- ❌ No multi-day unattended runner (next PR after this proves rules work)
- ❌ No fake significance theater (if sample is small, say so)
- ❌ No ORB fill simulation in live agent (backtest is evidence only)

## Next Steps

After PR #5 merges:
1. **Run with real Alpaca history** on larger date range (1-3 months)
2. **Prove multi-day paper trading session** (scheduler stability + live ORB fills)
3. **Compare backtest R vs actual live R** (does edge translate to execution?)
4. **Regime filter validation** (if `chop` trades underperform, strengthen filter)

## Usage

```bash
# Stub mode (always works, no broker required)
uv run trader backtest --stub

# With custom date range and symbols
uv run trader backtest --start 2026-09-01 --end 2026-09-30 --symbols SPY,QQQ,NVDA --stub

# Live mode with Alpaca historical bars (requires ALPACA_API_KEY)
uv run trader backtest --start 2026-09-01 --end 2026-09-30 --symbols SPY,QQQ

# Machine-readable output
uv run trader backtest --start 2026-09-01 --end 2026-09-30 --stub --json

# Run all tests
uv run pytest tests/test_backtest.py -v
```

## Technical Notes

### Look-Ahead Bias Prevention
- Uses `_bar_time()` to parse bar timestamps to local market time
- Only considers bars after 09:45 ET for entry detection
- Checks `close` price for range breakout, not `high`/`low` (wicks don't count)
- Exit simulation checks `high`/`low` for target/stop but respects bar order

### Regime Computation
- Same `classify_regime()` logic from `scanner.py`
- 15-minute periods aggregated from 5-minute bars
- `up`: last close > session midpoint, 15m highs rising
- `down`: last close < session midpoint, 15m lows falling
- `chop`: otherwise

### Range Validation
- **Min**: 0.15% of price (playbook: "skip if tiny")
- **Max**: 1.5% of price (playbook: "skip if enormous — already the move")
- Measured as `(range_height / avg_price) * 100`

### R Multiple Calculation
```python
risk = abs(entry_price - stop_price)
pnl = exit_price - entry_price  # (or entry - exit for short)
r_multiple = pnl / risk
```

Target is placed at `entry + 1.5 * range_height` (long) or `entry - 1.5 * range_height` (short), so a target hit should yield ~1.5R (may vary slightly due to price discretization).
