#!/usr/bin/env bash
# Simple watchdog: check heartbeat and restart if stale
# Usage: cron this every 5 minutes if you don't have systemd/launchd

set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/home/trader/day-trader}"
HEARTBEAT="$INSTALL_DIR/data/heartbeat.json"
MAX_AGE_SECONDS="${MAX_AGE_SECONDS:-600}"  # 10 minutes

# Check if heartbeat exists and is recent
if [ ! -f "$HEARTBEAT" ]; then
    echo "$(date -Iseconds): No heartbeat file, assuming first run or manual management"
    exit 0
fi

# Parse timestamp from heartbeat
TIMESTAMP=$(jq -r '.timestamp' "$HEARTBEAT" 2>/dev/null || echo "")
if [ -z "$TIMESTAMP" ]; then
    echo "$(date -Iseconds): Malformed heartbeat, not restarting"
    exit 0
fi

# Calculate age
NOW=$(date +%s)
HEARTBEAT_TIME=$(date -d "$TIMESTAMP" +%s 2>/dev/null || echo "0")
AGE=$((NOW - HEARTBEAT_TIME))

if [ "$AGE" -gt "$MAX_AGE_SECONDS" ]; then
    echo "$(date -Iseconds): Heartbeat stale (${AGE}s > ${MAX_AGE_SECONDS}s), restarting"
    
    # Try systemd first
    if systemctl is-active --quiet trader.service 2>/dev/null; then
        systemctl restart trader.service
        exit 0
    fi
    
    # Try launchd
    if launchctl list | grep -q com.daytrader.harness 2>/dev/null; then
        launchctl kickstart -k system/com.daytrader.harness
        exit 0
    fi
    
    # Manual restart (adjust to your setup)
    # pkill -f "trader.cli run" || true
    # cd "$INSTALL_DIR"
    # nohup .venv/bin/python -m trader.cli run >> logs/trader.jsonl 2>&1 &
    
    echo "$(date -Iseconds): No supervisor found, manual restart required"
    exit 1
fi

echo "$(date -Iseconds): Heartbeat healthy (${AGE}s old)"
exit 0
