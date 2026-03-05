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
       ↓ silent refresh (all modes try first)
       ↓ notify: dialog | auto: webview | silent: done | standalone: idle
aws sso login → webview (or browser fallback) → user completes MFA
       ↓
S3 Proxy + Monitor reload credentials (no restart)
```

See [docs/sso-watcher.md](docs/sso-watcher.md) for details.

## Quick Start

### Prerequisites

- [mise](https://mise.jdx.dev/) (`brew install mise`) — manages Python and project tasks
- [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) >= 2.9 (`brew install awscli`) — not managed by mise (no native ARM macOS build available via mise)
- Podman (preferred) or Docker
- Xcode Command Line Tools (`xcode-select --install`) — for building the login webview

> **Note:** Python 3.11 is managed by mise and installed automatically on `mise install`.

### 1. Run setup

```bash
mise run setup
```

This interactive wizard will:
- Verify prerequisites (aws, podman/docker, swiftc)
- Prompt for AWS profile, region, S3 bucket, and create `.env`
- Ask whether you're behind a corporate proxy that intercepts HTTPS (sets `SKIP_TLS_VERIFY`)
- Run `aws configure sso` if your profile isn't configured yet (creates a new one)
- Install Python via mise
- Build the login webview and install the SSO watcher (launchd)
- Optionally start containers

> **Profile note:** `AWS_PROFILE` in `.env` must match a profile name in `~/.aws/config` with SSO configured. If you already have a profile with access to the S3 bucket, enter that name during setup. If not, the setup wizard will run `aws configure sso` to create one — the profile name you choose there must match what goes in `.env`. All components (proxy, monitor, watcher) read this single value.

<details>
<summary>Manual setup (alternative)</summary>

**Configure AWS SSO:**
```bash
aws configure sso
# SSO session name (Recommended): my-sso
# SSO start URL: https://mycompany.awsapps.com/start
# SSO region: us-west-2
# SSO registration scopes [sso:account:access]:    ← press Enter (critical for token refresh)
# Select account → select role
# CLI profile name: bazel-cache
```

**Important:** The `sso_registration_scopes = sso:account:access` scope is required for silent token refresh.

See [`examples/aws_config_example`](examples/aws_config_example) for a fully annotated config file.

**Create .env:**
```bash
cp .env.example .env
# Set AWS_PROFILE to the profile name from above (e.g. bazel-cache)
# Set AWS_REGION and S3_BUCKET_NAME for your S3 bucket
```

**Install and start:**
```bash
mise install                # Python
mise run sso-install        # Webview + launchd agent
mise run start              # Containers
```
</details>

### 2. Configure Bazel

**.bazelrc**:
```
build --define=maven_repo=http://localhost:8888/
```

**WORKSPACE**:
```python
maven_install(
    name = "maven",
    artifacts = [
        "com.example:my-library:1.0.0",
    ],
    repositories = [
        "http://localhost:8888/",  # S3 proxy
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

### Configuration

View and modify `.env` settings after installation:

```bash
mise run config                          # Show current configuration
mise run config:set KEY=VALUE            # Update any setting (e.g. PROXY_PORT=9000)
mise run tls-skip:enable                 # Enable TLS skip (corporate proxy)
mise run tls-skip:disable                # Disable TLS skip
```

`config:set` will remind you which service needs restarting after each change.

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
mise run sso-mode:auto        # Switch to auto (webview immediately)
mise run sso-mode:silent      # Switch to silent (token refresh only)
mise run sso-mode:standalone  # Switch to standalone (manual only)
mise run sso-restart          # Restart watcher
mise run sso-clean            # Clear state/signals/cooldown
mise run sso-clean:cookies    # Clear webview cookies (forces full re-auth)
```

### Watcher Modes

| Mode | Behavior | Best for |
|------|----------|----------|
| `notify` (default) | Silent refresh, then dialog: Refresh / Snooze / Don't Remind | Daily use |
| `auto` | Silent refresh, then opens webview immediately | Unattended |
| `silent` | Silent token refresh only, never opens webview/browser | Headless/CI |
| `standalone` | Watcher idle, manual `sso-login` only | Full control |

All modes except `standalone` attempt silent token refresh first using the cached refresh token. Only when that fails do they fall back to their mode-specific behavior.

**Proactive refresh:** The watcher also checks token expiry every 60s (independently of the monitor signal). When the token is within 30 minutes of expiry, it silently refreshes — keeping credentials alive without any user interaction, even across sleep/wake cycles.

Switch at runtime: `mise run sso-mode:notify|auto|silent|standalone` — takes effect within seconds.

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
| `AWS_PROFILE` | AWS CLI profile name (must exist in `~/.aws/config` with SSO) | `default` |
| `AWS_REGION` | AWS region for S3 | `us-west-2` |
| `S3_BUCKET_NAME` | Maven S3 bucket name | Required |
| `PROXY_PORT` | Local proxy port | `8888` |
| `REFRESH_INTERVAL` | Credential refresh check (ms) | `60000` |
| `LOG_LEVEL` | Logging level | `info` |
| `CHECK_INTERVAL` | Monitor check interval (seconds) | `60` |
| `SSO_COOLDOWN_SECONDS` | Watcher cooldown between logins | `600` |
| `SSO_POLL_SECONDS` | Watcher signal poll interval | `5` |
| `SSO_LOGIN_MODE` | `notify`, `auto`, `silent`, or `standalone` | `notify` |
| `SSO_PROACTIVE_REFRESH_MINUTES` | Refresh token N min before expiry (0=disable) | `30` |
| `CONTAINER_ENGINE` | `podman` or `docker` | auto-detect |
| `SKIP_TLS_VERIFY` | Skip TLS cert verification for container pulls (podman only) — set to `true` when behind a corporate proxy that replaces HTTPS certificates | `false` |

## Troubleshooting

### Port conflicts

```bash
lsof -i :8888
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
mise run sso-clean            # Clear stuck state/signals/cooldown
mise run sso-clean:cookies    # Clear webview cookies if login keeps failing
```

### S3 access

```bash
aws s3 ls s3://your-bucket/ --profile bazel-cache
```

### Corporate proxy / TLS certificate errors

If you see an error like:
```
x509: certificate signed by unknown authority
tls: failed to verify certificate
```

Your network proxy is intercepting HTTPS and replacing certificates. Enable TLS skip for podman:

```bash
mise run tls-skip:enable
mise run containers:restart
```

Or set it during initial setup when the wizard asks about corporate proxies. To configure manually:

```bash
mise run config:set SKIP_TLS_VERIFY=true
mise run containers:restart
```

> **Docker users:** `SKIP_TLS_VERIFY` only applies to Podman. For Docker, add the corporate CA certificate via Docker Desktop → Settings → Docker Engine, or mount it into `/etc/docker/certs.d/registry-1.docker.io/`.

## Documentation

| Document | Description |
|----------|-------------|
| [docs/sso-watcher.md](docs/sso-watcher.md) | SSO watcher architecture and internals |
| [docs/state-machine.md](docs/state-machine.md) | State diagrams (Mermaid) for modes, signals, cooldown |
| [docs/testing.md](docs/testing.md) | Test structure and coverage (385 tests) |
| [CONTRIBUTING.md](CONTRIBUTING.md) | How to contribute |

## Testing

```bash
pytest              # Run all 385 tests
./run_tests.sh      # Helper script
```

See [docs/testing.md](docs/testing.md) for details.

## License

MIT License

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).
