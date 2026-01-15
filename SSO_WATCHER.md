# AWS SSO Watcher (Host-Side)

Production-quality host-side watcher that monitors for AWS SSO login signals and triggers interactive authentication.

## Purpose

The SSO watcher solves the following problem:

1. **The S3 proxy cache is stateful** - Must never restart for cache consistency
2. **AWS SSO tokens expire** - Typically after 1 hour
3. **Login must be interactive** - Browser-based auth with MFA on the host
4. **No container restarts** - Cache stays running, only credentials refresh

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  S3 Proxy (Docker, stateful, never restarts)        │
│  - Periodically checks credentials                   │
│  - Writes signal file when expired                   │
└───────────────────┬─────────────────────────────────┘
                    │ writes signal
                    ▼
         ~/.aws/sso-renewer/login-required.json
                    │
                    ▼ watches
┌─────────────────────────────────────────────────────┐
│  SSO Watcher (launchd user agent, host-side)        │
│  - Polls for signal file                             │
│  - Triggers: aws sso login --profile X               │
│  - Opens browser for user auth                       │
│  - Clears signal on success                          │
└─────────────────────────────────────────────────────┘
                    │
                    ▼ user completes auth
         Browser opens → AWS SSO portal → MFA → Success
                    │
                    ▼ credentials refreshed
         ~/.aws/sso/cache/*.json updated
                    │
                    ▼ proxy detects new creds
         S3 Proxy reloads credentials (no restart)
```

## Components

### 1. `sso-watcher/watcher.py`

Python daemon that:
- Watches `~/.aws/sso-renewer/login-required.json`
- Uses atomic directory-based locking (no concurrent logins)
- Implements cooldown (default: 60 seconds, configurable) to prevent popup spam
- Runs `aws sso login` interactively when signal detected
- Keeps signal on failure for retry
- Clears signal on success

**Key Features:**
- **Atomic locking**: `mkdir` for POSIX-atomic lock acquisition
- **Cooldown**: Prevents repeated popups if user ignores or login fails
- **Retry logic**: Signal persists until login succeeds
- **Environment configured**: No argparse, all env vars
- **launchd-friendly**: Flushes stdout, handles exceptions gracefully

### 2. `launchd/com.bazel.sso-watcher.plist`

macOS launchd user agent configuration:
- Runs in user session (can open browser)
- Starts at login (`RunAtLoad`)
- Auto-restarts on crash (`KeepAlive`)
- Throttled restarts (30s interval)
- Logs to `~/Library/Logs/sso-watcher.log`
- Interactive process type (GUI access for browser)

### 3. mise tasks (`.mise.toml`)

Convenient management tasks:
- `mise run sso-install` - Install launchd agent
- `mise run sso-uninstall` - Uninstall agent
- `mise run sso-status` - Check agent status
- `mise run sso-logs` - Tail logs
- `mise run sso-restart` - Restart agent
- `mise run sso-test` - Create test signal
- `mise run sso-clean` - Clean state files

## Installation

### Prerequisites

- macOS (launchd-based)
- Python 3.11+ (via mise or system)
- AWS CLI installed and configured
- mise installed (optional, for convenience)

### Quick Install

```bash
# 1. Install mise (if not already installed)
brew install mise

# 2. Configure .env file (copy from .env.example)
cp .env.example .env
# Edit .env:
#   AWS_PROFILE=bazel-cache
#   SSO_COOLDOWN_SECONDS=60
#   SSO_POLL_SECONDS=5

# 3. Install the watcher
mise run sso-install
```

### Manual Install (without mise)

```bash
# 1. Expand template placeholders
PYTHON_PATH="$(which python3)"
REPO_PATH="$(pwd)"
AWS_PROFILE="${AWS_PROFILE:-default}"

sed -e "s|{{PYTHON_PATH}}|$PYTHON_PATH|g" \
    -e "s|{{REPO_PATH}}|$REPO_PATH|g" \
    -e "s|{{HOME}}|$HOME|g" \
    -e "s|{{AWS_PROFILE}}|$AWS_PROFILE|g" \
    launchd/com.bazel.sso-watcher.plist > \
    "$HOME/Library/LaunchAgents/com.bazel.sso-watcher.plist"

# 2. Load the agent
launchctl bootstrap gui/$(id -u) \
    "$HOME/Library/LaunchAgents/com.bazel.sso-watcher.plist"
```

## Usage

### Check Status

```bash
mise run sso-status
```

Output shows:
- Plist location
- Agent load status
- Process ID
- Resource usage

### View Logs

```bash
# Tail logs in real-time
mise run sso-logs

# Or manually
tail -f ~/Library/Logs/sso-watcher.log
```

### Test the Watcher

```bash
# Create a test signal file
mise run sso-test

# Watcher should trigger login within 5 seconds
# Check logs to see activity
```

### Restart After Config Changes

```bash
mise run sso-restart
```

### Uninstall

```bash
mise run sso-uninstall
```

## Configuration

All configuration in `.env` file:

| Variable | Default | Description |
|----------|---------|-------------|
| `AWS_PROFILE` | `default` | AWS CLI profile to use |
| `SSO_COOLDOWN_SECONDS` | `60` | Min seconds between login attempts |
| `SSO_POLL_SECONDS` | `5` | Seconds between directory checks |
| `SSO_SIGNAL_FILE` | `~/.aws/sso-renewer/login-required.json` | Signal file path (advanced) |
| `SSO_STATE_DIR` | `~/.aws/sso-renewer` | State directory (advanced) |

### Customizing Configuration

Edit `.env` file in the repository:

```bash
# .env
AWS_PROFILE=my-custom-profile
SSO_COOLDOWN_SECONDS=300
SSO_POLL_SECONDS=10
CHECK_INTERVAL=30
```

Apply changes (idempotent):
```bash
mise run sso-install
```

## Signal File Format

The signal file is JSON:

```json
{
  "profile": "bazel-cache",
  "reason": "Token expired",
  "timestamp": "2026-01-15T20:30:00Z",
  "nextAttemptAfter": 1705349400
}
```

**Fields:**
- `profile` (required): AWS profile name
- `reason` (optional): Why login is needed
- `timestamp` (optional): When signal was created
- `nextAttemptAfter` (optional): Unix epoch - skip login until this time

The watcher will:
1. Check if signal file exists
2. Verify cooldown period passed
3. Check `nextAttemptAfter` if present
4. Acquire lock
5. Run `aws sso login --profile <profile>`
6. Clear signal on success, keep on failure

## How It Works with S3 Proxy

### Normal Flow

1. S3 proxy periodically checks credentials (every 60s)
2. When invalid/expired, proxy writes signal file
3. Watcher detects signal within 5s
4. Watcher triggers `aws sso login`
5. User completes auth in browser
6. AWS CLI writes new credentials to `~/.aws/sso/cache/`
7. S3 proxy detects new credentials on next check (within 60s)
8. Proxy reloads credentials **without restarting**
9. Cache stays intact, builds continue

### Failure Handling

**If login fails:**
- Signal file kept
- Watcher waits for cooldown (60s default, configurable)
- Retries when cooldown expires

**If user cancels:**
- Signal file kept
- Watcher waits for cooldown
- User can manually login: `aws sso login --profile bazel-cache`

**If watcher crashes:**
- launchd auto-restarts (throttled)
- State persists in files
- No duplicate logins (atomic locking)

## Troubleshooting

### Watcher not starting

```bash
# Check status
mise run sso-status

# Check system logs
log show --predicate 'subsystem == "com.apple.launchd"' --last 5m

# Try manual start
./sso-watcher/watcher.py
```

### Login not triggering

```bash
# Check signal file exists
ls -la ~/.aws/sso-renewer/login-required.json

# Check cooldown status
cat ~/.aws/sso-renewer/last-login-at.txt

# Check watcher logs
mise run sso-logs
```

### Browser not opening

Ensure `ProcessType = Interactive` in plist:
```xml
<key>ProcessType</key>
<string>Interactive</string>
```

Restart: `mise run sso-restart`

### Lock file stuck

```bash
mise run sso-clean
```

## Design Decisions

### Why atomic directory locking?

- `mkdir` is atomic on POSIX filesystems
- Simpler than fcntl/flock
- Works across all POSIX systems
- No file descriptor leaks

### Why cooldown?

Prevents popup spam if:
- User ignores the login prompt
- Login fails repeatedly
- Multiple signals written rapidly

### Why keep signal on failure?

Allows retry without losing context:
- User can fix issue (network, credentials)
- Watcher will retry after cooldown
- No silent failures

### Why launchd user agent (not system daemon)?

System daemons can't:
- Open browser windows
- Access user keychain
- Run in user session

User agents can do all of above.

## Security Considerations

- **No credential storage**: Watcher doesn't handle credentials
- **No credential transmission**: Only triggers AWS CLI
- **User interaction required**: Can't bypass MFA
- **Lock prevents concurrency**: Single login at a time
- **Cooldown prevents abuse**: Rate limiting built-in

## System Integration

The watcher integrates with the Docker-based SSO monitor:
- Monitor (Docker) detects expired credentials
- Writes signal file to shared volume
- Watcher (host launchd) detects signal
- Triggers browser login automatically
- Runs continuously via launchd
- Reacts to signal files
- No manual intervention needed

### With S3 Proxy

S3 proxy writes signal files when credentials expire.
Watcher handles the signals automatically.
No coordination needed - simple file-based protocol.

## Files and Locations

```
Repository:
├── sso-watcher/watcher.py                    # Watcher daemon
├── launchd/
│   └── com.bazel.sso-watcher.plist   # launchd config template
└── .mise.toml                         # Management tasks

Host:
├── ~/Library/LaunchAgents/
│   └── com.bazel.sso-watcher.plist   # Installed config
├── ~/Library/Logs/
│   ├── sso-watcher.log               # Stdout
│   └── sso-watcher.error.log         # Stderr
└── ~/.aws/sso-renewer/
    ├── login-required.json           # Signal file
    ├── last-login-at.txt             # Cooldown tracking
    └── login.lock/                   # Lock directory
```

## Advanced Usage

### Run watcher in foreground (debugging)

```bash
./sso-watcher/watcher.py
```

Press Ctrl+C to stop.

### Custom signal directory

```bash
export SSO_STATE_DIR=~/custom/path
mise run sso-restart
```

### Different profile per signal

Signal file can override profile:
```json
{
  "profile": "prod-cache",
  "reason": "Production credentials expired"
}
```

Watcher uses profile from signal, falling back to env var.

## Contributing

When modifying:
- Keep it simple and boring
- Test with launchd (not just foreground)
- Verify atomic locking works
- Check logs flush correctly
- Test failure scenarios
