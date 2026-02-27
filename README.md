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

1. **S3 Proxy Service** (Docker) - HTTP server that caches and serves Maven artifacts from S3
2. **SSO Monitor Service** (Docker) - Continuously checks credentials and writes signal files
3. **SSO Watcher** (Host) - Watches for signals, notifies user, and triggers login on confirmation

### Automated Workflow

```
SSO Monitor (Docker) checks credentials every 60s
       ↓ detects expiration
Writes signal → ~/.aws/sso-renewer/login-required.json (shared volume)
       ↓ watcher polls every 5s
SSO Watcher (launchd on host) detects signal
       ↓ notify mode (default): shows dialog (Refresh/Snooze/Don't Remind)
       ↓ auto mode: opens browser immediately
aws sso login opens browser for auth + MFA
       ↓ user completes
New credentials → ~/.aws/sso/cache/*.json
       ↓ both containers detect
S3 Proxy + Monitor reload credentials (no restart)
       ↓
Builds continue automatically
```

Install watcher: `mise run sso-install` (macOS only)

See [SSO_WATCHER.md](SSO_WATCHER.md) for details.

## Quick Start

### Prerequisites

- Docker & Docker Compose
- AWS CLI
- Python 3.11+
- mise (optional but recommended - `brew install mise`)

### 1. Install dependencies

```bash
# Using mise (recommended)
mise install python
pip install boto3

# Or system Python
pip3 install boto3
```

### 2. Configure AWS CLI v2 with SSO

**IMPORTANT:** Requires AWS CLI v2 with SSO session config (not credentials file).

**Option A: Interactive setup (recommended)**
```bash
# Create SSO session
aws configure sso-session
# SSO session name: my-sso
# SSO start URL: https://mycompany.awsapps.com/start
# SSO region: us-west-2
# SSO registration scopes: sso:account:access

# Create profile pointing to session
aws configure sso --profile bazel-cache
# SSO session name: my-sso
# Account ID: 123456789012
# Role name: DeveloperRole
# Region: us-west-2
```

**Option B: Manual config**

Add to `~/.aws/config`:
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

Do NOT use `~/.aws/credentials` file - SSO tokens managed by AWS CLI.

### 3. Set up environment

```bash
cp .env.example .env
# Edit .env with your settings:
#   AWS_PROFILE=bazel-cache
#   AWS_REGION=us-west-2
#   S3_BUCKET_NAME=your-maven-bucket
#   PROXY_PORT=9000
```

### 4. Start services

**Option A: Start everything with mise (recommended for macOS)**
```bash
mise run start  # Starts Docker services + SSO watcher
```

**Option B: Start Docker services only**
```bash
mise run docker:up  # or: docker-compose up -d
```

### 5. Configure Bazel

In your Bazel project:

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

## Usage

### Automated Monitoring (macOS)

The SSO watcher runs as a launchd service. By default (`notify` mode), it shows a macOS dialog with three options: **Refresh** (login now), **Snooze** (pick 15m/30m/1h/4h), or **Don't Remind** (suppress until next signal). Set `SSO_LOGIN_MODE=auto` in `.env` for automatic browser login.

```bash
# Install watcher (runs in background)
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

In `notify` mode (default), the watcher shows a dialog with Refresh/Snooze/Don't Remind options. In `auto` mode it opens the browser immediately.

## How It Works

### S3 Proxy Service

- Flask HTTP server on configurable port (default: 9000)
- Caches Maven artifacts locally (Docker volume)
- On cache miss, fetches from S3 using AWS credentials
- Periodically refreshes credentials (every 60 seconds by default)
- Health check endpoint: `/healthz`

### SSO Monitor Tool

- **Primary check**: `boto3.client('sts').get_caller_identity()`
  - Authoritative - directly tests AWS API access
  - Returns success if credentials valid
  - Returns error if expired/invalid

- **Secondary check** (with `--proactive`):
  - Parses `~/.aws/sso/cache/*.json`
  - Extracts `expiresAt` timestamp
  - Warns if expiring soon

- **Login trigger**:
  - In `notify` mode (default): shows macOS dialog, waits for user confirmation
  - In `auto` mode: proceeds immediately
  - Runs `aws sso login --profile <profile>` subprocess
  - Opens browser for SSO authentication
  - Respects MFA requirements
  - No credential manipulation

## Configuration

Environment variables (`.env` file):

| Variable | Description | Default |
|----------|-------------|---------|
| `AWS_PROFILE` | AWS CLI profile to use | `default` |
| `AWS_REGION` | AWS region for S3 bucket | `us-west-2` |
| `S3_BUCKET_NAME` | Maven S3 bucket name | Required |
| `PROXY_PORT` | Local proxy port | `9000` |
| `REFRESH_INTERVAL` | Credential refresh check (ms) | `60000` |
| `LOG_LEVEL` | Logging level | `info` |
| `CHECK_INTERVAL` | SSO monitor check interval (seconds) | `60` |
| `SSO_COOLDOWN_SECONDS` | Watcher cooldown between logins | `600` |
| `SSO_POLL_SECONDS` | Watcher signal file poll interval | `5` |
| `SSO_LOGIN_MODE` | `notify` (ask user) or `auto` (open browser immediately) | `notify` |

## Commands

### Complete System (mise)

```bash
# Start everything (Docker + SSO watcher)
mise run start

# Stop everything
mise run stop

# View all Docker logs
mise run docker:logs

# View SSO watcher logs
mise run sso-logs
```

### Docker Services

```bash
# Start Docker services (s3proxy + sso-monitor)
docker-compose up -d

# View logs
docker-compose logs -f

# Stop services
docker-compose down

# Restart services
docker-compose restart
```

### SSO Watcher (macOS)

```bash
# Install watcher
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


## Troubleshooting

### Port conflicts

Check if port is available:
```bash
lsof -i :9000
```

Change port in `.env`:
```
PROXY_PORT=8888
```

### Expired credentials

Manual login:
```bash
aws sso login --profile bazel-cache
docker-compose restart s3proxy
```

### S3 access issues

Verify AWS credentials work:
```bash
aws s3 ls s3://your-bucket-name/ --profile bazel-cache
```

### Watcher not triggering

Check watcher logs:
```bash
mise run sso-logs
```

Verify configuration:
```bash
mise run sso-status
```

Check logs:
```bash
docker-compose logs s3proxy
```

## macOS Support

Fully supported on macOS. Install dependencies:

```bash
# Using Homebrew
brew install awscli

# Using mise
mise install python@3.11
pip3 install boto3
```

## Testing

Run tests:
```bash
pytest
./run_tests.sh
```

## License

MIT License

## Contributing

Contributions welcome. This is a focused tool - please keep changes simple and aligned with the core use case.
