#!/usr/bin/env bash
# Native process wrapper for sso-monitor (runs directly without containers)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIDFILE="$HOME/.bazel-aws-maven-proxy/sso-monitor.pid"
LOG_DIR="$HOME/.bazel-aws-maven-proxy/logs"
mkdir -p "$HOME/.bazel-aws-maven-proxy" "$LOG_DIR"

# Load environment
export AWS_PROFILE="${AWS_PROFILE:-default}"
export CHECK_INTERVAL="${CHECK_INTERVAL:-60}"
export SIGNAL_FILE="${SIGNAL_FILE:-$HOME/.aws/sso-renewer/login-required.json}"

# Create signal directory
mkdir -p "$(dirname "$SIGNAL_FILE")"

# Check if already running
if [ -f "$PIDFILE" ]; then
  OLD_PID=$(cat "$PIDFILE" 2>/dev/null || echo "")
  if kill -0 "$OLD_PID" 2>/dev/null; then
    echo "sso-monitor already running (PID: $OLD_PID)"
    exit 0
  else
    rm -f "$PIDFILE"
  fi
fi

# Start sso-monitor
echo "Starting sso-monitor..."
cd "$REPO_ROOT/sso-monitor"
python3 monitor.py > "$LOG_DIR/sso-monitor.log" 2>&1 &
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
