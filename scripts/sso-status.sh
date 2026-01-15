#!/usr/bin/env bash
set -euo pipefail

echo "SSO Watcher Status:"
echo ""

PLIST_DEST="$HOME/Library/LaunchAgents/com.bazel.sso-watcher.plist"

if [ -f "$PLIST_DEST" ]; then
    echo "✓ Plist installed: $PLIST_DEST"
else
    echo "✗ Plist not installed"
    exit 1
fi

echo ""
USER_ID=$(id -u)
launchctl print "gui/$USER_ID/com.bazel.sso-watcher" 2>/dev/null || {
    echo "✗ Agent not loaded"
    exit 1
}
