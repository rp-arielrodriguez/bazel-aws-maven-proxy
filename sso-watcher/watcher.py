#!/usr/bin/env python3
"""
Host-side AWS SSO watcher for bazel-aws-maven-proxy.

Watches for a signal file indicating login is required, then either:
- Silently refreshes token without browser (silent mode)
- Shows a macOS notification asking the user to confirm (notify mode, default)
- Automatically triggers aws sso login (auto mode)
- Does nothing, manual login only (standalone mode)

All modes except standalone try silent token refresh first. If the cached
refresh token is still valid, credentials are renewed without browser
interaction. Only when silent refresh fails does it fall back to the
mode-specific behavior (dialog, browser, or nothing).

Proactive refresh: independently of the signal file, the watcher checks
token expiry every 60s. If the token expires within PROACTIVE_REFRESH_MINUTES
(default 30), it attempts silent refresh before the monitor even detects
expiry. This keeps tokens alive across sleep/wake cycles.

Modes:
- notify (default): try silent refresh, then dialog with Refresh/Snooze/Don't Remind
- auto: try silent refresh, then open browser immediately
- silent: try silent refresh only, never open browser
- standalone: watcher idles, user must run `mise run sso-login` manually

Mode is read from state file (~/.aws/sso-renewer/mode) first, then
SSO_LOGIN_MODE env var, then defaults to "notify". Toggle at runtime
with `mise run sso-mode:notify|auto|silent|standalone`.
"""
import configparser
import glob
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone

VALID_MODES = ("notify", "auto", "silent", "standalone")

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
MODE_FILE = STATE_DIR / "mode"
COOLDOWN_SECONDS = int(os.environ.get("SSO_COOLDOWN_SECONDS", "600"))  # 10 min
POLL_SECONDS = int(os.environ.get("SSO_POLL_SECONDS", "5"))
PROACTIVE_REFRESH_MINUTES = int(os.environ.get("SSO_PROACTIVE_REFRESH_MINUTES", "30"))

# Login mode: state file > env var > default
_ENV_MODE = os.environ.get("SSO_LOGIN_MODE", "notify")


def read_mode() -> str:
    """Read current mode. Priority: state file > env var > default."""
    try:
        mode = MODE_FILE.read_text().strip()
        if mode in VALID_MODES:
            return mode
    except Exception:
        pass
    return _ENV_MODE if _ENV_MODE in VALID_MODES else "notify"


def write_mode(mode: str) -> None:
    """Write mode to state file."""
    if mode not in VALID_MODES:
        raise ValueError(f"Invalid mode: {mode} (expected: {', '.join(VALID_MODES)})")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    MODE_FILE.write_text(f"{mode}\n")


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


LOCK_STALE_SECONDS = 300  # 5 min — lock older than this is stale


def try_acquire_lock() -> bool:
    """
    Try to acquire lock using atomic directory creation.

    If the lock directory exists but is older than LOCK_STALE_SECONDS,
    it's considered stale (from a crashed/killed process) and removed.

    Returns:
        True if lock acquired, False if already held
    """
    try:
        LOCK_DIR.mkdir(parents=True, exist_ok=False)
        return True
    except FileExistsError:
        # Check for stale lock
        try:
            age = time.time() - LOCK_DIR.stat().st_mtime
            if age > LOCK_STALE_SECONDS:
                print(f"[sso-watcher] removing stale lock (age={int(age)}s)", flush=True)
                LOCK_DIR.rmdir()
                LOCK_DIR.mkdir(parents=True, exist_ok=False)
                return True
        except Exception:
            pass
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


# ---- AWS config/cache paths ----
AWS_CONFIG_FILE = Path(os.environ.get(
    "AWS_CONFIG_FILE",
    str(Path.home() / ".aws" / "config")
))
SSO_CACHE_DIR = Path(os.environ.get(
    "SSO_CACHE_DIR",
    str(Path.home() / ".aws" / "sso" / "cache")
))


def _get_sso_session_config(profile: str) -> dict:
    """
    Read sso-session config for a profile from ~/.aws/config.

    Returns dict with keys: start_url, region, session_name.
    Returns empty dict if not found or not configured.
    """
    try:
        config = configparser.ConfigParser()
        config.read(str(AWS_CONFIG_FILE))
    except Exception:
        return {}

    # Find the profile section
    profile_section = f"profile {profile}" if profile != "default" else "default"
    if not config.has_section(profile_section):
        return {}

    session_name = config.get(profile_section, "sso_session", fallback=None)
    if not session_name:
        return {}

    # Find the sso-session section
    session_section = f"sso-session {session_name}"
    if not config.has_section(session_section):
        return {}

    return {
        "session_name": session_name,
        "start_url": config.get(session_section, "sso_start_url", fallback=""),
        "region": config.get(session_section, "sso_region", fallback=""),
    }


def _find_sso_cache_file(start_url: str) -> dict | None:
    """
    Find the SSO cache file matching a start URL.

    Scans ~/.aws/sso/cache/*.json for a file with a matching startUrl
    that contains accessToken, refreshToken, clientId, clientSecret.

    Returns the parsed cache data dict, or None if not found.
    """
    cache_pattern = str(SSO_CACHE_DIR / "*.json")
    for path in glob.glob(cache_pattern):
        try:
            with open(path) as f:
                data = json.load(f)
            if (data.get("startUrl") == start_url
                    and "refreshToken" in data
                    and "clientId" in data
                    and "clientSecret" in data):
                data["_cache_path"] = path
                return data
        except Exception:
            continue
    return None


def check_token_near_expiry(profile: str, threshold_minutes: int | None = None) -> bool:
    """
    Check if the SSO access token is near expiry.

    Reads the cache file for the profile's sso-session and checks expiresAt.

    Args:
        profile: AWS profile name
        threshold_minutes: minutes before expiry to trigger (default: PROACTIVE_REFRESH_MINUTES)

    Returns:
        True if token expires within threshold (or is already expired), False if
        token is healthy or no cache file found.
    """
    threshold = threshold_minutes if threshold_minutes is not None else PROACTIVE_REFRESH_MINUTES
    session = _get_sso_session_config(profile)
    if not session.get("start_url"):
        return False

    cache = _find_sso_cache_file(session["start_url"])
    if not cache:
        return False

    expires_at = cache.get("expiresAt", "")
    if not expires_at:
        return False

    try:
        exp_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        remaining = exp_dt - datetime.now(timezone.utc)
        return remaining < timedelta(minutes=threshold)
    except Exception:
        return False


def try_silent_refresh(profile: str) -> bool:
    """
    Attempt to silently refresh SSO token using the cached refresh token.

    Flow:
    1. Read sso-session config from ~/.aws/config for the profile
    2. Find matching cache file in ~/.aws/sso/cache/ by startUrl
    3. Call `aws sso-oidc create-token --grant-type refresh_token`
    4. Write new token back to cache file

    Returns True on success, False if silent refresh is not possible
    or fails (caller should fall back to browser-based login).
    """
    print(f"[sso-watcher] attempting silent token refresh for {profile}", flush=True)

    # Step 1: Get sso-session config
    session = _get_sso_session_config(profile)
    if not session.get("start_url"):
        print("[sso-watcher] silent refresh: no sso-session config found", flush=True)
        return False

    # Step 2: Find cache file
    cache = _find_sso_cache_file(session["start_url"])
    if not cache:
        print("[sso-watcher] silent refresh: no cache file with refresh token", flush=True)
        return False

    # Check if client registration is still valid
    reg_expires = cache.get("registrationExpiresAt", "")
    if reg_expires:
        try:
            exp_dt = datetime.fromisoformat(reg_expires.replace("Z", "+00:00"))
            if exp_dt < datetime.now(timezone.utc):
                print("[sso-watcher] silent refresh: client registration expired", flush=True)
                return False
        except Exception:
            pass

    # Step 3: Call sso-oidc create-token
    region = session.get("region") or cache.get("region", "us-east-1")
    cmd = [
        "aws", "sso-oidc", "create-token",
        "--client-id", cache["clientId"],
        "--client-secret", cache["clientSecret"],
        "--grant-type", "refresh_token",
        "--refresh-token", cache["refreshToken"],
        "--region", region,
        "--no-sign-request",
    ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"[sso-watcher] silent refresh: command error: {e}", flush=True)
        return False

    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        print(f"[sso-watcher] silent refresh failed: {stderr}", flush=True)
        return False

    # Step 4: Parse response and write back to cache
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        print("[sso-watcher] silent refresh: invalid JSON response", flush=True)
        return False

    new_access = result.get("accessToken")
    new_expires = result.get("expiresIn")
    new_refresh = result.get("refreshToken")

    if not new_access:
        print("[sso-watcher] silent refresh: no accessToken in response", flush=True)
        return False

    # Update cache file
    cache_path = cache["_cache_path"]
    try:
        with open(cache_path) as f:
            cache_data = json.load(f)

        cache_data["accessToken"] = new_access
        if new_expires:
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=new_expires)
            cache_data["expiresAt"] = expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        if new_refresh:
            cache_data["refreshToken"] = new_refresh

        with open(cache_path, "w") as f:
            json.dump(cache_data, f)

        exp_str = cache_data.get("expiresAt", "unknown")
        refresh_rotated = "yes" if new_refresh else "no"
        print(f"[sso-watcher] silent refresh successful (expires={exp_str}, new_refresh_token={refresh_rotated})", flush=True)
        return True
    except Exception as e:
        print(f"[sso-watcher] silent refresh: failed to write cache: {e}", flush=True)
        return False


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
        "To manually refresh credentials later, run either:" & return & return & ¬
        "    mise run sso-login" & return & ¬
        "  or" & return & ¬
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

# Webview .app bundle path (built by sso-install.sh)
WEBVIEW_APP = STATE_DIR / "bin" / "SSOLogin.app" / "Contents" / "MacOS" / "sso-webview"


def _extract_authorize_url(proc: subprocess.Popen, timeout: float = 10) -> str | None:
    """Read stdout from aws sso login --no-browser until we find the authorize URL."""
    if proc.stdout is None:
        return None
    deadline = time.time() + timeout
    for line in proc.stdout:
        stripped = line.strip()
        if stripped.startswith("https://"):
            return stripped
        if time.time() > deadline:
            break
    return None


def _extract_callback_host(authorize_url: str) -> str:
    """Extract callback host:port from the redirect_uri query parameter."""
    try:
        from urllib.parse import urlparse, parse_qs
        params = parse_qs(urlparse(authorize_url).query)
        redirect_uri = params.get("redirect_uri", [""])[0]
        if redirect_uri:
            parsed = urlparse(redirect_uri)
            host = parsed.hostname or "127.0.0.1"
            return f"{host}:{parsed.port}" if parsed.port else host
    except Exception:
        pass
    return "127.0.0.1"


def _kill_webview() -> None:
    """Terminate the webview app if running.

    Since the webview is launched via `open -a` (not direct exec), we
    can't track its PID through the Popen object. Use osascript to
    quit it gracefully, falling back to killall.
    """
    try:
        subprocess.run(
            ["osascript", "-e", 'tell application "AWS SSO Login" to quit'],
            timeout=3, capture_output=True,
        )
    except Exception:
        try:
            subprocess.run(["killall", "sso-webview"], timeout=3, capture_output=True)
        except Exception:
            pass


def _launch_webview(url: str, callback_host: str) -> subprocess.Popen | None:
    """Launch the sandboxed webview .app bundle via macOS `open -a`.

    Uses `open -a` instead of direct exec so macOS properly activates
    the app window, even when called from a launchd background agent.

    Returns Popen or None if unavailable.
    """
    app_bundle = WEBVIEW_APP.parent.parent.parent  # .../SSOLogin.app
    if not WEBVIEW_APP.exists():
        print("[sso-watcher] webview not found, will use system browser", flush=True)
        return None
    try:
        return subprocess.Popen(
            ["open", "-a", str(app_bundle), "--args", url, callback_host],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        print(f"[sso-watcher] webview launch failed: {e}", flush=True)
        return None


def run_aws_sso_login(profile: str | None = None) -> int:
    """
    Run aws sso login using --no-browser with a sandboxed webview.

    Flow:
    1. Start `aws sso login --no-browser` to get the authorize URL
    2. Launch sandboxed webview (or fall back to system browser)
    3. Wait for aws sso login to complete (user does MFA)

    The webview provides a dedicated window with persistent cookie
    storage (Google/IdP credentials cached), no browser tab pollution,
    and auto-close on callback detection.

    Args:
        profile: AWS profile to use (defaults to PROFILE env var)

    Returns:
        Exit code from aws command (or -1 on timeout)
    """
    profile = profile or PROFILE
    cmd = ["aws", "sso", "login", "--no-browser", "--profile", profile]
    print(f"[sso-watcher] running: {' '.join(cmd)} (timeout={SSO_LOGIN_TIMEOUT}s)", flush=True)

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    webview_proc = None

    try:
        # Extract the authorize URL from aws cli output
        url = _extract_authorize_url(proc)
        if not url:
            print("[sso-watcher] failed to extract authorize URL", flush=True)
            proc.kill()
            return -1

        callback_host = _extract_callback_host(url)
        print("[sso-watcher] authorize URL obtained, opening login window", flush=True)

        # Try sandboxed webview first, fall back to system browser
        webview_proc = _launch_webview(url, callback_host)
        if webview_proc is None:
            print("[sso-watcher] falling back to system browser", flush=True)
            subprocess.Popen(["open", url])

        # Wait for aws sso login to complete (blocks until MFA done or timeout)
        proc.wait(timeout=SSO_LOGIN_TIMEOUT)
        output = proc.stdout.read() if proc.stdout else ""
        if output.strip():
            print(output.strip(), flush=True)
        return proc.returncode

    except subprocess.TimeoutExpired:
        print(f"[sso-watcher] aws sso login timed out after {SSO_LOGIN_TIMEOUT}s", flush=True)
        proc.kill()
        return -1
    finally:
        if webview_proc is not None:
            _kill_webview()


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

    All modes except standalone try silent token refresh first.
    If silent refresh succeeds, returns "success" without any user
    interaction. Otherwise falls back to mode-specific behavior.

    Modes:
    - notify: silent refresh → dialog → browser login
    - auto: silent refresh → browser login (no dialog)
    - silent: silent refresh only, no browser
    - standalone: should not reach here

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
    mode = read_mode()

    if mode == "standalone":
        # Should not reach here, but guard anyway
        return "dismiss"

    # Try silent refresh first (all active modes)
    if try_silent_refresh(profile):
        return "success"

    # Silent mode: no browser fallback
    if mode == "silent":
        print("[sso-watcher] silent refresh failed, no browser fallback in silent mode", flush=True)
        return "failed"

    if mode == "notify":
        print(f"[sso-watcher] showing notification dialog for {profile}", flush=True)
        action = show_notification(profile)
        print(f"[sso-watcher] dialog result: {action}", flush=True)

        if action == "refresh":
            pass  # fall through to run_aws_sso_login below
        elif action.startswith("snooze:"):
            return action
        elif action == "suppress":
            return "suppress"
        else:
            print("[sso-watcher] login skipped by user", flush=True)
            return "dismiss"

    elif mode == "auto":
        pass  # fall through to run_aws_sso_login below

    rc = run_aws_sso_login(profile)
    return "success" if rc == 0 else "failed"


MIN_AWS_CLI_VERSION = (2, 9)


def _check_aws_cli() -> None:
    """Verify aws CLI exists and is >= 2.9 (needed for refresh_token grant)."""
    try:
        proc = subprocess.run(
            ["aws", "--version"], capture_output=True, text=True, timeout=10,
        )
        # Output: "aws-cli/2.33.2 Python/3.13.11 ..."
        version_str = proc.stdout.strip().split()[0].split("/")[1]
        parts = tuple(int(x) for x in version_str.split(".")[:2])
        if parts < MIN_AWS_CLI_VERSION:
            print(f"[sso-watcher] WARNING: aws-cli {version_str} < {'.'.join(map(str, MIN_AWS_CLI_VERSION))} "
                  f"— silent refresh requires >= {'.'.join(map(str, MIN_AWS_CLI_VERSION))}", flush=True)
        else:
            print(f"[sso-watcher] aws-cli {version_str}", flush=True)
    except FileNotFoundError:
        print("[sso-watcher] WARNING: aws CLI not found — install with: brew install awscli", flush=True)
    except Exception as e:
        print(f"[sso-watcher] WARNING: could not check aws version: {e}", flush=True)


def main() -> int:
    """Main watch loop."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    _check_aws_cli()
    print(f"[sso-watcher] watching {SIGNAL_FILE} (profile={PROFILE})", flush=True)
    print(f"[sso-watcher] mode={read_mode()}, cooldown={COOLDOWN_SECONDS}s, poll={POLL_SECONDS}s", flush=True)
    if PROACTIVE_REFRESH_MINUTES > 0:
        print(f"[sso-watcher] proactive refresh: {PROACTIVE_REFRESH_MINUTES}min before expiry", flush=True)

    last_proactive_check: float = 0
    # Check token expiry every 60s (not every poll cycle)
    proactive_check_interval = 60

    while True:
        try:
            # Re-read mode each loop so toggles take effect immediately
            current_mode = read_mode()

            if current_mode == "standalone":
                time.sleep(POLL_SECONDS)
                continue

            # Proactive refresh: check token expiry independently of signal
            if (PROACTIVE_REFRESH_MINUTES > 0
                    and not SIGNAL_FILE.exists()
                    and time.time() - last_proactive_check >= proactive_check_interval):
                last_proactive_check = time.time()
                if check_token_near_expiry(PROFILE, PROACTIVE_REFRESH_MINUTES):
                    print(f"[sso-watcher] proactive: token near expiry, attempting silent refresh", flush=True)
                    if try_silent_refresh(PROFILE):
                        print("[sso-watcher] proactive: token refreshed successfully", flush=True)
                    else:
                        print("[sso-watcher] proactive: silent refresh failed, waiting for signal", flush=True)

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
