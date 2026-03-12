#!/usr/bin/env bash
# Unified logs command with bash-style flags
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/container-engine.sh" 2>/dev/null || true

usage() {
  cat <<EOF
Usage: bazel-proxy logs [OPTIONS]

Show logs for bazel-aws-maven-proxy services.

By default, shows SSO watcher logs. Use --all to see all services.

Filter Options:
  --all           Show all logs (s3proxy, monitor, sso)
  --s3proxy       Show s3proxy logs only
  --monitor       Show sso-monitor logs only
  --sso           Show SSO watcher logs only

Output Control:
  --tail N        Show last N lines (default: 50)
  --follow        Stream logs in real-time

Mode Override:
  --native        Force native mode
  --container     Force container mode

Other:
  --help          Show this help message

Examples:
  bazel-proxy logs                    # SSO watcher logs (default)
  bazel-proxy logs --all              # All logs
  bazel-proxy logs --s3proxy          # s3proxy logs only
  bazel-proxy logs --follow           # Stream SSO logs
  bazel-proxy logs --follow --all     # Stream all logs
  bazel-proxy logs --tail 100         # Last 100 lines
  bazel-proxy logs --s3proxy --follow # Stream s3proxy logs
EOF
}

# Default values
LINES=50
SHOW_ALL=false
FOLLOW_MODE=false
FORCE_MODE=""
SHOW_S3PROXY=false
SHOW_MONITOR=false
SHOW_SSO=false

# Parse arguments
while [[ $# -gt 0 ]]; do
  case $1 in
    --all) SHOW_ALL=true ;;
    --s3proxy) SHOW_S3PROXY=true; SHOW_ALL=false ;;
    --monitor) SHOW_MONITOR=true; SHOW_ALL=false ;;
    --sso) SHOW_SSO=true; SHOW_ALL=false ;;
    --tail) LINES="$2"; shift ;;
    --follow) FOLLOW_MODE=true ;;
    --native) FORCE_MODE="native" ;;
    --container) FORCE_MODE="container" ;;
    --help) usage; exit 0 ;;
    -[0-9]*) LINES="${1#-}" ;;
    *) echo "Unknown option: $1"; usage; exit 1 ;;
  esac
  shift
done

# Detect current mode (or use forced mode)
if [ -n "$FORCE_MODE" ]; then
  MODE="$FORCE_MODE"
elif [ -f "$HOME/.bazel-aws-maven-proxy/s3proxy.pid" ]; then
  PID=$(cat "$HOME/.bazel-aws-maven-proxy/s3proxy.pid" 2>/dev/null)
  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    MODE="native"
  else
    MODE="stopped"
  fi
elif $COMPOSE_CMD ps 2>/dev/null | grep -q "bazel-s3-proxy.*Up"; then
  MODE="container"
else
  MODE="stopped"
fi

# Log file locations
SSO_LOG="$HOME/Library/Logs/sso-watcher.log"
SSO_ERROR="$HOME/Library/Logs/sso-watcher.error.log"
NATIVE_LOG_DIR="$HOME/.bazel-aws-maven-proxy/logs"

# Handle specific log requests (--s3proxy, --monitor, --sso)
if $SHOW_S3PROXY; then
  if $FOLLOW_MODE; then
    [ -f "$NATIVE_LOG_DIR/s3proxy.log" ] && tail -f "$NATIVE_LOG_DIR/s3proxy.log" || echo "No s3proxy logs found"
  else
    [ -f "$NATIVE_LOG_DIR/s3proxy.log" ] && tail -n "$LINES" "$NATIVE_LOG_DIR/s3proxy.log" || echo "No s3proxy logs found"
  fi
  exit 0
fi

if $SHOW_MONITOR; then
  if $FOLLOW_MODE; then
    [ -f "$NATIVE_LOG_DIR/sso-monitor.log" ] && tail -f "$NATIVE_LOG_DIR/sso-monitor.log" || echo "No sso-monitor logs found"
  else
    [ -f "$NATIVE_LOG_DIR/sso-monitor.log" ] && tail -n "$LINES" "$NATIVE_LOG_DIR/sso-monitor.log" || echo "No sso-monitor logs found"
  fi
  exit 0
fi

if $SHOW_SSO; then
  if $FOLLOW_MODE; then
    tail -f "$SSO_LOG" "$SSO_ERROR" 2>/dev/null
  else
    [ -f "$SSO_ERROR" ] && [ -s "$SSO_ERROR" ] && echo "=== errors ===" && tail -n "$LINES" "$SSO_ERROR"
    [ -f "$SSO_LOG" ] && echo "=== log ===" && tail -n "$LINES" "$SSO_LOG"
  fi
  exit 0
fi

# Handle --all or default behavior
if $FOLLOW_MODE; then
  # Stream logs (--follow follows SSO by default, --follow --all for all)
  if $SHOW_ALL; then
    echo "Streaming all logs (Mode: $MODE) - Ctrl+C to stop..."
    trap 'exit 0' INT TERM
    
    if [ "$MODE" = "native" ]; then
      tail -f "$NATIVE_LOG_DIR/s3proxy.log" "$NATIVE_LOG_DIR/sso-monitor.log" "$SSO_LOG" "$SSO_ERROR" 2>/dev/null &
    elif [ "$MODE" = "container" ]; then
      $COMPOSE_CMD logs -f &
      tail -f "$SSO_LOG" "$SSO_ERROR" 2>/dev/null &
    else
      # Stopped - just show SSO logs
      tail -f "$SSO_LOG" "$SSO_ERROR" 2>/dev/null &
    fi
    wait
  else
    # --follow without --all: follow SSO logs only
    echo "Streaming SSO watcher logs - Ctrl+C to stop..."
    trap 'exit 0' INT TERM
    tail -f "$SSO_LOG" "$SSO_ERROR" 2>/dev/null &
    wait
  fi
else
  # Show logs (SSO by default, --all for everything)
  if $SHOW_ALL; then
    echo "=== All Logs (Mode: $MODE) ==="
    if [ "$MODE" = "native" ]; then
      [ -f "$NATIVE_LOG_DIR/s3proxy.log" ] && echo "--- s3proxy ---" && tail -n "$LINES" "$NATIVE_LOG_DIR/s3proxy.log"
      [ -f "$NATIVE_LOG_DIR/sso-monitor.log" ] && echo "--- sso-monitor ---" && tail -n "$LINES" "$NATIVE_LOG_DIR/sso-monitor.log"
    elif [ "$MODE" = "container" ]; then
      $COMPOSE_CMD logs --tail="$LINES"
    else
      echo "No services running"
    fi
    echo ""
  fi
  
  # Always show SSO logs (default behavior)
  echo "=== SSO Watcher ==="
  [ -f "$SSO_ERROR" ] && [ -s "$SSO_ERROR" ] && echo "--- errors ---" && tail -n "$LINES" "$SSO_ERROR"
  [ -f "$SSO_LOG" ] && echo "--- log ---" && tail -n "$LINES" "$SSO_LOG"
fi
