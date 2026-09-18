# Unattended Multi-Day Paper Run Runbook

This runbook provides a concrete prove path for the still-unproven gap: leave `trader run` up across multiple sessions/days on Alpaca paper with supervision + alerts, then evaluate.

**Goal**: Prove the harness can run unattended for N trading days without silent death, unbounded orphans, or other operational failures.

**Explicit**: Still paper-only. Not proof of edge (that's `evaluate`), not proof of strategy (that's ORB backtest). This proves **operational stability**.

---

## Preflight Checklist

Run **before** starting the first multi-day session.

### 1. Paper Keys Only

```bash
# Check .env has paper keys (Alpaca or Robinhood)
grep -E "ALPACA_API_KEY|ALPACA_SECRET_KEY" .env
# OR
grep -E "ROBINHOOD" .env

# Verify broker endpoint is paper
uv run trader status  # Should show "paper" or "robinhood agentic"
```

**Hard fail**: If any live broker keys are present, **STOP**. This runbook is paper-only.

### 2. Risk Configuration

Choose your risk limits:

```bash
# Option A: Use default risk.toml (current limits)
ls -la risk.toml

# Option B: Use conservative risk config (if available)
# cp risk.conservative.toml risk.toml  # (if you have this)

# Verify risk limits are sane for paper
uv run trader risk
```

**Check**:
- `max_daily_loss`: Reasonable for paper account size (e.g. $10,000 on $100k account)
- `max_position_notional`: Capped per position (e.g. $50,000)
- `max_total_exposure`: Total notional cap (e.g. $100,000)
- `symbol_allowlist`: Only names you're comfortable trading

### 3. Kill Switch Off

```bash
# Verify kill switch is not engaged
uv run trader status | grep -i kill

# If engaged, disengage:
# uv run trader kill --off
```

**Hard fail**: If kill switch is ON, the harness will not trade.

### 4. Disk Space & Log Rotation

```bash
# Check free disk space
df -h .

# Check current log size
ls -lh logs/trader.jsonl

# Install logrotate config (if ops PR #4 merged)
# sudo cp deploy/logrotate-trader.conf /etc/logrotate.d/trader
# sudo logrotate -f /etc/logrotate.d/trader

# OR: Manual rotation before starting
# mv logs/trader.jsonl logs/trader.jsonl.$(date +%Y%m%d)
# touch logs/trader.jsonl
```

**Check**: >1 GB free disk, log <500 MB (or rotated).

### 5. Alert Webhook (Optional but Recommended)

```bash
# Set webhook in .env (Slack, Discord, etc.)
echo 'TRADER_ALERT_WEBHOOK=https://hooks.slack.com/services/YOUR/WEBHOOK/URL' >> .env

# Test alert (if ops PR #4 merged)
# uv run trader alerts  # Should show recent alerts or "(none)"
```

**Soft warn**: Without a webhook, you won't get remote notifications. You'll need to check `trader alerts` manually.

### 6. Supervision Setup

Choose one supervision method:

#### Option A: systemd (Linux, if ops PR #4 merged)

```bash
# Copy and edit unit file
sudo cp deploy/trader.service /etc/systemd/system/
sudo nano /etc/systemd/system/trader.service  # Adjust paths, user

# Enable but don't start yet
sudo systemctl enable trader.service
```

#### Option B: launchd (macOS, if ops PR #4 merged)

```bash
# Copy and edit plist
cp deploy/com.daytrader.plist ~/Library/LaunchAgents/
nano ~/Library/LaunchAgents/com.daytrader.plist  # Adjust paths

# Load but don't start yet
launchctl load ~/Library/LaunchAgents/com.daytrader.plist
```

#### Option C: nohup + watchdog (fallback)

```bash
# Manual start (see "Start" section below)
# Optional watchdog cron (if ops PR #4 merged):
# */5 * * * * /path/to/day-trader/scripts/watchdog.sh >> /tmp/watchdog.log 2>&1
```

**Check**: Supervisor configured but not started yet.

### 7. Run Preflight Script (Optional)

```bash
# Run preflight checks
./scripts/preflight_unattended.sh

# Exit code 0: all checks passed
# Exit code 1: soft warnings (proceed with caution)
# Exit code 2: hard failures (fix before starting)
```

---

## Start: Launch the Unattended Session

### If Using Supervisor (systemd/launchd)

```bash
# systemd
sudo systemctl start trader.service
sudo systemctl status trader.service
sudo journalctl -u trader.service -f  # Watch logs

# launchd
launchctl start com.daytrader.harness
launchctl list | grep daytrader
tail -f logs/trader.jsonl  # Watch logs
```

### If Using nohup (fallback)

```bash
# Start in background
mkdir -p logs
nohup uv run trader run >> logs/trader.jsonl 2>&1 &
echo $! > logs/trader.pid

# Verify it started
ps -p $(cat logs/trader.pid)
tail -f logs/trader.jsonl
```

**Check**:
- Process running: `ps -p $(cat logs/trader.pid)` or `systemctl status trader.service`
- First cycle logged: `tail logs/trader.jsonl | grep cycle_end`
- Heartbeat file created (if ops PR #4 merged): `cat data/heartbeat.json`

---

## During: Monitoring the Session

Check these **daily** (or more frequently if nervous):

### 1. Process Health

```bash
# systemd
sudo systemctl status trader.service

# launchd
launchctl list | grep daytrader

# nohup
ps -p $(cat logs/trader.pid) || echo "Process died!"

# Heartbeat (if ops PR #4 merged)
# cat data/heartbeat.json  # Should be recent
```

**Fail**: If process is dead and supervisor didn't restart, investigate logs.

### 2. Cycle Status

```bash
# Recent cycles
uv run trader cycles --limit 10

# Today's session
uv run trader status

# Show one cycle's details (if something looks odd)
uv run trader show CYCLE_ID
```

**Check**:
- Cycles running every N minutes (default: 5)
- Status `ok` (or `skipped_closed` outside RTH)
- No extended streak of `error` status

### 3. Alerts (if ops PR #4 merged)

```bash
# Recent alerts
uv run trader alerts --limit 20

# Check for:
# - consecutive_error_cycles
# - kill_switch_engaged
# - max_daily_loss_latched
# - heartbeat_stale
```

**Fail**: If `max_daily_loss_latched` fired, the harness has stopped trading for the day. Evaluate and restart tomorrow.

### 4. Risk Rejections

```bash
# Recent rejections by check
uv run trader rejections --limit 20

# Check for repeated rejections on same check (e.g. max_position_notional)
```

**Insight**: High rejection rate may indicate risk limits are too tight, or playbook is over-aggressive.

### 5. Orphan Reconciliation

```bash
# Check for orphans (positions without stop)
uv run trader status | grep -A 20 "Open positions"

# Or check DB directly
sqlite3 data/trader.db "SELECT * FROM theses WHERE closed_at IS NULL"
```

**Check**: Orphans should be rare and quickly closed. If unbounded orphan accumulation, **STOP** and investigate.

### 6. Disk Space

```bash
# Check free disk
df -h .

# Check log size
ls -lh logs/trader.jsonl
```

**Fail**: If disk <100 MB free, rotate logs or stop to investigate.

---

## Stop: Graceful Shutdown

After N trading days (e.g. 5 days, 1 week), stop the session gracefully.

### 1. Send SIGTERM

```bash
# systemd
sudo systemctl stop trader.service

# launchd
launchctl stop com.daytrader.harness

# nohup
kill -TERM $(cat logs/trader.pid)
```

**Check**: Process should log "shutdown_signal" and exit cleanly within ~30 seconds.

### 2. Reconcile Final State

```bash
# Check final cycle status
uv run trader status

# Reconcile open orders/positions
uv run trader reconcile

# Check for orphan theses
sqlite3 data/trader.db "SELECT * FROM theses WHERE closed_at IS NULL"
```

**Expected**: All positions closed, all theses closed, no open orders.

### 3. Evaluate Performance

```bash
# Evaluate the date range
uv run trader evaluate --start YYYY-MM-DD --end YYYY-MM-DD

# Example: one week
uv run trader evaluate --start 2026-09-15 --end 2026-09-20

# Machine-readable output
uv run trader evaluate --start 2026-09-15 --end 2026-09-20 --json
```

**Metrics**:
- Total return (vs cash)
- Return vs SPY
- Win rate
- Number of trades
- Sharpe ratio (if implemented)

---

## Pass/Fail Criteria: "Unattended Week Proven"

### ✅ Pass Criteria

1. **N trading days completed** (e.g. 5 days minimum)
   - Process ran unattended across multiple sessions
   - No manual intervention required (except monitoring)
2. **No silent death**
   - Process did not exit unexpectedly
   - Supervisor restarted if crashed (check restart count)
3. **Orphans bounded**
   - At most 1-2 orphans per day (quickly reconciled)
   - No unbounded orphan accumulation
4. **Evaluate completes**
   - `trader evaluate` runs successfully on date range
   - Reports are generated (even if P&L is negative)
5. **Risk layer enforced**
   - No rejected orders bypassed the risk layer
   - `max_daily_loss` latched if triggered (did not continue trading)
6. **Logs healthy**
   - `logs/trader.jsonl` contains cycle_end events for all cycles
   - No extended error streaks (3+ consecutive errors without recovery)

### ❌ Fail Criteria

1. **Silent death**: Process exited without restart and was not detected
2. **Unbounded orphans**: >5 orphan theses at any time, or >10 across the week
3. **Evaluate fails**: `trader evaluate` crashes or produces corrupt reports
4. **Risk bypass**: Evidence of orders bypassing risk layer (inspect `test_no_bypass.py`)
5. **Data corruption**: SQLite DB corruption, missing cycle rows, or log parse failures

### ⚠️ Soft Failures (Investigate but Don't Block)

- High rejection rate (>50% of proposed orders rejected)
- Low trade count (<5 trades over 5 days — may indicate overly conservative playbook)
- Extended market-closed periods (expected on weekends/holidays)
- Negative P&L (this runbook doesn't prove edge, only operational stability)

---

## What This Proves (and Doesn't)

### ✅ Proves

1. **Operational stability**: The harness can run unattended for multiple days
2. **Supervisor works**: Process restarts on crash (if supervisor configured)
3. **Alerting works**: You get notified of critical events (if webhook configured)
4. **Risk layer holds**: `max_daily_loss`, `max_position_notional`, etc. are enforced
5. **Logging persists**: All cycles are recorded, evaluate can reconstruct history
6. **Orphan reconciliation works**: Orphans are detected and bounded

### ❌ Doesn't Prove

1. **Edge**: Positive P&L over 5 days is noise, not proof. Run ORB backtest for edge evidence.
2. **Optimal parameters**: Risk limits, playbook rules may need tuning after this prove.
3. **Multi-month stability**: One week is not one quarter. Extend if needed.
4. **Live trading readiness**: Still paper-only. Live requires additional diligence.

---

## Troubleshooting

### Process Died and Didn't Restart

**Check**:
- Supervisor status: `systemctl status trader.service` or `launchctl list | grep daytrader`
- Restart count: `systemctl show trader.service | grep Restart`
- Last logs: `tail -100 logs/trader.jsonl`

**Common causes**:
- Unhandled exception (check logs for traceback)
- Broker API outage (check `broker_error` events)
- Disk full (check `df -h`)
- OOM killer (check `dmesg | grep oom`)

**Fix**: Investigate root cause, fix, restart.

### Consecutive Error Cycles

**Check**:
- `uv run trader cycles --limit 20 | grep error`
- `tail -100 logs/trader.jsonl | grep agent_error`

**Common causes**:
- Anthropic API rate limit (check `rate_limit` in logs)
- Broker API outage (check `broker_error` in logs)
- Prompt loading failure (check `prompt_error` in logs)

**Fix**: Wait for transient failure to clear, or fix prompt/config issue.

### Orphan Accumulation

**Check**:
- `sqlite3 data/trader.db "SELECT * FROM theses WHERE closed_at IS NULL"`
- `uv run trader status | grep -A 20 "Open positions"`

**Common causes**:
- Broker-side stop triggered but not reconciled (run `trader reconcile`)
- Position opened but cycle crashed before writing thesis
- Bug in `close_orphan_theses()` logic

**Fix**: Manually close orphans with `trader reconcile`, investigate root cause.

### Max Daily Loss Latched

**Check**:
- `uv run trader alerts | grep max_daily_loss_latched`
- `uv run trader status` (should show kill switch or daily loss flag)

**Expected behavior**: The harness **should** stop trading for the day. This is the risk layer working.

**Action**: Evaluate the day's trades, understand why loss threshold was hit, restart tomorrow.

---

## Next Steps After Week Proven

1. **Tune risk limits**: Based on rejection rate and orphan frequency
2. **Tune playbook**: Based on `evaluate` metrics (win rate, avg R)
3. **Extend to multi-week**: Run for 2-4 weeks to prove longer-term stability
4. **Compare to backtest**: Does live R match ORB backtest R? If not, investigate slippage/execution quality.
5. **Consider live**: Only after multi-week paper stability **and** positive edge evidence.

---

## Appendix: Minimal Nohup Fallback (No Supervisor)

If `deploy/` units are not available (ops PR #4 not merged), use this minimal nohup approach:

```bash
# Start
mkdir -p logs
nohup uv run trader run >> logs/trader.jsonl 2>&1 &
echo $! > logs/trader.pid

# Monitor
ps -p $(cat logs/trader.pid)
tail -f logs/trader.jsonl

# Stop
kill -TERM $(cat logs/trader.pid)
wait $(cat logs/trader.pid) || true

# Restart after crash (manual)
if ! ps -p $(cat logs/trader.pid) > /dev/null 2>&1; then
    echo "Process died, restarting..."
    nohup uv run trader run >> logs/trader.jsonl 2>&1 &
    echo $! > logs/trader.pid
fi
```

**Limitation**: No auto-restart. You must manually check and restart if process dies.

**Watchdog cron** (checks every 5 minutes):

```bash
# Add to crontab (crontab -e)
*/5 * * * * cd /path/to/day-trader && ./scripts/check_alive.sh >> /tmp/check_alive.log 2>&1
```

Where `scripts/check_alive.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

PID_FILE="logs/trader.pid"
LOG_FILE="logs/trader.jsonl"

if [ ! -f "$PID_FILE" ]; then
    echo "$(date -Iseconds): No PID file, assuming first run"
    exit 0
fi

PID=$(cat "$PID_FILE")

if ! ps -p "$PID" > /dev/null 2>&1; then
    echo "$(date -Iseconds): Process $PID died, restarting..."
    nohup uv run trader run >> "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    echo "$(date -Iseconds): Restarted with PID $(cat $PID_FILE)"
else
    echo "$(date -Iseconds): Process $PID alive"
fi
```

(This script is **not** included in the repo; create it manually if needed.)
