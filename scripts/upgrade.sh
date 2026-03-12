#!/usr/bin/env bash
# Smart upgrade: git pull + selective rebuild/restart based on what changed.
#
# Detects which components were affected by the update and only
# rebuilds/restarts what's needed:
#   - s3proxy/sso-monitor sources → restart native processes (new default)
#   - wrapper script changes → restart native processes
#   - container mode users → migrate to native mode
#   - install.sh changes → regenerate bazel-proxy shim
#   - Swift webview changes → rebuild webview (via sso-install)
#   - watcher.py/launchd changes → reinstall watcher daemon
#   - .env.example changes → warn about new config vars
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

source scripts/container-engine.sh 2>/dev/null || true

echo "Checking for updates..."

# macOS doesn't have timeout(1); use a background subshell with kill
_fetch_with_timeout() {
  git fetch origin main --quiet 2>/dev/null &
  local pid=$!
  ( sleep 15 && kill "$pid" 2>/dev/null ) &
  local timer=$!
  wait "$pid" 2>/dev/null
  local rc=$?
  kill "$timer" 2>/dev/null
  wait "$timer" 2>/dev/null
  return $rc
}

if ! _fetch_with_timeout; then
  echo "✗ Could not reach remote"
  exit 1
fi

# Track what was running before upgrade (before any changes)
WAS_RUNNING=false
if [ -f "$HOME/.bazel-aws-maven-proxy/s3proxy.pid" ]; then
  PID=$(cat "$HOME/.bazel-aws-maven-proxy/s3proxy.pid" 2>/dev/null)
  [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null && WAS_RUNNING=true
elif $COMPOSE_CMD ps 2>/dev/null | grep -q "bazel-s3-proxy.*Up"; then
  WAS_RUNNING=true
fi

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

# Check if shim is out of date BEFORE checking git status
# (so we regenerate even when already up to date)
SHIM_FILE="$HOME/.local/bin/bazel-proxy"
NEED_SHIM=false
if [ -f "$SHIM_FILE" ]; then
  SHIM_VERSION=$(grep "^# SHIM_VERSION:" "$SHIM_FILE" 2>/dev/null | cut -d: -f2 | tr -d ' ' || echo "")
  CURRENT_VERSION=$(git rev-parse --short HEAD)
  if [ "$SHIM_VERSION" != "$CURRENT_VERSION" ]; then
    NEED_SHIM=true
  fi
else
  NEED_SHIM=true
fi

if [ "$LOCAL" = "$REMOTE" ]; then
  # No git changes, but may need to regenerate shim
  if $NEED_SHIM; then
    echo ""
    echo "Command shim outdated — regenerating bazel-proxy..."
    if bash scripts/install.sh --shim-only 2>&1; then
      echo "✓ Command shim updated"
    else
      echo "⚠ Shim regeneration failed — try: bash scripts/install.sh --shim-only"
    fi
  else
    echo "✓ Already up to date"
  fi
  exit 0
fi

BEHIND=$(git rev-list --count HEAD..origin/main)
# Detect current mode
_detect_mode() {
  # Check for native mode (PID files)
  if [ -f "$HOME/.bazel-aws-maven-proxy/s3proxy.pid" ] || [ -f "$HOME/.bazel-aws-maven-proxy/sso-monitor.pid" ]; then
    echo "native"
    return
  fi
  # Check for container mode
  if $COMPOSE_CMD ps --filter "name=bazel-s3-proxy" --format "{{.Names}}" 2>/dev/null | grep -q "bazel-s3-proxy"; then
    echo "container"
    return
  fi
  # Default to native for new users
  echo "native"
}

CURRENT_MODE=$(_detect_mode)
NEED_NATIVE_RESTART=false
CHANGED_FILES=$(git diff --name-only HEAD..origin/main)

# Pull
if ! git pull --ff-only origin main; then
  echo "✗ Pull failed (local changes?). Resolve manually, then re-run."
  exit 1
fi

# If upgrade.sh itself changed, re-execute the new version
if echo "$CHANGED_FILES" | grep -q "scripts/upgrade.sh"; then
  echo "Upgrade script updated — restarting with new version..."
  export BAZEL_PROXY_WAS_RUNNING=$WAS_RUNNING
  exec bash "$0" "$@"
fi

# Re-check shim version after pull (in case install.sh changed)
SHIM_FILE="$HOME/.local/bin/bazel-proxy"
if [ -f "$SHIM_FILE" ]; then
  SHIM_VERSION=$(grep "^# SHIM_VERSION:" "$SHIM_FILE" 2>/dev/null | cut -d: -f2 | tr -d ' ' || echo "")
  CURRENT_VERSION=$(git rev-parse --short HEAD)
  if [ "$SHIM_VERSION" != "$CURRENT_VERSION" ]; then
    NEED_SHIM=true
  fi
else
  NEED_SHIM=true
fi

# Categorize changes
NEED_CONTAINERS=false
NEED_NATIVE=false
NEED_WEBVIEW=false
NEED_WATCHER=false
NEED_SETUP=false
NEW_CONFIG=false

while IFS= read -r file; do
  case "$file" in
    s3proxy/*|sso-monitor/*|docker-compose.yaml) NEED_CONTAINERS=true ;;
    scripts/s3proxy-*.sh|scripts/sso-monitor-*.sh) NEED_NATIVE=true ;;
    sso-watcher/webview/*) NEED_WEBVIEW=true ;;
    sso-watcher/watcher.py|launchd/*) NEED_WATCHER=true ;;
    scripts/setup.py|scripts/setup.sh) NEED_SETUP=true ;;
    .env.example) NEW_CONFIG=true ;;
  esac
done <<< "$CHANGED_FILES"

# Detect mode transition (container -> native) - always migrate if in container mode
if [ "$CURRENT_MODE" = "container" ]; then
  echo ""
  echo "Note: Switching from container mode to native mode (new default)"
  echo "  Stopping containers before migrating..."
  $COMPOSE_CMD down 2>/dev/null || true
  CURRENT_MODE="native"
  NEED_NATIVE_RESTART=true
fi

ACTIONS_TAKEN=""

# 1. Restart native processes if needed (new default)
if [ "$CURRENT_MODE" = "native" ] && { $NEED_NATIVE || $NEED_CONTAINERS || $NEED_NATIVE_RESTART; }; then
  echo ""
  echo "Native service sources changed — restarting..."
  # Stop any running native processes first
  bash scripts/s3proxy-stop.sh 2>/dev/null || true
  bash scripts/sso-monitor-stop.sh 2>/dev/null || true
  sleep 1
  # Start native processes
  if bash scripts/s3proxy-start.sh 2>&1 && bash scripts/sso-monitor-start.sh 2>&1; then
    ACTIONS_TAKEN="${ACTIONS_TAKEN:+$ACTIONS_TAKEN, }native services restarted"
  else
    echo "⚠ Native service restart failed — try: mise run start"
  fi
elif [ "$CURRENT_MODE" = "container" ] && $NEED_CONTAINERS; then
  echo ""
  echo "Container sources changed — rebuilding..."
  if $COMPOSE_CMD up -d --build 2>&1; then
    ACTIONS_TAKEN="${ACTIONS_TAKEN:+$ACTIONS_TAKEN, }containers rebuilt"
  else
    echo "⚠ Container rebuild failed — try: mise run containers:rebuild"
  fi
fi

# 2. Regenerate shim if install.sh changed or shim version is stale
if $NEED_SHIM; then
  echo ""
  echo "Command shim outdated — regenerating bazel-proxy..."
  if bash scripts/install.sh --shim-only 2>&1; then
    ACTIONS_TAKEN="${ACTIONS_TAKEN:+$ACTIONS_TAKEN, }command shim updated"
  else
    echo "⚠ Shim regeneration failed — try: bash scripts/install.sh --shim-only"
  fi
fi

# 3. Reinstall watcher if webview or watcher changed
if $NEED_WEBVIEW || $NEED_WATCHER; then
  echo ""
  if $NEED_WEBVIEW; then
    echo "Webview sources changed — rebuilding..."
    # Force rebuild by removing the binary (sso-install skips if newer)
    rm -f "$HOME/.aws/sso-renewer/bin/SSOLogin.app/Contents/MacOS/sso-webview" 2>/dev/null || true
  fi
  if $NEED_WATCHER; then
    echo "Watcher sources changed — reinstalling daemon..."
  fi
  if bash scripts/sso-install.sh 2>&1; then
    ACTIONS_TAKEN="${ACTIONS_TAKEN:+$ACTIONS_TAKEN, }watcher reinstalled"
  else
    echo "⚠ Watcher reinstall failed — try: mise run sso-install"
  fi
fi

# 4. Check for new .env vars
if $NEW_CONFIG; then
  echo ""
  echo "⚠ .env.example changed — check for new configuration variables:"
  # Show new vars that aren't in current .env
  if [ -f .env ] && [ -f .env.example ]; then
    NEW_VARS=""
    while IFS= read -r line; do
      # Skip comments and empty lines
      [[ "$line" =~ ^[[:space:]]*# ]] && continue
      [[ -z "$line" ]] && continue
      KEY=$(echo "$line" | cut -d= -f1)
      if ! grep -q "^${KEY}=" .env 2>/dev/null; then
        NEW_VARS="${NEW_VARS}  ${line}\n"
      fi
    done < .env.example
    if [ -n "$NEW_VARS" ]; then
      echo -e "$NEW_VARS"
      echo "  Add these to your .env or run: mise run config"
    else
      echo "  No new variables needed"
    fi
  fi
fi

# 5. Inform about setup changes
if $NEED_SETUP; then
  echo ""
  echo "ℹ Setup scripts changed. If you experience issues, re-run: mise run setup"
fi

# 6. Restore running state if services were running before upgrade
if [ "${BAZEL_PROXY_WAS_RUNNING:-false}" = "true" ] || [ "$WAS_RUNNING" = "true" ]; then
  echo ""
  echo "Restoring services..."
  if [ "$CURRENT_MODE" = "native" ]; then
    bash scripts/s3proxy-start.sh 2>&1
    bash scripts/sso-monitor-start.sh 2>&1
  else
    $COMPOSE_CMD up -d 2>&1
  fi
fi

# Summary
echo ""
if [ -n "$ACTIONS_TAKEN" ]; then
  echo "✓ Upgraded: $ACTIONS_TAKEN"
else
  echo "✓ Updated (no rebuild/restart needed)"
fi
echo "  Now at: $(git log --oneline -1 HEAD)"
