#!/usr/bin/env bash
# Stop native s3proxy process
set -euo pipefail

PIDFILE="$HOME/.bazel-aws-maven-proxy/s3proxy.pid"

if [ ! -f "$PIDFILE" ]; then
  echo "s3proxy not running"
  exit 0
fi

PID=$(cat "$PIDFILE" 2>/dev/null || echo "")
if [ -z "$PID" ]; then
  echo "s3proxy not running"
  rm -f "$PIDFILE"
  exit 0
fi

if kill -0 "$PID" 2>/dev/null; then
  echo "Stopping s3proxy (PID: $PID)..."
  kill "$PID" 2>/dev/null || true
  sleep 1
  if kill -0 "$PID" 2>/dev/null; then
    kill -9 "$PID" 2>/dev/null || true
  fi
  echo "✓ s3proxy stopped"
else
  echo "s3proxy not running"
fi

rm -f "$PIDFILE"
