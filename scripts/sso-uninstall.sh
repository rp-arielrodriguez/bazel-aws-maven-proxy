#!/usr/bin/env bash
set -euo pipefail

PLIST_DEST="$HOME/Library/LaunchAgents/com.bazel.sso-watcher.plist"

echo "Uninstalling SSO watcher..."

# Unload the agent
USER_ID=$(id -u)
if launchctl bootout "gui/$USER_ID/com.bazel.sso-watcher" 2>/dev/null; then
    echo "✓ Unloaded launchd agent"
else
    echo "  Agent was not loaded"
fi

# Remove plist
if [ -f "$PLIST_DEST" ]; then
    rm "$PLIST_DEST"
    echo "✓ Removed plist"
else
    echo "  Plist not found"
fi

echo ""
echo "SSO watcher uninstalled"
