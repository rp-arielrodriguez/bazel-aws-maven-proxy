#!/usr/bin/env bash
# Show sso-monitor logs only
set -e
NATIVE_LOG="$HOME/.bazel-aws-maven-proxy/logs/sso-monitor.log"
LINES="${1:-50}"
[ -f "$NATIVE_LOG" ] && tail -n "$LINES" "$NATIVE_LOG" || echo "No sso-monitor logs found"
