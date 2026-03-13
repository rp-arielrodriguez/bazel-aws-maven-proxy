#!/usr/bin/env bash
# Native process wrapper for s3proxy (runs directly without containers)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIDFILE="$HOME/.bazel-aws-maven-proxy/s3proxy.pid"
LOG_DIR="$HOME/.bazel-aws-maven-proxy/logs"
mkdir -p "$HOME/.bazel-aws-maven-proxy" "$LOG_DIR"

# Load environment
export AWS_PROFILE="${AWS_PROFILE:-default}"
export AWS_REGION="${AWS_REGION:-us-west-2}"
export S3_BUCKET_NAME="${S3_BUCKET_NAME:-}"
export PROXY_PORT="${PROXY_PORT:-8888}"
export LOG_LEVEL="${LOG_LEVEL:-info}"
export REFRESH_INTERVAL="${REFRESH_INTERVAL:-60000}"
export CACHE_DIR="${CACHE_DIR:-$HOME/.bazel-aws-maven-proxy/cache}"
mkdir -p "$CACHE_DIR"

# Check if already running
if [ -f "$PIDFILE" ]; then
  OLD_PID=$(cat "$PIDFILE" 2>/dev/null || echo "")
  if kill -0 "$OLD_PID" 2>/dev/null; then
    echo "s3proxy already running (PID: $OLD_PID)"
    exit 0
  else
    rm -f "$PIDFILE"
  fi
fi

# Check required env
if [ -z "$S3_BUCKET_NAME" ]; then
  echo "Error: S3_BUCKET_NAME not set in .env"
  exit 1
fi

# Start s3proxy
echo "Starting s3proxy on port $PROXY_PORT..."
cd "$REPO_ROOT/s3proxy"

# Use Gunicorn if available (production WSGI), fall back to Flask dev server
if command -v gunicorn &>/dev/null; then
  gunicorn \
    --bind "0.0.0.0:$PROXY_PORT" \
    --workers 2 \
    --threads 4 \
    --timeout 120 \
    --access-logfile - \
    --error-logfile - \
    --capture-output \
    app:app > "$LOG_DIR/s3proxy.log" 2>&1 &
else
  echo "⚠ Gunicorn not found, using Flask dev server (not recommended for production)"
  echo "  Install with: pip install gunicorn"
  python3 app.py > "$LOG_DIR/s3proxy.log" 2>&1 &
fi

PID=$!
echo $PID > "$PIDFILE"

# Wait for startup
sleep 2
if kill -0 $PID 2>/dev/null; then
  echo "✓ s3proxy started (PID: $PID, Port: $PROXY_PORT)"
  echo "  Logs: $LOG_DIR/s3proxy.log"
else
  echo "✗ s3proxy failed to start"
  rm -f "$PIDFILE"
  exit 1
fi
