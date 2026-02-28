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
# Install watcher (runs at login)
mise run sso-install

# Check status
mise run sso-status

# View logs
mise run sso-logs

# Restart watcher
mise run sso-restart

# Uninstall
mise run sso-uninstall
```

### Configuration

Environment variables in `.env` (copy from `.env.example`):
- `AWS_PROFILE`: AWS CLI profile (default: default)
- `AWS_REGION`: AWS region for S3
- `S3_BUCKET_NAME`: Maven S3 bucket (required)
- `PROXY_PORT`: Local port (default: 9000)
- `REFRESH_INTERVAL`: Proxy credential refresh in ms (default: 60000)
- `LOG_LEVEL`: Logging level (debug, info, warn, error)
- `CHECK_INTERVAL`: Monitor check interval in seconds (default: 60)
- `SSO_COOLDOWN_SECONDS`: Watcher cooldown (default: 600)
- `SSO_POLL_SECONDS`: Watcher poll interval (default: 5)
- `SSO_LOGIN_MODE`: Login behavior - `notify` (default, asks user) or `auto` (opens browser immediately)
- `CONTAINER_ENGINE`: `podman` or `docker` (auto-detect if unset, prefers podman)

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
  - In `notify` mode (default): shows macOS dialog with 3 options:
    - **Refresh**: runs `aws sso login` (opens browser)
    - **Snooze**: pick 15m/30m/1h/4h, writes `nextAttemptAfter` to signal file
    - **Don't Remind**: shows warning, clears signal (manual `mise run sso-login` needed later)
  - In `auto` mode: triggers `aws sso login` immediately (opens browser)
  - Clears signal on success
  - Atomic locking, cooldown protection (default 600s)
- **Installation**: `mise run sso-install`


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
│  - notify: Refresh/Snooze/Don't Remind │
│  - auto mode: triggers immediately  │
└──────────────┬──────────────────────┘
               │ user clicks Refresh / auto
               ▼
         aws sso login
               │ opens browser
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
build --define=maven_repo=http://localhost:9000/
```

**WORKSPACE**:
```python
maven_install(
    name = "maven",
    artifacts = [...],
    repositories = [
        "http://localhost:9000/",  # S3 proxy
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
pytest
./run_tests.sh
```
