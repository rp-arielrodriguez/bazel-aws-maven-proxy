#!/usr/bin/env bash
set -eo pipefail

# Interactive first-time setup for bazel-aws-maven-proxy
# Thin wrapper — delegates to scripts/setup.py for testability.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

exec python3 "$SCRIPT_DIR/setup.py"
