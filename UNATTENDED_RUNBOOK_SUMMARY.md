# Unattended Multi-Day Paper Run Runbook - Implementation Summary

This document summarizes the unattended runbook implementation in PR #6.

## Overview

**Closes the documentation/prove path gap.** Provides Ian with a concrete, actionable runbook to prove operational stability: leave `trader run` up across multiple sessions/days on Alpaca paper with supervision + alerts, then evaluate.

**Key principle**: This is documentation and helper scripts only. No implementation code from other PRs. Fresh off main.

## Deliverables

### 1. UNATTENDED_WEEK.md (~450 lines)

Complete checklist-driven runbook for proving multi-day operational stability.

#### Structure

**Preflight Checklist**:
1. Paper keys only (hard fail if live keys detected)
2. Risk configuration (`risk.toml` or `risk.conservative.toml`)
3. Kill switch off
4. Disk space >1GB, logs <500MB (or rotated)
5. Alert webhook (Slack/Discord, optional but recommended)
6. Supervision setup (systemd/launchd from ops PR #4, or nohup fallback)
7. Run preflight script (`./scripts/preflight_unattended.sh`)

**Start Section**:
- systemd: `sudo systemctl start trader.service`
- launchd: `launchctl start com.daytrader.harness`
- nohup fallback: `nohup uv run trader run >> logs/trader.jsonl 2>&1 &`
- Verification: process running, first cycle logged, heartbeat created

**During Section** (daily monitoring):
1. Process health: supervisor status, heartbeat age
2. Cycle status: `trader cycles`, `trader status`
3. Alerts: `trader alerts` (consecutive errors, kill switch, daily loss, heartbeat)
4. Risk rejections: `trader rejections` (check for patterns)
5. Orphan accumulation: `trader status | grep "Open positions"`
6. Disk space: `df -h .`

**Stop Section**:
1. Graceful SIGTERM: `systemctl stop` or `kill -TERM`
2. Reconcile: `trader reconcile` (check final state)
3. Evaluate: `trader evaluate --start DATE --end DATE`

**Pass/Fail Criteria**:

✅ **Pass** (unattended week proven):
- N trading days completed (≥5 days minimum)
- No silent death (process ran or supervisor restarted)
- Orphans bounded (≤1-2 per day, <5 at any time)
- Evaluate completes successfully
- Risk layer enforced (no bypasses)
- Logs healthy (cycle_end events, no extended error streaks)

❌ **Fail** (fix before declaring proven):
- Silent death without detection
- Unbounded orphans (>5 at once, >10 across week)
- Evaluate crashes or corrupt reports
- Evidence of risk bypass
- DB corruption or missing cycle rows

⚠️ **Soft failures** (investigate but don't block):
- High rejection rate (>50%)
- Low trade count (<5 trades over 5 days)
- Negative P&L (not proof of edge)

**Troubleshooting Section**:
- Process died: check supervisor, logs, disk, OOM
- Consecutive errors: API rate limits, broker outage, prompt errors
- Orphan accumulation: reconcile, investigate close logic
- Max daily loss latched: expected behavior (risk layer working)

**What This Proves/Doesn't**:
- ✅ Operational stability, supervisor, alerting, risk layer, logging
- ❌ Edge (use backtest), optimal params, multi-month stability, live readiness

**Next Steps**:
1. Tune risk limits based on rejection rate
2. Tune playbook based on evaluate metrics
3. Extend to multi-week (2-4 weeks)
4. Compare live R to backtest R
5. Consider live (only after multi-week + positive edge)

**Appendix**: Minimal nohup fallback with manual watchdog script (if no supervisor)

### 2. scripts/preflight_unattended.sh (~150 lines)

Bash script for automated pre-flight checks before starting unattended run.

#### Checks Performed

**Hard Checks** (exit 2 on failure):
1. `.env` file exists
2. Broker keys present (`ALPACA_API_KEY` or `ROBINHOOD`)
3. `ANTHROPIC_API_KEY` present
4. `risk.toml` exists

**Soft Checks** (exit 1 on warn):
1. `data/` directory exists
2. `logs/` directory exists
3. Disk space >1GB free
4. Log file <500MB (if exists)
5. Alert webhook configured (`TRADER_ALERT_WEBHOOK` in `.env`)
6. Supervisor configured (systemd/launchd detected)

#### Output Format

Color-coded with emoji markers:
- 🟢 `✓` Green: Check passed
- 🟡 `⚠ SOFT WARN`: Soft warning
- 🔴 `✗ HARD FAIL`: Hard failure

#### Exit Codes

- `0`: All checks passed, ready to start
- `1`: Soft warnings detected, proceed with caution
- `2`: Hard failures detected, fix before starting

#### Usage

```bash
./scripts/preflight_unattended.sh

# Example output:
# ===================================================================
# Preflight Checks for Unattended Multi-Day Paper Run
# ===================================================================
#
# ✓ .env file exists
# ✓ Broker keys present
# ✓ ANTHROPIC_API_KEY present
# ✓ risk.toml exists
# ✓ data/ directory exists
# ✓ logs/ directory exists
# ✓ Disk space >1GB free
# ⚠ SOFT WARN: Alert webhook configured
#   No TRADER_ALERT_WEBHOOK in .env. You won't get remote alerts.
# ⚠ SOFT WARN: Supervisor configured
#   No systemd or launchctl found. Use nohup fallback.
#
# ===================================================================
# SOFT WARNINGS DETECTED: Proceed with caution.
# See UNATTENDED_WEEK.md for recommended fixes.
```

### 3. README.md Pointer (+10 lines)

Added minimal "Unattended multi-day sessions" subsection under "Running and monitoring".

**Content**:
- Links to `UNATTENDED_WEEK.md`
- Summarizes what runbook covers
- Quick start: run preflight script, follow runbook
- Intentionally brief (full details in runbook)

**Location**: Right before "Control panel" subsection in "Running and monitoring"

## What This Closes

**This PR closes the "documentation/prove path" gap.**

Before this PR:
- ❌ No concrete guide for multi-day unattended runs
- ❌ No preflight checklist
- ❌ No pass/fail criteria for "proven"
- ❌ No monitoring guide
- ❌ No troubleshooting guide

After this PR:
- ✅ Complete runbook (`UNATTENDED_WEEK.md`)
- ✅ Automated preflight checks (`scripts/preflight_unattended.sh`)
- ✅ Clear pass/fail criteria
- ✅ Monitoring guide (what to check daily)
- ✅ Troubleshooting for common issues
- ✅ README pointer for discoverability

**Ian can now**: Follow the runbook alone to prove operational stability over 5+ trading days on Alpaca paper.

**Still requires**: Ian to actually run it (not automated, intentionally manual).

## Non-Goals (Delivered As Requested)

- ❌ Does not start actual multi-day process in CI
- ❌ Does not modify risk limits, prompts, broker, or rsi/backtest code
- ❌ Does not vendor `deploy/` units (links to them; nohup fallback documented)
- ❌ Does not include leftover files from ops/rsi/backtest PRs
- ❌ Fresh off main (no dependencies on other PRs)

## Files Changed

- **New**: `UNATTENDED_WEEK.md` (~450 lines)
- **New**: `scripts/preflight_unattended.sh` (~150 lines)
- **Modified**: `README.md` (+10 lines)

**Total**: +610 lines, 3 files, 0 implementation code

## Testing

### Preflight Script

```bash
# Syntax check
bash -n scripts/preflight_unattended.sh
# Output: (none, exit 0 if valid)

# Dry run on current repo
./scripts/preflight_unattended.sh
# Output: colored check results, exit code 0/1/2
```

Script is syntactically valid, executable, and checks work on current repo state.

### Runbook

- ✅ All commands are copy-pasteable
- ✅ All paths reference existing or documented files
- ✅ Supervisor paths reference `deploy/` (from ops PR #4) with fallback
- ✅ Monitoring commands use existing CLI (`trader status`, `trader cycles`, etc.)
- ✅ Pass/fail criteria are objective and measurable

## Dependencies on Other PRs

### Ops PR #4 (Optional, Graceful Degradation)

Runbook references:
- `deploy/trader.service` (systemd unit)
- `deploy/com.daytrader.plist` (launchd plist)
- `deploy/logrotate-trader.conf` (log rotation)
- `scripts/watchdog.sh` (heartbeat-based restart)
- `trader alerts` command (alert viewing)
- `data/heartbeat.json` (heartbeat file)

**Graceful degradation**: If ops PR #4 not merged:
- Runbook documents nohup fallback (no supervisor)
- Preflight script soft-warns "no supervisor found"
- Appendix provides minimal nohup + manual watchdog approach
- Core prove path still works (just less automated)

### No Other Dependencies

- ❌ Does not depend on RSI PRs (#2, #3)
- ❌ Does not depend on ORB backtest PR (#5)
- ❌ Clean fresh branch off main

## Example Workflow (Post-Merge)

1. **Clone repo, checkout main**
2. **Run preflight**:
   ```bash
   ./scripts/preflight_unattended.sh
   # Fix any hard failures
   ```
3. **Review runbook**:
   ```bash
   cat UNATTENDED_WEEK.md
   # Understand preflight → start → monitor → stop → evaluate
   ```
4. **Start unattended run** (e.g. Monday morning):
   ```bash
   # If ops PR #4 merged:
   sudo systemctl start trader.service
   
   # Fallback:
   nohup uv run trader run >> logs/trader.jsonl 2>&1 &
   echo $! > logs/trader.pid
   ```
5. **Monitor daily** (M-F):
   ```bash
   uv run trader status
   uv run trader cycles --limit 10
   uv run trader alerts --limit 20
   ```
6. **Stop Friday EOD**:
   ```bash
   sudo systemctl stop trader.service
   # or: kill -TERM $(cat logs/trader.pid)
   ```
7. **Evaluate**:
   ```bash
   uv run trader reconcile
   uv run trader evaluate --start 2026-09-15 --end 2026-09-20
   ```
8. **Check pass/fail**:
   - ✅ 5 trading days completed
   - ✅ No silent death
   - ✅ Orphans bounded
   - ✅ Evaluate completed
   - ✅ Risk layer enforced
   - ✅ Logs healthy

9. **Document**: Add "Proven: week of 2026-09-15" note to runbook

## PR Details

- **Branch**: `cursor/unattended-runbook-9f88`
- **PR**: https://github.com/ianmccourt/day-trader/pull/6
- **Status**: Ready for review (non-draft)
- **Commits**: 1 (runbook + preflight + README pointer)
- **Base**: `main` (fresh, no other PR dependencies)

## Key Principles Followed

1. **Poteto-mode**: Few files, obvious names, no implementation code
2. **Fresh off main**: No leftover files from other PRs
3. **Documentation-first**: Runbook is actionable alone
4. **Graceful degradation**: Works with or without ops PR #4
5. **Objective criteria**: Pass/fail is measurable
6. **Honest limitations**: Doesn't claim to prove edge or optimality

## Next Steps (Post-Prove)

After Ian runs the prove and meets pass criteria:

1. **Document in runbook**: Add "Proven: week of YYYY-MM-DD" section
2. **Tune based on findings**:
   - Risk limits (if rejection rate high)
   - Playbook rules (if win rate low)
   - Supervision (if restarts common)
3. **Extend prove**:
   - 2-4 week run for longer-term stability
   - Different market conditions (volatile vs calm)
4. **Compare to backtest**:
   - Live R vs ORB backtest R
   - Edge translation quality
5. **Consider live** (only after multi-week + positive edge + additional diligence)
