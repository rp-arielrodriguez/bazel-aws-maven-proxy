#!/usr/bin/env bash
# Detect container engine: podman (preferred) or docker.
# Source this file to get COMPOSE_CMD variable.
#
# Override with: CONTAINER_ENGINE=docker (or podman)

set -euo pipefail

_detect_engine() {
  if [[ -n "${CONTAINER_ENGINE:-}" ]]; then
    case "$CONTAINER_ENGINE" in
      podman) echo "podman" ;;
      docker) echo "docker" ;;
      *)
        echo "Unknown CONTAINER_ENGINE=$CONTAINER_ENGINE (expected: podman, docker)" >&2
        exit 1
        ;;
    esac
    return
  fi

  if command -v podman &>/dev/null; then
    echo "podman"
  elif command -v docker &>/dev/null; then
    echo "docker"
  else
    echo "No container engine found. Install podman or docker." >&2
    exit 1
  fi
}

ENGINE=$(_detect_engine)

# Determine compose command:
#   podman  → podman compose
#   docker  → docker compose (plugin) or docker-compose (standalone)
_detect_compose() {
  if [[ "$ENGINE" == "podman" ]]; then
    echo "podman compose"
    return
  fi

  # Docker: prefer plugin, fall back to standalone
  if docker compose version &>/dev/null; then
    echo "docker compose"
  elif command -v docker-compose &>/dev/null; then
    echo "docker-compose"
  else
    echo "No compose command found. Install 'docker compose' plugin or 'docker-compose'." >&2
    exit 1
  fi
}

COMPOSE_CMD=$(_detect_compose)

# When behind a corporate HTTPS proxy that replaces certificates, skip TLS verification.
# Set SKIP_TLS_VERIFY=true in .env to enable. Podman only — Docker requires daemon-level config.
#
# Uses CONTAINERS_REGISTRIES_CONF (not PODMAN_ARGS) because --tls-verify is a per-subcommand
# flag in podman, not a global one. CONTAINERS_REGISTRIES_CONF applies at the library level
# and covers all registry operations: pulls, builds, and compose image resolution.
if [[ "${SKIP_TLS_VERIFY:-false}" == "true" ]]; then
  if [[ "$ENGINE" == "podman" ]]; then
    _SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    export CONTAINERS_REGISTRIES_CONF="$_SCRIPT_DIR/registries-skip-tls.conf"
    unset _SCRIPT_DIR
  fi
fi

export ENGINE COMPOSE_CMD
