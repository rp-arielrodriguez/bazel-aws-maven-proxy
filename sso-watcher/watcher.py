#!/usr/bin/env python3
"""
Host-side AWS SSO watcher for bazel-aws-maven-proxy.

Watches for a signal file indicating login is required, then triggers
interactive aws sso login on the host (allowing browser interaction).

Safe, boring, production-quality implementation:
- Atomic directory-based locking (no concurrent logins)
- Cooldown via last-run timestamp (no popup spam)
- Keeps signal on failure for retry
- Environment variable configuration
- Designed for launchd integration
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

# ---- Configuration (override via environment) ----
PROFILE = os.environ.get("AWS_PROFILE", "default")
SIGNAL_FILE = Path(os.environ.get(
    "SSO_SIGNAL_FILE",
    str(Path.home() / ".aws" / "sso-renewer" / "login-required.json")
))
STATE_DIR = Path(os.environ.get(
    "SSO_STATE_DIR",
    str(Path.home() / ".aws" / "sso-renewer")
))
LOCK_DIR = STATE_DIR / "login.lock"
LAST_RUN_FILE = STATE_DIR / "last-login-at.txt"
COOLDOWN_SECONDS = int(os.environ.get("SSO_COOLDOWN_SECONDS", "600"))  # 10 min
POLL_SECONDS = int(os.environ.get("SSO_POLL_SECONDS", "5"))


def utc_now() -> datetime:
    """Get current UTC time."""
    return datetime.now(timezone.utc)


def read_last_run() -> float | None:
    """Read last login attempt timestamp."""
    try:
        return float(LAST_RUN_FILE.read_text().strip())
    except Exception:
        return None


def write_last_run(ts: float) -> None:
    """Write login attempt timestamp."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LAST_RUN_FILE.write_text(f"{ts}\n")


def try_acquire_lock() -> bool:
    """
    Try to acquire lock using atomic directory creation.

    Returns:
        True if lock acquired, False if already held
    """
    try:
        LOCK_DIR.mkdir(parents=True, exist_ok=False)
        return True
    except FileExistsError:
        return False


def release_lock() -> None:
    """Release lock by removing directory."""
    try:
        LOCK_DIR.rmdir()
    except Exception:
        pass


def load_signal() -> dict:
    """Load signal file data."""
    try:
        with SIGNAL_FILE.open("r") as f:
            return json.load(f)
    except Exception:
        return {}


def should_trigger_login() -> bool:
    """
    Check if login should be triggered.

    Checks:
    1. Signal file exists
    2. Cooldown period has passed
    3. Optional nextAttemptAfter in signal not reached yet

    Returns:
        True if login should be triggered
    """
    if not SIGNAL_FILE.exists():
        return False

    # Cooldown: prevent repeated popups if user ignores or login fails
    last = read_last_run()
    if last is not None:
        elapsed = time.time() - last
        if elapsed < COOLDOWN_SECONDS:
            return False

    # Optional: signal file can include nextAttemptAfter epoch seconds
    signal = load_signal()
    next_attempt = signal.get("nextAttemptAfter")
    if isinstance(next_attempt, (int, float)) and time.time() < float(next_attempt):
        return False

    return True


def run_aws_sso_login(profile: str = None) -> int:
    """
    Run aws sso login interactively.

    AWS CLI will open the browser when needed.
    This works fine under macOS user launchd agents.

    Args:
        profile: AWS profile to use (defaults to PROFILE env var)

    Returns:
        Exit code from aws command
    """
    profile = profile or PROFILE
    cmd = ["aws", "sso", "login", "--profile", profile]
    print(f"[sso-watcher] running: {' '.join(cmd)}", flush=True)

    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True
    )

    print(proc.stdout, flush=True)
    return proc.returncode


def clear_signal() -> None:
    """Remove signal file."""
    try:
        SIGNAL_FILE.unlink()
    except FileNotFoundError:
        pass


def main() -> int:
    """Main watch loop."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[sso-watcher] watching {SIGNAL_FILE} (profile={PROFILE})", flush=True)
    print(f"[sso-watcher] cooldown={COOLDOWN_SECONDS}s, poll={POLL_SECONDS}s", flush=True)

    while True:
        try:
            if should_trigger_login():
                if not try_acquire_lock():
                    # Another watcher instance is handling it
                    print("[sso-watcher] lock held by another instance", flush=True)
                    time.sleep(POLL_SECONDS)
                    continue

                try:
                    write_last_run(time.time())

                    # Read profile from signal file
                    signal = load_signal()
                    profile = signal.get("profile", PROFILE)

                    rc = run_aws_sso_login(profile)

                    # Clear signal only on success; keep it for retry on failure
                    if rc == 0:
                        clear_signal()
                        print("[sso-watcher] login successful, signal cleared", flush=True)
                    else:
                        print(
                            f"[sso-watcher] login failed (rc={rc}), keeping signal for retry",
                            flush=True
                        )

                finally:
                    release_lock()

            time.sleep(POLL_SECONDS)

        except KeyboardInterrupt:
            print("[sso-watcher] exiting", flush=True)
            return 0
        except Exception as e:
            # Don't crash the launchd loop; log and continue
            print(f"[sso-watcher] error: {e}", file=sys.stderr, flush=True)
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
