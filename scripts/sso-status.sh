#!/usr/bin/env bash
set -euo pipefail

PLIST_DEST="$HOME/Library/LaunchAgents/com.bazel.sso-watcher.plist"
STATE_DIR="$HOME/.aws/sso-renewer"
MODE_FILE="$STATE_DIR/mode"
SIGNAL_FILE="$STATE_DIR/login-required.json"

echo "SSO Watcher Status"
echo "────────────────────────────────────────────"

# Installed
if [ -f "$PLIST_DEST" ]; then
    echo "  Installed:    ✓ yes"
else
    echo "  Installed:    ✗ not installed"
    echo ""
    echo "Run: mise run sso-install"
    exit 1
fi

# Running
USER_ID=$(id -u)
if launchctl print "gui/$USER_ID/com.bazel.sso-watcher" &>/dev/null; then
    PID=$(launchctl print "gui/$USER_ID/com.bazel.sso-watcher" 2>/dev/null | grep -m1 'pid =' | awk '{print $NF}')
    echo "  Running:      ✓ pid $PID"
else
    echo "  Running:      ✗ stopped"
fi

# Mode
if [ -f "$MODE_FILE" ]; then
    MODE=$(tr -d '[:space:]' < "$MODE_FILE")
else
    MODE="${SSO_LOGIN_MODE:-notify}"
fi
case "$MODE" in
    standalone) echo "  Mode:         $MODE — idle, manual login only" ;;
    auto)       echo "  Mode:         $MODE — opens browser on expiry" ;;
    silent)     echo "  Mode:         $MODE — token refresh only, no browser" ;;
    notify)     echo "  Mode:         $MODE — asks before opening browser" ;;
    *)          echo "  Mode:         $MODE (unknown)" ;;
esac

# Credentials & signal
if [ -f "$SIGNAL_FILE" ]; then
    if [ "$MODE" = "standalone" ] || [ "$MODE" = "silent" ]; then
        if [ "$MODE" = "silent" ]; then
            echo "  Credentials:  ⚠ expired — silent refresh will retry"
        else
            echo "  Credentials:  ⚠ expired — run: mise run sso-login"
        fi
    else
        # Check if snooze is active
        SNOOZE_UNTIL=""
        if command -v python3 &>/dev/null; then
            SNOOZE_UNTIL=$(python3 -c "
import json, sys
try:
    d = json.load(open('$SIGNAL_FILE'))
    print(d.get('nextAttemptAfter', ''))
except: pass
" 2>/dev/null)
        fi
        if [ -n "$SNOOZE_UNTIL" ]; then
            echo "  Credentials:  ⚠ expired — snoozed until next poll"
        else
            echo "  Credentials:  ⚠ expired — waiting for login"
        fi

        # Next action (only in notify/auto)
        COOLDOWN_FILE="$STATE_DIR/last-login-at.txt"
        if [ -f "$COOLDOWN_FILE" ]; then
            LAST=$(cat "$COOLDOWN_FILE" | cut -d. -f1)
            COOLDOWN="${SSO_COOLDOWN_SECONDS:-600}"
            NOW=$(date +%s)
            ELAPSED=$((NOW - LAST))
            REMAINING=$((COOLDOWN - ELAPSED))
            if [ "$REMAINING" -gt 0 ]; then
                MINS=$((REMAINING / 60))
                SECS=$((REMAINING % 60))
                if [ "$MODE" = "auto" ]; then
                    echo "  Next login:   throttled (${MINS}m ${SECS}s remaining)"
                else
                    echo "  Next dialog:  throttled (${MINS}m ${SECS}s remaining)"
                fi
            else
                if [ "$MODE" = "auto" ]; then
                    echo "  Next login:   ready (will open browser within ${SSO_POLL_SECONDS:-5}s)"
                else
                    echo "  Next dialog:  ready (will show within ${SSO_POLL_SECONDS:-5}s)"
                fi
            fi
        else
            if [ "$MODE" = "auto" ]; then
                echo "  Next login:   ready (will open browser within ${SSO_POLL_SECONDS:-5}s)"
            else
                echo "  Next dialog:  ready (will show within ${SSO_POLL_SECONDS:-5}s)"
            fi
        fi
    fi
else
    echo "  Credentials:  ✓ valid (no renewal needed)"
fi

echo "────────────────────────────────────────────"
