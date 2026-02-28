# Bazel AWS Maven Proxy

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Transparent proxy layer between Bazel builds and private AWS S3 Maven repositories with AWS SSO support.

## The Problem

Engineers using Bazel with private Maven repositories in AWS S3 face challenges:

- Legacy tools only work with static AWS credentials
- AWS SSO tokens expire frequently
- Security best practices (temporary credentials, SSO) clash with developer productivity

This project provides a stable HTTP endpoint for Bazel while handling AWS S3 authentication behind the scenes.

## Architecture

Three-component automated system:

1. **S3 Proxy Service** (Container) — HTTP server that caches and serves Maven artifacts from S3
2. **SSO Monitor Service** (Container) — Checks credentials periodically, writes signal on expiry
3. **SSO Watcher** (Host) — Watches for signals, notifies user, triggers login

```
SSO Monitor checks credentials every 60s
       ↓ detects expiration
Writes signal → ~/.aws/sso-renewer/login-required.json
       ↓ watcher polls every 5s
SSO Watcher (launchd) detects signal
       ↓ notify: dialog | auto: browser | standalone: idle
aws sso login → browser → user completes MFA
       ↓
S3 Proxy + Monitor reload credentials (no restart)
```

See [docs/sso-watcher.md](docs/sso-watcher.md) for details.

## Quick Start

### Prerequisites

- Podman (preferred) or Docker
- AWS CLI v2
- Python 3.11+
- mise (`brew install mise`)

### 1. Configure AWS CLI with SSO

```bash
# Create SSO session
aws configure sso-session
# SSO session name: my-sso
# SSO start URL: https://mycompany.awsapps.com/start
# SSO region: us-west-2
# SSO registration scopes: sso:account:access

# Create profile
aws configure sso --profile bazel-cache
```

Or manually add to `~/.aws/config`:
```ini
[profile bazel-cache]
sso_session = my-sso
sso_account_id = 123456789012
sso_role_name = DeveloperRole
region = us-west-2

[sso-session my-sso]
sso_start_url = https://mycompany.awsapps.com/start
sso_region = us-west-2
sso_registration_scopes = sso:account:access
```

Do NOT use `~/.aws/credentials` — SSO tokens are managed by AWS CLI.

### 2. Set up environment

```bash
cp .env.example .env
# Edit .env:
#   AWS_PROFILE=bazel-cache
#   AWS_REGION=us-west-2
#   S3_BUCKET_NAME=your-maven-bucket
```

### 3. Start services

```bash
# Everything (containers + SSO watcher)
mise run start

# Or containers only
mise run containers:up
```

### 4. Configure Bazel

**.bazelrc**:
```
build --define=maven_repo=http://localhost:9000/
```

**WORKSPACE**:
```python
maven_install(
    name = "maven",
    artifacts = [
        "com.example:my-library:1.0.0",
    ],
    repositories = [
        "http://localhost:9000/",  # S3 proxy
        "https://repo1.maven.org/maven2",  # Fallback
    ],
)
```

## Commands

### System

```bash
mise run start              # Start everything (containers + watcher)
mise run stop               # Stop everything
```

### Container Services

```bash
mise run containers:up      # Start containers
mise run containers:down    # Stop containers
mise run containers:restart # Restart containers
mise run containers:logs    # View container logs
```

Supports Podman (preferred) and Docker. Auto-detected, or set `CONTAINER_ENGINE` in `.env`.

### SSO Watcher (macOS)

```bash
mise run sso-install          # Install watcher (launchd agent)
mise run sso-uninstall        # Uninstall
mise run sso-status           # Dashboard: running, mode, credentials
mise run sso-login            # Trigger login (dialog or direct per mode)
mise run sso-logout           # Invalidate credentials, trigger renewal
mise run sso-logs             # Show recent logs (last 50 lines)
mise run sso-logs:follow      # Stream logs (Ctrl+C to stop)
mise run sso-mode             # Show current mode
mise run sso-mode:notify      # Switch to notify (dialog)
mise run sso-mode:auto        # Switch to auto (browser immediately)
mise run sso-mode:standalone  # Switch to standalone (manual only)
mise run sso-restart          # Restart watcher
mise run sso-clean            # Clear state/signals
```

### Watcher Modes

| Mode | Behavior | Best for |
|------|----------|----------|
| `notify` (default) | Shows dialog: Refresh / Snooze / Don't Remind | Daily use |
| `auto` | Opens browser immediately on expiry | Unattended |
| `standalone` | Watcher idle, manual `sso-login` only | Full control |

Switch at runtime: `mise run sso-mode:notify|auto|standalone` — takes effect within seconds.

### Dialog Actions Quick Reference

| Action | Signal | Next attempt |
|--------|--------|-------------|
| **Refresh** → success | cleared | on next credential expiry |
| **Refresh** → timeout/fail | kept | ~30s (auto-retry) |
| **Snooze** | kept | user-chosen (15m/30m/1h/4h) |
| **Dismiss** / ignore | kept | ~10 min (cooldown) |
| **Don't Remind** | cleared | only on new expiry signal |

## Configuration

Environment variables in `.env`:

| Variable | Description | Default |
|----------|-------------|---------|
| `AWS_PROFILE` | AWS CLI profile | `default` |
| `AWS_REGION` | AWS region for S3 | `us-west-2` |
| `S3_BUCKET_NAME` | Maven S3 bucket name | Required |
| `PROXY_PORT` | Local proxy port | `9000` |
| `REFRESH_INTERVAL` | Credential refresh check (ms) | `60000` |
| `LOG_LEVEL` | Logging level | `info` |
| `CHECK_INTERVAL` | Monitor check interval (seconds) | `60` |
| `SSO_COOLDOWN_SECONDS` | Watcher cooldown between logins | `600` |
| `SSO_POLL_SECONDS` | Watcher signal poll interval | `5` |
| `SSO_LOGIN_MODE` | `notify`, `auto`, or `standalone` | `notify` |
| `CONTAINER_ENGINE` | `podman` or `docker` | auto-detect |

## Troubleshooting

### Port conflicts

```bash
lsof -i :9000
# Change in .env: PROXY_PORT=8888
```

### Expired credentials

```bash
mise run sso-login            # Trigger login
mise run sso-logout           # Force re-auth
aws sso login --profile bazel-cache  # Manual fallback
```

### Watcher issues

```bash
mise run sso-status           # Check running/mode/credentials
mise run sso-logs             # View recent logs
mise run sso-clean            # Clear stuck state
```

### S3 access

```bash
aws s3 ls s3://your-bucket/ --profile bazel-cache
```

## Documentation

| Document | Description |
|----------|-------------|
| [docs/sso-watcher.md](docs/sso-watcher.md) | SSO watcher architecture and internals |
| [docs/state-machine.md](docs/state-machine.md) | State diagrams (Mermaid) for modes, signals, cooldown |
| [docs/testing.md](docs/testing.md) | Test structure and coverage (88 tests) |
| [CONTRIBUTING.md](CONTRIBUTING.md) | How to contribute |

## Testing

```bash
pytest              # Run all 88 tests
./run_tests.sh      # Helper script
```

See [docs/testing.md](docs/testing.md) for details.

## License

MIT License

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).
