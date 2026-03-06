# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This project provides a transparent proxy layer between Bazel builds and S3-hosted Maven repositories with automated AWS SSO authentication support via container-based monitoring and host-side watcher integration. Supports Podman (preferred) and Docker.

## Key Commands

### Starting and Managing Services

```bash
# Start everything (containers + SSO watcher)
mise run start

# Stop everything
mise run stop

# View container logs
mise run containers:logs

# View SSO watcher logs
mise run sso-logs

# Or use compose directly (podman preferred, docker also supported):
podman compose up -d      # Start services
podman compose logs -f    # View logs
podman compose down       # Stop services
```

### SSO Watcher Management (macOS)

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

### Configuration

```bash
mise run config                          # Show current .env values
mise run config:set KEY=VALUE            # Update any .env setting post-install
mise run tls-skip:enable                 # Enable TLS skip (corporate proxy)
mise run tls-skip:disable                # Disable TLS skip
```

Environment variables in `.env` (copy from `.env.example`):
- `AWS_PROFILE`: AWS CLI profile (default: default)
- `AWS_REGION`: AWS region for S3
- `S3_BUCKET_NAME`: Maven S3 bucket (required)
- `PROXY_PORT`: Local port (default: 8888)
- `REFRESH_INTERVAL`: Proxy credential refresh in ms (default: 60000)
- `LOG_LEVEL`: Logging level (debug, info, warn, error)
- `CHECK_INTERVAL`: Monitor check interval in seconds (default: 60)
- `SSO_COOLDOWN_SECONDS`: Watcher cooldown (default: 600)
- `SSO_POLL_SECONDS`: Watcher poll interval (default: 5)
- `SSO_LOGIN_MODE`: Login behavior - `notify` (default, dialog), `auto` (webview immediately), `silent` (token refresh only, no webview/browser), `standalone` (manual only). Toggleable at runtime via `mise run sso-mode:*`
- `SSO_PROACTIVE_REFRESH_MINUTES`: Refresh token N min before expiry, 0 to disable (default: 30)
- `CONTAINER_ENGINE`: `podman` or `docker` (auto-detect if unset, prefers podman)
- `SKIP_TLS_VERIFY`: `true` to skip TLS cert verification for container pulls (podman only). Use when behind a corporate proxy that replaces HTTPS certificates. Toggleable via `mise run tls-skip:enable/disable`

## Architecture

### S3 Proxy Service (`s3proxy/`)
- **Language**: Python (Flask)
- **Main file**: `s3proxy/app.py`
- **Purpose**: HTTP server for Bazel Maven artifacts
- **Key functionality**:
  - Serves artifacts from local cache
  - Fetches from S3 on cache miss
  - Provides directory listings
  - Refreshes AWS credentials periodically
  - Health check at `/healthz`
- **Port**: Configurable via `PROXY_PORT`
- **Cache**: `/data` (container volume)

### SSO Monitor Service (`sso-monitor/`)
- **Language**: Python (container)
- **Main file**: `sso-monitor/monitor.py`
- **Purpose**: Continuously monitor credential validity
- **Key functionality**:
  - Runs in container alongside s3proxy
  - Checks credentials every 60 seconds (configurable)
  - Writes signal file to shared volume when expired
  - Uses `boto3 sts.get_caller_identity()` for validation
- **Signal output**: `~/.aws/sso-renewer/login-required.json`

### SSO Watcher (Host) (`sso-watcher/watcher.py` + launchd)
- **Language**: Python (host-side, via launchd)
- **Purpose**: Detect signals and trigger login on host
- **Key functionality**:
  - Watches `~/.aws/sso-renewer/` for signal files
  - All modes except standalone try silent token refresh first (via cached refresh token)
  - In `notify` mode (default): silent refresh → all-in-one webview (notification page with Refresh/Snooze/Don't Remind → auth on Refresh)
  - In `auto` mode: silent refresh → opens webview immediately for auth
  - In `silent` mode: silent refresh only, never opens webview/browser
  - In `standalone` mode: watcher idles, manual `mise run sso-login` only
  - Clears signal on success
  - Atomic locking, cooldown protection (default 600s)
- **Installation**: `mise run sso-install`

### SSO Login Webview (`sso-watcher/webview/`)
- **Language**: Swift (compiled at install time via `swiftc`)
- **Main file**: `sso-watcher/webview/SSOLoginView.swift`
- **Purpose**: Sandboxed login window for SSO auth (replaces browser tabs)
- **Key functionality**:
  - WKWebView with persistent cookie storage (Google/IdP creds cached)
  - OAuth callback detection via `WKNavigationDelegate`, auto-close on auth
  - Portal redirect detection — auto-retries authorize URL if OIDC errors to `*.awsapps.com/start`
  - Auto-retry on WebKit "Frame load interrupted" (error 102, max 2 retries)
  - `--clear-cookies` mode to wipe persistent cookie storage
  - Launched via `open -a` for launchd compatibility
  - Falls back to system browser if `swiftc` unavailable
- **Bundle**: Built to `~/.aws/sso-renewer/bin/SSOLogin.app/` by `mise run sso-install`
- **Requires**: Xcode Command Line Tools (`xcode-select --install`)


## State Machines

See [docs/state-machine.md](docs/state-machine.md) for formal state diagrams (Mermaid) covering:
- Watcher mode transitions (notify/auto/silent/standalone)
- Signal lifecycle (created → snoozed → handled → cleared)
- Cooldown vs snooze mechanics
- Machine-readable transition table

## Data Flow

### Automated Flow (with SSO Watcher)

```
┌─────────────────────────────────────┐
│  SSO Monitor (Container)             │
│  - Checks credentials every 60s     │
│  - Detects expiration               │
└──────────────┬──────────────────────┘
               │ writes signal
               ▼
~/.aws/sso-renewer/login-required.json (shared volume)
               │
               ▼ watches (5s poll)
┌─────────────────────────────────────┐
│  SSO Watcher (launchd on host)      │
│  - Detects signal file              │
│  - Tries silent token refresh first │
│  - Success? Done — no browser needed│
└──────────────┬──────────────────────┘
               │ silent refresh failed
               ▼ fallback per mode:
┌─────────────────────────────────────┐
│  notify: webview (notification→auth)│
│  auto: webview immediately          │
│  silent: gives up                   │
│  standalone: idle (manual only)     │
└──────────────┬──────────────────────┘
               │ user completes auth
               ▼
         aws sso login --no-browser
               │ opens webview (or browser fallback)
               ▼
    User completes SSO auth + MFA
               │
               ▼ writes new token
      ~/.aws/sso/cache/*.json
               │
               ▼ both containers detect
┌──────────────┴──────────────────────┐
│  S3 Proxy + Monitor reload creds    │
│  - No container restart needed       │
└─────────────────────────────────────┘
```


## Implementation Details

### S3 Proxy (`s3proxy/app.py`)
- Uses boto3 with explicit credential extraction
- Thread-safe credential refresh using `credentials_lock`
- Decorator `@with_s3_client` ensures endpoints get fresh client
- Cache structure mirrors S3 bucket structure
- Handles file serving and directory listings

### SSO Monitor (`sso-monitor/monitor.py`)
- Container daemon that checks credentials periodically
- Primary check: `boto3.client('sts').get_caller_identity()`
- Writes signal files when credentials expire
- State-based signaling (only on transitions)

## Bazel Integration

**.bazelrc**:
```
build --define=maven_repo=http://localhost:8888/
```

**WORKSPACE**:
```python
maven_install(
    name = "maven",
    artifacts = [...],
    repositories = [
        "http://localhost:8888/",  # S3 proxy
        "https://repo1.maven.org/maven2",  # Fallback
    ],
)
```

## Common Issues

- **Port conflicts**: Check if `PROXY_PORT` available
- **AWS credentials**: Verify with `aws s3 ls s3://bucket-name/`
- **Watcher not triggering**: Check logs with `mise run sso-logs`
- **Profile not found**: Update `AWS_PROFILE` in `.env` and run `mise run sso-install`

## Testing

Run tests:
```bash
pytest              # All tests (446)
./run_tests.sh      # Helper script
```

## Post-Change Checklist (MANDATORY)

After every code change, before committing:

1. **Run full test suite** — `python3 -m pytest --no-cov -q`. Fix failures.
2. **Grep for stale references** — search all `.md` and `.py` files for outdated test counts, renamed functions, removed behavior descriptions, dead imports.
3. **Verify docs match behavior** — if a function's behavior changed (e.g. warn→fail, optional→prompted), scan docs/state-machine.md, docs/testing.md, README.md, CLAUDE.md for descriptions that are now wrong.
4. **Check for dead code** — if a function/constant was renamed or removed, grep the entire repo for old references.
5. **Verify replay scenarios** — if setup.py changed, run `python3 tests/interactive/replay_setup.py all` (with zeroed delays) to confirm scenarios still work.

Do NOT wait to be asked. Do NOT skip steps. Do NOT commit until all 5 are done.
