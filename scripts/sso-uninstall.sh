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

# Clean up webview app bundle
if [ -d "$HOME/.aws/sso-renewer/bin" ]; then
    rm -rf "$HOME/.aws/sso-renewer/bin"
    echo "✓ Removed webview app bundle"
else
    echo "  Webview app bundle not found"
fi

# Remove bazel-proxy shim from PATH
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
SHIM_PATH="$BIN_DIR/bazel-proxy"
if [ -f "$SHIM_PATH" ]; then
    rm -f "$SHIM_PATH"
    echo "✓ Removed bazel-proxy command from $BIN_DIR"
else
    echo "  bazel-proxy command not found in $BIN_DIR"
fi

echo ""
echo "SSO watcher uninstalled"
