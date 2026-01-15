# SSO Watcher Quick Start

5-minute setup for automated AWS SSO login on macOS.

## What This Does

Automatically opens your browser for AWS SSO login when credentials expire. No manual intervention, no container restarts.

## Prerequisites

- macOS
- mise installed (`brew install mise`)
- AWS CLI configured with SSO
- This repo checked out

## Setup

```bash
# 1. Configure settings in .env (copy from .env.example)
cp .env.example .env
# Edit .env:
#   AWS_PROFILE=bazel-cache
#   SSO_COOLDOWN_SECONDS=60
#   SSO_POLL_SECONDS=5

# 2. Install the watcher
mise run sso-install

# 3. Done!
```

The watcher is now running in the background via launchd, using settings from `.env`.

## Usage

### Check it's running

```bash
mise run sso-status
```

### View logs

```bash
mise run sso-logs
```

### Test it

```bash
# Create a fake signal
mise run sso-test

# Watcher will trigger login within 5 seconds
# Check logs to see it working
```

### Stop it

```bash
mise run sso-uninstall
```

## How It Works

1. S3 proxy detects expired credentials
2. Writes signal file: `~/.aws/sso-renewer/login-required.json`
3. Watcher detects signal within 5 seconds
4. Opens browser: `aws sso login --profile <profile>`
5. You complete auth in browser
6. Watcher clears signal
7. Proxy reloads credentials automatically

No container restarts. Cache stays intact.

## Troubleshooting

**Watcher not responding?**
```bash
mise run sso-restart
```

**Check logs:**
```bash
cat ~/Library/Logs/sso-watcher.log
```

**Clean stuck state:**
```bash
mise run sso-clean
```

## Configuration

All config in `.env` file:
```bash
# Edit .env
nano .env

# Update settings:
SSO_COOLDOWN_SECONDS=60    # Cooldown between logins
SSO_POLL_SECONDS=5         # Poll interval
AWS_PROFILE=bazel-cache    # AWS profile to use
```

Apply changes:
```bash
mise run sso-install  # Reinstall with new config (idempotent)
```

## See Also

- [SSO_WATCHER.md](SSO_WATCHER.md) - Full documentation
- [README.md](README.md) - Main project docs
