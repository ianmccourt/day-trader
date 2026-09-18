# Ops Hardening Implementation Summary

This document summarizes the operational hardening features implemented in PR #4.

## Implementation Status

### ✅ 1. Alerting System
**Files:**
- `src/trader/alerts.py` (new)
- `src/trader/cli.py` (modified)
- `src/trader/cycle.py` (modified)
- `src/trader/execution.py` (modified)
- `src/trader/scheduler.py` (modified)
- `tests/test_ops_hardening.py` (new)

**Features:**
- `AlertSink` class writes to `data/alerts.jsonl`
- Optional webhook support via `TRADER_ALERT_WEBHOOK` env var
- Fires on:
  - Consecutive error cycles (threshold: 3)
  - Kill switch engaged
  - Max daily loss latched
  - Heartbeat stale (600s threshold)
- Integrated into existing cycle end / kill / daily-loss paths
- CLI: `trader alerts [--limit N] [--json]`
- Best-effort webhook delivery (failures never block harness)
- Heartbeat file tracking (`data/heartbeat.json`)

### ✅ 2. Supervision
**Files:**
- `deploy/trader.service` (new, systemd)
- `deploy/com.daytrader.plist` (new, launchd)
- `scripts/watchdog.sh` (new, cron fallback)
- `README.md` (modified)

**Features:**
- systemd unit with `Restart=always`, working directory, env file reference
- launchd plist with `KeepAlive=true`, paths adjusted for macOS
- Watchdog script checks heartbeat and restarts via systemd/launchd/manual
- README "Running Unattended" section with complete setup instructions

### ✅ 3. Log Rotation
**Files:**
- `deploy/logrotate-trader.conf` (new)
- `src/trader/startup.py` (new)
- `src/trader/cli.py` (modified)
- `tests/test_ops_hardening.py` (new)

**Features:**
- logrotate config: daily rotation, 14-day retention, compress, 1MB min size
- Startup checks:
  - Free disk space warning (default: 1 GB minimum)
  - Log file size warning (default: 500 MB maximum)
- CLI flags: `--strict` (fail instead of warn)
- Checks run automatically on `trader run`
- Documents external rotation approach

### ✅ 4. Token Headroom / No-Action Fallback
**Files:**
- `src/trader/llm.py` (modified)
- `tests/test_token_headroom.py` (new)

**Features:**
- `_check_headroom()` method: checks before starting tool loop
- Reserves 2000 tokens for tool results and thinking blocks
- Clean abort: returns `no_action` with `stop_reason="budget_headroom"` if insufficient
- Logging: `budget_headroom_abort` event with tokens/threshold/budget
- Helper methods: `_load_system()`, `_render_user_turn()` for testing
- Test validates mandated workflow fits within `MAX_PROMPT_TOKENS` with margin

## Testing

### Unit Tests
- `tests/test_ops_hardening.py`:
  - `test_alert_sink_writes_to_file`
  - `test_consecutive_errors_triggers_alert`
  - `test_consecutive_errors_no_alert_below_threshold`
  - `test_kill_switch_alert`
  - `test_daily_loss_alert`
  - `test_heartbeat_updates`
  - `test_heartbeat_stale_detection`
  - `test_heartbeat_fresh`
  - `test_disk_space_check_warns_on_low_space`
  - `test_disk_space_check_strict_mode_raises`
  - `test_log_file_size_check`
  - `test_log_file_size_check_strict_mode_raises`
  - `test_log_file_size_ok_for_small_files`
  - `test_alert_webhook_best_effort`

- `tests/test_token_headroom.py`:
  - `test_mandated_workflow_fits_in_budget` (validates workflow token usage)
  - `test_headroom_abort_produces_clean_no_action` (documents expected behavior)

### Integration Points
- `run_cycle()` now accepts `alert_sink` parameter
- `run_scheduler()` now accepts `alert_sink` parameter
- `place_order()` now accepts `alert_sink` parameter
- `cmd_run()` creates `AlertSink` unless `--no-alerts`
- `cmd_cycle()` creates `AlertSink` unless `--no-alerts`
- Startup checks run in `cmd_run()` before scheduler launch

## CLI Changes

### New Commands
- `trader alerts [--limit N] [--json]`: Show recent alerts

### Modified Commands
- `trader run`:
  - `--strict`: Fail startup checks rather than warning
  - `--no-alerts`: Disable alerting
- `trader cycle`:
  - `--no-alerts`: Disable alerting

## Non-Goals (Deferred to Future PRs)
- ❌ ORB historical backtest
- ❌ Multi-day live run on paper account
- ❌ RSI critique/replay changes (already in PRs #2/#3)
- ❌ Live trading
- ❌ `risk.toml` loosening

## What's Next
After this PR merges, remaining CRITICAL/HIGH items:
1. ORB historical backtest (prove strategy edge on 2025 data)
2. Multi-day paper trading session (prove scheduler stability)
3. Broker adapter parity checks (Robinhood MCP vs Alpaca coverage)

## Files Changed Summary
- **14 files changed**: 986 insertions(+), 9 deletions(-)
- **New modules**: 2 (`alerts.py`, `startup.py`)
- **Modified modules**: 5 (`cycle.py`, `execution.py`, `scheduler.py`, `llm.py`, `cli.py`)
- **Deployment configs**: 3 (`trader.service`, `com.daytrader.plist`, `logrotate-trader.conf`)
- **Scripts**: 1 (`watchdog.sh`)
- **Tests**: 2 (`test_ops_hardening.py`, `test_token_headroom.py`)
- **Documentation**: 1 (`README.md`)
