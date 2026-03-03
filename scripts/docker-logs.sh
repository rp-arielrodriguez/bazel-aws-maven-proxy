#!/usr/bin/env bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/container-engine.sh"

# Trap SIGINT (Ctrl+C) and SIGTERM to exit gracefully
trap 'exit 0' INT TERM

# Follow compose logs (COMPOSE_CMD may be multi-word e.g. "podman compose")
$COMPOSE_CMD logs -f &
wait $!
