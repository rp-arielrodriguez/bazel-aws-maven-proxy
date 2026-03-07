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
    exit 0
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
    auto)       echo "  Mode:         $MODE — opens webview on expiry" ;;
    silent)     echo "  Mode:         $MODE — token refresh only, no webview" ;;
    notify)     echo "  Mode:         $MODE — asks before opening webview" ;;
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
            SNOOZE_UNTIL=$(SIGNAL_FILE="$SIGNAL_FILE" python3 -c "
import json, os
try:
    d = json.load(open(os.environ['SIGNAL_FILE']))
    print(d.get('nextAttemptAfter', ''))
except Exception: pass
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
            LAST=$(cut -d. -f1 < "$COOLDOWN_FILE")
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
                    echo "  Next login:   ready (will open webview within ${SSO_POLL_SECONDS:-5}s)"
                else
                    echo "  Next dialog:  ready (will show within ${SSO_POLL_SECONDS:-5}s)"
                fi
            fi
        else
            if [ "$MODE" = "auto" ]; then
                echo "  Next login:   ready (will open webview within ${SSO_POLL_SECONDS:-5}s)"
            else
                echo "  Next dialog:  ready (will show within ${SSO_POLL_SECONDS:-5}s)"
            fi
        fi
    fi
else
    echo "  Credentials:  ✓ valid (no renewal needed)"
fi

# Update available
UPDATE_FILE="$STATE_DIR/update-available.json"
if [ -f "$UPDATE_FILE" ]; then
    COMMITS=$(python3 -c "import json; print(json.load(open('$UPDATE_FILE')).get('commits_behind','?'))" 2>/dev/null || echo "?")
    ACTIONS=$(python3 -c "import json; print(json.load(open('$UPDATE_FILE')).get('actions',''))" 2>/dev/null || echo "")
    echo "  Update:       ⚠ $COMMITS commit(s) behind origin/main"
    if [ -n "$ACTIONS" ]; then
        echo "                $ACTIONS"
    fi
    echo "                Run: mise run upgrade"
fi

echo "────────────────────────────────────────────"
