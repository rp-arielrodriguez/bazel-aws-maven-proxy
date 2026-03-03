#!/usr/bin/env bash
set -euo pipefail

PLIST_DEST="$HOME/Library/LaunchAgents/com.bazel.sso-watcher.plist"

if [ ! -f "$PLIST_DEST" ]; then
    echo "Error: SSO watcher not installed. Run: mise run sso-install"
    exit 1
fi

echo "Restarting SSO watcher..."

USER_ID=$(id -u)
DOMAIN="gui/$USER_ID"

# Check if GUI domain is available (not available via SSH)
if ! launchctl print "$DOMAIN" &>/dev/null 2>&1; then
    echo "Error: GUI session not available (running over SSH?)"
    echo "  The agent can only be restarted from a console session."
    echo "  Alternatively, load manually after connecting to the console:"
    echo "    launchctl bootstrap gui/\$(id -u) $PLIST_DEST"
    exit 1
fi

# Unload
launchctl bootout "$DOMAIN/com.bazel.sso-watcher" 2>/dev/null || true
echo "✓ Stopped agent"

# Load
launchctl bootstrap "$DOMAIN" "$PLIST_DEST"
echo "✓ Started agent"

echo ""
echo "SSO watcher restarted"
