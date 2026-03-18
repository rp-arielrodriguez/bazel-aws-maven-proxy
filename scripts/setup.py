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


def _ensure_tty() -> None:
    """Reopen stdin from /dev/tty when piped (e.g. curl | bash).

    When running via ``curl -sL .../install.sh | bash``, stdin is the
    script content from the pipe.  Python's ``input()`` and Rich's
    ``Prompt.ask()`` both read from ``sys.stdin``, so they'd consume
    script lines instead of user input.

    This is the standard fix used by rustup, Homebrew, and similar
    installers that support ``curl | bash``.
    """
    if sys.stdin.isatty():
        return
    try:
        tty = open("/dev/tty", "r")  # noqa: SIM115
        sys.stdin = tty
    except OSError:
        pass  # no terminal (CI, headless SSH, etc.)


def _ensure_rich() -> bool:
    """Install Rich if missing. Returns True if available."""
    try:
        import rich  # noqa: F401
        return True
    except ImportError:
        pass
    import subprocess
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "rich"],
            capture_output=True, timeout=60,
        )
        import rich  # noqa: F401
        return True
    except Exception:
        return False


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
    aws_ca_bundle: str = ""


@dataclass
class SsoCheckResult:
    """Result of SSO configuration check."""
    configured: bool = False
    style: str = ""  # "modern", "legacy", "none"
    session_name: str = ""


# ---------------------------------------------------------------------------
# ANSI fallbacks (used when Rich is unavailable)
# ---------------------------------------------------------------------------
BOLD = "\033[1m"
GREEN = "\033[0;32m"
YELLOW = "\033[0;33m"
RED = "\033[0;31m"
NC = "\033[0m"

VALID_SSO_MODES = {"notify", "auto", "silent", "standalone"}

# Rich availability flag — set by _ensure_rich() in main()
_RICH_AVAILABLE = False

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

    When Rich is available, output uses styled markup and prompts
    use Rich.Prompt for a modern CLI experience. Falls back to plain
    ANSI when Rich is not installed.
    """

    def __init__(
        self,
        repo_root: Optional[Path] = None,
        env: Optional[dict] = None,
    ):
        self.repo_root = repo_root or Path.cwd()
        self.env = env if env is not None else dict(os.environ)
        self._output: list[str] = []
        self._console = None
        if _RICH_AVAILABLE:
            try:
                from rich.console import Console
                self._console = Console()
            except ImportError:
                pass

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

    def run_cmd_live(self, cmd: list[str], timeout: int = 120,
                     spinner_text: str = "") -> CmdResult:
        """Run a command with a Rich spinner. Falls back to run_cmd.

        Use for long-running operations where we want visual feedback.
        The spinner runs while the command executes in capture mode.
        """
        if not self._console or not spinner_text:
            return self.run_cmd(cmd, timeout=timeout)
        from rich.spinner import Spinner  # noqa: F401
        with self._console.status(f"  {spinner_text}", spinner="dots"):
            return self.run_cmd(cmd, timeout=timeout)

    def which(self, name: str) -> Optional[str]:
        """Check if a command exists. Returns path or None."""
        import shutil
        return shutil.which(name)

    # -- User interaction --

    def prompt(self, text: str, default: str = "") -> str:
        """Prompt user for text input. Override in tests."""
        if self._console:
            from rich.prompt import Prompt
            try:
                value = Prompt.ask(f"  [cyan]>[/] {text}", default=default,
                                   console=self._console)
                return value.strip() or default
            except EOFError:
                return default
        try:
            value = input(f"  {text} [{default}]: ")
            return value.strip() or default
        except EOFError:
            return default

    def confirm(self, text: str, default: bool = True) -> bool:
        """Ask yes/no question. Override in tests."""
        if self._console:
            from rich.prompt import Confirm
            try:
                return Confirm.ask(f"  [cyan]?[/] {text}", default=default,
                                   console=self._console)
            except EOFError:
                return default
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
        if self._console:
            from rich.prompt import Prompt
            try:
                value = Prompt.ask(
                    f"  [cyan]?[/] {text}",
                    choices=["y", "n", "s"],
                    default="y",
                    console=self._console,
                )
            except EOFError:
                return "yes"
            if value in ("s", "skip"):
                return "skip"
            if value in ("n", "no"):
                return "no"
            return "yes"
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
        if self._console:
            from rich.panel import Panel
            from rich.table import Table
            from rich.prompt import IntPrompt
            table = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
            table.add_column(style="dim", justify="right", width=3)
            table.add_column(style="bold")
            for i, item in enumerate(items, 1):
                table.add_row(str(i), item)
            self._console.print()
            self._console.print(
                Panel(table, title=f"[bold]{label}[/]",
                      border_style="cyan", expand=False, padding=(1, 2))
            )
            self._output.append("\n".join(
                f"    {i}) {item}" for i, item in enumerate(items, 1)
            ))
            try:
                idx = IntPrompt.ask(
                    f"  [cyan]>[/] {label}", default=1,
                    console=self._console,
                ) - 1
                return idx if 0 <= idx < len(items) else 0
            except (EOFError, ValueError):
                return 0
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
        if self._console:
            self._console.print(msg, highlight=False)
        else:
            print(msg)
        self._output.append(msg)

    def ok(self, msg: str) -> None:
        if self._console:
            self._console.print(f"  [green]:heavy_check_mark:[/green] {msg}")
            self._output.append(f"  ✓ {msg}")
        else:
            self.print(f"  {GREEN}✓{NC} {msg}")

    def warn(self, msg: str) -> None:
        if self._console:
            self._console.print(f"  [yellow]:warning:[/yellow]  {msg}")
            self._output.append(f"  ⚠ {msg}")
        else:
            self.print(f"  {YELLOW}⚠{NC} {msg}")

    def fail(self, msg: str) -> None:
        if self._console:
            self._console.print(f"  [red]:cross_mark:[/red] {msg}")
            self._output.append(f"  ✗ {msg}")
        else:
            self.print(f"  {RED}✗{NC} {msg}")

    def header(self, msg: str) -> None:
        if self._console:
            from rich.rule import Rule
            self._console.print(Rule(msg, style="bold"))
            self._output.append(msg)
        else:
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


def detect_tls_skip(ctx: SetupContext, engine: str) -> bool:
    """Auto-detect whether TLS verification should be skipped.

    Runs a test container image pull. If it fails with a certificate error
    (x509, tls, certificate), enables skip automatically. Only relevant for
    podman — Docker requires daemon-level CA trust configuration.
    """
    if engine != "podman":
        return False

    ctx.print("")
    r = ctx.run_cmd_live(
        ["podman", "pull", "--quiet", "docker.io/library/alpine:latest"],
        timeout=30,
        spinner_text="Checking container registry connectivity...",
    )
    if r.ok:
        ctx.ok("Container pull OK")
        return False

    stderr = (r.stderr or r.stdout or "").lower()
    tls_indicators = ["x509", "certificate", "tls"]
    if any(ind in stderr for ind in tls_indicators):
        ctx.warn("TLS certificate error detected — enabling SKIP_TLS_VERIFY")
        ctx.print("    (Corporate proxy likely intercepting HTTPS)")
        return True

    # Non-TLS failure (network, DNS, etc.) — don't auto-enable
    ctx.warn(f"Container pull failed: {(r.stderr or r.stdout or '').strip()}")
    ctx.print("    If this is a TLS/certificate issue, set SKIP_TLS_VERIFY=true in .env")
    return False


# ---------------------------------------------------------------------------
# Corporate Proxy SSL Inspection Detection
# ---------------------------------------------------------------------------

# Known public CAs that sign AWS certificates
KNOWN_PUBLIC_CAS = [
    "Amazon Root CA",
    "DigiCert",
    "GlobalSign",
    "Let's Encrypt",
    "Let's Encrypt Authority",
    "ISRG Root",
    "Starfield",
    "GoDaddy",
    "Comodo",
    "Sectigo",
    "GeoTrust",
    "Thawte",
    "VeriSign",
]


def _test_connection_with_ca(ca_path: str) -> bool:
    """Test if connection works with given CA bundle.
    
    Returns True if SSL connection succeeds.
    """
    import ssl
    import socket
    
    if not Path(ca_path).exists():
        return False
    
    try:
        context = ssl.create_default_context()
        context.load_verify_locations(ca_path)
        with socket.create_connection(("sts.amazonaws.com", 443), timeout=10) as sock:
            with context.wrap_socket(sock, server_hostname="sts.amazonaws.com"):
                return True
    except Exception:
        return False


def _test_connection_without_ca() -> bool:
    """Test if connection works without special CA bundle.
    
    Returns True if SSL connection succeeds (no proxy).
    Returns False if SSL verification fails (proxy detected).
    """
    import ssl
    import socket
    
    try:
        context = ssl.create_default_context()
        with socket.create_connection(("sts.amazonaws.com", 443), timeout=10) as sock:
            with context.wrap_socket(sock, server_hostname="sts.amazonaws.com") as ssock:
                # Connection succeeded - check if cert is from known CA
                cert_dict = ssock.getpeercert()
                if not cert_dict:
                    return True
                
                issuer_tuple = cert_dict.get("issuer", ())
                issuer_str = str(issuer_tuple)
                
                for known_ca in KNOWN_PUBLIC_CAS:
                    if known_ca.lower() in issuer_str.lower():
                        return True
                
                # Unknown CA but connection succeeded (rare case)
                return False
    except ssl.SSLCertVerificationError:
        return False
    except Exception:
        return False


def _get_current_ca_bundle(ctx: SetupContext) -> str | None:
    """Get current AWS_CA_BUNDLE from environment or .env file.
    
    Returns path or None if not set.
    """
    # Check environment first
    ca_bundle = ctx.env.get("AWS_CA_BUNDLE", "")
    if ca_bundle:
        return ca_bundle
    
    # Check .env file
    env_path = ctx.repo_root / ".env"
    if not ctx.file_exists(env_path):
        return None
    
    try:
        content = ctx.read_file(env_path)
    except (OSError, PermissionError):
        return None
    
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("AWS_CA_BUNDLE="):
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
            # Expand $HOME
            if "$HOME" in value:
                home = ctx.env.get("HOME", str(Path.home()))
                value = value.replace("$HOME", home)
            return value
    
    return None


def detect_proxy_status(ctx: SetupContext) -> str:
    """Detect corporate proxy SSL inspection status.
    
    Returns:
        "none" - no SSL inspection detected
        "detected" - SSL inspection detected, needs configuration
        "configured" - SSL inspection detected, already configured
    """
    # Step 1: Check if connection works without CA bundle
    if _test_connection_without_ca():
        return "none"
    
    # Step 2: Connection fails without CA - proxy exists
    # Check if already configured
    ca_bundle = _get_current_ca_bundle(ctx)
    
    if ca_bundle and Path(ca_bundle).exists():
        # Verify it works
        if _test_connection_with_ca(ca_bundle):
            return "configured"
    
    # Step 3: Proxy detected but not configured
    return "detected"


def detect_ssl_inspection(ctx: SetupContext) -> bool:
    """Legacy function - returns True if proxy detected (any state).
    
    Kept for backward compatibility.
    """
    status = detect_proxy_status(ctx)
    return status != "none"


def find_corporate_ca(ctx: SetupContext) -> str | None:
    """Find corporate CA certificate.

    Strategy:
    1. Export from system keychain (macOS)
    2. Check common vendor locations
    3. Return path or None

    Returns path to corporate CA bundle, or None if not found.
    """
    import tempfile

    # macOS: export from system keychain
    if sys.platform == "darwin":
        try:
            # Export all certs from system keychain
            result = ctx.run_cmd(
                ["security", "export", "-k", "/Library/Keychains/System.keychain",
                 "-t", "certs", "-p"],
                timeout=10,
            )
            if result.ok and result.stdout:
                # Parse to find non-public CA
                certs = result.stdout.split("-----END CERTIFICATE-----")
                corporate_certs = []
                for cert in certs:
                    if not cert.strip():
                        continue
                    cert += "-----END CERTIFICATE-----\n"
                    # Check if this is a known public CA
                    is_public = False
                    for known_ca in KNOWN_PUBLIC_CAS:
                        if known_ca.lower() in cert.lower():
                            is_public = True
                            break
                    if not is_public and "BEGIN CERTIFICATE" in cert:
                        corporate_certs.append(cert)

                if corporate_certs:
                    # Write to temp file
                    bundle_path = Path.home() / ".aws" / "corporate-ca-bundle.pem"
                    bundle_path.parent.mkdir(parents=True, exist_ok=True)
                    bundle_path.write_text("\n".join(corporate_certs))
                    return str(bundle_path)
        except Exception:
            pass

    # Linux: check common locations
    linux_paths = [
        "/etc/ssl/certs/ca-certificates.crt",
        "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",
        "/etc/ssl/ca-bundle.pem",
    ]
    for path in linux_paths:
        if Path(path).exists():
            return path

    return None


def create_combined_ca_bundle(ctx: SetupContext, corporate_ca_path: str) -> str:
    """Create combined CA bundle: system CAs + corporate CA.

    Returns path to combined bundle.
    """
    bundle_path = Path.home() / ".aws" / "combined-ca-bundle.pem"
    bundle_path.parent.mkdir(parents=True, exist_ok=True)

    # System CAs location
    if sys.platform == "darwin":
        system_cas_path = "/etc/ssl/cert.pem"
    else:
        system_cas_path = "/etc/ssl/certs/ca-certificates.crt"

    # Combine
    parts = []
    if Path(system_cas_path).exists():
        parts.append(Path(system_cas_path).read_text())

    if Path(corporate_ca_path).exists():
        parts.append(Path(corporate_ca_path).read_text())

    bundle_path.write_text("\n\n".join(parts))
    return str(bundle_path)


def setup_ca_bundle(ctx: SetupContext, force: bool = False) -> tuple[str | None, bool]:
    """Setup CA bundle for corporate proxy SSL inspection.
    
    Args:
        ctx: Setup context
        force: If True, re-run detection even if already configured
    
    Returns:
        (ca_bundle_path, needs_watcher_reinstall)
    """
    ctx.print("")
    ctx.header("Checking for corporate proxy SSL inspection...")
    
    # Step 1: Detect status
    status = detect_proxy_status(ctx)
    
    if status == "none":
        ctx.ok("No SSL inspection detected")
        return (None, False)
    
    if status == "configured" and not force:
        # Already configured - verify and return
        ca_bundle = _get_current_ca_bundle(ctx)
        ctx.ok("SSL inspection detected, already configured")
        ctx.print(f"  AWS_CA_BUNDLE={ca_bundle}")
        if ca_bundle and _test_connection_with_ca(ca_bundle):
            ctx.print("  Connection verified ✓")
            return (ca_bundle, False)
        else:
            ctx.warn("  CA bundle configured but connection failed")
            ctx.print("  Re-running detection...")
            status = "detected"
    
    # status == "detected" or force
    ctx.warn("SSL inspection detected (corporate proxy)")
    ctx.print("  Your network proxy is intercepting HTTPS traffic.")
    
    # Step 2: Try to find CA
    corporate_ca = find_corporate_ca(ctx)
    
    if corporate_ca:
        ctx.ok(f"Found corporate CA: {corporate_ca}")
    else:
        # Step 3: Prompt user
        ctx.print("")
        ctx.print("  Could not automatically find corporate CA certificate.")
        ctx.print("  Common locations:")
        if sys.platform == "darwin":
            ctx.print("    ~/Library/Application Support/<vendor>/")
            ctx.print("    /Library/Application Support/<vendor>/")
        else:
            ctx.print("    /etc/ssl/certs/")
            ctx.print("    /usr/local/share/ca-certificates/")
        ctx.print("")
        corporate_ca = ctx.prompt("Path to corporate CA certificate (or 'skip')", "skip")
        
        if corporate_ca.lower() == "skip" or not Path(corporate_ca).exists():
            ctx.warn("Skipping CA bundle setup")
            ctx.print("  If you see SSL errors later, run: bazel-proxy detect-proxy")
            return (None, False)
    
    # Step 4: Create combined bundle
    combined = create_combined_ca_bundle(ctx, corporate_ca)
    ctx.ok(f"Created combined CA bundle: {combined}")
    
    # Step 5: Update .env file
    env_path = ctx.repo_root / ".env"
    if ctx.file_exists(env_path):
        try:
            content = ctx.read_file(env_path)
            # Check if AWS_CA_BUNDLE already set
            lines = content.splitlines()
            updated = False
            new_lines = []
            for line in lines:
                if line.strip().startswith("AWS_CA_BUNDLE="):
                    new_lines.append(f'AWS_CA_BUNDLE="{combined}"')
                    updated = True
                else:
                    new_lines.append(line)
            
            if not updated:
                # Add after SKIP_TLS_VERIFY or at end
                inserted = False
                final_lines = []
                for i, line in enumerate(new_lines):
                    final_lines.append(line)
                    if "SKIP_TLS_VERIFY" in line and not inserted:
                        final_lines.append("")
                        final_lines.append("# Corporate Proxy / SSL Inspection")
                        final_lines.append(f'AWS_CA_BUNDLE="{combined}"')
                        inserted = True
                if not inserted:
                    final_lines.append("")
                    final_lines.append("# Corporate Proxy / SSL Inspection")
                    final_lines.append(f'AWS_CA_BUNDLE="{combined}"')
                new_lines = final_lines
            
            ctx.write_file(env_path, "\n".join(new_lines) + "\n")
            ctx.ok(f"Updated .env: AWS_CA_BUNDLE=\"{combined}\"")
        except Exception:
            ctx.warn("Could not update .env file")
    
    # Step 5.5: Update AWS profiles
    update_aws_profiles_ca_bundle(ctx, combined)
    
    # Step 6: Verify connection
    if _test_connection_with_ca(combined):
        ctx.print("  Connection verified ✓")
    else:
        ctx.warn("  Connection test failed - CA bundle may be incomplete")
    
    return (combined, True)  # needs watcher reinstall


def reinstall_watcher_if_needed(ctx: SetupContext, ca_bundle: str) -> None:
    """Reinstall watcher if AWS_CA_BUNDLE changed.
    
    Only reinstalls if watcher is installed and CA bundle differs.
    """
    if sys.platform != "darwin":
        return
    
    plist_path = Path.home() / "Library/LaunchAgents/com.bazel.sso-watcher.plist"
    if not plist_path.exists():
        return
    
    try:
        plist_content = plist_path.read_text()
    except (OSError, PermissionError):
        return
    
    # Check if plist has correct AWS_CA_BUNDLE
    if ca_bundle and ca_bundle not in plist_content:
        ctx.print("")
        ctx.print("  Reinstalling watcher to apply CA bundle...")
        result = ctx.run_cmd(["bash", "scripts/sso-install.sh"], timeout=60)
        if result.ok:
            ctx.ok("Watcher reinstalled")
        else:
            ctx.warn("Watcher reinstall failed - run manually: bazel-proxy install")


def update_aws_profiles_ca_bundle(ctx: SetupContext, ca_bundle: str) -> None:
    """Update ca_bundle in all AWS profiles that have it set.
    
    Updates ~/.aws/config to use the new CA bundle path.
    """
    config_path = Path.home() / ".aws" / "config"
    if not ctx.file_exists(config_path):
        return
    
    try:
        content = ctx.read_file(config_path)
    except (OSError, PermissionError):
        return
    
    lines = content.splitlines()
    updated = False
    new_lines = []
    
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("ca_bundle ="):
            old_path = stripped.split("=", 1)[1].strip()
            if old_path and old_path != ca_bundle:
                indent = line[:len(line) - len(line.lstrip())]
                new_lines.append(f"{indent}ca_bundle = {ca_bundle}")
                updated = True
                continue
        new_lines.append(line)
    
    if updated:
        ctx.write_file(config_path, "\n".join(new_lines) + "\n")
        ctx.ok("Updated AWS profiles to use new CA bundle")


def prompt_env_config(ctx: SetupContext, container_engine: str = "") -> EnvConfig:
    """Interactively prompt for .env values."""
    profiles = list_aws_profiles(ctx)
    if profiles:
        ctx.print("  Available AWS profiles:")
        for p in profiles:
            ctx.print(f"    - {p}")
        ctx.print("  Type an existing name to use it, or enter a new name to create one.")
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

    config.skip_tls_verify = detect_tls_skip(ctx, container_engine)

    return config


def generate_env_content(config: EnvConfig) -> str:
    """Generate .env file content from config."""
    skip_tls_line = (
        "SKIP_TLS_VERIFY=true"
        if config.skip_tls_verify
        else "# SKIP_TLS_VERIFY=false"
    )
    ca_bundle_line = (
        f'AWS_CA_BUNDLE="{config.aws_ca_bundle}"'
        if config.aws_ca_bundle
        else "# AWS_CA_BUNDLE=~/.aws/combined-ca-bundle.pem"
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

# Corporate Proxy / SSL Inspection
# Path to CA certificate bundle for SSL inspection bypass.
# Required when behind corporate proxies that intercept HTTPS traffic.
{ca_bundle_line}
"""


def configure_env(ctx: SetupContext, container_engine: str = "") -> EnvConfig:
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
        config = prompt_env_config(ctx, container_engine)
        # Detect and setup CA bundle for corporate proxy SSL inspection
        ca_bundle, needs_reinstall = setup_ca_bundle(ctx)
        if ca_bundle:
            config.aws_ca_bundle = ca_bundle
            if needs_reinstall:
                reinstall_watcher_if_needed(ctx, ca_bundle)
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
        elif key == "AWS_CA_BUNDLE":
            config.aws_ca_bundle = val

    return config


# ---------------------------------------------------------------------------
# Phase 3: Install tools via mise
# ---------------------------------------------------------------------------

def install_tools(ctx: SetupContext) -> bool:
    """Phase 3: Run mise install. Returns True on success."""
    ctx.header("Installing tools via mise...")
    r = ctx.run_cmd_live(["mise", "install", "--yes"], timeout=300,
                         spinner_text="Installing tools...")
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
    r = ctx.run_cmd_live(["mise", "run", "sso-install"], timeout=120,
                         spinner_text="Building webview and installing watcher...")
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

    # Dialog display — verifies the app can show UI (non-fatal).
    # Use a silent test (get frontmost process name) instead of popping up a
    # dialog box. This avoids the annoying "OK" dialog on re-runs while still
    # exercising the Automation permission.
    r = ctx.run_cmd([
        "osascript", "-e",
        'tell application "System Events" to get name of first process '
        'whose frontmost is true'
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


def _upgrade_legacy_profile(ctx: SetupContext, profile: str) -> bool:
    """Upgrade legacy SSO profile to modern sso-session style.

    Reads sso_start_url, sso_region, sso_account_id, sso_role_name from
    the legacy [profile X] section via configparser, removes legacy SSO
    fields (sso_start_url, sso_region), adds sso_session reference, and
    appends a new [sso-session X] block with sso_registration_scopes.

    Returns True on success.
    """
    import configparser
    import io

    config_path = Path.home() / ".aws" / "config"
    config_text = _read_aws_config(ctx)
    if not config_text:
        ctx.warn("Cannot upgrade: ~/.aws/config is empty")
        return False

    parser = configparser.ConfigParser()
    parser.read_string(config_text)

    # AWS config uses "profile X" as section name (except [default])
    section = f"profile {profile}" if profile != "default" else "default"
    if not parser.has_section(section):
        ctx.warn(f"Cannot upgrade: section [{section}] not found")
        return False

    # Extract required legacy fields
    legacy_keys = ("sso_start_url", "sso_region", "sso_account_id", "sso_role_name")
    fields = {}
    for key in legacy_keys:
        val = parser.get(section, key, fallback=None)
        if val:
            fields[key] = val

    missing = [k for k in legacy_keys if k not in fields]
    if missing:
        ctx.warn(f"Cannot upgrade: missing {', '.join(missing)} in profile")
        return False

    # Check sso-session doesn't already exist
    session_name = profile
    session_section = f"sso-session {session_name}"
    if parser.has_section(session_section):
        ctx.warn(f"Session '{session_name}' already exists — not overwriting")
        return False

    # Remove legacy SSO fields from profile, add sso_session reference
    for key in ("sso_start_url", "sso_region"):
        parser.remove_option(section, key)
    parser.set(section, "sso_session", session_name)

    # Add sso-session section
    parser.add_section(session_section)
    parser.set(session_section, "sso_start_url", fields["sso_start_url"])
    parser.set(session_section, "sso_region", fields["sso_region"])
    parser.set(session_section, "sso_registration_scopes", "sso:account:access")

    # Write back
    buf = io.StringIO()
    parser.write(buf)
    ctx.write_file(config_path, buf.getvalue())
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
    """Remove stale OIDC client registrations that use device-code grant type.

    Surgical: only removes registrations where ``grantTypes`` contains
    ``"device_code"`` (old CLI < 2.22.0) or where ``grantTypes`` is absent
    (very old CLI that didn't cache the field).  Modern PKCE registrations
    (``authorization_code``) and all access tokens are preserved.

    This ensures ``aws sso login --no-browser`` creates a fresh PKCE
    registration instead of reusing a cached device-code one, which would
    show the "Authorization requested — enter code" screen instead of the
    direct IdP login page in the webview.
    """
    import json as _json
    cache_dir = Path.home() / ".aws" / "sso" / "cache"
    pattern = str(cache_dir / "*.json")
    for path_str in ctx.glob_files(pattern):
        try:
            content = ctx.read_file(Path(path_str))
            data = _json.loads(content)
            # Skip access tokens (have accessToken field)
            if "accessToken" in data:
                continue
            # Only look at client registrations (have clientId)
            if "clientId" not in data:
                continue
            grant_types = data.get("grantTypes", [])
            # Remove if device_code present (exact or URN form) OR
            # grantTypes missing/empty (very old CLI didn't cache it)
            has_device_code = any("device_code" in gt for gt in grant_types)
            if not grant_types or has_device_code:
                ctx.remove_file(Path(path_str))
        except (OSError, _json.JSONDecodeError):
            continue


def _discover_account_and_role(
    ctx: SetupContext,
    profile: str,
    start_url: str,
    sso_region: str,
) -> tuple[str, str]:
    """Login to SSO, list accounts/roles, let user pick.

    Returns (account_id, role_name) or ("", "") on failure.
    """
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
    """Remove temporary sso-session sections from config and cached token."""
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
    # Clean up the token cache file — AWS CLI stores it as sha1(session_name).json
    _remove_temp_token_cache(ctx, session_name)


def _remove_temp_token_cache(ctx: SetupContext, session_name: str) -> None:
    """Remove the SSO token cache file for a temporary session.

    AWS CLI computes token cache filenames as sha1(session_name).json
    for modern sso-session profiles. Leftover temp tokens can confuse
    startUrl-scanning lookups (e.g. the watcher's silent refresh).
    """
    import hashlib

    cache_key = hashlib.sha1(session_name.encode("utf-8")).hexdigest()
    cache_file = Path.home() / ".aws" / "sso" / "cache" / f"{cache_key}.json"
    ctx.remove_file(cache_file)


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
            ctx.print("")
            return True
        # Legacy profile — offer upgrade to modern sso-session for PKCE
        ctx.warn(f"Profile '{profile}' uses legacy SSO config (no sso-session)")
        ctx.print("  Legacy config uses device-code flow (manual code entry).")
        ctx.print("  Modern config uses PKCE (direct login, no code needed).")
        ctx.print("")
        if ctx.confirm("Upgrade to modern sso-session config?"):
            if _upgrade_legacy_profile(ctx, profile):
                ctx.ok(f"Upgraded '{profile}' to modern sso-session style")
                ctx.print("")
                return True
            ctx.warn("Upgrade failed — keeping legacy config")
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

    Clears stale device-code OIDC registrations first to ensure the
    webview gets the PKCE flow (direct IdP login, no code entry).

    Returns True on success. Runs interactive (no capture) so errors
    and webview prompts are visible to the user.
    """
    _clear_sso_cache(ctx)
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

def _containers_running(ctx: SetupContext) -> bool:
    """Check if proxy containers are already running and healthy."""
    r = ctx.run_cmd(["bash", "-c",
                     "source scripts/container-engine.sh && "
                     "$COMPOSE_CMD ps --status running --format '{{.Name}}' 2>/dev/null"],
                    timeout=15)
    if not r.ok:
        return False
    names = [n.strip() for n in r.stdout.strip().splitlines() if n.strip()]
    return len(names) >= 2  # s3proxy + sso-monitor


def start_containers(ctx: SetupContext) -> bool:
    """Phase 8: Optionally start containers. Returns True if started."""
    if _containers_running(ctx):
        ctx.ok("Containers already running")
        return True

    if not ctx.confirm("Start containers now?", default=True):
        return False

    r = ctx.run_cmd_live(["mise", "run", "containers:up"], timeout=300,
                         spinner_text="Starting containers...")
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
    if ctx._console:
        from rich.panel import Panel
        from rich.table import Table
        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_column(style="bold", width=10)
        table.add_column()
        table.add_row("Proxy", f"http://localhost:{port}/")
        table.add_row("Logs", "mise run logs")
        table.add_row("Watcher", "mise run sso-status")
        table.add_row("Health", f"curl http://localhost:{port}/healthz")
        ctx._console.print()
        ctx._console.print(Panel(table, title="[bold green]Setup complete![/]",
                                 border_style="green"))
        ctx._console.print()
        ctx._output.append("Setup complete!")
    else:
        ctx.print("")
        ctx.print(f"{BOLD}{GREEN}Setup complete!{NC}")
        ctx.print("")
        ctx.print(f"  Proxy:   http://localhost:{port}/")
        ctx.print("  Logs:    mise run logs")
        ctx.print("  Watcher: mise run sso-status")
        ctx.print(f"  Health:  curl http://localhost:{port}/healthz")
        ctx.print("")


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def run_setup(ctx: SetupContext) -> int:
    """Run the full setup flow. Returns exit code (0=success, 1=failure)."""
    if ctx._console:
        from rich.panel import Panel
        ctx._console.print()
        ctx._console.print(Panel("[bold]bazel-aws-maven-proxy setup[/]",
                                 style="blue", expand=False))
        ctx._console.print()
        ctx._output.append("bazel-aws-maven-proxy setup")
    else:
        ctx.print(f"\n{BOLD}bazel-aws-maven-proxy setup{NC}\n")

    # Phase 1: Prerequisites
    ctx.header("Checking prerequisites...")
    prereqs = check_prerequisites(ctx)
    if not prereqs.ok:
        n = len(prereqs.errors)
        if ctx._console:
            ctx._console.print(
                f"\n[bold red]{n} prerequisite(s) missing. Fix the above and re-run.[/]"
            )
            ctx._output.append(f"{n} prerequisite(s) missing.")
        else:
            ctx.print(f"\n{RED}{n} prerequisite(s) missing. Fix the above and re-run.{NC}")
        return 1
    ctx.print("")

    # Phase 2: .env
    engine = prereqs.container.name if prereqs.container else ""
    config = configure_env(ctx, engine)

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
    global _RICH_AVAILABLE
    _ensure_tty()
    _RICH_AVAILABLE = _ensure_rich()
    
    # Check for --check-proxy-status flag (for bazel-proxy start)
    if "--check-proxy-status" in sys.argv:
        try:
            ctx = SetupContext()
            status = detect_proxy_status(ctx)
            # Exit codes: 0 = none, 1 = detected, 2 = configured
            if status == "none":
                sys.exit(0)
            elif status == "configured":
                sys.exit(2)
            else:  # detected
                sys.exit(1)
        except Exception:
            sys.exit(1)
    
    # Check for --detect-proxy flag
    if "--detect-proxy" in sys.argv:
        force = "--force" in sys.argv
        try:
            ctx = SetupContext()
            ca_bundle, needs_reinstall = setup_ca_bundle(ctx, force=force)
            if ca_bundle:
                if needs_reinstall:
                    reinstall_watcher_if_needed(ctx, ca_bundle)
                ctx.print("")
            else:
                ctx.print("")
            # Exit codes: 0 = none or configured, 1 = detected (needs config), 2 = error
            status = detect_proxy_status(ctx)
            if status == "none":
                sys.exit(0)
            elif status == "configured":
                sys.exit(0)
            else:
                sys.exit(1)
        except KeyboardInterrupt:
            print(f"\n{YELLOW}Interrupted.{NC}")
            sys.exit(130)
    
    try:
        ctx = SetupContext()
        sys.exit(run_setup(ctx))
    except KeyboardInterrupt:
        print(f"\n{YELLOW}Setup interrupted.{NC}")
        sys.exit(130)


if __name__ == "__main__":
    main()
