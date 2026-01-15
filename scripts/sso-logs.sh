#!/usr/bin/env bash

LOG_FILE="$HOME/Library/Logs/sso-watcher.log"
ERROR_LOG="$HOME/Library/Logs/sso-watcher.error.log"

echo "Tailing SSO watcher logs (Ctrl+C to exit)..."
echo "Log file: $LOG_FILE"
echo "Error log: $ERROR_LOG"
echo ""

# Trap SIGINT (Ctrl+C) and SIGTERM to exit gracefully
trap 'exit 0' INT TERM

# Suppress job control messages
tail -f "$LOG_FILE" "$ERROR_LOG" 2>/dev/null &
wait $!
