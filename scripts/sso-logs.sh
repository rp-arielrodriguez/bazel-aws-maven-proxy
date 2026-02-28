#!/usr/bin/env bash

LOG_FILE="$HOME/Library/Logs/sso-watcher.log"
ERROR_LOG="$HOME/Library/Logs/sso-watcher.error.log"

LINES="${1:-50}"

echo "SSO watcher logs (last $LINES lines)"
echo "Log file: $LOG_FILE"
echo "Error log: $ERROR_LOG"
echo ""

if [ -f "$ERROR_LOG" ] && [ -s "$ERROR_LOG" ]; then
  echo "=== Errors ==="
  tail -n "$LINES" "$ERROR_LOG" 2>/dev/null
  echo ""
fi

if [ -f "$LOG_FILE" ]; then
  echo "=== Log ==="
  tail -n "$LINES" "$LOG_FILE" 2>/dev/null
else
  echo "No log file found yet"
fi
