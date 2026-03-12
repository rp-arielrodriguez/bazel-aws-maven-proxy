#!/usr/bin/env bash
# Unified logs command - mode-aware
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/container-engine.sh" 2>/dev/null || true

LINES="50"
SHOW_ALL=false
FOLLOW_MODE=false
FORCE_MODE=""

for arg in "$@"; do
  case "$arg" in
    --all) SHOW_ALL=true ;;
    --follow) FOLLOW_MODE=true ;;
    --native) FORCE_MODE="native" ;;
    --container) FORCE_MODE="container" ;;
    -[0-9]*) LINES="${arg#-}" ;;
  esac
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

if $FOLLOW_MODE; then
  # Stream all logs (follow mode shows everything by default)
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
  
  # Always show SSO logs
  echo "=== SSO Watcher ==="
  [ -f "$SSO_ERROR" ] && [ -s "$SSO_ERROR" ] && echo "--- errors ---" && tail -n "$LINES" "$SSO_ERROR"
  [ -f "$SSO_LOG" ] && echo "--- log ---" && tail -n "$LINES" "$SSO_LOG"
fi
