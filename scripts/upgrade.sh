#!/usr/bin/env bash
# Smart upgrade: git pull + selective rebuild/restart based on what changed.
#
# Detects which components were affected by the update and only
# rebuilds/restarts what's needed:
#   - s3proxy/sso-monitor/compose changes → rebuild containers
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

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
  echo "✓ Already up to date"
  exit 0
fi

BEHIND=$(git rev-list --count HEAD..origin/main)
echo "Pulling $BEHIND commit(s)..."

# Snapshot changed files BEFORE pull
CHANGED_FILES=$(git diff --name-only HEAD..origin/main)

# Pull
if ! git pull --ff-only origin main; then
  echo "✗ Pull failed (local changes?). Resolve manually, then re-run."
  exit 1
fi

# Categorize changes
NEED_CONTAINERS=false
NEED_WEBVIEW=false
NEED_WATCHER=false
NEED_SETUP=false
NEW_CONFIG=false

while IFS= read -r file; do
  case "$file" in
    s3proxy/*|sso-monitor/*|docker-compose.yaml) NEED_CONTAINERS=true ;;
    sso-watcher/webview/*) NEED_WEBVIEW=true ;;
    sso-watcher/watcher.py|launchd/*) NEED_WATCHER=true ;;
    scripts/setup.py|scripts/setup.sh) NEED_SETUP=true ;;
    .env.example) NEW_CONFIG=true ;;
  esac
done <<< "$CHANGED_FILES"

ACTIONS_TAKEN=""

# 1. Rebuild containers if needed
if $NEED_CONTAINERS; then
  echo ""
  echo "Container sources changed — rebuilding..."
  if $COMPOSE_CMD up -d --build 2>&1; then
    ACTIONS_TAKEN="${ACTIONS_TAKEN:+$ACTIONS_TAKEN, }containers rebuilt"
  else
    echo "⚠ Container rebuild failed — try: mise run containers:rebuild"
  fi
fi

# 2. Reinstall watcher if webview or watcher changed
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
elif $NEED_CONTAINERS; then
  : # containers already handled above, watcher doesn't need restart
fi

# 3. Check for new .env vars
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

# 4. Inform about setup changes
if $NEED_SETUP; then
  echo ""
  echo "ℹ Setup scripts changed. If you experience issues, re-run: mise run setup"
fi

# Summary
echo ""
if [ -n "$ACTIONS_TAKEN" ]; then
  echo "✓ Upgraded: $ACTIONS_TAKEN"
else
  echo "✓ Updated (no rebuild/restart needed)"
fi
echo "  Now at: $(git log --oneline -1 HEAD)"
