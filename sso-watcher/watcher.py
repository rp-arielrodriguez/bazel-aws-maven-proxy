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
import logging
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [sso-watcher] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("sso-watcher")

VALID_MODES = ("notify", "auto", "silent", "standalone")

# Named constants for magic numbers
AUTO_RETRY_SNOOZE_SECONDS = 30
DEFAULT_SNOOZE_SECONDS = 900
WEBVIEW_CLOSE_GRACE_SECONDS = 10
CRED_CHECK_INTERVAL_SECONDS = 15

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
try:
    COOLDOWN_SECONDS = int(os.environ.get("SSO_COOLDOWN_SECONDS", "600"))
except ValueError:
    COOLDOWN_SECONDS = 600  # 10 min default
try:
    POLL_SECONDS = int(os.environ.get("SSO_POLL_SECONDS", "5"))
except ValueError:
    POLL_SECONDS = 5
try:
    PROACTIVE_REFRESH_MINUTES = int(os.environ.get("SSO_PROACTIVE_REFRESH_MINUTES", "30"))
except ValueError:
    PROACTIVE_REFRESH_MINUTES = 30

# Repository path for update checks (set by launchd plist)
REPO_PATH = os.environ.get("REPO_PATH", "")
UPDATE_CHECK_INTERVAL = 12 * 3600  # 12 hours between checks
UPDATE_STATE_FILE = STATE_DIR / "update-available.json"

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


def _clear_cooldown() -> None:
    """Remove last-login-at so cooldown doesn't block the first signal check.

    Called at watcher startup — previous dismiss/timeout cooldown should not
    survive a restart, otherwise ``mise run start`` with expired creds sits
    idle until the old cooldown elapses.
    """
    try:
        LAST_RUN_FILE.unlink()
    except FileNotFoundError:
        pass


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
        age = -1.0
        try:
            age = time.time() - LOCK_DIR.stat().st_mtime
            if age > LOCK_STALE_SECONDS:
                log.info(f"removing stale lock (age={int(age)}s)")
                LOCK_DIR.rmdir()
                LOCK_DIR.mkdir(parents=True, exist_ok=False)
                return True
        except Exception:
            log.info(f"stale lock detected, age={age:.0f}s")
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


def _find_sso_cache_file(start_url: str, session_name: str | None = None) -> dict | None:
    """
    Find the SSO cache file for a specific profile.

    The AWS CLI computes the token cache filename as sha1(session_name)
    for modern profiles or sha1(start_url) for legacy ones.  When
    session_name is provided, looks up the exact file instead of
    scanning — this avoids picking up tokens from other sessions
    (e.g. setup-tmp sessions) that share the same startUrl.

    Falls back to scanning by startUrl if the exact file doesn't exist
    or session_name is not provided.

    Returns the parsed cache data dict, or None if not found.
    """
    import hashlib

    # Direct lookup by session_name (preferred — exact match)
    if session_name:
        cache_key = hashlib.sha1(session_name.encode("utf-8")).hexdigest()
        path = SSO_CACHE_DIR / f"{cache_key}.json"
        if path.exists():
            try:
                with open(path) as f:
                    data = json.load(f)
                if ("refreshToken" in data
                        and "clientId" in data
                        and "clientSecret" in data):
                    data["_cache_path"] = str(path)
                    return data
            except Exception:
                pass

    # Fallback: scan by startUrl (legacy profiles or missing session_name)
    cache_pattern = str(SSO_CACHE_DIR / "*.json")
    for path_str in glob.glob(cache_pattern):
        try:
            with open(path_str) as f:
                data = json.load(f)
            if (data.get("startUrl") == start_url
                    and "refreshToken" in data
                    and "clientId" in data
                    and "clientSecret" in data):
                data["_cache_path"] = path_str
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

    cache = _find_sso_cache_file(session["start_url"], session.get("session_name"))
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
    log.info(f"attempting silent token refresh for {profile}")

    # Step 1: Get sso-session config
    session = _get_sso_session_config(profile)
    if not session.get("start_url"):
        log.info("silent refresh: no sso-session config found")
        return False

    # Step 2: Find cache file by session_name (exact) or startUrl (fallback)
    cache = _find_sso_cache_file(session["start_url"], session.get("session_name"))
    if not cache:
        log.info("silent refresh: no cache file with refresh token")
        return False

    # Check if client registration is still valid
    reg_expires = cache.get("registrationExpiresAt", "")
    if reg_expires:
        try:
            exp_dt = datetime.fromisoformat(reg_expires.replace("Z", "+00:00"))
            if exp_dt < datetime.now(timezone.utc):
                log.info("silent refresh: client registration expired")
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
        log.info(f"silent refresh: command error: {e}")
        return False

    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        log.info(f"silent refresh failed: {stderr}")
        return False

    # Step 4: Parse response and write back to cache
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        log.info("silent refresh: invalid JSON response")
        return False

    new_access = result.get("accessToken")
    new_expires = result.get("expiresIn")
    new_refresh = result.get("refreshToken")

    if not new_access:
        log.info("silent refresh: no accessToken in response")
        return False

    # Update cache file (atomic write to prevent corruption)
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

        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(Path(cache_path).parent), suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w") as f:
                json.dump(cache_data, f)
            os.replace(tmp_path, cache_path)
        except Exception:
            os.unlink(tmp_path)
            raise

        exp_str = cache_data.get("expiresAt", "unknown")
        refresh_rotated = "yes" if new_refresh else "no"
        log.info(f"silent refresh successful (expires={exp_str}, new_refresh_token={refresh_rotated})")
        return True
    except Exception as e:
        log.info(f"silent refresh: failed to write cache: {e}")
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

    log.info(f"signal detected, triggering login")
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
    # Escape profile name for safe AppleScript injection
    safe_profile = profile.replace("\\", "\\\\").replace('"', '\\"')
    snooze_items = ", ".join(f'"{k}"' for k in SNOOZE_OPTIONS)
    script = _NOTIFICATION_SCRIPT.format(
        profile=safe_profile,
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
        log.info("dialog timed out (subprocess)")
        return "dismiss"
    except FileNotFoundError:
        log.info("osascript not found, falling back to auto mode")
        return "refresh"
    except Exception as e:
        log.info(f"notification error: {e}")
        return "dismiss"

    output = proc.stdout.strip()

    # User closed dialog (Escape/Cmd-W) or osascript error
    if proc.returncode != 0:
        log.info("user closed dialog")
        return "dismiss"

    # Parse the returned action string
    if output == "Refresh":
        return "refresh"
    elif output.startswith("snooze:"):
        label = output[len("snooze:"):]
        seconds = SNOOZE_OPTIONS.get(label)
        if seconds:
            log.info(f"snoozed for {label}")
            return f"snooze:{seconds}"
        return "dismiss"
    elif output == "suppress":
        log.info("user suppressed reminders")
        return "suppress"
    elif output == "dismiss":
        log.info("dialog timed out")
        return "dismiss"

    return "dismiss"


try:
    SSO_LOGIN_TIMEOUT = int(os.environ.get("SSO_LOGIN_TIMEOUT", "120"))
except ValueError:
    SSO_LOGIN_TIMEOUT = 120  # seconds default

# Webview .app bundle path (built by sso-install.sh)
WEBVIEW_APP = STATE_DIR / "bin" / "SSOLogin.app" / "Contents" / "MacOS" / "sso-webview"
WEBVIEW_APP_BUNDLE = STATE_DIR / "bin" / "SSOLogin.app"


def _extract_authorize_url(proc: subprocess.Popen, timeout: float = 30) -> str | None:
    """Read stdout from aws sso login --no-browser until we find the authorize URL.

    Uses a daemon thread + queue to avoid blocking indefinitely on readline.
    The OIDC device registration can take 5-15s before the URL is printed.
    If no URL is found within *timeout* seconds, returns None.
    """
    if proc.stdout is None:
        return None

    line_queue: queue.Queue[str | None] = queue.Queue()

    def _reader():
        try:
            for line in proc.stdout:
                line_queue.put(line)
            line_queue.put(None)  # EOF sentinel
        except Exception:
            line_queue.put(None)

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    deadline = time.time() + timeout
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        try:
            line = line_queue.get(timeout=min(remaining, 1.0))
        except queue.Empty:
            continue
        if line is None:
            break
        stripped = line.strip()
        if stripped.startswith("https://"):
            return stripped
        if stripped:
            log.info(f"aws: {stripped}")

    return None


def _extract_callback_host(authorize_url: str) -> str:
    """Extract callback host:port from the redirect_uri query parameter."""
    try:
        params = parse_qs(urlparse(authorize_url).query)
        redirect_uri = params.get("redirect_uri", [""])[0]
        if redirect_uri:
            parsed = urlparse(redirect_uri)
            host = parsed.hostname or "127.0.0.1"
            return f"{host}:{parsed.port}" if parsed.port else host
    except Exception:
        pass
    return "127.0.0.1"


def _is_webview_running() -> bool:
    """Check if the webview app is still running."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "SSOLogin.app"],
            capture_output=True, timeout=3,
        )
        return result.returncode == 0
    except Exception:
        return False


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
    if not WEBVIEW_APP.exists():
        log.info("webview not found, will use system browser")
        return None
    try:
        return subprocess.Popen(
            ["open", "-a", str(WEBVIEW_APP_BUNDLE), "-n", "--args", url, callback_host],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        log.info(f"webview launch failed: {e}")
        return None


def _get_token_cache_mtime(profile: str) -> float:
    """Return mtime of the SSO token cache file for *profile*, or 0."""
    import hashlib
    session = _get_sso_session_config(profile)
    name = session.get("session_name")
    if not name:
        return 0
    path = SSO_CACHE_DIR / f"{hashlib.sha1(name.encode('utf-8')).hexdigest()}.json"
    try:
        return path.stat().st_mtime
    except OSError:
        return 0


def _check_credentials_valid(profile: str) -> bool:
    """Quick check if AWS credentials are currently valid."""
    try:
        proc = subprocess.run(
            ["aws", "sts", "get-caller-identity", "--profile", profile],
            capture_output=True, timeout=10,
        )
        return proc.returncode == 0
    except Exception:
        return False


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

    Secondary exit: if aws sso login hangs but the token cache file is
    updated (e.g. after sleep/wake where the CLI stalls after writing
    tokens), we detect this via mtime comparison and exit successfully.
    Using mtime instead of STS avoids false positives where an old
    (not-yet-expired) access token passes but the refresh token is stale.

    Args:
        profile: AWS profile to use (defaults to PROFILE env var)

    Returns:
        Exit code from aws command (or -1 on timeout)
    """
    profile = profile or PROFILE
    cmd = ["aws", "sso", "login", "--no-browser", "--profile", profile]
    log.info(f"running: {' '.join(cmd)} (timeout={SSO_LOGIN_TIMEOUT}s)")

    # Snapshot token file mtime so we can detect when aws writes new tokens
    token_mtime_before = _get_token_cache_mtime(profile)

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    webview_proc = None
    let_webview_self_close = False

    try:
        # Extract the authorize URL from aws cli output
        url = _extract_authorize_url(proc)
        if not url:
            log.info("failed to extract authorize URL")
            proc.kill()
            proc.wait()
            return -1

        callback_host = _extract_callback_host(url)
        log.info("authorize URL obtained, opening login window")

        # Try sandboxed webview first, fall back to system browser
        webview_proc = _launch_webview(url, callback_host)
        if webview_proc is None:
            log.info("falling back to system browser")
            subprocess.run(["open", url], timeout=5)

        # Give webview time to launch before checking if it's running
        if webview_proc is not None:
            time.sleep(2)

        # Poll aws process, also watch for webview exit (user closed window)
        deadline = time.time() + SSO_LOGIN_TIMEOUT
        last_token_check = 0
        while True:
            rc = proc.poll()
            if rc is not None:
                output = proc.stdout.read() if proc.stdout else ""
                if output.strip():
                    print(output.strip(), flush=True)
                if rc == 0:
                    let_webview_self_close = True
                return rc

            # If webview exited, give aws a few seconds to finish (callback
            # may have been detected and webview auto-closed — aws needs time
            # to complete the token exchange). Only abort if aws doesn't finish.
            if webview_proc is not None and not _is_webview_running():
                log.info("webview closed, waiting for aws to finish...")
                try:
                    proc.wait(timeout=WEBVIEW_CLOSE_GRACE_SECONDS)
                    output = proc.stdout.read() if proc.stdout else ""
                    if output.strip():
                        print(output.strip(), flush=True)
                    return proc.returncode
                except subprocess.TimeoutExpired:
                    log.info("aws did not finish after webview close, aborting")
                    proc.kill()
                    proc.wait()
                    return -1

            # Secondary exit: detect that aws wrote fresh tokens to the cache
            # file. We compare the file's mtime against the snapshot taken
            # before starting aws sso login. This avoids a false positive
            # where an old (not-yet-expired) access token passes an STS check
            # even though the CLI hasn't finished the PKCE exchange — which
            # would leave a stale refresh token in the cache.
            now = time.time()
            if now - last_token_check >= CRED_CHECK_INTERVAL_SECONDS:
                last_token_check = now
                current_mtime = _get_token_cache_mtime(profile)
                if current_mtime > token_mtime_before:
                    log.info("token file updated, giving aws time to finish...")
                    let_webview_self_close = True
                    try:
                        proc.wait(timeout=WEBVIEW_CLOSE_GRACE_SECONDS)
                        output = proc.stdout.read() if proc.stdout else ""
                        if output.strip():
                            print(output.strip(), flush=True)
                        log.info("aws finished normally after token update")
                        return proc.returncode
                    except subprocess.TimeoutExpired:
                        log.info("aws did not finish in grace period, killing")
                        proc.kill()
                        proc.wait()
                        return 0

            if time.time() > deadline:
                log.info(f"aws sso login timed out after {SSO_LOGIN_TIMEOUT}s")
                proc.kill()
                proc.wait()
                return -1

            time.sleep(0.5)
    finally:
        if webview_proc is not None and not let_webview_self_close:
            _kill_webview()


def check_for_updates() -> None:
    """Check if the repo is behind origin/main and write state file.

    Runs ``check-update.sh --json`` from the repo directory. The result
    is written to ``~/.aws/sso-renewer/update-available.json`` so that
    ``sso-status`` can display it.  Errors are silently ignored — this
    is best-effort and must never block the main loop.
    """
    if not REPO_PATH:
        return
    check_script = os.path.join(REPO_PATH, "scripts", "check-update.sh")
    if not os.path.isfile(check_script):
        return
    try:
        result = subprocess.run(
            ["bash", check_script, "--json"],
            capture_output=True, text=True, timeout=30,
            cwd=REPO_PATH,
        )
        data = json.loads(result.stdout) if result.stdout.strip() else {}
        status = data.get("status", "")
        if status == "update_available":
            log.info(f"update available: {data.get('commits_behind', '?')} commits behind")
            UPDATE_STATE_FILE.write_text(result.stdout)
        elif status == "up_to_date":
            # Clear stale state
            try:
                UPDATE_STATE_FILE.unlink()
            except FileNotFoundError:
                pass
        # "error" status: leave existing state file (if any) untouched
    except Exception:
        pass  # network down, timeout, parse error — all ok


def write_signal(profile: str, reason: str = "proactive refresh failed") -> None:
    """Write signal file so notify/webview flow can trigger."""
    try:
        SIGNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
        signal_data = {
            "profile": profile,
            "reason": reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "sso-watcher-proactive",
        }
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(SIGNAL_FILE.parent), suffix=".tmp"
        )
        try:
            with os.fdopen(tmp_fd, "w") as f:
                json.dump(signal_data, f)
            os.replace(tmp_path, str(SIGNAL_FILE))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception as e:
        log.error(f"failed to write signal: {e}")


def clear_signal() -> None:
    """Remove signal file."""
    try:
        SIGNAL_FILE.unlink()
    except FileNotFoundError:
        pass
    except Exception as e:
        log.info(f"failed to clear signal: {e}")


def update_signal_snooze(seconds: int) -> None:
    """Write nextAttemptAfter into existing signal file for snooze (atomic)."""
    signal = load_signal()
    signal["nextAttemptAfter"] = time.time() + seconds
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(SIGNAL_FILE.parent), suffix=".tmp"
        )
        try:
            with os.fdopen(tmp_fd, "w") as f:
                json.dump(signal, f)
            os.replace(tmp_path, str(SIGNAL_FILE))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception as e:
        log.info(f"failed to write snooze: {e}")


class _WebviewHandle:
    """Wrapper providing stdin/stdout-like interface over named pipes.

    macOS requires `open -a` to properly activate GUI apps from background
    processes (launchd). Since `open` doesn't support pipe redirection to
    the child process, we use named FIFOs for bidirectional communication.
    The webview reads from the stdin FIFO and writes to the stdout FIFO.
    """

    def __init__(self, stdout_file, stdin_file, fifo_dir: str,
                 open_proc: subprocess.Popen, app_name: str):
        self.stdout = stdout_file    # reads lines from webview
        self.stdin = stdin_file      # writes to webview
        self._fifo_dir = fifo_dir
        self._open_proc = open_proc  # `open -W` process
        self._app_name = app_name

    def poll(self):
        """Check if webview is still running."""
        return self._open_proc.poll()

    def cleanup(self):
        """Remove FIFOs."""
        import shutil
        try:
            shutil.rmtree(self._fifo_dir, ignore_errors=True)
        except Exception:
            pass


def _launch_notify_webview(profile: str, callback_host: str) -> _WebviewHandle | None:
    """Launch webview in --notify mode (notification page + auth in one window).

    Uses `open -a` with named FIFOs for stdin/stdout communication so macOS
    properly activates the GUI window from background processes (launchd).

    The webview shows a notification page with Refresh/Snooze/Don't Remind.
    On Refresh, it signals SSO_ACTION:refresh and waits for an authorize URL
    on stdin, then navigates to it.

    Returns _WebviewHandle with stdin/stdout, or None if webview unavailable.
    """
    if not WEBVIEW_APP.exists():
        log.info("webview not found, falling back to dialog")
        return None

    fifo_dir = None
    try:
        # Create named pipes for communication
        fifo_dir = tempfile.mkdtemp(prefix="sso-webview-")
        stdin_fifo = os.path.join(fifo_dir, "stdin")
        stdout_fifo = os.path.join(fifo_dir, "stdout")
        os.mkfifo(stdin_fifo)
        os.mkfifo(stdout_fifo)

        # Launch via `open -a` for proper GUI activation
        # -n = new instance, -W = wait for exit
        open_proc = subprocess.Popen(
            ["open", "-a", str(WEBVIEW_APP_BUNDLE), "-n", "-W",
             "--stdin", stdin_fifo,
             "--stdout", stdout_fifo,
             "--args", "--notify", profile, callback_host],
            stderr=subprocess.DEVNULL,
        )

        # Open both FIFOs in threads to avoid deadlock
        stdout_file = [None]
        stdout_err = [None]
        stdin_file = [None]
        stdin_err = [None]

        def open_stdout():
            try:
                stdout_file[0] = open(stdout_fifo, "r")
            except Exception as e:
                stdout_err[0] = e

        def open_stdin():
            try:
                stdin_file[0] = open(stdin_fifo, "w")
            except Exception as e:
                stdin_err[0] = e

        t_out = threading.Thread(target=open_stdout, daemon=True)
        t_in = threading.Thread(target=open_stdin, daemon=True)
        t_out.start()
        t_in.start()
        t_out.join(timeout=10)
        t_in.join(timeout=10)

        if stdout_file[0] is None:
            raise RuntimeError(f"Failed to open stdout FIFO: {stdout_err[0]}")
        if stdin_file[0] is None:
            # Close stdout if it opened
            if stdout_file[0]:
                stdout_file[0].close()
            raise RuntimeError(f"Failed to open stdin FIFO: {stdin_err[0]}")

        return _WebviewHandle(
            stdout_file=stdout_file[0],
            stdin_file=stdin_file[0],
            fifo_dir=fifo_dir,
            open_proc=open_proc,
            app_name="AWS SSO Login",
        )
    except Exception as e:
        log.info(f"webview notify launch failed: {e}")
        if fifo_dir:
            import shutil
            shutil.rmtree(fifo_dir, ignore_errors=True)
        return None


def _run_notify_login(profile: str) -> str:
    """Handle login via all-in-one webview (notify mode).

    Launches webview in --notify mode. Reads user action from stdout.
    If user clicks Refresh, starts aws sso login --no-browser, extracts
    the authorize URL, sends it to webview via stdin, then waits for
    the auth to complete.

    Returns action result string (same as handle_login).
    """
    # We don't know the callback host yet — use a placeholder; the webview
    # will get the real one when we send the authorize URL. For --notify mode
    # the callback host is set when we start aws sso login.
    webview = _launch_notify_webview(profile, "127.0.0.1")
    if webview is None:
        # Fallback to AppleScript dialog + separate webview
        log.info(f"showing notification dialog for {profile}")
        action = show_notification(profile)
        log.info(f"dialog result: {action}")
        if action == "refresh":
            rc = run_aws_sso_login(profile)
            return "success" if rc == 0 else "failed"
        elif action.startswith("snooze:"):
            return action
        elif action == "suppress":
            return "suppress"
        return "dismiss"

    aws_proc = None
    let_webview_self_close = False
    try:
        # Wait for user action from webview stdout
        log.info(f"notify webview launched for {profile}")
        action_line = None
        for line in webview.stdout:
            stripped = line.strip()
            if stripped.startswith("SSO_ACTION:"):
                action_line = stripped
                break
            elif stripped in ("SSO_WINDOW_CLOSED", "SSO_TIMEOUT") or stripped.startswith("SSO_ERROR"):
                log.info(f"webview: {stripped}")
                return "dismiss"

        if not action_line:
            log.info("webview closed without action")
            return "dismiss"

        action = action_line[len("SSO_ACTION:"):]
        log.info(f"webview action: {action}")

        if action.startswith("snooze:"):
            seconds = action[len("snooze:"):]
            return f"snooze:{seconds}"
        elif action == "suppress":
            return "suppress"
        elif action != "refresh":
            return "dismiss"

        # User clicked Refresh — start aws sso login --no-browser
        cmd = ["aws", "sso", "login", "--no-browser", "--profile", profile]
        log.info(f"running: {' '.join(cmd)}")

        # Snapshot token file mtime to detect when aws writes new tokens
        token_mtime_before = _get_token_cache_mtime(profile)

        aws_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        # Extract authorize URL from aws output
        url = _extract_authorize_url(aws_proc)
        if not url:
            log.info("failed to extract authorize URL")
            aws_proc.kill()
            aws_proc.wait()
            # Tell webview there's an error by closing its stdin
            try:
                webview.stdin.close()
            except Exception:
                pass
            return "failed"

        # Send URL to webview via stdin
        log.info(f"authorize URL obtained, sending to webview")
        try:
            webview.stdin.write(url + "\n")
            webview.stdin.flush()
        except Exception as e:
            log.info(f"failed to send URL to webview: {e}")
            aws_proc.kill()
            aws_proc.wait()
            return "failed"
        finally:
            try:
                webview.stdin.close()
            except Exception:
                pass

        # Now poll aws process for completion, same as run_aws_sso_login
        deadline = time.time() + SSO_LOGIN_TIMEOUT
        last_token_check = 0
        while True:
            rc = aws_proc.poll()
            if rc is not None:
                output = aws_proc.stdout.read() if aws_proc.stdout else ""
                if output.strip():
                    print(output.strip(), flush=True)
                if rc == 0:
                    let_webview_self_close = True
                return "success" if rc == 0 else "failed"

            # Check if webview exited (user closed window during auth)
            if webview.poll() is not None:
                log.info("webview closed during auth")
                try:
                    aws_proc.wait(timeout=WEBVIEW_CLOSE_GRACE_SECONDS)
                    return "success" if aws_proc.returncode == 0 else "failed"
                except subprocess.TimeoutExpired:
                    aws_proc.kill()
                    aws_proc.wait()
                    return "dismiss"

            # Secondary exit: detect that aws wrote fresh tokens to cache.
            # Uses mtime comparison instead of STS to avoid false positives
            # where an old access token passes but refresh token is stale.
            now = time.time()
            if now - last_token_check >= CRED_CHECK_INTERVAL_SECONDS:
                last_token_check = now
                current_mtime = _get_token_cache_mtime(profile)
                if current_mtime > token_mtime_before:
                    log.info("token file updated, giving aws time to finish...")
                    let_webview_self_close = True
                    try:
                        aws_proc.wait(timeout=WEBVIEW_CLOSE_GRACE_SECONDS)
                        log.info("aws finished normally after token update")
                        return "success" if aws_proc.returncode == 0 else "failed"
                    except subprocess.TimeoutExpired:
                        log.info("aws did not finish in grace period, killing")
                        aws_proc.kill()
                        aws_proc.wait()
                        return "success"

            if time.time() > deadline:
                log.info(f"aws sso login timed out after {SSO_LOGIN_TIMEOUT}s")
                aws_proc.kill()
                aws_proc.wait()
                return "failed"

            time.sleep(0.5)

    finally:
        # Clean up: kill webview and aws process if still running
        if aws_proc and aws_proc.poll() is None:
            aws_proc.kill()
            aws_proc.wait()
        if not let_webview_self_close and webview.poll() is None:
            _kill_webview()
        try:
            webview.stdin.close()
        except Exception:
            pass
        try:
            webview.stdout.close()
        except Exception:
            pass
        if hasattr(webview, 'cleanup'):
            webview.cleanup()


def handle_login(profile: str) -> str:
    """
    Handle the login flow based on configured mode.

    All modes except standalone try silent token refresh first.
    If silent refresh succeeds, returns "success" without any user
    interaction. Otherwise falls back to mode-specific behavior.

    Modes:
    - notify: silent refresh → webview notification → auth (all-in-one)
    - auto: silent refresh → webview auth (no notification)
    - silent: silent refresh only, no webview
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

    # Silent mode: no webview fallback
    if mode == "silent":
        log.info("silent refresh failed, no browser fallback in silent mode")
        return "failed"

    if mode == "notify":
        return _run_notify_login(profile)

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
            log.warning(f"aws-cli {version_str} < {'.'.join(map(str, MIN_AWS_CLI_VERSION))} "
                       f"— silent refresh requires >= {'.'.join(map(str, MIN_AWS_CLI_VERSION))}")
        else:
            log.info(f"aws-cli {version_str}")
    except FileNotFoundError:
        log.warning("aws CLI not found — install with: brew install awscli")
    except Exception as e:
        log.warning(f"could not check aws version: {e}")


def main() -> int:
    """Main watch loop."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    _check_aws_cli()
    log.info(f"watching {SIGNAL_FILE} (profile={PROFILE})")
    log.info(f"mode={read_mode()}, cooldown={COOLDOWN_SECONDS}s, poll={POLL_SECONDS}s")
    if PROACTIVE_REFRESH_MINUTES > 0:
        log.info(f"proactive refresh: {PROACTIVE_REFRESH_MINUTES}min before expiry")

    # Clear stale cooldown from previous run so we respond to signals immediately
    _clear_cooldown()

    last_proactive_check: float = 0
    # Check token expiry every 60s (not every poll cycle)
    proactive_check_interval = 60
    proactive_failures = 0
    max_proactive_failures = 3  # after N failures, write signal and stop proactive

    last_update_check: float = 0

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
                    and proactive_failures < max_proactive_failures
                    and time.time() - last_proactive_check >= proactive_check_interval):
                last_proactive_check = time.time()
                if check_token_near_expiry(PROFILE, PROACTIVE_REFRESH_MINUTES):
                    log.info("proactive: token near expiry, attempting silent refresh")
                    if try_silent_refresh(PROFILE):
                        log.info("proactive: token refreshed successfully")
                        proactive_failures = 0
                    else:
                        proactive_failures += 1
                        if proactive_failures >= max_proactive_failures:
                            log.info(f"proactive: {proactive_failures} consecutive failures, writing signal for interactive login")
                            write_signal(PROFILE, "proactive silent refresh exhausted")
                        else:
                            log.info(f"proactive: silent refresh failed ({proactive_failures}/{max_proactive_failures})")

            # Periodic update check (every 12h, non-blocking)
            if (REPO_PATH
                    and time.time() - last_update_check >= UPDATE_CHECK_INTERVAL):
                last_update_check = time.time()
                check_for_updates()

            if should_trigger_login():
                if not try_acquire_lock():
                    # Another watcher instance is handling it
                    log.info("lock held by another instance")
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
                        proactive_failures = 0  # reset so proactive refresh works again
                        # Reset proactive timer so it doesn't immediately re-check
                        # the token that was just refreshed via interactive login
                        last_proactive_check = time.time()
                        log.info("login successful, signal cleared")
                    elif result.startswith("snooze:"):
                        try:
                            seconds = int(result.split(":")[1])
                        except (ValueError, IndexError):
                            log.info(f"bad snooze value: {result}, using {DEFAULT_SNOOZE_SECONDS}s")
                            seconds = DEFAULT_SNOOZE_SECONDS
                        update_signal_snooze(seconds)
                        log.info(f"snoozed, next attempt in {seconds}s")
                    elif result == "suppress":
                        write_last_run(time.time())
                        clear_signal()
                        log.info("reminders suppressed, signal cleared")
                    elif result == "dismiss":
                        # User saw dialog but dismissed — cooldown to avoid popup spam
                        write_last_run(time.time())
                        log.info("dismissed, retry after cooldown")
                    elif result == "failed":
                        # Login may have timed out but auth could have succeeded
                        # (e.g. post-sleep: webview completed but aws CLI hung).
                        # Check credentials before retrying.
                        if _check_credentials_valid(profile):
                            write_last_run(time.time())
                            clear_signal()
                            log.info("login reported failed but credentials valid, signal cleared")
                        else:
                            log.info(f"login failed, will retry in {AUTO_RETRY_SNOOZE_SECONDS}s")
                            update_signal_snooze(AUTO_RETRY_SNOOZE_SECONDS)
                    else:
                        log.info(f"unexpected result: {result}")

                finally:
                    release_lock()

            time.sleep(POLL_SECONDS)

        except KeyboardInterrupt:
            log.info("exiting")
            return 0
        except Exception as e:
            # Don't crash the launchd loop; log and continue
            log.error(f"error: {e}")
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
