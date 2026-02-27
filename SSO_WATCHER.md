# SSO Watcher

Host-side daemon for AWS SSO credential management on macOS.

## Quick Start

See [QUICKSTART_SSO_WATCHER.md](QUICKSTART_SSO_WATCHER.md) for setup instructions.

## Architecture

```
SSO Monitor (Docker) checks credentials
       ↓ detects expiration
Writes signal → ~/.aws/sso-renewer/login-required.json
       ↓ polls every 5s
SSO Watcher (launchd) detects signal
       ↓ notify mode (default): shows dialog, user clicks "Refresh"
       ↓ auto mode: proceeds immediately
aws sso login opens browser
       ↓ user completes MFA
New credentials → ~/.aws/sso/cache/*.json
       ↓ both containers detect
S3 Proxy + Monitor reload (no restart)
```

### Why This Design

**Problem:** S3 proxy cache is stateful and can't restart. AWS SSO tokens expire hourly. Login requires browser + MFA on host.

**Solution:**
- Monitor (Docker) detects expiration, writes signal file
- Watcher (host) reads signal, asks user (notify mode) or triggers login directly (auto mode)
- Proxy auto-reloads credentials without restart

## Components

### `sso-watcher/watcher.py`

Host daemon (launchd user agent) that:
- Polls `~/.aws/sso-renewer/login-required.json` every 5s
- In `notify` mode (default): shows macOS dialog, only proceeds on user confirmation
- In `auto` mode: runs login immediately (opens browser)
- Uses atomic directory locking (`mkdir`)
- Cooldown (default 60s) prevents popup spam
- Runs `aws sso login --profile <profile>`
- Clears signal on success, keeps on failure for retry

### `launchd/com.bazel.sso-watcher.plist`

macOS launchd configuration with:
- User agent (not daemon) for browser access
- Environment variables from `.env`
- Logging to `~/Library/Logs/sso-watcher.{log,error.log}`
- KeepAlive for automatic restart

### Signal File Format

```json
{
  "profile": "bazel-cache",
  "reason": "Credentials expired",
  "timestamp": "2025-01-15T18:00:00Z",
  "source": "sso-monitor-container"
}
```

## Installation

```bash
# Configure in .env
AWS_PROFILE=bazel-cache
SSO_COOLDOWN_SECONDS=60
SSO_POLL_SECONDS=5

# Install (idempotent)
mise run sso-install

# Verify
mise run sso-status

# View logs
mise run sso-logs
```

## Configuration

All settings via `.env`:

| Variable | Description | Default |
|----------|-------------|---------|
| `AWS_PROFILE` | AWS CLI profile | `default` |
| `SSO_LOGIN_MODE` | `notify` (ask user) or `auto` (open browser immediately) | `notify` |
| `SSO_COOLDOWN_SECONDS` | Cooldown between logins | `60` |
| `SSO_POLL_SECONDS` | Signal poll interval | `5` |

After changing `.env`, run `mise run sso-install` to apply (idempotent).

## Implementation Details

### Atomic Locking

```python
def try_acquire_lock() -> bool:
    try:
        LOCK_DIR.mkdir(parents=True, exist_ok=False)
        return True
    except FileExistsError:
        return False
```

Uses `mkdir()` for POSIX-atomic lock. Simpler than fcntl, works across processes.

### Cooldown Mechanism

```python
last_login = read_timestamp("last-login-at.txt")
if time.time() - last_login < COOLDOWN_SECONDS:
    return  # Skip, too soon
```

Prevents repeated popups if user ignores or login fails repeatedly.

### State Management

- **Signal file**: Written by monitor, read by watcher
- **Lock directory**: Created on login attempt, removed on completion
- **Cooldown timestamp**: Written on login attempt, checked before next

Shared directory: `~/.aws/sso-renewer/`

### Error Handling

- **Login failure**: Signal kept, retry on next poll
- **Profile not found**: Logs error, keeps signal
- **Lock conflict**: Skips, tries next poll
- **Signal parse error**: Logs warning, removes bad signal

## Troubleshooting

### Watcher not starting

```bash
# Check status
mise run sso-status

# View logs
cat ~/Library/Logs/sso-watcher.log

# Reinstall
mise run sso-uninstall
mise run sso-install
```

### Browser not opening

- Check `SSO_LOGIN_MODE` — if `notify`, you must click "Refresh" in the dialog first
- Check `PATH` in plist includes AWS CLI location
- Verify profile exists: `aws configure list-profiles`
- Check cooldown not active: `cat ~/.aws/sso-renewer/last-login-at.txt`

### Signal file stuck

```bash
# Clean state
mise run sso-clean

# Check monitor is running
docker-compose ps sso-monitor

# View monitor logs
docker-compose logs sso-monitor
```

### Multiple login popups

- Switch to `notify` mode (`SSO_LOGIN_MODE=notify` in `.env`) — browser only opens on user confirmation
- Increase `SSO_COOLDOWN_SECONDS` in `.env`
- Run `mise run sso-install` to apply
- Current cooldown shown in logs: `cooldown=60s`

## Security

- **No credential storage**: Watcher never handles credentials
- **Browser-based auth**: Standard AWS SSO flow
- **MFA respected**: No bypass, user must complete
- **User agent**: Runs as user, not root
- **Lock prevents concurrency**: Single login at a time
- **Cooldown prevents abuse**: Rate limiting built-in

## System Integration

Works with Docker-based SSO monitor:
- Monitor detects expired credentials in container
- Writes signal to shared volume
- Watcher on host detects signal
- In `notify` mode: shows dialog, user confirms
- In `auto` mode: triggers login immediately
- Browser opens for user authentication
- Both containers reload credentials automatically

No container restarts needed.
