#!/usr/bin/env python3
"""Interactive first-time setup for bazel-aws-maven-proxy.

Checks prerequisites, configures .env, installs tools, starts services.
All external dependencies are injected via SetupContext for testability.
"""

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class CmdResult:
    """Result of running an external command."""
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass
class ToolInfo:
    """Info about a detected tool."""
    name: str
    version: str = ""
    path: str = ""


@dataclass
class PrereqResult:
    """Aggregate result of prerequisite checks."""
    mise: Optional[ToolInfo] = None
    aws: Optional[ToolInfo] = None
    container: Optional[ToolInfo] = None
    swiftc: Optional[ToolInfo] = None
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0


@dataclass
class EnvConfig:
    """User-provided .env configuration."""
    aws_profile: str = "default"
    aws_region: str = "sa-east-1"
    s3_bucket: str = "your-maven-bucket"
    proxy_port: str = "8888"
    sso_mode: str = "notify"
    skip_tls_verify: bool = False


@dataclass
class SsoCheckResult:
    """Result of SSO configuration check."""
    configured: bool = False
    style: str = ""  # "modern", "legacy", "none"
    session_name: str = ""


# ---------------------------------------------------------------------------
# ANSI helpers
# ---------------------------------------------------------------------------
BOLD = "\033[1m"
GREEN = "\033[0;32m"
YELLOW = "\033[0;33m"
RED = "\033[0;31m"
NC = "\033[0m"

VALID_SSO_MODES = {"notify", "auto", "silent", "standalone"}

MIN_AWS_MAJOR = 2
MIN_AWS_MINOR = 9


# ---------------------------------------------------------------------------
# SetupContext — injectable dependencies
# ---------------------------------------------------------------------------

class SetupContext:
    """Dependency injection container for setup.

    All external interactions go through this context:
    - run_cmd: execute shell commands
    - prompt: ask user for input
    - confirm: ask yes/no question
    - print_fn: output text
    - file_exists / read_file / write_file / mkdir: filesystem ops
    - env: environment variables
    - repo_root: project root directory
    """

    def __init__(
        self,
        repo_root: Optional[Path] = None,
        env: Optional[dict] = None,
    ):
        self.repo_root = repo_root or Path.cwd()
        self.env = env if env is not None else dict(os.environ)
        self._output: list[str] = []

    # -- Command execution --

    def run_cmd(self, cmd: list[str], timeout: int = 120,
                capture: bool = True, interactive: bool = False) -> CmdResult:
        """Run a command. Override in tests."""
        import subprocess
        try:
            if interactive:
                r = subprocess.run(cmd, timeout=timeout)
                return CmdResult(r.returncode)
            r = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout
            )
            return CmdResult(r.returncode, r.stdout, r.stderr)
        except FileNotFoundError:
            return CmdResult(-1, "", f"command not found: {cmd[0]}")
        except subprocess.TimeoutExpired:
            return CmdResult(-1, "", "timeout")

    def which(self, name: str) -> Optional[str]:
        """Check if a command exists. Returns path or None."""
        import shutil
        return shutil.which(name)

    # -- User interaction --

    def prompt(self, text: str, default: str = "") -> str:
        """Prompt user for text input. Override in tests."""
        try:
            value = input(f"  {text} [{default}]: ")
            return value.strip() or default
        except EOFError:
            return default

    def confirm(self, text: str, default: bool = True) -> bool:
        """Ask yes/no question. Override in tests."""
        suffix = "[Y/n]" if default else "[y/N]"
        try:
            value = input(f"  {text} {suffix}: ").strip().lower()
        except EOFError:
            return default
        if not value:
            return default
        return value in ("y", "yes")

    def confirm_three_way(self, text: str) -> str:
        """Ask Y/n/s question. Returns 'yes', 'no', or 'skip'."""
        try:
            value = input(f"  {text} [Y/n/s(kip)]: ").strip().lower()
        except EOFError:
            return "yes"
        if not value:
            return "yes"
        if value in ("s", "skip"):
            return "skip"
        if value in ("n", "no"):
            return "no"
        return "yes"

    def choose(self, items: list[str], label: str = "Choice") -> int:
        """Show numbered list, return 0-based index. Returns 0 on bad input."""
        for i, item in enumerate(items, 1):
            self.print(f"    {i}) {item}")
        try:
            value = input(f"  {label} [1]: ").strip()
        except EOFError:
            return 0
        if not value:
            return 0
        try:
            idx = int(value) - 1
            return idx if 0 <= idx < len(items) else 0
        except ValueError:
            return 0

    # -- Output --

    def print(self, msg: str = "") -> None:
        """Print a message. Override in tests to capture output."""
        print(msg)
        self._output.append(msg)

    def ok(self, msg: str) -> None:
        self.print(f"  {GREEN}✓{NC} {msg}")

    def warn(self, msg: str) -> None:
        self.print(f"  {YELLOW}⚠{NC} {msg}")

    def fail(self, msg: str) -> None:
        self.print(f"  {RED}✗{NC} {msg}")

    def header(self, msg: str) -> None:
        self.print(f"{BOLD}{msg}{NC}")

    # -- Filesystem --

    def file_exists(self, path: Path) -> bool:
        return path.exists()

    def read_file(self, path: Path) -> str:
        return path.read_text()

    def write_file(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def glob_files(self, pattern: str) -> list[str]:
        """Return file paths matching a glob pattern. Override in tests."""
        import glob as _glob
        return _glob.glob(pattern)

    def remove_file(self, path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def mkdir(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)

    def get_output(self) -> list[str]:
        """Return all output lines (for testing)."""
        return list(self._output)


# ---------------------------------------------------------------------------
# Phase 1: Check prerequisites
# ---------------------------------------------------------------------------

def parse_aws_version(version_output: str) -> Optional[str]:
    """Extract version from 'aws-cli/2.15.0 ...' output."""
    m = re.search(r"aws-cli/(\d+\.\d+\.\d+)", version_output)
    return m.group(1) if m else None


def check_aws_version(version: str) -> bool:
    """Return True if version >= MIN_AWS_MAJOR.MIN_AWS_MINOR."""
    parts = version.split(".")
    if len(parts) < 2:
        return False
    try:
        major, minor = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    if major > MIN_AWS_MAJOR:
        return True
    if major == MIN_AWS_MAJOR and minor >= MIN_AWS_MINOR:
        return True
    return False


def check_prerequisites(ctx: SetupContext) -> PrereqResult:
    """Phase 1: Check all required tools are installed."""
    result = PrereqResult()

    # mise
    if ctx.which("mise"):
        r = ctx.run_cmd(["mise", "--version"])
        ver = r.stdout.strip().split("\n")[0] if r.ok else "unknown"
        result.mise = ToolInfo("mise", ver)
        ctx.ok(f"mise {ver}")
    else:
        ctx.fail("mise not found — install with: brew install mise")
        result.errors.append("mise not found")

    # aws CLI
    if ctx.which("aws"):
        r = ctx.run_cmd(["aws", "--version"])
        raw = r.stdout.strip() or r.stderr.strip()
        version = parse_aws_version(raw)
        if version and check_aws_version(version):
            result.aws = ToolInfo("aws", version)
            ctx.ok(f"aws-cli {version}")
        elif version:
            ctx.fail(f"aws-cli {version} too old (need >= {MIN_AWS_MAJOR}.{MIN_AWS_MINOR}) — brew upgrade awscli")
            result.errors.append(f"aws-cli {version} too old")
        else:
            ctx.fail("aws CLI version could not be determined")
            result.errors.append("aws version unknown")
    else:
        ctx.fail("aws CLI not found — install with: brew install awscli")
        result.errors.append("aws not found")

    # Container engine (podman preferred)
    if ctx.which("podman"):
        r = ctx.run_cmd(["podman", "--version"])
        ver = r.stdout.strip().split()[-1] if r.ok else "unknown"
        result.container = ToolInfo("podman", ver)
        ctx.ok(f"podman {ver}")
    elif ctx.which("docker"):
        r = ctx.run_cmd(["docker", "--version"])
        ver = r.stdout.strip().split()[-1].rstrip(",") if r.ok else "unknown"
        result.container = ToolInfo("docker", ver)
        ctx.ok(f"docker {ver}")
    else:
        ctx.fail("No container engine — install podman (preferred) or docker")
        result.errors.append("no container engine")

    # swiftc (optional — needed for sandboxed webview login)
    if ctx.which("swiftc"):
        result.swiftc = ToolInfo("swiftc")
        ctx.ok("swiftc (Xcode CLT)")
    else:
        ctx.warn("swiftc not found — SSO login will fall back to browser (slower, no cookie caching)")
        choice = ctx.confirm_three_way(
            "Install Xcode Command Line Tools now? (yes/no/skip)"
        )
        if choice == "yes":
            r = ctx.run_cmd(["xcode-select", "--install"], timeout=300)
            if r.ok:
                ctx.ok("Xcode CLT install started — re-run setup after it completes")
                result.warnings.append("xcode-clt installing")
            else:
                ctx.warn("xcode-select --install failed — install manually")
                result.warnings.append("swiftc not found")
        else:
            ctx.print("       SSO login will use browser instead of webview")
            result.warnings.append("swiftc not found")

    return result


# ---------------------------------------------------------------------------
# Phase 2: Configure .env
# ---------------------------------------------------------------------------

def list_aws_profiles(ctx: SetupContext) -> list[str]:
    """Read profile names from ~/.aws/config."""
    config_path = Path.home() / ".aws" / "config"
    if not ctx.file_exists(config_path):
        return []
    try:
        content = ctx.read_file(config_path)
    except (OSError, PermissionError):
        return []
    return re.findall(r"^\[profile\s+(.+?)\]", content, re.MULTILINE)


def prompt_env_config(ctx: SetupContext) -> EnvConfig:
    """Interactively prompt for .env values."""
    profiles = list_aws_profiles(ctx)
    if profiles:
        ctx.print("  Available AWS profiles:")
        for p in profiles:
            ctx.print(f"    - {p}")
        ctx.print("")

    config = EnvConfig()
    config.aws_profile = ctx.prompt("AWS CLI profile", config.aws_profile)
    config.aws_region = ctx.prompt("AWS region", config.aws_region)
    config.s3_bucket = ctx.prompt("S3 bucket name", config.s3_bucket)
    config.proxy_port = ctx.prompt("Local proxy port", config.proxy_port)

    ctx.print("  SSO login modes:")
    ctx.print("    notify     — asks before opening SSO login (default)")
    ctx.print("    auto       — automatically opens SSO login when needed")
    ctx.print("    silent     — background token refresh only, no UI")
    ctx.print("    standalone — manual only (mise run sso-login)")

    config.sso_mode = ctx.prompt("SSO login mode", config.sso_mode)
    if config.sso_mode not in VALID_SSO_MODES:
        ctx.warn(f"Invalid mode '{config.sso_mode}', defaulting to 'notify'")
        config.sso_mode = "notify"

    ctx.print("")
    ctx.print("  Corporate proxy / TLS:")
    ctx.print("    If your network proxy intercepts HTTPS and replaces certificates,")
    ctx.print("    container image pulls may fail with x509 certificate errors.")
    ctx.print("    Enabling this sets --tls-verify=false for podman.")
    config.skip_tls_verify = ctx.confirm(
        "Skip TLS verification for container pulls? (corporate proxy with custom certs)",
        default=False,
    )

    return config


def generate_env_content(config: EnvConfig) -> str:
    """Generate .env file content from config."""
    skip_tls_line = (
        "SKIP_TLS_VERIFY=true"
        if config.skip_tls_verify
        else "# SKIP_TLS_VERIFY=false"
    )
    return f"""\
# AWS Configuration
AWS_PROFILE="{config.aws_profile}"
AWS_REGION="{config.aws_region}"

# S3 Bucket Information
S3_BUCKET_NAME="{config.s3_bucket}"

# Proxy Configuration
PROXY_PORT="{config.proxy_port}"
REFRESH_INTERVAL=60000
LOG_LEVEL=info

# SSO Monitor Configuration
CHECK_INTERVAL=60

# SSO Watcher Configuration (macOS launchd)
SSO_COOLDOWN_SECONDS=600
SSO_POLL_SECONDS=5
SSO_LOGIN_MODE="{config.sso_mode}"
SSO_PROACTIVE_REFRESH_MINUTES=30

# Container Engine (auto-detect if unset)
# CONTAINER_ENGINE=podman

# Corporate Proxy / TLS
# Set to true if behind a proxy that intercepts HTTPS and replaces certificates.
# Enables --tls-verify=false for podman. For Docker, configure daemon-level CA trust.
{skip_tls_line}
"""


def configure_env(ctx: SetupContext) -> EnvConfig:
    """Phase 2: Configure .env file. Returns the active config."""
    ctx.header("Configuring .env...")

    env_path = ctx.repo_root / ".env"
    write_env = True

    if ctx.file_exists(env_path):
        ctx.print("  .env already exists.")
        if not ctx.confirm("Overwrite with fresh config?", default=False):
            ctx.print("  Keeping existing .env")
            write_env = False

    config = EnvConfig()
    if write_env:
        ctx.print("")
        config = prompt_env_config(ctx)
        content = generate_env_content(config)
        ctx.write_file(env_path, content)
        ctx.ok("Wrote .env")
    else:
        # Parse existing .env
        config = parse_existing_env(ctx, env_path)

    ctx.print("")
    return config


def parse_existing_env(ctx: SetupContext, path: Path) -> EnvConfig:
    """Parse an existing .env file into EnvConfig."""
    config = EnvConfig()
    try:
        content = ctx.read_file(path)
    except (OSError, PermissionError):
        return config

    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key == "AWS_PROFILE":
            config.aws_profile = val
        elif key == "AWS_REGION":
            config.aws_region = val
        elif key == "S3_BUCKET_NAME":
            config.s3_bucket = val
        elif key == "PROXY_PORT":
            config.proxy_port = val
        elif key == "SSO_LOGIN_MODE":
            config.sso_mode = val
        elif key == "SKIP_TLS_VERIFY":
            config.skip_tls_verify = val.lower() in ("true", "1", "yes")

    return config


# ---------------------------------------------------------------------------
# Phase 3: Install tools via mise
# ---------------------------------------------------------------------------

def install_tools(ctx: SetupContext) -> bool:
    """Phase 3: Run mise install. Returns True on success."""
    ctx.header("Installing tools via mise...")
    r = ctx.run_cmd(["mise", "install", "--yes"], timeout=300)
    if not r.ok:
        ctx.warn("mise install failed — you may need to run 'mise install' manually")
        return False

    py = ctx.run_cmd(["python3", "--version"])
    ver = py.stdout.strip().split()[-1] if py.ok else "unknown"
    ctx.ok(f"Python {ver}")
    ctx.print("")
    return True


# ---------------------------------------------------------------------------
# Phase 4: Install SSO watcher (webview build + launchd)
# ---------------------------------------------------------------------------

def install_sso_watcher(ctx: SetupContext) -> bool:
    """Phase 4: Run mise run sso-install. Returns True on success."""
    ctx.header("Installing SSO watcher...")
    r = ctx.run_cmd(["mise", "run", "sso-install"], timeout=120)
    if not r.ok:
        ctx.warn("SSO watcher install failed — run 'mise run sso-install' manually")
        ctx.print("")
        return False
    ctx.print("")
    return True


# ---------------------------------------------------------------------------
# Phase 5: macOS permission pre-flight
# ---------------------------------------------------------------------------

def is_gui_session(ctx: SetupContext) -> bool:
    """Detect if running in a GUI session (not SSH/headless)."""
    if ctx.env.get("DISPLAY") or ctx.env.get("TERM_PROGRAM"):
        return True
    r = ctx.run_cmd(["pgrep", "-q", "WindowServer"])
    return r.ok


PERMISSION_TIMEOUT = 60  # seconds — user should be at the machine during setup


def check_macos_permissions(ctx: SetupContext) -> dict:
    """Phase 5: Check System Events and dialog permissions.

    Returns dict with 'system_events', 'dialog', 'skipped', 'failed' fields.
    Fails hard on denial or timeout — SSO watcher can't function without these.
    """
    ctx.header("Checking macOS permissions...")

    result = {"system_events": False, "dialog": False, "skipped": False, "failed": False}

    if not is_gui_session(ctx):
        ctx.warn("No GUI session detected (SSH?) — skipping permission pre-flight")
        result["skipped"] = True
        ctx.print("")
        return result

    ctx.print("  If prompted, grant permissions — these are needed for SSO login dialogs.")
    ctx.print("")

    # System Events access — needed for SSO watcher notifications
    r = ctx.run_cmd([
        "osascript", "-e",
        'tell application "System Events" to return name of current user'
    ], timeout=PERMISSION_TIMEOUT)
    if r.ok:
        ctx.ok("System Events access")
        result["system_events"] = True
    elif "timeout" in r.stderr:
        ctx.fail("System Events permission timed out")
        ctx.print("       Re-run setup and grant when prompted")
        result["failed"] = True
        ctx.print("")
        return result
    else:
        ctx.fail("System Events access denied")
        ctx.print("       Grant in: System Settings → Privacy & Security → Automation")
        result["failed"] = True
        ctx.print("")
        return result

    # Dialog display — verifies the app can show UI (non-fatal)
    r = ctx.run_cmd([
        "osascript", "-e",
        'display dialog "Setup complete — SSO watcher permissions verified." '
        'buttons {"OK"} default button "OK"'
    ], timeout=PERMISSION_TIMEOUT)
    if r.ok:
        ctx.ok("Dialog permissions")
        result["dialog"] = True
    elif "timeout" in r.stderr:
        ctx.warn("Dialog permission timed out — SSO login dialogs may not appear")
        ctx.print("       You can grant later in: System Settings → Privacy & Security → Automation")
    else:
        ctx.warn("Dialog display denied — SSO login dialogs may not appear")
        ctx.print("       Grant in: System Settings → Privacy & Security → Automation")

    ctx.print("")
    return result


# ---------------------------------------------------------------------------
# Phase 6: Check/configure AWS SSO
# ---------------------------------------------------------------------------

def check_sso_configuration(ctx: SetupContext, profile: str) -> SsoCheckResult:
    """Check if AWS profile has SSO configured."""
    # Modern: sso_session
    r = ctx.run_cmd(["aws", "configure", "get", "sso_session", "--profile", profile])
    if r.ok and r.stdout.strip():
        return SsoCheckResult(True, "modern", r.stdout.strip())

    # Legacy: sso_account_id
    r = ctx.run_cmd(["aws", "configure", "get", "sso_account_id", "--profile", profile])
    if r.ok and r.stdout.strip():
        return SsoCheckResult(True, "legacy")

    return SsoCheckResult(False, "none")


def _read_aws_config(ctx: SetupContext) -> str:
    """Read ~/.aws/config content, return empty string if missing."""
    config_path = Path.home() / ".aws" / "config"
    if not ctx.file_exists(config_path):
        return ""
    try:
        return ctx.read_file(config_path)
    except (OSError, PermissionError):
        return ""


def _write_sso_config(
    ctx: SetupContext,
    profile: str,
    session_name: str,
    start_url: str,
    sso_region: str,
    account_id: str,
    role_name: str,
) -> bool:
    """Append SSO profile + session to ~/.aws/config.

    Writes modern sso-session style config with sso_registration_scopes.
    Returns True on success.
    """
    config_path = Path.home() / ".aws" / "config"

    existing = _read_aws_config(ctx)

    # Check for existing sections to avoid duplicates
    if re.search(rf"^\[profile\s+{re.escape(profile)}\]", existing, re.MULTILINE):
        ctx.warn(f"Profile '{profile}' already exists in config — not overwriting")
        return False
    if re.search(rf"^\[sso-session\s+{re.escape(session_name)}\]", existing, re.MULTILINE):
        ctx.warn(f"Session '{session_name}' already exists in config — not overwriting")
        return False

    # Build new sections
    new_sections = f"""
[profile {profile}]
sso_account_id = {account_id}
sso_role_name = {role_name}
sso_session = {session_name}

[sso-session {session_name}]
sso_start_url = {start_url}
sso_region = {sso_region}
sso_registration_scopes = sso:account:access
"""

    # Ensure existing content ends with newline
    if existing and not existing.endswith("\n"):
        existing += "\n"

    ctx.write_file(config_path, existing + new_sections)
    return True


def _find_sso_access_token(ctx: SetupContext, start_url: str) -> str:
    """Find a valid SSO access token from ~/.aws/sso/cache/ matching start_url."""
    import json as _json
    cache_dir = Path.home() / ".aws" / "sso" / "cache"
    pattern = str(cache_dir / "*.json")

    best_token = ""
    best_expiry = ""

    try:
        for path_str in ctx.glob_files(pattern):
            try:
                content = ctx.read_file(Path(path_str))
                data = _json.loads(content)
                token = data.get("accessToken", "")
                url = data.get("startUrl", "")
                expiry = data.get("expiresAt", "")
                if token and url and start_url.rstrip("/#") in url.rstrip("/#"):
                    if expiry > best_expiry:
                        best_token = token
                        best_expiry = expiry
            except (OSError, _json.JSONDecodeError, KeyError):
                continue
    except Exception:
        pass

    return best_token


def _sso_list_accounts(ctx: SetupContext, token: str, sso_region: str,
                       profile: str = "default") -> list[dict]:
    """Call aws sso list-accounts, return list of {accountId, accountName}.

    Explicit --profile avoids inheriting AWS_PROFILE from env (which may
    point to a profile that doesn't exist yet during setup).
    """
    import json as _json
    r = ctx.run_cmd([
        "aws", "sso", "list-accounts",
        "--access-token", token,
        "--region", sso_region,
        "--profile", profile,
    ])
    if not r.ok:
        detail = (r.stderr or r.stdout or "").strip()
        if detail:
            ctx.print(f"  list-accounts error: {detail[:200]}")
        return []
    try:
        data = _json.loads(r.stdout)
        return data.get("accountList", [])
    except (ValueError, KeyError):
        return []


def _sso_list_roles(ctx: SetupContext, token: str, account_id: str,
                    sso_region: str, profile: str = "default") -> list[dict]:
    """Call aws sso list-account-roles, return list of {roleName, accountId}.

    Explicit --profile avoids inheriting AWS_PROFILE from env.
    """
    import json as _json
    r = ctx.run_cmd([
        "aws", "sso", "list-account-roles",
        "--access-token", token,
        "--account-id", account_id,
        "--region", sso_region,
        "--profile", profile,
    ])
    if not r.ok:
        detail = (r.stderr or r.stdout or "").strip()
        if detail:
            ctx.print(f"  list-roles error: {detail[:200]}")
        return []
    try:
        data = _json.loads(r.stdout)
        return data.get("roleList", [])
    except (ValueError, KeyError):
        return []


def _clear_sso_cache(ctx: SetupContext) -> None:
    """Remove all cached OIDC files to force fresh registration and login.

    Clears both client registrations (stale device-code grants from
    CLI < 2.22.0) and access tokens (which may lack sso:account:access
    scope needed for list-accounts/list-roles). The fresh login with
    our temp profile (which has sso_registration_scopes) creates a
    properly scoped token.
    """
    cache_dir = Path.home() / ".aws" / "sso" / "cache"
    pattern = str(cache_dir / "*.json")
    for path_str in ctx.glob_files(pattern):
        ctx.remove_file(Path(path_str))


def _discover_account_and_role(
    ctx: SetupContext,
    profile: str,
    start_url: str,
    sso_region: str,
) -> tuple[str, str]:
    """Login to SSO, list accounts/roles, let user pick.

    Clears stale OIDC cache first to ensure PKCE flow (not device-code).
    Returns (account_id, role_name) or ("", "") on failure.
    """
    # Clear SSO cache — stale registrations (device-code) and tokens
    # (missing sso:account:access scope) both cause discover to fail
    _clear_sso_cache(ctx)

    # Write temporary sso-session for login
    session_name = f"{profile}-setup-tmp"
    config_path = Path.home() / ".aws" / "config"
    existing = _read_aws_config(ctx)

    tmp_section = f"""
[profile {session_name}]
sso_session = {session_name}

[sso-session {session_name}]
sso_start_url = {start_url}
sso_region = {sso_region}
sso_registration_scopes = sso:account:access
"""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    ctx.write_file(config_path, existing + tmp_section)

    try:
        ctx.print("")
        ctx.print("  Logging in to SSO to discover accounts and roles...")
        ctx.print("  The sandboxed webview will open for authentication.")
        ctx.print("")

        if not do_sso_login(ctx, session_name):
            ctx.warn("SSO login failed — falling back to manual entry")
            return ("", "")

        # Find access token from cache
        token = _find_sso_access_token(ctx, start_url)
        if not token:
            # Diagnostic: list cache files so user can report what's there
            cache_dir = Path.home() / ".aws" / "sso" / "cache"
            files = ctx.glob_files(str(cache_dir / "*.json"))
            ctx.warn(f"Could not find SSO token after login (cache files: {len(files)})")
            ctx.warn("Falling back to manual entry")
            return ("", "")

        # List accounts (use temp profile to avoid AWS_PROFILE env interference)
        accounts = _sso_list_accounts(ctx, token, sso_region, profile=session_name)
        if not accounts:
            ctx.warn("No accounts found — falling back to manual entry")
            return ("", "")

        ctx.print("")
        ctx.print("  Available accounts:")
        account_labels = [
            f"{a['accountName']} ({a['accountId']})" for a in accounts
        ]
        idx = ctx.choose(account_labels, "Select account")
        account = accounts[idx]
        account_id = account["accountId"]
        ctx.ok(f"Account: {account['accountName']} ({account_id})")

        # List roles
        roles = _sso_list_roles(ctx, token, account_id, sso_region, profile=session_name)
        if not roles:
            ctx.warn("No roles found — falling back to manual entry")
            return (account_id, "")

        if len(roles) == 1:
            role_name = roles[0]["roleName"]
            ctx.ok(f"Role: {role_name} (only role available)")
        else:
            ctx.print("  Available roles:")
            role_labels = [r["roleName"] for r in roles]
            ridx = ctx.choose(role_labels, "Select role")
            role_name = roles[ridx]["roleName"]
            ctx.ok(f"Role: {role_name}")

        return (account_id, role_name)
    finally:
        _remove_temp_config(ctx, config_path, session_name)


def _remove_temp_config(ctx: SetupContext, config_path: Path, session_name: str) -> None:
    """Remove temporary sso-session sections from config."""
    try:
        content = ctx.read_file(config_path)
    except (OSError, PermissionError):
        return
    # Remove [profile session_name] and [sso-session session_name] blocks
    cleaned = re.sub(
        rf"\n?\[profile {re.escape(session_name)}\][^\[]*", "", content
    )
    cleaned = re.sub(
        rf"\n?\[sso-session {re.escape(session_name)}\][^\[]*", "", cleaned
    )
    ctx.write_file(config_path, cleaned)


def configure_sso(ctx: SetupContext, profile: str) -> bool:
    """Phase 6: Check SSO config, optionally configure it.

    Prompts for start URL + region, then logs in to SSO and auto-discovers
    accounts and roles. Falls back to manual entry if login fails.
    Writes directly to ~/.aws/config with modern sso-session style.

    Returns True if SSO is configured (pre-existing or newly configured).
    """
    ctx.header("Checking AWS SSO configuration...")

    check = check_sso_configuration(ctx, profile)

    if check.configured:
        if check.style == "modern":
            ctx.ok(f"Profile '{profile}' uses sso-session '{check.session_name}'")
        else:
            ctx.ok(f"Profile '{profile}' has SSO configured (legacy style)")
        ctx.print("")
        return True

    ctx.warn(f"Profile '{profile}' has no SSO configured")
    ctx.print("")

    answer = ctx.confirm_three_way("Configure SSO now?")
    if answer == "skip":
        ctx.print("  Skipping SSO configuration.")
        ctx.print("")
        return False
    if answer == "no":
        ctx.print("  Skipping. Run setup again to configure later.")
        ctx.print("")
        return False

    # Prompt for SSO start URL and region (visible on AWS portal page)
    ctx.print("")
    ctx.print("  You can find these values on your AWS access portal page.")
    ctx.print("")

    start_url = ctx.prompt("SSO start URL", "https://your-org.awsapps.com/start")
    sso_region = ctx.prompt("SSO region", "us-east-1")

    # Try auto-discover: login → list accounts → list roles
    account_id, role_name = _discover_account_and_role(ctx, profile, start_url, sso_region)

    # Fall back to manual entry for missing values
    if not account_id:
        ctx.print("")
        ctx.print("  Enter account ID and role name manually:")
        account_id = ctx.prompt("AWS account ID", "")
    if not role_name:
        if account_id:
            role_name = ctx.prompt("SSO role name", "")

    # Validate required fields
    if not account_id:
        ctx.warn("Account ID is required")
        ctx.print("")
        return False
    if not role_name:
        ctx.warn("Role name is required")
        ctx.print("")
        return False

    session_name = profile

    if _write_sso_config(ctx, profile, session_name, start_url, sso_region, account_id, role_name):
        ctx.ok(f"Wrote SSO config for profile '{profile}'")
        ctx.ok("sso_registration_scopes = sso:account:access (token refresh enabled)")
        ctx.print("")
        return True

    ctx.print("")
    return False


# ---------------------------------------------------------------------------
# Phase 7: First login + validate S3 access
# ---------------------------------------------------------------------------

def check_credentials_valid(ctx: SetupContext, profile: str) -> bool:
    """Check if AWS credentials are currently valid."""
    r = ctx.run_cmd(["aws", "sts", "get-caller-identity", "--profile", profile])
    return r.ok


def do_sso_login(ctx: SetupContext, profile: str) -> bool:
    """Perform SSO login using watcher's run_aws_sso_login.

    Returns True on success. Runs interactive (no capture) so errors
    and webview prompts are visible to the user.
    """
    repo = str(ctx.repo_root)
    r = ctx.run_cmd([
        "python3", "-c",
        f"import os, sys; sys.path.insert(0, os.path.join({repo!r}, 'sso-watcher')); "
        f"from watcher import run_aws_sso_login; "
        f"sys.exit(run_aws_sso_login({profile!r}))"
    ], interactive=True, timeout=180)
    return r.ok


def first_login_and_validate(ctx: SetupContext, config: EnvConfig,
                              sso_configured: bool) -> dict:
    """Phase 7: SSO login if needed, validate S3 access.

    Returns dict with 'credentials_valid', 'login_attempted', 'login_ok',
    's3_accessible' fields.
    """
    result = {
        "credentials_valid": False,
        "login_attempted": False,
        "login_ok": False,
        "s3_accessible": False,
        "s3_skipped": False,
        "skipped": False,
    }

    if not sso_configured:
        result["skipped"] = True
        return result

    profile = config.aws_profile

    # Check if credentials are already valid
    if check_credentials_valid(ctx, profile):
        ctx.ok(f"Credentials valid for profile '{profile}'")
        result["credentials_valid"] = True
    else:
        ctx.header("Initial SSO login...")
        ctx.print("  Uses the sandboxed webview to cache IdP credentials for faster future logins.")
        ctx.print("")

        # Pause watcher to prevent duplicate dialogs
        signal_dir = Path.home() / ".aws" / "sso-renewer"
        mode_file = signal_dir / "mode"
        saved_mode = None
        if ctx.file_exists(mode_file):
            try:
                saved_mode = ctx.read_file(mode_file).strip()
            except OSError:
                pass
        ctx.mkdir(signal_dir)
        ctx.write_file(mode_file, "standalone")

        try:
            result["login_attempted"] = True
            if do_sso_login(ctx, profile):
                ctx.ok("SSO login successful")
                result["login_ok"] = True
                result["credentials_valid"] = True
            else:
                ctx.warn("SSO login failed — run 'mise run sso-login' to retry")
        finally:
            # Restore watcher mode
            if saved_mode:
                ctx.write_file(mode_file, saved_mode)
            else:
                ctx.remove_file(mode_file)

    ctx.print("")

    # Validate S3 bucket access
    bucket = config.s3_bucket
    if not bucket or bucket == "your-maven-bucket":
        result["s3_skipped"] = True
        return result

    ctx.header("Validating S3 access...")
    r = ctx.run_cmd([
        "aws", "s3", "ls", f"s3://{bucket}/", "--profile", profile
    ])
    if r.ok:
        ctx.ok(f"Can access s3://{bucket}/")
        result["s3_accessible"] = True
    else:
        ctx.warn(f"Cannot access s3://{bucket}/ with profile '{profile}'")
        ctx.print("       Verify the profile has the correct role and permissions.")
    ctx.print("")

    return result


# ---------------------------------------------------------------------------
# Phase 8: Start containers
# ---------------------------------------------------------------------------

def start_containers(ctx: SetupContext) -> bool:
    """Phase 8: Optionally start containers. Returns True if started."""
    if not ctx.confirm("Start containers now?", default=True):
        return False

    r = ctx.run_cmd(["mise", "run", "containers:up"], timeout=300)
    if r.ok:
        ctx.print("")
        ctx.ok("Containers started")
        return True

    ctx.warn("Container start failed — run 'mise run containers:up' manually")
    return False


# ---------------------------------------------------------------------------
# Phase 9: Summary
# ---------------------------------------------------------------------------

def print_summary(ctx: SetupContext, config: EnvConfig) -> None:
    """Print setup completion summary."""
    port = config.proxy_port or "8888"
    ctx.print("")
    ctx.print(f"{BOLD}{GREEN}Setup complete!{NC}")
    ctx.print("")
    ctx.print(f"  Proxy:   http://localhost:{port}/")
    ctx.print("  Logs:    mise run containers:logs")
    ctx.print("  Watcher: mise run sso-status")
    ctx.print(f"  Health:  curl http://localhost:{port}/healthz")
    ctx.print("")


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def run_setup(ctx: SetupContext) -> int:
    """Run the full setup flow. Returns exit code (0=success, 1=failure)."""
    ctx.print(f"\n{BOLD}bazel-aws-maven-proxy setup{NC}\n")

    # Phase 1: Prerequisites
    ctx.header("Checking prerequisites...")
    prereqs = check_prerequisites(ctx)
    if not prereqs.ok:
        n = len(prereqs.errors)
        ctx.print(f"\n{RED}{n} prerequisite(s) missing. Fix the above and re-run.{NC}")
        return 1
    ctx.print("")

    # Phase 2: .env
    config = configure_env(ctx)

    # Phase 3: mise install
    install_tools(ctx)

    # Phase 4: SSO watcher
    install_sso_watcher(ctx)

    # Phase 5: macOS permissions
    perms = check_macos_permissions(ctx)
    if perms["failed"]:
        return 1

    # Phase 6: SSO configuration
    sso_configured = configure_sso(ctx, config.aws_profile)

    # Phase 7: Login + validate
    first_login_and_validate(ctx, config, sso_configured)

    # Phase 8: Containers
    start_containers(ctx)

    # Summary
    print_summary(ctx, config)

    return 0


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    try:
        ctx = SetupContext()
        sys.exit(run_setup(ctx))
    except KeyboardInterrupt:
        print(f"\n{YELLOW}Setup interrupted.{NC}")
        sys.exit(130)


if __name__ == "__main__":
    main()
