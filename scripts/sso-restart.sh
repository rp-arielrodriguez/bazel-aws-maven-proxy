#!/usr/bin/env bash
set -euo pipefail

echo "Restarting SSO watcher..."

USER_ID=$(id -u)
PLIST_DEST="$HOME/Library/LaunchAgents/com.bazel.sso-watcher.plist"

# Unload
launchctl bootout "gui/$USER_ID/com.bazel.sso-watcher" 2>/dev/null || true
echo "✓ Stopped agent"

# Load
launchctl bootstrap "gui/$USER_ID" "$PLIST_DEST"
echo "✓ Started agent"

echo ""
echo "SSO watcher restarted"
