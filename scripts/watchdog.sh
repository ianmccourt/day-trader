#!/usr/bin/env bash
# Heartbeat watchdog. Cron every 5 minutes if you don't have systemd/launchd.
# INSTALL_DIR must point at the repo root.

set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/home/trader/day-trader}"
HEARTBEAT="$INSTALL_DIR/data/heartbeat.json"
MAX_AGE_SECONDS="${MAX_AGE_SECONDS:-600}"

if [ ! -f "$HEARTBEAT" ]; then
    echo "$(date -Iseconds): No heartbeat file, assuming first run or manual management"
    exit 0
fi

TIMESTAMP=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('timestamp',''))" "$HEARTBEAT" 2>/dev/null || echo "")
if [ -z "$TIMESTAMP" ]; then
    echo "$(date -Iseconds): Malformed heartbeat, not restarting"
    exit 0
fi

NOW=$(date +%s)
HEARTBEAT_TIME=$(python3 -c "from datetime import datetime; t='$TIMESTAMP'; print(int(datetime.fromisoformat(t.replace('Z','+00:00')).timestamp()))" 2>/dev/null || echo "0")
AGE=$((NOW - HEARTBEAT_TIME))

if [ "$AGE" -gt "$MAX_AGE_SECONDS" ]; then
    echo "$(date -Iseconds): Heartbeat stale (${AGE}s > ${MAX_AGE_SECONDS}s), restarting"

    if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet trader.service 2>/dev/null; then
        systemctl restart trader.service
        exit 0
    fi

    if command -v launchctl >/dev/null 2>&1 && launchctl list 2>/dev/null | grep -q com.daytrader.harness; then
        launchctl kickstart -k "gui/$(id -u)/com.daytrader.harness" 2>/dev/null \
            || launchctl kickstart -k system/com.daytrader.harness
        exit 0
    fi

    echo "$(date -Iseconds): No supervisor found, manual restart required"
    exit 1
fi

echo "$(date -Iseconds): Heartbeat healthy (${AGE}s old)"
exit 0
