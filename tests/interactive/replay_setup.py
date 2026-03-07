#!/usr/bin/env python3
"""Interactive setup replay runner.

Executes setup scenarios with injected behaviors, showing colored output
with annotations for what's being mocked/injected at each step.
"""

import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

from scripts.setup import (
    SetupContext, CmdResult, EnvConfig,
    run_setup, check_prerequisites, configure_env, install_tools,
    install_sso_watcher, check_macos_permissions, configure_sso,
    first_login_and_validate, start_containers, print_summary,
    BOLD, GREEN, YELLOW, RED, NC,
)
from pathlib import Path

# ---------------------------------------------------------------------------
# Colors for annotations
# ---------------------------------------------------------------------------
CYAN = "\033[0;36m"
MAGENTA = "\033[0;35m"
DIM = "\033[2m"
RESET = "\033[0m"

PHASE_DELAY = 0.6   # seconds between phases
STEP_DELAY = 0.08   # seconds between output lines
INJECT_DELAY = 0.15 # seconds for injection annotations


def annotation(text: str) -> None:
    """Print an injection annotation."""
    time.sleep(INJECT_DELAY)
    print(f"  {CYAN}⟵ {text}{RESET}")


def phase_banner(num: int, title: str) -> None:
    """Print a phase separator."""
    time.sleep(PHASE_DELAY)
    print(f"\n{'─' * 60}")
    print(f"  {MAGENTA}{BOLD}Phase {num}: {title}{RESET}")
    print(f"{'─' * 60}\n")


def scenario_banner(name: str, description: str) -> None:
    """Print a scenario header."""
    print(f"\n{'━' * 60}")
    print(f"  {BOLD}{GREEN}SCENARIO: {name}{RESET}")
    print(f"  {DIM}{description}{RESET}")
    print(f"{'━' * 60}")


# ---------------------------------------------------------------------------
# ReplayContext — annotated mock context
# ---------------------------------------------------------------------------

class ReplayContext(SetupContext):
    """Mock context that shows annotations for injected behavior."""

    def __init__(
        self,
        tools: dict[str, str] = None,      # tool_name -> version_output
        commands: dict = None,               # command key -> CmdResult
        prompts: list[str] = None,
        confirms: list[bool] = None,
        three_way: list[str] = None,
        choices: list[int] = None,
        files: dict[str, str] = None,
        env: dict[str, str] = None,
    ):
        super().__init__(
            repo_root=Path("/mock/repo"),
            env=env or {},
        )
        self._tools = tools or {}
        self._commands = commands or {}
        self._prompts = list(prompts or [])
        self._confirms = list(confirms or [])
        self._three_way = list(three_way or [])
        self._choices = list(choices or [])
        self._files = dict(files or {})

    def which(self, name: str) -> str | None:
        if name in self._tools:
            return f"/usr/bin/{name}"
        annotation(f"which {name} → NOT FOUND")
        return None

    def run_cmd(self, cmd: list[str], timeout: int = 120,
                capture: bool = True, interactive: bool = False) -> CmdResult:
        key = cmd[0]
        # Try tuple match first, then first-arg match
        tuple_key = tuple(cmd)
        if tuple_key in self._commands:
            result = self._commands[tuple_key]
        elif key in self._commands:
            result = self._commands[key]
        else:
            # Try prefix matches
            for k, v in self._commands.items():
                if isinstance(k, str) and " ".join(cmd).startswith(k):
                    result = v
                    break
            else:
                result = CmdResult(-1, "", f"not mocked: {' '.join(cmd)}")

        if callable(result):
            result = result(cmd)

        status = f"{GREEN}OK{RESET}" if result.ok else f"{RED}FAIL (rc={result.returncode}){RESET}"
        annotation(f"run: {' '.join(cmd[:4])}{'...' if len(cmd) > 4 else ''} → {status}")
        if result.stdout.strip() and len(result.stdout.strip()) < 80:
            annotation(f"  stdout: {DIM}{result.stdout.strip()}{RESET}")
        return result

    def prompt(self, text: str, default: str = "") -> str:
        if self._prompts:
            answer = self._prompts.pop(0)
        else:
            answer = default
        display = answer if answer != default else f"{default} (default)"
        annotation(f"prompt: \"{text}\" → \"{display}\"")
        time.sleep(STEP_DELAY)
        return answer or default

    def confirm(self, text: str, default: bool = True) -> bool:
        if self._confirms:
            answer = self._confirms.pop(0)
        else:
            answer = default
        yn = "Yes" if answer else "No"
        annotation(f"confirm: \"{text}\" → {yn}")
        time.sleep(STEP_DELAY)
        return answer

    def confirm_three_way(self, text: str) -> str:
        if self._three_way:
            answer = self._three_way.pop(0)
        else:
            answer = "yes"
        annotation(f"confirm_three_way: \"{text}\" → {answer}")
        time.sleep(STEP_DELAY)
        return answer

    def choose(self, items: list[str], label: str = "Choice") -> int:
        if self._choices:
            idx = self._choices.pop(0)
        else:
            idx = 0
        for i, item in enumerate(items, 1):
            marker = " ←" if i - 1 == idx else ""
            self._output.append(f"    {i}) {item}{marker}")
            print(f"    {i}) {item}{marker}")
        annotation(f"choose: \"{label}\" → {idx} ({items[idx] if idx < len(items) else '?'})")
        time.sleep(STEP_DELAY)
        return idx

    def print(self, msg: str = "") -> None:
        time.sleep(STEP_DELAY)
        print(msg)
        self._output.append(msg)

    def file_exists(self, path: Path) -> bool:
        exists = str(path) in self._files
        return exists

    def read_file(self, path: Path) -> str:
        key = str(path)
        if key in self._files:
            return self._files[key]
        raise FileNotFoundError(key)

    def write_file(self, path: Path, content: str) -> None:
        self._files[str(path)] = content
        annotation(f"write: {path.name} ({len(content)} bytes)")

    def remove_file(self, path: Path) -> None:
        key = str(path)
        self._files.pop(key, None)

    def glob_files(self, pattern: str) -> list[str]:
        """Match virtual files against a glob-style pattern."""
        import fnmatch
        return [p for p in self._files if fnmatch.fnmatch(p, pattern)]

    def mkdir(self, path: Path) -> None:
        pass


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

def _base_tools_all():
    """All tools present with good versions."""
    return {
        "mise": "2024.12.0",
        "aws": "aws-cli/2.15.30",
        "podman": "podman version 5.0.0",
        "swiftc": "",
    }

def _base_commands_all(profile="default"):
    """Standard command results for happy path."""
    return {
        "mise": CmdResult(0, "2024.12.0\n"),
        "aws": CmdResult(0, "aws-cli/2.15.30 Python/3.11.8\n"),
        "podman": CmdResult(0, "podman version 5.0.0\n"),
        "python3": CmdResult(0, "Python 3.11.14\n"),
        ("aws", "configure", "get", "sso_session", "--profile", profile):
            CmdResult(0, "my-sso-session\n"),
        ("aws", "sts", "get-caller-identity", "--profile", profile):
            CmdResult(0, '{"Account":"123456"}\n'),
        ("aws", "s3", "ls", f"s3://my-bucket/", "--profile", profile):
            CmdResult(0, "2024-01-01 file.jar\n"),
        ("mise", "install", "--yes"): CmdResult(0),
        ("mise", "run", "sso-install"): CmdResult(0),
        ("mise", "run", "containers:up"): CmdResult(0),
        "osascript": CmdResult(0, "ariel\n"),
        "pgrep": CmdResult(0),
    }


def scenario_happy_path():
    """Everything works perfectly on first try."""
    scenario_banner(
        "Happy Path",
        "All tools installed, fresh .env, SSO configured, credentials valid, S3 accessible"
    )

    ctx = ReplayContext(
        tools=_base_tools_all(),
        commands=_base_commands_all("bazel-proxy"),
        prompts=["bazel-proxy", "sa-east-1", "my-bucket", "8888", "notify"],
        confirms=[True],  # start containers
        env={"TERM_PROGRAM": "Apple_Terminal"},
    )

    phase_banner(1, "Prerequisites")
    rc = run_setup(ctx)
    print(f"\n  {BOLD}Exit code: {rc}{RESET}")


def scenario_missing_aws():
    """AWS CLI not installed — should fail at prerequisites."""
    scenario_banner(
        "Missing AWS CLI",
        "mise + podman present, but aws not installed → should exit 1"
    )

    tools = {"mise": "", "podman": ""}
    commands = {
        "mise": CmdResult(0, "2024.12.0\n"),
        "podman": CmdResult(0, "podman version 5.0.0\n"),
    }

    ctx = ReplayContext(tools=tools, commands=commands)

    rc = run_setup(ctx)
    print(f"\n  {BOLD}Exit code: {rc}{RESET}")


def scenario_aws_too_old():
    """AWS CLI version 2.7 — below minimum 2.9."""
    scenario_banner(
        "AWS CLI Too Old",
        "aws-cli/2.7.0 installed (need >= 2.9) → should exit 1"
    )

    tools = {"mise": "", "aws": "", "podman": "", "swiftc": ""}
    commands = {
        "mise": CmdResult(0, "2024.12.0\n"),
        "aws": CmdResult(0, "aws-cli/2.7.0 Python/3.9\n"),
        "podman": CmdResult(0, "podman version 5.0.0\n"),
    }

    ctx = ReplayContext(tools=tools, commands=commands)
    rc = run_setup(ctx)
    print(f"\n  {BOLD}Exit code: {rc}{RESET}")


def scenario_no_container_engine():
    """No podman or docker."""
    scenario_banner(
        "No Container Engine",
        "mise + aws present, but no podman or docker → should exit 1"
    )

    tools = {"mise": "", "aws": ""}
    commands = {
        "mise": CmdResult(0, "2024.12.0\n"),
        "aws": CmdResult(0, "aws-cli/2.15.0 Python/3.11\n"),
    }

    ctx = ReplayContext(tools=tools, commands=commands)
    rc = run_setup(ctx)
    print(f"\n  {BOLD}Exit code: {rc}{RESET}")


def scenario_docker_fallback():
    """No podman, but docker works."""
    scenario_banner(
        "Docker Fallback",
        "No podman but docker present → should succeed with docker"
    )

    tools = {"mise": "", "aws": "", "docker": "", "swiftc": ""}
    commands = {
        **_base_commands_all(),
        "docker": CmdResult(0, "Docker version 24.0.7, build afdd53b\n"),
    }
    # Remove podman from tools
    del commands["podman"]

    ctx = ReplayContext(
        tools=tools,
        commands=commands,
        prompts=["default", "us-west-2", "my-bucket", "8888", "notify"],
        confirms=[True],
        env={"TERM_PROGRAM": "iTerm2"},
    )

    rc = run_setup(ctx)
    print(f"\n  {BOLD}Exit code: {rc}{RESET}")


def scenario_no_swiftc_decline():
    """swiftc missing — user declines install, continues with browser fallback."""
    scenario_banner(
        "No swiftc — user declines",
        "swiftc missing → prompted to install → says no → browser fallback"
    )

    tools = {"mise": "", "aws": "", "podman": ""}  # no swiftc
    commands = _base_commands_all()

    ctx = ReplayContext(
        tools=tools,
        commands=commands,
        prompts=["default", "us-west-2", "test-bucket", "8888", "notify"],
        confirms=[True],
        three_way=["no"],  # decline xcode-select --install
        env={"TERM_PROGRAM": "Terminal"},
    )

    rc = run_setup(ctx)
    print(f"\n  {BOLD}Exit code: {rc}{RESET}")


def scenario_no_swiftc_accept():
    """swiftc missing — user accepts install, xcode-select runs."""
    scenario_banner(
        "No swiftc — user installs",
        "swiftc missing → prompted to install → says yes → xcode-select --install runs"
    )

    tools = {"mise": "", "aws": "", "podman": ""}  # no swiftc
    commands = {
        **_base_commands_all(),
        ("xcode-select", "--install"): CmdResult(0),
    }

    ctx = ReplayContext(
        tools=tools,
        commands=commands,
        prompts=["default", "us-west-2", "test-bucket", "8888", "notify"],
        confirms=[True],
        three_way=["yes"],  # accept xcode-select --install
        env={"TERM_PROGRAM": "Terminal"},
    )

    rc = run_setup(ctx)
    print(f"\n  {BOLD}Exit code: {rc}{RESET}")


def scenario_existing_env_keep():
    """User has existing .env and chooses to keep it."""
    scenario_banner(
        "Keep Existing .env",
        ".env already exists, user says No to overwrite → parsed and used"
    )

    existing_env = '''\
AWS_PROFILE="my-profile"
AWS_REGION="eu-west-1"
S3_BUCKET_NAME="existing-bucket"
PROXY_PORT="9999"
SSO_LOGIN_MODE="auto"
'''
    commands = {
        **_base_commands_all("my-profile"),
        ("aws", "configure", "get", "sso_session", "--profile", "my-profile"):
            CmdResult(0, "my-session\n"),
        ("aws", "sts", "get-caller-identity", "--profile", "my-profile"):
            CmdResult(0, '{"Account":"123"}\n'),
        ("aws", "s3", "ls", "s3://existing-bucket/", "--profile", "my-profile"):
            CmdResult(0, "2024-01-01 file.jar\n"),
    }

    ctx = ReplayContext(
        tools=_base_tools_all(),
        commands=commands,
        confirms=[False, True],  # don't overwrite, start containers
        files={"/mock/repo/.env": existing_env},
        env={"TERM_PROGRAM": "Terminal"},
    )

    rc = run_setup(ctx)
    print(f"\n  {BOLD}Exit code: {rc}{RESET}")


def scenario_sso_not_configured():
    """Profile has no SSO config, user says configure, login fails, empty account ID."""
    scenario_banner(
        "SSO Not Configured + Login Fails + Missing Account ID",
        "No SSO → configure → login fails → manual fallback → empty account ID → fail"
    )

    home = str(Path.home())
    config_path = f"{home}/.aws/config"

    commands = {
        **_base_commands_all(),
        ("aws", "configure", "get", "sso_session", "--profile", "default"):
            CmdResult(1, "", "not set"),
        ("aws", "configure", "get", "sso_account_id", "--profile", "default"):
            CmdResult(1, "", "not set"),
        # do_sso_login (webview) fails → triggers manual fallback
        "python3": lambda cmd: CmdResult(1, "", "login failed") if "-c" in cmd
                   else CmdResult(0, "Python 3.11.14\n"),
    }

    ctx = ReplayContext(
        tools=_base_tools_all(),
        commands=commands,
        # env prompts + SSO prompts (start_url, region) + manual fallback (account_id="")
        prompts=["default", "us-west-2", "my-bucket", "8888", "notify",
                 "https://myorg.awsapps.com/start", "us-east-1", ""],
        three_way=["yes"],  # yes to configure SSO
        confirms=[False, True],  # skip TLS, start containers
        files={config_path: ""},
        env={"TERM_PROGRAM": "Terminal"},
    )

    rc = run_setup(ctx)
    print(f"\n  {BOLD}Exit code: {rc}{RESET}")


def scenario_sso_skip():
    """SSO not configured, user chooses to skip."""
    scenario_banner(
        "SSO Not Configured + User Skips",
        "No SSO, user says 'skip' → no login, no S3 validation"
    )

    commands = {
        **_base_commands_all(),
        ("aws", "configure", "get", "sso_session", "--profile", "default"):
            CmdResult(1),
        ("aws", "configure", "get", "sso_account_id", "--profile", "default"):
            CmdResult(1),
    }

    ctx = ReplayContext(
        tools=_base_tools_all(),
        commands=commands,
        prompts=["default", "us-west-2", "my-bucket", "8888", "notify"],
        three_way=["skip"],
        confirms=[False],  # don't start containers
        env={"TERM_PROGRAM": "Terminal"},
    )

    rc = run_setup(ctx)
    print(f"\n  {BOLD}Exit code: {rc}{RESET}")


def scenario_creds_expired_login_succeeds():
    """SSO configured but credentials expired, login via webview succeeds."""
    scenario_banner(
        "Expired Credentials + Login Success",
        "SSO configured, sts fails (expired), login succeeds, S3 accessible"
    )

    commands = {
        **_base_commands_all(),
        ("aws", "sts", "get-caller-identity", "--profile", "default"):
            CmdResult(1, "", "ExpiredTokenException"),
        ("python3", "-c"): CmdResult(0),  # login succeeds
    }
    # python3 -c needs special handling
    def handle_python(cmd):
        if "-c" in cmd:
            annotation(f"  → webview SSO login simulation → {GREEN}SUCCESS{RESET}")
            return CmdResult(0)
        return CmdResult(0, "Python 3.11.14\n")
    commands["python3"] = handle_python

    ctx = ReplayContext(
        tools=_base_tools_all(),
        commands=commands,
        prompts=["default", "us-west-2", "my-bucket", "8888", "notify"],
        confirms=[True],
        env={"TERM_PROGRAM": "Terminal"},
    )

    rc = run_setup(ctx)
    print(f"\n  {BOLD}Exit code: {rc}{RESET}")


def scenario_creds_expired_login_fails():
    """SSO configured, credentials expired, login fails."""
    scenario_banner(
        "Expired Credentials + Login Failure",
        "SSO configured, sts fails, login also fails → warns but continues"
    )

    def handle_python(cmd):
        if "-c" in cmd:
            annotation(f"  → webview SSO login simulation → {RED}FAILED{RESET}")
            return CmdResult(1, "", "login timeout")
        return CmdResult(0, "Python 3.11.14\n")

    commands = {
        **_base_commands_all(),
        ("aws", "sts", "get-caller-identity", "--profile", "default"):
            CmdResult(1),
        "python3": handle_python,
        ("aws", "s3", "ls", "s3://my-bucket/", "--profile", "default"):
            CmdResult(1, "", "access denied"),
    }

    ctx = ReplayContext(
        tools=_base_tools_all(),
        commands=commands,
        prompts=["default", "us-west-2", "my-bucket", "8888", "notify"],
        confirms=[False],  # don't start containers
        env={"TERM_PROGRAM": "Terminal"},
    )

    rc = run_setup(ctx)
    print(f"\n  {BOLD}Exit code: {rc}{RESET}")


def scenario_s3_bucket_placeholder():
    """User leaves bucket as placeholder — S3 validation skipped."""
    scenario_banner(
        "Placeholder S3 Bucket",
        "User accepts default bucket name 'your-maven-bucket' → S3 check skipped"
    )

    ctx = ReplayContext(
        tools=_base_tools_all(),
        commands=_base_commands_all(),
        prompts=["default", "us-west-2", "your-maven-bucket", "8888", "notify"],
        confirms=[True],
        env={"TERM_PROGRAM": "Terminal"},
    )

    rc = run_setup(ctx)
    print(f"\n  {BOLD}Exit code: {rc}{RESET}")


def scenario_headless_ssh():
    """Running via SSH — no GUI, permission checks skipped."""
    scenario_banner(
        "Headless / SSH Session",
        "No DISPLAY, no TERM_PROGRAM, no WindowServer → permissions skipped"
    )

    commands = {
        **_base_commands_all(),
        "pgrep": CmdResult(1),  # no WindowServer
    }

    ctx = ReplayContext(
        tools=_base_tools_all(),
        commands=commands,
        prompts=["default", "us-west-2", "my-bucket", "8888", "notify"],
        confirms=[True],
        env={},  # no DISPLAY, no TERM_PROGRAM
    )

    rc = run_setup(ctx)
    print(f"\n  {BOLD}Exit code: {rc}{RESET}")


def scenario_mise_install_fails():
    """mise install fails but setup continues."""
    scenario_banner(
        "mise install Fails",
        "mise install --yes returns error → warns but continues with rest of setup"
    )

    commands = {
        **_base_commands_all(),
        ("mise", "install", "--yes"): CmdResult(1, "", "install error"),
    }

    def handle_python(cmd):
        if "-c" in cmd:
            return CmdResult(0)
        return CmdResult(0, "Python 3.11.14\n")
    commands["python3"] = handle_python

    ctx = ReplayContext(
        tools=_base_tools_all(),
        commands=commands,
        prompts=["default", "us-west-2", "my-bucket", "8888", "notify"],
        confirms=[True],
        env={"TERM_PROGRAM": "Terminal"},
    )

    rc = run_setup(ctx)
    print(f"\n  {BOLD}Exit code: {rc}{RESET}")


def scenario_invalid_sso_mode():
    """User enters invalid SSO mode — defaults to notify with warning."""
    scenario_banner(
        "Invalid SSO Mode",
        "User enters 'bogus' for SSO mode → warned, defaults to 'notify'"
    )

    ctx = ReplayContext(
        tools=_base_tools_all(),
        commands=_base_commands_all(),
        prompts=["default", "us-west-2", "my-bucket", "8888", "bogus"],
        confirms=[True],
        env={"TERM_PROGRAM": "Terminal"},
    )

    rc = run_setup(ctx)
    print(f"\n  {BOLD}Exit code: {rc}{RESET}")


def scenario_everything_fails():
    """Nothing installed at all."""
    scenario_banner(
        "Everything Missing",
        "No mise, no aws, no container engine, no swiftc → 3 errors, exit 1"
    )

    ctx = ReplayContext(tools={}, commands={}, three_way=["no"])
    rc = run_setup(ctx)
    print(f"\n  {BOLD}Exit code: {rc}{RESET}")


def scenario_legacy_sso():
    """Profile uses legacy SSO style (no sso_session, has sso_account_id).

    Now prompts to upgrade legacy → modern sso-session config.
    Decline upgrade here to keep the legacy behavior visible.
    """
    scenario_banner(
        "Legacy SSO Configuration",
        "Profile has sso_account_id but no sso_session → detected as legacy,\n"
        "  user is offered upgrade to modern sso-session config"
    )

    commands = {
        **_base_commands_all(),
        ("aws", "configure", "get", "sso_session", "--profile", "default"):
            CmdResult(1),
        ("aws", "configure", "get", "sso_account_id", "--profile", "default"):
            CmdResult(0, "123456789\n"),
    }

    ctx = ReplayContext(
        tools=_base_tools_all(),
        commands=commands,
        prompts=["default", "us-west-2", "my-bucket", "8888", "notify"],
        confirms=[False, True],  # decline upgrade, start containers
        env={"TERM_PROGRAM": "Terminal"},
    )

    rc = run_setup(ctx)
    print(f"\n  {BOLD}Exit code: {rc}{RESET}")


def scenario_container_start_fails():
    """Containers fail to start."""
    scenario_banner(
        "Container Start Failure",
        "Everything works except containers:up fails → warns but setup still 'complete'"
    )

    commands = {
        **_base_commands_all(),
        ("mise", "run", "containers:up"): CmdResult(1, "", "compose error"),
    }

    ctx = ReplayContext(
        tools=_base_tools_all(),
        commands=commands,
        prompts=["default", "us-west-2", "my-bucket", "8888", "notify"],
        confirms=[True],
        env={"TERM_PROGRAM": "Terminal"},
    )

    rc = run_setup(ctx)
    print(f"\n  {BOLD}Exit code: {rc}{RESET}")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

SCENARIOS = {
    "1": ("Happy Path (everything works)", scenario_happy_path),
    "2": ("Missing AWS CLI", scenario_missing_aws),
    "3": ("AWS CLI Too Old (2.7)", scenario_aws_too_old),
    "4": ("No Container Engine", scenario_no_container_engine),
    "5": ("Docker Fallback (no podman)", scenario_docker_fallback),
    "6": ("No swiftc — user declines", scenario_no_swiftc_decline),
    "6b": ("No swiftc — user installs", scenario_no_swiftc_accept),
    "7": ("Keep Existing .env", scenario_existing_env_keep),
    "8": ("SSO Not Configured + Login Fails", scenario_sso_not_configured),
    "9": ("SSO Not Configured + Skip", scenario_sso_skip),
    "10": ("Expired Creds + Login OK", scenario_creds_expired_login_succeeds),
    "11": ("Expired Creds + Login Fails", scenario_creds_expired_login_fails),
    "12": ("Placeholder S3 Bucket", scenario_s3_bucket_placeholder),
    "13": ("Headless / SSH Session", scenario_headless_ssh),
    "14": ("mise install Fails", scenario_mise_install_fails),
    "15": ("Invalid SSO Mode", scenario_invalid_sso_mode),
    "16": ("Everything Missing", scenario_everything_fails),
    "17": ("Legacy SSO Config", scenario_legacy_sso),
    "18": ("Container Start Failure", scenario_container_start_fails),
    "all": ("Run ALL scenarios", None),
}


def run_all():
    for key, (name, fn) in SCENARIOS.items():
        if key == "all" or fn is None:
            continue
        fn()
        print(f"\n{'─' * 60}\n")


def main():
    if len(sys.argv) > 1:
        choice = sys.argv[1]
        if choice == "all":
            run_all()
            return
        if choice in SCENARIOS:
            SCENARIOS[choice][1]()
            return
        print(f"Unknown scenario: {choice}")
        sys.exit(1)

    # Menu
    print(f"\n{BOLD}Setup Replay Runner — Interactive Test Scenarios{RESET}\n")
    for key, (name, _) in SCENARIOS.items():
        print(f"  {BOLD}{key:>3}{RESET}  {name}")
    print()

    try:
        choice = input(f"  Pick scenario (or 'all'): ").strip()
    except (EOFError, KeyboardInterrupt):
        return

    if choice == "all":
        run_all()
    elif choice in SCENARIOS and SCENARIOS[choice][1]:
        SCENARIOS[choice][1]()
    else:
        print(f"Unknown: {choice}")


if __name__ == "__main__":
    main()
