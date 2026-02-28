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

    print(f"[sso-watcher] signal detected, triggering login", flush=True)
    return True


# Snooze intervals: label -> seconds
SNOOZE_OPTIONS = {
    "15 min": 900,
    "30 min": 1800,
    "1 hour": 3600,
    "4 hours": 14400,
}

# AppleScript that handles the entire notification flow in a single process:
# Main dialog (3 buttons) -> Snooze picker or suppress warning if needed.
_NOTIFICATION_SCRIPT = '''
beep
beep
tell application "System Events" to set frontmost of process "osascript" to true
tell me to activate
set dialogResult to display dialog ¬
    "AWS SSO credentials expired for profile: {profile}." & return & return & ¬
    "Refresh now, snooze, or disable reminders?" ¬
    with title "AWS SSO Login Required" ¬
    buttons {{"Don't Remind", "Snooze", "Refresh"}} ¬
    default button "Refresh" ¬
    giving up after 120
set btn to button returned of dialogResult
set gaveUp to gave up of dialogResult
if gaveUp then
    return "dismiss"
else if btn is "Snooze" then
    set picked to choose from list ¬
        {{{snooze_items}}} ¬
        with title "Snooze" ¬
        with prompt "Snooze for how long?" ¬
        default items {{"30 min"}}
    if picked is false then
        return "dismiss"
    else
        return "snooze:" & (item 1 of picked)
    end if
else if btn is "Don't Remind" then
    display dialog ¬
        "Reminders will be disabled until a new signal is received." & return & return & ¬
        "To manually refresh credentials later, run:" & return & return & ¬
        "    mise run sso-login" & return & ¬
        "    aws sso login --profile {profile}" ¬
        with title "Disable SSO Reminders?" ¬
        with icon caution ¬
        buttons {{"Cancel", "Disable Reminders"}} ¬
        default button "Cancel"
    if button returned of result is "Disable Reminders" then
        return "suppress"
    else
        return "dismiss"
    end if
else
    return "Refresh"
end if
'''


def show_notification(profile: str) -> str:
    """
    Show macOS notification with Refresh/Snooze/Don't Remind options.

    Runs a single osascript process that handles the entire flow:
    buttons dialog -> snooze picker or suppress warning if needed.

    Args:
        profile: AWS profile name to show in the notification

    Returns:
        Action string:
        - "refresh"          user wants to login now
        - "snooze:<seconds>" user wants to snooze for N seconds
        - "suppress"         user chose don't remind
        - "dismiss"          user closed/timed out/cancelled
    """
    snooze_items = ", ".join(f'"{k}"' for k in SNOOZE_OPTIONS)
    script = _NOTIFICATION_SCRIPT.format(
        profile=profile,
        snooze_items=snooze_items,
    )

    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=135,
        )
    except subprocess.TimeoutExpired:
        print("[sso-watcher] dialog timed out (subprocess)", flush=True)
        return "dismiss"
    except FileNotFoundError:
        print("[sso-watcher] osascript not found, falling back to auto mode", flush=True)
        return "refresh"
    except Exception as e:
        print(f"[sso-watcher] notification error: {e}", flush=True)
        return "dismiss"

    output = proc.stdout.strip()

    # User closed dialog (Escape/Cmd-W) or osascript error
    if proc.returncode != 0:
        print("[sso-watcher] user closed dialog", flush=True)
        return "dismiss"

    # Parse the returned action string
    if output == "Refresh":
        return "refresh"
    elif output.startswith("snooze:"):
        label = output[len("snooze:"):]
        seconds = SNOOZE_OPTIONS.get(label)
        if seconds:
            print(f"[sso-watcher] snoozed for {label}", flush=True)
            return f"snooze:{seconds}"
        return "dismiss"
    elif output == "suppress":
        print("[sso-watcher] user suppressed reminders", flush=True)
        return "suppress"
    elif output == "dismiss":
        print("[sso-watcher] dialog timed out", flush=True)
        return "dismiss"

    return "dismiss"


SSO_LOGIN_TIMEOUT = int(os.environ.get("SSO_LOGIN_TIMEOUT", "120"))  # seconds


def run_aws_sso_login(profile: str | None = None) -> int:
    """
    Run aws sso login with a timeout.

    AWS CLI will open the browser when needed. If the user doesn't
    complete auth within SSO_LOGIN_TIMEOUT seconds (default 120),
    the process is killed so the watcher can retry later.

    Args:
        profile: AWS profile to use (defaults to PROFILE env var)

    Returns:
        Exit code from aws command (or -1 on timeout)
    """
    profile = profile or PROFILE
    cmd = ["aws", "sso", "login", "--profile", profile]
    print(f"[sso-watcher] running: {' '.join(cmd)} (timeout={SSO_LOGIN_TIMEOUT}s)", flush=True)

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=SSO_LOGIN_TIMEOUT,
        )
        print(proc.stdout, flush=True)
        return proc.returncode
    except subprocess.TimeoutExpired:
        print(f"[sso-watcher] aws sso login timed out after {SSO_LOGIN_TIMEOUT}s", flush=True)
        return -1


def clear_signal() -> None:
    """Remove signal file."""
    try:
        SIGNAL_FILE.unlink()
    except FileNotFoundError:
        pass


def update_signal_snooze(seconds: int) -> None:
    """Write nextAttemptAfter into existing signal file for snooze."""
    signal = load_signal()
    signal["nextAttemptAfter"] = time.time() + seconds
    try:
        with SIGNAL_FILE.open("w") as f:
            json.dump(signal, f)
    except Exception as e:
        print(f"[sso-watcher] failed to write snooze: {e}", flush=True)


def handle_login(profile: str) -> str:
    """
    Handle the login flow based on configured mode.

    In "notify" mode: shows dialog with Refresh/Snooze/Don't Remind options.
    In "auto" mode: runs aws sso login immediately.

    Args:
        profile: AWS profile to login with

    Returns:
        Action result string:
        - "success"          login completed successfully
        - "failed"           login command failed
        - "snooze:<seconds>" user chose to snooze
        - "suppress"         user chose don't remind
        - "dismiss"          user dismissed/cancelled
    """
    if LOGIN_MODE == "notify":
        print(f"[sso-watcher] showing notification dialog for {profile}", flush=True)
        action = show_notification(profile)
        print(f"[sso-watcher] dialog result: {action}", flush=True)

        if action == "refresh":
            pass  # fall through to login
        elif action.startswith("snooze:"):
            return action
        elif action == "suppress":
            return "suppress"
        else:
            print("[sso-watcher] login skipped by user", flush=True)
            return "dismiss"

    rc = run_aws_sso_login(profile)
    return "success" if rc == 0 else "failed"


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
                    # Read profile from signal file
                    signal = load_signal()
                    profile = str(signal.get("profile") or PROFILE)

                    result = handle_login(profile)

                    if result == "success":
                        write_last_run(time.time())
                        clear_signal()
                        print("[sso-watcher] login successful, signal cleared", flush=True)
                    elif result.startswith("snooze:"):
                        seconds = int(result.split(":")[1])
                        update_signal_snooze(seconds)
                        print(f"[sso-watcher] snoozed, next attempt in {seconds}s", flush=True)
                    elif result == "suppress":
                        write_last_run(time.time())
                        clear_signal()
                        print("[sso-watcher] reminders suppressed, signal cleared", flush=True)
                    elif result == "dismiss":
                        # User saw dialog but dismissed — cooldown to avoid popup spam
                        write_last_run(time.time())
                        print("[sso-watcher] dismissed, retry after cooldown", flush=True)
                    elif result == "failed":
                        # Login failed or timed out — short delay then re-show dialog
                        print("[sso-watcher] login failed, will retry in 30s", flush=True)
                        update_signal_snooze(30)
                    else:
                        print(f"[sso-watcher] unexpected result: {result}", flush=True)

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
