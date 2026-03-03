#!/usr/bin/env bash
set -euo pipefail

PLIST_DEST="$HOME/Library/LaunchAgents/com.bazel.sso-watcher.plist"

if [ ! -f "$PLIST_DEST" ]; then
    echo "Error: SSO watcher not installed. Run: mise run sso-install"
    exit 1
fi

echo "Restarting SSO watcher..."

USER_ID=$(id -u)

# Unload
launchctl bootout "gui/$USER_ID/com.bazel.sso-watcher" 2>/dev/null || true
echo "✓ Stopped agent"

# Load
launchctl bootstrap "gui/$USER_ID" "$PLIST_DEST"
echo "✓ Started agent"

echo ""
echo "SSO watcher restarted"
