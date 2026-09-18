#!/usr/bin/env bash
# Preflight checks for unattended multi-day paper run
# Exit codes: 0 = all checks passed, 1 = soft warnings, 2 = hard failures

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_DIR"

# Colors for output
RED='\033[0;31m'
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
NC='\033[0m' # No Color

HARD_FAIL=0
SOFT_WARN=0

check_hard() {
    local name="$1"
    local condition="$2"
    local message="$3"
    
    if ! eval "$condition"; then
        echo -e "${RED}✗ HARD FAIL${NC}: $name"
        echo "  $message"
        HARD_FAIL=1
    else
        echo -e "${GREEN}✓${NC} $name"
    fi
}

check_soft() {
    local name="$1"
    local condition="$2"
    local message="$3"
    
    if ! eval "$condition"; then
        echo -e "${YELLOW}⚠ SOFT WARN${NC}: $name"
        echo "  $message"
        SOFT_WARN=1
    else
        echo -e "${GREEN}✓${NC} $name"
    fi
}

echo "==================================================================="
echo "Preflight Checks for Unattended Multi-Day Paper Run"
echo "==================================================================="
echo ""

# 1. Check .env file exists
check_hard ".env file exists" \
    "[ -f .env ]" \
    ".env file not found. Create it with broker keys."

# 2. Check for broker keys (at least one set)
if [ -f .env ]; then
    check_hard "Broker keys present" \
        "grep -qE 'ALPACA_API_KEY|ROBINHOOD' .env" \
        "No broker keys found in .env. Add ALPACA_API_KEY or ROBINHOOD tokens."
fi

# 3. Check risk.toml exists
check_hard "risk.toml exists" \
    "[ -f risk.toml ]" \
    "risk.toml not found. Copy from risk.toml.example or create one."

# 4. Check data directory exists
check_soft "data/ directory exists" \
    "[ -d data ]" \
    "data/ directory not found. It will be created on first run."

# 5. Check logs directory exists
check_soft "logs/ directory exists" \
    "[ -d logs ]" \
    "logs/ directory not found. Create it: mkdir -p logs"

# 6. Check disk space (>1GB free)
if command -v df &> /dev/null; then
    FREE_SPACE_KB=$(df . | tail -1 | awk '{print $4}')
    FREE_SPACE_MB=$((FREE_SPACE_KB / 1024))
    check_soft "Disk space >1GB free" \
        "[ $FREE_SPACE_MB -gt 1000 ]" \
        "Low disk space: ${FREE_SPACE_MB}MB free. Need >1GB."
fi

# 7. Check log file size (if exists)
if [ -f logs/trader.jsonl ]; then
    LOG_SIZE_BYTES=$(stat -f%z logs/trader.jsonl 2>/dev/null || stat -c%s logs/trader.jsonl 2>/dev/null || echo "0")
    LOG_SIZE_MB=$((LOG_SIZE_BYTES / 1024 / 1024))
    check_soft "Log file size <500MB" \
        "[ $LOG_SIZE_MB -lt 500 ]" \
        "Large log file: ${LOG_SIZE_MB}MB. Consider rotation."
fi

# 8. Check for alert webhook (optional but recommended)
if [ -f .env ]; then
    check_soft "Alert webhook configured" \
        "grep -qE 'TRADER_ALERT_WEBHOOK' .env" \
        "No TRADER_ALERT_WEBHOOK in .env. You won't get remote alerts."
fi

# 9. Check ANTHROPIC_API_KEY present (unless --stub mode)
if [ -f .env ]; then
    check_hard "ANTHROPIC_API_KEY present" \
        "grep -qE 'ANTHROPIC_API_KEY' .env" \
        "No ANTHROPIC_API_KEY in .env. Add it or use --stub mode."
fi

# 10. Check for supervision (systemd/launchd) or note fallback
if command -v systemctl &> /dev/null; then
    if systemctl is-enabled trader.service &> /dev/null; then
        echo -e "${GREEN}✓${NC} systemd supervision configured (trader.service)"
    else
        check_soft "Supervisor configured" "false" \
            "systemd available but trader.service not enabled. Use nohup fallback or enable service."
    fi
elif command -v launchctl &> /dev/null; then
    if launchctl list | grep -q com.daytrader.harness; then
        echo -e "${GREEN}✓${NC} launchd supervision configured (com.daytrader.harness)"
    else
        check_soft "Supervisor configured" "false" \
            "launchctl available but com.daytrader.harness not loaded. Use nohup fallback or load plist."
    fi
else
    check_soft "Supervisor available" "false" \
        "No systemd or launchctl found. Use nohup fallback (see UNATTENDED_WEEK.md)"
fi

echo ""
echo "==================================================================="

# Summary
if [ $HARD_FAIL -eq 1 ]; then
    echo -e "${RED}HARD FAILURES DETECTED${NC}: Fix the issues above before starting."
    echo "See UNATTENDED_WEEK.md for details."
    exit 2
elif [ $SOFT_WARN -eq 1 ]; then
    echo -e "${YELLOW}SOFT WARNINGS DETECTED${NC}: Proceed with caution."
    echo "See UNATTENDED_WEEK.md for recommended fixes."
    exit 1
else
    echo -e "${GREEN}ALL CHECKS PASSED${NC}: Ready to start unattended run."
    echo "Next: Review UNATTENDED_WEEK.md 'Start' section."
    exit 0
fi
