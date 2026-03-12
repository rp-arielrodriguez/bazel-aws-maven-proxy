#!/usr/bin/env bash
# Show s3proxy logs only
set -e
NATIVE_LOG="$HOME/.bazel-aws-maven-proxy/logs/s3proxy.log"
LINES="${1:-50}"
[ -f "$NATIVE_LOG" ] && tail -n "$LINES" "$NATIVE_LOG" || echo "No s3proxy logs found"
