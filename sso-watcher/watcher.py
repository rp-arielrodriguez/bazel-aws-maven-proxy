#!/usr/bin/env python3
"""
Host-side AWS SSO watcher for bazel-aws-maven-proxy.

Watches for a signal file indicating login is required, then either:
- Shows a macOS notification asking the user to confirm (notify mode, default)
- Automatically triggers aws sso login (auto mode)

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

# Login mode: "notify" (default) = ask user first, "auto" = open browser immediately
LOGIN_MODE = os.environ.get("SSO_LOGIN_MODE", "notify")


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


def show_notification(profile: str) -> bool:
    """
    Show macOS notification asking user to confirm SSO login.

    Uses osascript to display a dialog with Refresh/Dismiss buttons.
    The dialog has a 120-second timeout to avoid zombie dialogs.

    Args:
        profile: AWS profile name to show in the notification

    Returns:
        True if user clicked Refresh, False otherwise
    """
    script = (
        'display dialog '
        '"AWS SSO credentials expired for profile: {profile}.\\n\\n'
        'Refresh now? This will open a browser for authentication." '
        'with title "AWS SSO Login Required" '
        'buttons {{"Dismiss", "Refresh"}} '
        'default button "Refresh" '
        'giving up after 120'
    ).format(profile=profile)

    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=130  # slightly more than dialog timeout
        )

        output = proc.stdout.strip()
        normalized = output.replace(" ", "")

        # Dialog timed out (gave up) - always reject regardless of button
        if "gaveup:true" in normalized:
            print("[sso-watcher] dialog timed out", flush=True)
            return False

        # User cancelled/closed the dialog
        if proc.returncode != 0:
            print("[sso-watcher] user dismissed dialog", flush=True)
            return False

        # User clicked Refresh
        if "Refresh" in output:
            return True

        return False

    except subprocess.TimeoutExpired:
        print("[sso-watcher] notification dialog timed out", flush=True)
        return False
    except FileNotFoundError:
        # osascript not available (not macOS)
        print("[sso-watcher] osascript not found, falling back to auto mode", flush=True)
        return True
    except Exception as e:
        print(f"[sso-watcher] notification error: {e}", flush=True)
        return False


def run_aws_sso_login(profile: str | None = None) -> int:
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


def handle_login(profile: str) -> bool:
    """
    Handle the login flow based on configured mode.

    In "notify" mode: shows a macOS dialog, only proceeds if user confirms.
    In "auto" mode: runs aws sso login immediately (legacy behavior).

    Args:
        profile: AWS profile to login with

    Returns:
        True if login succeeded, False otherwise
    """
    if LOGIN_MODE == "notify":
        user_accepted = show_notification(profile)
        if not user_accepted:
            print("[sso-watcher] login skipped by user", flush=True)
            return False

    rc = run_aws_sso_login(profile)
    return rc == 0


def main() -> int:
    """Main watch loop."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[sso-watcher] watching {SIGNAL_FILE} (profile={PROFILE})", flush=True)
    print(f"[sso-watcher] mode={LOGIN_MODE}, cooldown={COOLDOWN_SECONDS}s, poll={POLL_SECONDS}s", flush=True)

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
                    profile = str(signal.get("profile") or PROFILE)

                    success = handle_login(profile)

                    # Clear signal only on success; keep it for retry on failure
                    if success:
                        clear_signal()
                        print("[sso-watcher] login successful, signal cleared", flush=True)
                    else:
                        print("[sso-watcher] login not completed, keeping signal for retry", flush=True)

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
