#!/usr/bin/env bash
# Stop native sso-monitor process
set -euo pipefail

PIDFILE="$HOME/.bazel-aws-maven-proxy/sso-monitor.pid"

if [ ! -f "$PIDFILE" ]; then
  echo "sso-monitor not running"
  exit 0
fi

PID=$(cat "$PIDFILE" 2>/dev/null || echo "")
if [ -z "$PID" ]; then
  echo "sso-monitor not running"
  rm -f "$PIDFILE"
  exit 0
fi

if kill -0 "$PID" 2>/dev/null; then
  echo "Stopping sso-monitor (PID: $PID)..."
  kill "$PID" 2>/dev/null || true
  sleep 1
  if kill -0 "$PID" 2>/dev/null; then
    kill -9 "$PID" 2>/dev/null || true
  fi
  echo "✓ sso-monitor stopped"
else
  echo "sso-monitor not running"
fi

rm -f "$PIDFILE"
