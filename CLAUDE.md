# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This project provides a transparent proxy layer between Bazel builds and S3-hosted Maven repositories, automatically handling AWS credential rotation including SSO authentication. It eliminates the need to restart build environments when AWS credentials are refreshed.

## Key Commands

### Starting and Managing Services

```bash
# Start all services
docker-compose up -d

# View logs from all services
docker-compose logs

# View logs from specific service
docker-compose logs s3proxy
docker-compose logs credential-monitor
docker-compose logs credential-renewer

# Stop all services
docker-compose down

# Restart a specific service
docker-compose restart s3proxy
```

### AWS SSO Login

```bash
# Interactive login helper (uses profile from .env or defaults to 'bazel-cache')
./login.sh [profile-name]

# Check if login is required
./check_login.sh
```

### Configuration

Environment variables are configured in `.env` file (copy from `.env.example`):
- `AWS_PROFILE`: AWS CLI profile to use
- `AWS_REGION`: AWS region for S3 bucket
- `S3_BUCKET_NAME`: Name of the Maven S3 bucket (required)
- `PROXY_PORT`: Local port for proxy service (default: 9000)
- `REFRESH_INTERVAL`: Token expiration check interval in milliseconds (default: 60000)
- `LOG_LEVEL`: Logging verbosity (debug, info, warn, error)

## Architecture

The system consists of three containerized services that work together:

### 1. S3 Proxy Service (`s3proxy/`)
- **Language**: Python (Flask)
- **Main file**: `s3proxy/app.py`
- **Purpose**: HTTP server that Bazel uses to fetch Maven artifacts
- **Key functionality**:
  - Serves artifacts from local cache when available
  - Fetches from S3 bucket on cache miss and stores locally
  - Provides directory listings for repository browsing
  - Refreshes AWS credentials periodically (every `REFRESH_INTERVAL` seconds)
  - Exposes health check endpoint at `/healthz`
- **Port**: Configurable via `PROXY_PORT` (default: 9000)
- **Cache location**: `/data` inside container, persisted via Docker volume

### 2. Credential Monitor Service (`credential-monitor/`)
- **Language**: Python (watchdog)
- **Main file**: `credential-monitor/monitor.py`
- **Purpose**: Filesystem watcher that detects AWS credential changes
- **Key functionality**:
  - Monitors `~/.aws/credentials`, `~/.aws/config`, and `~/.aws/sso/cache/`
  - Uses event-based file watching (not polling) for immediate detection
  - Triggers restart of s3proxy container when credentials change
  - Has 5-second cooldown to prevent multiple restarts
  - Requires access to Docker socket to restart containers

### 3. Credential Renewer Service (`credential-renewer/`)
- **Language**: Python
- **Main file**: `credential-renewer/renewer.py`
- **Purpose**: Proactive credential expiration monitoring
- **Key functionality**:
  - Checks SSO token expiration every `CHECK_INTERVAL` seconds (default: 900)
  - Creates notification file when token expires within `RENEWAL_THRESHOLD` seconds (default: 3600)
  - Notification file location: `/app/data/login_required.txt`
  - Does NOT automatically refresh (requires manual `aws sso login`)

## Data Flow

1. **Normal Operation**:
   - Bazel requests artifact from `http://localhost:9000/path/to/artifact`
   - S3 Proxy checks local cache at `/data/path/to/artifact`
   - If cache miss, fetches from S3 bucket using current AWS credentials
   - Stores in cache and serves to Bazel

2. **Credential Refresh**:
   - User runs `aws sso login --profile <profile>`
   - Credential Monitor detects file changes in `~/.aws/`
   - Monitor triggers S3 Proxy container restart
   - S3 Proxy loads new credentials on startup
   - Builds continue without manual intervention

3. **Proactive Expiration**:
   - Credential Renewer checks token expiration periodically
   - When expiration approaching, creates notification file
   - User runs `./check_login.sh` or `./login.sh` to renew

## Important Implementation Details

### S3 Proxy (`s3proxy/app.py`)
- Uses boto3 with explicit credential extraction to avoid automatic token refresh
- Creates S3 client with frozen credentials (access_key, secret_key, session_token)
- Thread-safe credential refresh using `credentials_lock`
- Decorator `@with_s3_client` ensures endpoints get fresh client
- Cache structure mirrors S3 bucket structure
- Handles both file serving and directory listings

### Credential Monitor (`credential-monitor/monitor.py`)
- Uses watchdog library for efficient filesystem monitoring
- Monitors parent directory for files (not the file itself) to catch atomic writes
- Restarts s3proxy using `docker-compose restart s3proxy` command
- Requires Docker socket mount (`/var/run/docker.sock`) to execute Docker commands

### Credential Renewer (`credential-renewer/renewer.py`)
- Finds latest SSO token file in `~/.aws/sso/cache/*.json`
- Parses `expiresAt` field to determine time until expiration
- Creates human-readable notification file instead of automatic refresh
- Notification includes profile-specific SSO configuration

## Bazel Integration

In the Bazel project that uses this proxy:

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

- **Port conflicts**: Check if port 9000 (or custom `PROXY_PORT`) is available
- **Docker socket permission**: credential-monitor needs `/var/run/docker.sock` access
- **AWS credentials**: Verify with `aws s3 ls s3://your-bucket-name/` before starting
- **Container networking**: Services communicate via Docker Compose network
- **SSO cache permissions**: credential-renewer needs write access to `~/.aws/sso/cache`
