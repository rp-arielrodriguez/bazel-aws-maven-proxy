# SSO Watcher

Host-side daemon that watches for credential expiration signals and manages AWS SSO login with user confirmation.

## Components

- `watcher.py` - Main daemon (runs via launchd on macOS)
- `requirements.txt` - Dependencies (none, uses stdlib only)

## How It Works

1. Runs as macOS launchd user agent
2. Polls `~/.aws/sso-renewer/login-required.json` every 5 seconds
3. **notify mode** (default): shows macOS dialog — user clicks "Refresh" to proceed
4. **auto mode**: skips dialog, runs login immediately
5. Runs `aws sso login --profile <profile>` (opens browser, respects MFA)
6. Clears signal on successful login
7. Implements atomic locking and cooldown

## Installation

```bash
# Via mise (recommended)
mise run sso-install

# Configuration in .env
AWS_PROFILE=bazel-cache
SSO_LOGIN_MODE=notify      # "notify" (ask user) or "auto" (open browser immediately)
SSO_COOLDOWN_SECONDS=60
SSO_POLL_SECONDS=5
```

## Architecture

- **No dependencies**: Pure Python stdlib
- **Atomic locking**: Directory-based (mkdir)
- **Cooldown**: Prevents login spam
- **Signal-driven**: Only acts on state transitions
- **Browser-based auth**: No MFA bypass

See parent [SSO_WATCHER.md](../SSO_WATCHER.md) for detailed documentation.
