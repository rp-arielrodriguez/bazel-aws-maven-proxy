#!/usr/bin/env bash
set -euo pipefail

PLIST_TEMPLATE="launchd/com.bazel.sso-watcher.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/com.bazel.sso-watcher.plist"
REPO_PATH="$(pwd)"
PYTHON_PATH="$(command -v python3)"

# Load configuration from .env if it exists
if [ -f .env ]; then
    source .env
fi

AWS_PROFILE="${AWS_PROFILE:-default}"
SSO_COOLDOWN_SECONDS="${SSO_COOLDOWN_SECONDS:-600}"
SSO_POLL_SECONDS="${SSO_POLL_SECONDS:-5}"
SSO_LOGIN_MODE="${SSO_LOGIN_MODE:-notify}"

# Check AWS CLI version (>= 2.9 required for silent refresh)
if ! command -v aws &>/dev/null; then
    echo "ERROR: aws CLI not found. Install with: brew install awscli"
    exit 1
fi
AWS_VERSION=$(aws --version 2>&1 | awk '{print $1}' | cut -d/ -f2)
AWS_MAJOR=$(echo "$AWS_VERSION" | cut -d. -f1)
AWS_MINOR=$(echo "$AWS_VERSION" | cut -d. -f2)
if [ "$AWS_MAJOR" -lt 2 ] || { [ "$AWS_MAJOR" -eq 2 ] && [ "$AWS_MINOR" -lt 9 ]; }; then
    echo "ERROR: aws-cli $AWS_VERSION is too old. Need >= 2.9 for silent token refresh."
    echo "Update with: brew upgrade awscli"
    exit 1
fi

echo "Installing SSO watcher..."
echo "  Repository: $REPO_PATH"
echo "  Python: $PYTHON_PATH"
echo "  AWS Profile: $AWS_PROFILE"
echo "  Login mode: $SSO_LOGIN_MODE"
echo "  Cooldown: ${SSO_COOLDOWN_SECONDS}s"
echo "  Poll interval: ${SSO_POLL_SECONDS}s"
echo ""

# ---- Build SSO login webview (.app bundle) ----
WEBVIEW_SRC="$REPO_PATH/sso-watcher/webview/SSOLoginView.swift"
WEBVIEW_PLIST="$REPO_PATH/sso-watcher/webview/Info.plist"
WEBVIEW_APP_DIR="$HOME/.aws/sso-renewer/bin/SSOLogin.app"
WEBVIEW_BIN="$WEBVIEW_APP_DIR/Contents/MacOS/sso-webview"

if command -v swiftc &>/dev/null; then
    echo "Building SSO login webview..."
    mkdir -p "$WEBVIEW_APP_DIR/Contents/MacOS" "$WEBVIEW_APP_DIR/Contents/Resources"
    if swiftc "$WEBVIEW_SRC" -o "$WEBVIEW_BIN" -framework Cocoa -framework WebKit 2>&1; then
        cp "$WEBVIEW_PLIST" "$WEBVIEW_APP_DIR/Contents/Info.plist"
        echo "✓ Built webview: $WEBVIEW_APP_DIR"
    else
        echo "⚠ Webview build failed (will fall back to system browser)"
    fi
else
    echo "⚠ swiftc not found — install Xcode Command Line Tools for sandboxed login window"
    echo "  Without it, SSO login will open in your default browser"
    echo "  Install with: xcode-select --install"
fi
echo ""

# Create LaunchAgents directory if it doesn't exist
mkdir -p "$HOME/Library/LaunchAgents"

# Replace placeholders in template
sed -e "s|{{PYTHON_PATH}}|$PYTHON_PATH|g" \
    -e "s|{{REPO_PATH}}|$REPO_PATH|g" \
    -e "s|{{HOME}}|$HOME|g" \
    -e "s|{{AWS_PROFILE}}|$AWS_PROFILE|g" \
    -e "s|{{SSO_COOLDOWN_SECONDS}}|$SSO_COOLDOWN_SECONDS|g" \
    -e "s|{{SSO_POLL_SECONDS}}|$SSO_POLL_SECONDS|g" \
    -e "s|{{SSO_LOGIN_MODE}}|$SSO_LOGIN_MODE|g" \
    "$PLIST_TEMPLATE" > "$PLIST_DEST"

echo "✓ Installed plist to: $PLIST_DEST"

# Load launchd agent
USER_ID=$(id -u)
LABEL="com.bazel.sso-watcher"
DOMAIN="gui/$USER_ID"

# Check if GUI domain is available (not available via SSH)
if ! launchctl print "$DOMAIN" &>/dev/null 2>&1; then
    echo ""
    echo "✓ Plist installed. Launchd agent not loaded (no GUI session)."
    echo "  The agent will start automatically on next console login,"
    echo "  or load manually with:"
    echo "    launchctl bootstrap gui/\$(id -u) $PLIST_DEST"
    exit 0
fi

if launchctl print "$DOMAIN/$LABEL" &>/dev/null; then
    echo "Agent already loaded, updating..."

    # Bootout to unload
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true

    # Bootstrap with new config
    launchctl bootstrap "$DOMAIN" "$PLIST_DEST"

    # Kickstart to ensure it starts immediately
    launchctl kickstart -k "$DOMAIN/$LABEL" 2>/dev/null || true

    echo "✓ Updated and restarted launchd agent"
else
    echo "Loading new agent..."
    launchctl bootstrap "$DOMAIN" "$PLIST_DEST"
    echo "✓ Loaded launchd agent"
fi

echo ""
echo "SSO watcher is now running!"
echo "View logs with: mise run sso-logs"
