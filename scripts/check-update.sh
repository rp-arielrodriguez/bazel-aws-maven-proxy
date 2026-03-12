#!/usr/bin/env bash
# Check if the local repo is behind origin/main.
# Categorizes changed files to show what would need rebuilding.
#
# Exit codes:
#   0 — up to date or update available (both are success)
#   1 — error (network, git)
#
# Output (human-readable):
#   Shows commits behind, changed categories, suggested command.
#
# With --json flag: outputs machine-readable JSON for watcher consumption.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

JSON_MODE=false
QUIET_MODE=false
for arg in "$@"; do
  case "$arg" in
    --json)  JSON_MODE=true ;;
    --quiet) QUIET_MODE=true ;;
  esac
done

# Fetch with timeout to avoid hanging on bad network
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
  if $JSON_MODE; then
    echo '{"status":"error","message":"git fetch failed"}'
  elif ! $QUIET_MODE; then
    echo "✗ Could not reach remote (network issue?)"
  fi
  exit 1
fi

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
  if $JSON_MODE; then
    echo '{"status":"up_to_date","commits_behind":0}'
  elif ! $QUIET_MODE; then
    echo "✓ Up to date ($(git log --oneline -1 HEAD | cut -c1-7))"
  fi
  exit 0
fi

BEHIND=$(git rev-list --count HEAD..origin/main)
CHANGED_FILES=$(git diff --name-only HEAD..origin/main)

# Categorize changes
CONTAINERS=false
NATIVE=false
WEBVIEW=false
WATCHER=false
SETUP=false
CONFIG=false
DOCS=false

while IFS= read -r file; do
  case "$file" in
    s3proxy/*|sso-monitor/*|docker-compose.yaml) CONTAINERS=true ;;
    scripts/s3proxy-*.sh|scripts/sso-monitor-*.sh) NATIVE=true ;;
    sso-watcher/webview/*) WEBVIEW=true ;;
    sso-watcher/watcher.py|launchd/*) WATCHER=true ;;
    scripts/setup.py|scripts/setup.sh) SETUP=true ;;
    .env.example) CONFIG=true ;;
    *.md) DOCS=true ;;
  esac
done <<< "$CHANGED_FILES"

# Build actions list
ACTIONS=""
if $NATIVE; then ACTIONS="${ACTIONS:+$ACTIONS, }restart native services"; fi
if $CONTAINERS; then ACTIONS="${ACTIONS:+$ACTIONS, }rebuild containers"; fi
if $WEBVIEW; then ACTIONS="${ACTIONS:+$ACTIONS, }rebuild webview"; fi
if $WATCHER; then ACTIONS="${ACTIONS:+$ACTIONS, }restart watcher"; fi
if $SETUP; then ACTIONS="${ACTIONS:+$ACTIONS, }re-run setup (optional)"; fi
if $CONFIG; then ACTIONS="${ACTIONS:+$ACTIONS, }check new .env vars"; fi

if $JSON_MODE; then
  # JSON for watcher consumption
  cat <<EOF
{
  "status": "update_available",
  "commits_behind": $BEHIND,
  "local_commit": "$LOCAL",
  "remote_commit": "$REMOTE",
  "containers": $CONTAINERS,
  "webview": $WEBVIEW,
  "watcher": $WATCHER,
  "setup": $SETUP,
  "config": $CONFIG,
  "actions": "$(echo "$ACTIONS" | sed 's/"/\\"/g')"
}
EOF
  exit 0
fi

echo "Update available: $BEHIND commit(s) behind origin/main"
echo ""

# Show commit summaries
git log --oneline HEAD..origin/main | while IFS= read -r line; do
  echo "  $line"
done

echo ""

if [ -n "$ACTIONS" ]; then
  echo "Requires: $ACTIONS"
fi

echo ""
echo "Run: mise run upgrade"
exit 0
