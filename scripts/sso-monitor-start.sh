#!/usr/bin/env bash
# Native process wrapper for sso-monitor (runs directly without containers)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIDFILE="$HOME/.bazel-aws-maven-proxy/sso-monitor.pid"
LOG_DIR="$HOME/.bazel-aws-maven-proxy/logs"
mkdir -p "$HOME/.bazel-aws-maven-proxy" "$LOG_DIR"

# Resolve mise-managed Python (has boto3 installed)
PY3=""
if command -v mise &>/dev/null; then
    PY3="$(mise which python 2>/dev/null || echo "")"
fi
if [ -z "$PY3" ]; then
    PY3="$(command -v python3 2>/dev/null || echo "")"
fi
if [ -z "$PY3" ]; then
    echo "✗ python3 not found"
    exit 1
fi

# Load environment from .env
export AWS_PROFILE="${AWS_PROFILE:-default}"
export AWS_REGION="${AWS_REGION:-us-west-2}"
export CHECK_INTERVAL="${CHECK_INTERVAL:-60}"
export CACHE_DIR="${CACHE_DIR:-$HOME/.bazel-aws-maven-proxy/cache}"
mkdir -p "$CACHE_DIR"

if [ -f "$REPO_ROOT/.env" ]; then
    set -a
    source "$REPO_ROOT/.env"
    set +a
fi

# Check if already running
if [ -f "$PIDFILE" ]; then
  OLD_PID=$(cat "$PIDFILE" 2>/dev/null || echo "")
  if kill -0 "$OLD_PID" 2>/dev/null; then
    echo "sso-monitor already running (PID: $OLD_PID)"
    exit 0
  fi
  rm -f "$PIDFILE"
fi

cd "$REPO_ROOT/sso-monitor"
$PY3 monitor.py > "$LOG_DIR/sso-monitor.log" 2>&1 &
PID=$!
echo $PID > "$PIDFILE"

# Wait for startup
sleep 1
if kill -0 $PID 2>/dev/null; then
  echo "✓ sso-monitor started (PID: $PID)"
  echo "  Logs: $LOG_DIR/sso-monitor.log"
else
  echo "✗ sso-monitor failed to start"
  rm -f "$PIDFILE"
  exit 1
fi
