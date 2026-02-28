# SSO Watcher

Host-side daemon for AWS SSO credential management on macOS.

## Architecture

```
SSO Monitor (Container) checks credentials every 60s
       ↓ detects expiration
Writes signal → ~/.aws/sso-renewer/login-required.json
       ↓ polls every 5s
SSO Watcher (launchd) detects signal
       ↓ notify mode: Refresh/Snooze/Don't Remind
       ↓ auto mode: opens browser immediately
       ↓ standalone mode: idle (manual only)
aws sso login opens browser
       ↓ user completes MFA
New credentials → ~/.aws/sso/cache/*.json
       ↓ both containers detect
S3 Proxy + Monitor reload (no restart)
```

### Why This Design

**Problem:** S3 proxy cache is stateful and can't restart. AWS SSO tokens expire hourly. Login requires browser + MFA on host.

**Solution:**
- Monitor (container) detects expiration, writes signal file
- Watcher (host) reads signal, asks user (notify mode) or triggers login directly (auto mode)
- Proxy auto-reloads credentials without restart

## Components

### `sso-watcher/watcher.py`

Host daemon (launchd user agent) that:
- Polls `~/.aws/sso-renewer/login-required.json` every 5s
- In `notify` mode (default): shows macOS dialog with Refresh/Snooze/Don't Remind
  - **Refresh**: opens browser for SSO login
  - **Snooze**: pick 15m/30m/1h/4h, writes `nextAttemptAfter` to signal
  - **Don't Remind**: clears signal after warning (manual `mise run sso-login` needed later)
- In `auto` mode: runs login immediately (opens browser)
- In `standalone` mode: watcher idles, manual `mise run sso-login` only
- Uses atomic directory locking (`mkdir`)
- Cooldown (default 600s) prevents popup spam after dismiss
- Runs `aws sso login --profile <profile>` with 120s timeout
- Clears signal on success, keeps on failure (retries in ~30s)

### Dialog Actions Quick Reference

| Action | Signal | Next attempt |
|--------|--------|-------------|
| **Refresh** → success | cleared | on next credential expiry |
| **Refresh** → timeout/fail | kept | ~30s (auto-retry) |
| **Snooze** | kept | user-chosen (15m/30m/1h/4h) |
| **Dismiss** / ignore (120s timeout) | kept | ~10 min (cooldown) |
| **Don't Remind** | cleared | only on new expiry signal |

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
  "source": "sso-monitor-container",
  "nextAttemptAfter": 1700000000
}
```

## Installation

```bash
# Configure in .env
AWS_PROFILE=bazel-cache
SSO_COOLDOWN_SECONDS=600
SSO_POLL_SECONDS=5

# Install (idempotent)
mise run sso-install

# Verify
mise run sso-status

# View logs
mise run sso-logs
```

## Commands

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

## Configuration

All settings via `.env`:

| Variable | Description | Default |
|----------|-------------|---------|
| `AWS_PROFILE` | AWS CLI profile | `default` |
| `SSO_LOGIN_MODE` | `notify`, `auto`, or `standalone` | `notify` |
| `SSO_COOLDOWN_SECONDS` | Cooldown between logins (seconds) | `600` |
| `SSO_POLL_SECONDS` | Signal poll interval (seconds) | `5` |

After changing `.env`, run `mise run sso-install` to apply.

Runtime mode toggle: `mise run sso-mode:notify|auto|standalone` — takes effect within seconds, no restart needed.

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

Cooldown is written on dismiss only. On login failure/timeout, a 30s snooze is written to the signal file instead, allowing quick retry.

### State Management

- **Signal file**: Written by monitor, read/updated by watcher (snooze writes `nextAttemptAfter`)
- **Lock directory**: Created on login attempt, removed on completion
- **Cooldown timestamp**: Written on dismiss, checked before next attempt
- **Mode file**: Written by `mise run sso-mode:*`, read by watcher every poll cycle

Shared directory: `~/.aws/sso-renewer/`

### Error Handling

- **Login failure**: Signal kept, 30s snooze for quick retry
- **Login timeout (120s)**: Same as failure — signal kept, 30s snooze
- **Snooze**: Signal updated with `nextAttemptAfter`, retry after snooze expires
- **Suppress (Don't Remind)**: Signal cleared, no retry until new signal
- **Profile not found**: Logs error, keeps signal
- **Lock conflict**: Skips, tries next poll
- **Signal parse error**: Logs warning, removes bad signal

## Troubleshooting

### Watcher not starting

```bash
mise run sso-status         # Check installed/running/mode
mise run sso-logs           # View recent logs
mise run sso-uninstall && mise run sso-install  # Reinstall
```

### Browser not opening

- Check mode: `mise run sso-mode` — in `notify` mode, you must click "Refresh"
- In `standalone` mode, watcher is idle — use `mise run sso-login` directly
- Verify profile exists: `aws configure list-profiles`
- Check `PATH` in plist includes AWS CLI location

### Signal file stuck

```bash
mise run sso-clean                    # Clear state
mise run containers:logs              # Check monitor is running
```

### Multiple login popups

- Use `notify` mode (default) — browser only opens on user confirmation
- Increase `SSO_COOLDOWN_SECONDS` in `.env`
- Or switch to `standalone` for full manual control

## Security

- **No credential storage**: Watcher never handles credentials
- **Browser-based auth**: Standard AWS SSO flow
- **MFA respected**: No bypass, user must complete
- **User agent**: Runs as user, not root
- **Lock prevents concurrency**: Single login at a time
- **Cooldown prevents abuse**: Rate limiting built-in
