#!/usr/bin/env bash
set -euo pipefail

PLIST_TEMPLATE="launchd/com.bazel.sso-watcher.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/com.bazel.sso-watcher.plist"
REPO_PATH="$(pwd)"
PYTHON_PATH="$(which python3)"

# Load configuration from .env if it exists
if [ -f .env ]; then
    source .env
fi

AWS_PROFILE="${AWS_PROFILE:-default}"
SSO_COOLDOWN_SECONDS="${SSO_COOLDOWN_SECONDS:-600}"
SSO_POLL_SECONDS="${SSO_POLL_SECONDS:-5}"

echo "Installing SSO watcher..."
echo "  Repository: $REPO_PATH"
echo "  Python: $PYTHON_PATH"
echo "  AWS Profile: $AWS_PROFILE"
echo "  Cooldown: ${SSO_COOLDOWN_SECONDS}s"
echo "  Poll interval: ${SSO_POLL_SECONDS}s"
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
    "$PLIST_TEMPLATE" > "$PLIST_DEST"

echo "✓ Installed plist to: $PLIST_DEST"

# Check if already loaded
USER_ID=$(id -u)
LABEL="com.bazel.sso-watcher"
DOMAIN="gui/$USER_ID"

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
