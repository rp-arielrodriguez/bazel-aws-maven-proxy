# SSO Watcher

Host-side daemon that watches for credential expiration signals and manages AWS SSO login.

- `watcher.py` — Main daemon (runs via launchd on macOS)
- Pure Python stdlib, no pip dependencies

See [docs/sso-watcher.md](../docs/sso-watcher.md) for full documentation.
