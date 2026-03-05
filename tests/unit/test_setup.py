"""
Unit tests for scripts/setup.py — interactive first-time setup.
"""
import os
import re
import sys
from collections import deque
from pathlib import Path, PurePosixPath
from typing import Optional

import pytest

# Add scripts to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../scripts'))

from setup import (
    CmdResult,
    EnvConfig,
    PrereqResult,
    SetupContext,
    SsoCheckResult,
    check_aws_version,
    check_credentials_valid,
    check_macos_permissions,
    check_prerequisites,
    check_sso_configuration,
    configure_env,
    configure_sso,
    first_login_and_validate,
    generate_env_content,
    install_sso_watcher,
    install_tools,
    is_gui_session,
    list_aws_profiles,
    parse_aws_version,
    parse_existing_env,
    print_summary,
    prompt_env_config,
    run_setup,
    start_containers,
)


# ---------------------------------------------------------------------------
# MockSetupContext
# ---------------------------------------------------------------------------

class MockSetupContext(SetupContext):
    """Testable SetupContext with in-memory filesystem and configurable I/O."""

    def __init__(
        self,
        *,
        commands: dict | None = None,
        tools: set | None = None,
        prompts: list | None = None,
        confirms: list | None = None,
        three_way: list | None = None,
        files: dict | None = None,
        env: dict | None = None,
        repo_root: Path | None = None,
    ):
        super().__init__(repo_root=repo_root or Path("/fake/repo"), env=env or {})
        self._commands: dict = commands or {}
        self._tools: set = tools or set()
        self._prompts: deque = deque(prompts or [])
        self._confirms: deque = deque(confirms or [])
        self._three_way: deque = deque(three_way or [])
        # Virtual filesystem: str(path) → content
        self._files: dict[str, str] = {str(k): v for k, v in (files or {}).items()}
        self._dirs: set[str] = set()
        self._removed: list[str] = []

    # -- Commands --

    def run_cmd(self, cmd: list[str], timeout: int = 120,
                capture: bool = True, interactive: bool = False) -> CmdResult:
        # Try full tuple match first, then first-arg match
        key_full = tuple(cmd)
        key_first = cmd[0] if cmd else ""
        # Also try space-joined for convenience
        key_joined = " ".join(cmd)

        for k in (key_full, key_joined, key_first):
            if k in self._commands:
                val = self._commands[k]
                if callable(val):
                    return val(cmd)
                return val
        return CmdResult(0)  # default success

    def which(self, name: str) -> Optional[str]:
        if name in self._tools:
            return f"/usr/bin/{name}"
        return None

    # -- User interaction --

    def prompt(self, text: str, default: str = "") -> str:
        if self._prompts:
            val = self._prompts.popleft()
            return val if val else default
        return default

    def confirm(self, text: str, default: bool = True) -> bool:
        if self._confirms:
            return self._confirms.popleft()
        return default

    def confirm_three_way(self, text: str) -> str:
        if self._three_way:
            return self._three_way.popleft()
        return "yes"

    # -- Output (capture to _output) --

    def print(self, msg: str = "") -> None:
        self._output.append(msg)

    def ok(self, msg: str) -> None:
        self._output.append(f"[OK] {msg}")

    def warn(self, msg: str) -> None:
        self._output.append(f"[WARN] {msg}")

    def fail(self, msg: str) -> None:
        self._output.append(f"[FAIL] {msg}")

    def header(self, msg: str) -> None:
        self._output.append(f"[HDR] {msg}")

    # -- Filesystem --

    def file_exists(self, path: Path) -> bool:
        return str(path) in self._files

    def read_file(self, path: Path) -> str:
        key = str(path)
        if key not in self._files:
            raise OSError(f"File not found: {path}")
        return self._files[key]

    def write_file(self, path: Path, content: str) -> None:
        self._files[str(path)] = content

    def remove_file(self, path: Path) -> None:
        key = str(path)
        self._files.pop(key, None)
        self._removed.append(key)

    def mkdir(self, path: Path) -> None:
        self._dirs.add(str(path))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def output_text(ctx: MockSetupContext) -> str:
    """Join all output lines for assertion."""
    return "\n".join(ctx.get_output())


# ===================================================================
# TestParseAwsVersion
# ===================================================================

class TestParseAwsVersion:
    def test_standard_format(self):
        assert parse_aws_version("aws-cli/2.15.0 Python/3.11") == "2.15.0"

    def test_no_match(self):
        assert parse_aws_version("something else") is None

    def test_empty(self):
        assert parse_aws_version("") is None


# ===================================================================
# TestCheckAwsVersion
# ===================================================================

class TestCheckAwsVersion:
    def test_exact_minimum(self):
        assert check_aws_version("2.9.0") is True

    def test_above_minimum(self):
        assert check_aws_version("2.15.0") is True

    def test_below_minimum(self):
        assert check_aws_version("2.8.0") is False

    def test_major_3(self):
        assert check_aws_version("3.0.0") is True

    def test_major_1(self):
        assert check_aws_version("1.27.0") is False

    def test_invalid(self):
        assert check_aws_version("abc") is False

    def test_empty(self):
        assert check_aws_version("") is False


# ===================================================================
# TestCheckPrerequisites
# ===================================================================

class TestCheckPrerequisites:
    def _ctx(self, tools=None, commands=None, three_way=None):
        return MockSetupContext(
            tools=tools or set(),
            commands=commands or {},
            three_way=three_way or [],
        )

    def test_all_present(self):
        ctx = self._ctx(
            tools={"mise", "aws", "podman", "swiftc"},
            commands={
                ("mise", "--version"): CmdResult(0, "2024.1.0\n"),
                ("aws", "--version"): CmdResult(0, "aws-cli/2.15.0 Python/3.11\n"),
                ("podman", "--version"): CmdResult(0, "podman version 4.9.0\n"),
            },
        )
        r = check_prerequisites(ctx)
        assert r.ok
        assert r.mise is not None
        assert r.aws is not None
        assert r.container is not None
        assert r.container.name == "podman"
        assert r.swiftc is not None
        assert len(r.errors) == 0
        assert len(r.warnings) == 0

    def test_missing_mise(self):
        ctx = self._ctx(
            tools={"aws", "podman", "swiftc"},
            commands={
                ("aws", "--version"): CmdResult(0, "aws-cli/2.15.0 Python/3.11\n"),
                ("podman", "--version"): CmdResult(0, "podman version 4.9.0\n"),
            },
        )
        r = check_prerequisites(ctx)
        assert not r.ok
        assert any("mise not found" in e for e in r.errors)

    def test_missing_aws(self):
        ctx = self._ctx(
            tools={"mise", "podman", "swiftc"},
            commands={
                ("mise", "--version"): CmdResult(0, "2024.1.0\n"),
                ("podman", "--version"): CmdResult(0, "podman version 4.9.0\n"),
            },
        )
        r = check_prerequisites(ctx)
        assert not r.ok
        assert any("aws not found" in e for e in r.errors)

    def test_aws_too_old(self):
        ctx = self._ctx(
            tools={"mise", "aws", "podman", "swiftc"},
            commands={
                ("mise", "--version"): CmdResult(0, "2024.1.0\n"),
                ("aws", "--version"): CmdResult(0, "aws-cli/2.8.0 Python/3.11\n"),
                ("podman", "--version"): CmdResult(0, "podman version 4.9.0\n"),
            },
        )
        r = check_prerequisites(ctx)
        assert not r.ok
        assert any("too old" in e for e in r.errors)

    def test_aws_version_unknown(self):
        ctx = self._ctx(
            tools={"mise", "aws", "podman", "swiftc"},
            commands={
                ("mise", "--version"): CmdResult(0, "2024.1.0\n"),
                ("aws", "--version"): CmdResult(0, "gibberish output\n"),
                ("podman", "--version"): CmdResult(0, "podman version 4.9.0\n"),
            },
        )
        r = check_prerequisites(ctx)
        assert not r.ok
        assert any("version unknown" in e for e in r.errors)

    def test_docker_fallback(self):
        ctx = self._ctx(
            tools={"mise", "aws", "docker", "swiftc"},
            commands={
                ("mise", "--version"): CmdResult(0, "2024.1.0\n"),
                ("aws", "--version"): CmdResult(0, "aws-cli/2.15.0 Python/3.11\n"),
                ("docker", "--version"): CmdResult(0, "Docker version 24.0.0, build abc\n"),
            },
        )
        r = check_prerequisites(ctx)
        assert r.ok
        assert r.container is not None
        assert r.container.name == "docker"

    def test_no_container_engine(self):
        ctx = self._ctx(
            tools={"mise", "aws", "swiftc"},
            commands={
                ("mise", "--version"): CmdResult(0, "2024.1.0\n"),
                ("aws", "--version"): CmdResult(0, "aws-cli/2.15.0 Python/3.11\n"),
            },
        )
        r = check_prerequisites(ctx)
        assert not r.ok
        assert any("container" in e for e in r.errors)

    def test_missing_swiftc_is_warning(self):
        ctx = self._ctx(
            tools={"mise", "aws", "podman"},
            commands={
                ("mise", "--version"): CmdResult(0, "2024.1.0\n"),
                ("aws", "--version"): CmdResult(0, "aws-cli/2.15.0 Python/3.11\n"),
                ("podman", "--version"): CmdResult(0, "podman version 4.9.0\n"),
            },
            three_way=["no"],
        )
        r = check_prerequisites(ctx)
        assert r.ok  # swiftc is optional
        assert len(r.errors) == 0
        assert any("swiftc" in w for w in r.warnings)

    def test_all_missing(self):
        ctx = self._ctx(tools=set(), three_way=["no"])
        r = check_prerequisites(ctx)
        assert not r.ok
        # mise, aws, container
        assert len(r.errors) == 3

    def test_prereqs_ok_property(self):
        r = PrereqResult(errors=[])
        assert r.ok is True
        r2 = PrereqResult(errors=["x"])
        assert r2.ok is False


# ===================================================================
# TestListAwsProfiles
# ===================================================================

class TestListAwsProfiles:
    def test_reads_profiles(self):
        config_path = str(Path.home() / ".aws" / "config")
        content = (
            "[default]\nregion=us-east-1\n\n"
            "[profile dev]\nregion=us-west-2\n\n"
            "[profile staging]\nregion=eu-west-1\n"
        )
        ctx = MockSetupContext(files={config_path: content})
        result = list_aws_profiles(ctx)
        assert result == ["dev", "staging"]

    def test_no_config_file(self):
        ctx = MockSetupContext()
        result = list_aws_profiles(ctx)
        assert result == []

    def test_empty_config(self):
        config_path = str(Path.home() / ".aws" / "config")
        ctx = MockSetupContext(files={config_path: ""})
        result = list_aws_profiles(ctx)
        assert result == []

    def test_config_with_default_section(self):
        config_path = str(Path.home() / ".aws" / "config")
        ctx = MockSetupContext(files={config_path: "[default]\nregion=us-east-1\n"})
        result = list_aws_profiles(ctx)
        assert result == []


# ===================================================================
# TestPromptEnvConfig
# ===================================================================

class TestPromptEnvConfig:
    def test_all_defaults(self):
        # All empty strings → use defaults
        ctx = MockSetupContext(prompts=["", "", "", "", ""])
        config = prompt_env_config(ctx)
        assert config.aws_profile == "default"
        assert config.aws_region == "us-west-2"
        assert config.s3_bucket == "your-maven-bucket"
        assert config.proxy_port == "8888"
        assert config.sso_mode == "notify"

    def test_custom_values(self):
        ctx = MockSetupContext(
            prompts=["my-profile", "eu-west-1", "my-bucket", "9999", "auto"]
        )
        config = prompt_env_config(ctx)
        assert config.aws_profile == "my-profile"
        assert config.aws_region == "eu-west-1"
        assert config.s3_bucket == "my-bucket"
        assert config.proxy_port == "9999"
        assert config.sso_mode == "auto"

    def test_invalid_sso_mode_defaults_to_notify(self):
        ctx = MockSetupContext(
            prompts=["", "", "", "", "bogus"]
        )
        config = prompt_env_config(ctx)
        assert config.sso_mode == "notify"
        assert any("Invalid mode" in m for m in ctx.get_output())

    def test_profiles_shown_when_available(self):
        config_path = str(Path.home() / ".aws" / "config")
        ctx = MockSetupContext(
            files={config_path: "[profile foo]\n[profile bar]\n"},
            prompts=["", "", "", "", ""],
        )
        prompt_env_config(ctx)
        out = output_text(ctx)
        assert "foo" in out
        assert "bar" in out

    def test_skip_tls_verify_default_false(self):
        ctx = MockSetupContext(prompts=["", "", "", "", ""])
        config = prompt_env_config(ctx)
        assert config.skip_tls_verify is False

    def test_skip_tls_verify_enabled(self):
        ctx = MockSetupContext(
            prompts=["", "", "", "", ""],
            confirms=[True],
        )
        config = prompt_env_config(ctx)
        assert config.skip_tls_verify is True


# ===================================================================
# TestGenerateEnvContent
# ===================================================================

class TestGenerateEnvContent:
    def test_default_config(self):
        content = generate_env_content(EnvConfig())
        assert 'AWS_PROFILE="default"' in content
        assert 'AWS_REGION="us-west-2"' in content
        assert 'S3_BUCKET_NAME="your-maven-bucket"' in content
        assert 'PROXY_PORT="8888"' in content
        assert 'SSO_LOGIN_MODE="notify"' in content

    def test_custom_config(self):
        cfg = EnvConfig(
            aws_profile="prod", aws_region="ap-south-1",
            s3_bucket="prod-bucket", proxy_port="7777", sso_mode="silent"
        )
        content = generate_env_content(cfg)
        assert 'AWS_PROFILE="prod"' in content
        assert 'AWS_REGION="ap-south-1"' in content
        assert 'S3_BUCKET_NAME="prod-bucket"' in content
        assert 'PROXY_PORT="7777"' in content
        assert 'SSO_LOGIN_MODE="silent"' in content

    def test_contains_hardcoded_values(self):
        content = generate_env_content(EnvConfig())
        assert "REFRESH_INTERVAL=60000" in content
        assert "CHECK_INTERVAL=60" in content
        assert "SSO_COOLDOWN_SECONDS=600" in content
        assert "SSO_POLL_SECONDS=5" in content
        assert "SSO_PROACTIVE_REFRESH_MINUTES=30" in content
        assert "LOG_LEVEL=info" in content

    def test_container_engine_commented(self):
        content = generate_env_content(EnvConfig())
        assert "# CONTAINER_ENGINE=podman" in content

    def test_skip_tls_verify_false_is_commented(self):
        content = generate_env_content(EnvConfig())
        assert "# SKIP_TLS_VERIFY=false" in content
        assert "SKIP_TLS_VERIFY=true" not in content

    def test_skip_tls_verify_true_is_uncommented(self):
        content = generate_env_content(EnvConfig(skip_tls_verify=True))
        assert "SKIP_TLS_VERIFY=true" in content
        assert "# SKIP_TLS_VERIFY=false" not in content


# ===================================================================
# TestParseExistingEnv
# ===================================================================

class TestParseExistingEnv:
    def test_parses_all_fields(self):
        env_content = (
            'AWS_PROFILE="dev"\n'
            'AWS_REGION="eu-west-1"\n'
            'S3_BUCKET_NAME="my-bucket"\n'
            'PROXY_PORT="9999"\n'
            'SSO_LOGIN_MODE="auto"\n'
        )
        env_path = Path("/fake/.env")
        ctx = MockSetupContext(files={str(env_path): env_content})
        cfg = parse_existing_env(ctx, env_path)
        assert cfg.aws_profile == "dev"
        assert cfg.aws_region == "eu-west-1"
        assert cfg.s3_bucket == "my-bucket"
        assert cfg.proxy_port == "9999"
        assert cfg.sso_mode == "auto"

    def test_handles_quoted_values(self):
        env_content = 'AWS_PROFILE="my-profile"\n'
        env_path = Path("/fake/.env")
        ctx = MockSetupContext(files={str(env_path): env_content})
        cfg = parse_existing_env(ctx, env_path)
        assert cfg.aws_profile == "my-profile"

    def test_ignores_comments(self):
        env_content = (
            '# This is a comment\n'
            'AWS_PROFILE="active"\n'
            '# AWS_REGION="ignored"\n'
        )
        env_path = Path("/fake/.env")
        ctx = MockSetupContext(files={str(env_path): env_content})
        cfg = parse_existing_env(ctx, env_path)
        assert cfg.aws_profile == "active"
        assert cfg.aws_region == "us-west-2"  # default, comment was ignored

    def test_missing_file(self):
        env_path = Path("/fake/nonexistent/.env")
        ctx = MockSetupContext()
        cfg = parse_existing_env(ctx, env_path)
        # Should return defaults
        assert cfg.aws_profile == "default"
        assert cfg.aws_region == "us-west-2"

    def test_partial_env(self):
        env_content = 'AWS_PROFILE="custom"\n'
        env_path = Path("/fake/.env")
        ctx = MockSetupContext(files={str(env_path): env_content})
        cfg = parse_existing_env(ctx, env_path)
        assert cfg.aws_profile == "custom"
        assert cfg.aws_region == "us-west-2"  # default
        assert cfg.s3_bucket == "your-maven-bucket"  # default

    def test_skip_tls_verify_true(self):
        env_content = 'SKIP_TLS_VERIFY=true\n'
        env_path = Path("/fake/.env")
        ctx = MockSetupContext(files={str(env_path): env_content})
        cfg = parse_existing_env(ctx, env_path)
        assert cfg.skip_tls_verify is True

    def test_skip_tls_verify_false(self):
        env_content = 'SKIP_TLS_VERIFY=false\n'
        env_path = Path("/fake/.env")
        ctx = MockSetupContext(files={str(env_path): env_content})
        cfg = parse_existing_env(ctx, env_path)
        assert cfg.skip_tls_verify is False

    def test_skip_tls_verify_missing_defaults_false(self):
        env_content = 'AWS_PROFILE="dev"\n'
        env_path = Path("/fake/.env")
        ctx = MockSetupContext(files={str(env_path): env_content})
        cfg = parse_existing_env(ctx, env_path)
        assert cfg.skip_tls_verify is False


# ===================================================================
# TestConfigureEnv
# ===================================================================

class TestConfigureEnv:
    def test_no_existing_env_prompts_and_writes(self):
        ctx = MockSetupContext(
            repo_root=Path("/fake/repo"),
            prompts=["myprof", "us-east-1", "mybucket", "8080", "auto"],
        )
        cfg = configure_env(ctx)
        assert cfg.aws_profile == "myprof"
        assert cfg.s3_bucket == "mybucket"
        # File written
        written = ctx._files.get(str(Path("/fake/repo/.env")))
        assert written is not None
        assert 'AWS_PROFILE="myprof"' in written

    def test_existing_env_keep(self):
        env_path = str(Path("/fake/repo/.env"))
        env_content = (
            'AWS_PROFILE="existing"\n'
            'AWS_REGION="us-west-1"\n'
            'S3_BUCKET_NAME="old-bucket"\n'
            'PROXY_PORT="7777"\n'
            'SSO_LOGIN_MODE="silent"\n'
        )
        ctx = MockSetupContext(
            repo_root=Path("/fake/repo"),
            files={env_path: env_content},
            confirms=[False],  # Don't overwrite
        )
        cfg = configure_env(ctx)
        assert cfg.aws_profile == "existing"
        assert cfg.s3_bucket == "old-bucket"
        # Original content preserved
        assert ctx._files[env_path] == env_content

    def test_existing_env_overwrite(self):
        env_path = str(Path("/fake/repo/.env"))
        ctx = MockSetupContext(
            repo_root=Path("/fake/repo"),
            files={env_path: "OLD_CONTENT"},
            confirms=[True],  # Overwrite
            prompts=["new-prof", "ap-south-1", "new-bucket", "5555", "standalone"],
        )
        cfg = configure_env(ctx)
        assert cfg.aws_profile == "new-prof"
        written = ctx._files[env_path]
        assert 'AWS_PROFILE="new-prof"' in written
        assert "OLD_CONTENT" not in written


# ===================================================================
# TestInstallTools
# ===================================================================

class TestInstallTools:
    def test_success(self):
        ctx = MockSetupContext(commands={
            ("mise", "install", "--yes"): CmdResult(0, "installed\n"),
            ("python3", "--version"): CmdResult(0, "Python 3.11.5\n"),
        })
        assert install_tools(ctx) is True
        out = output_text(ctx)
        assert "3.11.5" in out

    def test_mise_install_fails(self):
        ctx = MockSetupContext(commands={
            ("mise", "install", "--yes"): CmdResult(1, "", "error\n"),
        })
        assert install_tools(ctx) is False
        assert any("failed" in m.lower() for m in ctx.get_output())


# ===================================================================
# TestInstallSsoWatcher
# ===================================================================

class TestInstallSsoWatcher:
    def test_success(self):
        ctx = MockSetupContext(commands={
            ("mise", "run", "sso-install"): CmdResult(0),
        })
        assert install_sso_watcher(ctx) is True

    def test_failure(self):
        ctx = MockSetupContext(commands={
            ("mise", "run", "sso-install"): CmdResult(1, "", "error"),
        })
        assert install_sso_watcher(ctx) is False
        assert any("failed" in m.lower() for m in ctx.get_output())


# ===================================================================
# TestIsGuiSession
# ===================================================================

class TestIsGuiSession:
    def test_display_set(self):
        ctx = MockSetupContext(env={"DISPLAY": ":0"})
        assert is_gui_session(ctx) is True

    def test_term_program_set(self):
        ctx = MockSetupContext(env={"TERM_PROGRAM": "Apple_Terminal"})
        assert is_gui_session(ctx) is True

    def test_window_server_running(self):
        ctx = MockSetupContext(
            env={},
            commands={("pgrep", "-q", "WindowServer"): CmdResult(0)},
        )
        assert is_gui_session(ctx) is True

    def test_headless(self):
        ctx = MockSetupContext(
            env={},
            commands={("pgrep", "-q", "WindowServer"): CmdResult(1)},
        )
        assert is_gui_session(ctx) is False


# ===================================================================
# TestCheckMacosPermissions
# ===================================================================

class TestCheckMacosPermissions:
    def _osascript_system_events(self):
        return (
            "osascript", "-e",
            'tell application "System Events" to return name of current user'
        )

    def _osascript_dialog(self):
        return (
            "osascript", "-e",
            'display dialog "Setup complete — SSO watcher permissions verified." '
            'buttons {"OK"} default button "OK"'
        )

    def test_all_ok(self):
        ctx = MockSetupContext(
            env={"DISPLAY": ":0"},
            commands={
                self._osascript_system_events(): CmdResult(0, "user\n"),
                self._osascript_dialog(): CmdResult(0),
            },
        )
        r = check_macos_permissions(ctx)
        assert r["system_events"] is True
        assert r["dialog"] is True
        assert r["skipped"] is False
        assert r["failed"] is False

    def test_system_events_denied_fails_setup(self):
        ctx = MockSetupContext(
            env={"DISPLAY": ":0"},
            commands={
                self._osascript_system_events(): CmdResult(1, "", "denied"),
                self._osascript_dialog(): CmdResult(0),
            },
        )
        r = check_macos_permissions(ctx)
        assert r["failed"] is True
        assert r["system_events"] is False
        # dialog is never reached — early return on System Events failure
        assert r["dialog"] is False

    def test_system_events_timeout_fails_setup(self):
        ctx = MockSetupContext(
            env={"DISPLAY": ":0"},
            commands={
                self._osascript_system_events(): CmdResult(-1, "", "timeout"),
            },
        )
        r = check_macos_permissions(ctx)
        assert r["failed"] is True
        assert r["system_events"] is False
        assert any("timed out" in l for l in ctx._output)

    def test_dialog_denied_fails_setup(self):
        ctx = MockSetupContext(
            env={"DISPLAY": ":0"},
            commands={
                self._osascript_system_events(): CmdResult(0, "user\n"),
                self._osascript_dialog(): CmdResult(1, "", "denied"),
            },
        )
        r = check_macos_permissions(ctx)
        assert r["failed"] is True
        assert r["system_events"] is True
        assert r["dialog"] is False

    def test_dialog_timeout_fails_setup(self):
        ctx = MockSetupContext(
            env={"DISPLAY": ":0"},
            commands={
                self._osascript_system_events(): CmdResult(0, "user\n"),
                self._osascript_dialog(): CmdResult(-1, "", "timeout"),
            },
        )
        r = check_macos_permissions(ctx)
        assert r["failed"] is True
        assert r["dialog"] is False
        assert any("timed out" in l for l in ctx._output)

    def test_headless_skips(self):
        ctx = MockSetupContext(
            env={},
            commands={("pgrep", "-q", "WindowServer"): CmdResult(1)},
        )
        r = check_macos_permissions(ctx)
        assert r["skipped"] is True
        assert r["failed"] is False
        assert r["system_events"] is False
        assert r["dialog"] is False


# ===================================================================
# TestCheckPrerequisitesSwiftc
# ===================================================================

class TestCheckPrerequisitesSwiftc:
    """Tests for swiftc missing → prompt to install Xcode CLT."""

    def _ctx(self, *, three_way: str, xcode_result: CmdResult = None):
        cmds = {
            ("mise", "--version"): CmdResult(0, "2024.1.0\n"),
            ("aws", "--version"): CmdResult(0, "aws-cli/2.15.0 Python/3.11\n"),
            ("podman", "--version"): CmdResult(0, "podman version 5.0.0\n"),
        }
        if xcode_result is not None:
            cmds[("xcode-select", "--install")] = xcode_result
        return MockSetupContext(
            tools={"mise", "aws", "podman"},  # no swiftc
            commands=cmds,
            three_way=[three_way],
        )

    def test_user_says_yes_install_succeeds(self):
        ctx = self._ctx(three_way="yes", xcode_result=CmdResult(0))
        r = check_prerequisites(ctx)
        assert r.ok
        assert any("xcode-clt installing" in w for w in r.warnings)
        assert any("re-run setup" in l for l in ctx._output)

    def test_user_says_yes_install_fails(self):
        ctx = self._ctx(three_way="yes", xcode_result=CmdResult(1, "", "error"))
        r = check_prerequisites(ctx)
        assert r.ok  # swiftc is still optional
        assert any("swiftc not found" in w for w in r.warnings)

    def test_user_says_no(self):
        ctx = self._ctx(three_way="no")
        r = check_prerequisites(ctx)
        assert r.ok
        assert any("swiftc not found" in w for w in r.warnings)
        assert any("browser instead" in l for l in ctx._output)

    def test_user_says_skip(self):
        ctx = self._ctx(three_way="skip")
        r = check_prerequisites(ctx)
        assert r.ok
        assert any("swiftc not found" in w for w in r.warnings)

    def test_swiftc_present_no_prompt(self):
        """When swiftc exists, no three_way prompt should fire."""
        ctx = MockSetupContext(
            tools={"mise", "aws", "podman", "swiftc"},
            commands={
                ("mise", "--version"): CmdResult(0, "2024.1.0\n"),
                ("aws", "--version"): CmdResult(0, "aws-cli/2.15.0 Python/3.11\n"),
                ("podman", "--version"): CmdResult(0, "podman version 5.0.0\n"),
            },
            three_way=[],  # empty — would crash if consumed
        )
        r = check_prerequisites(ctx)
        assert r.swiftc is not None
        assert len(r.warnings) == 0


# ===================================================================
# TestCheckSsoConfiguration
# ===================================================================

class TestCheckSsoConfiguration:
    def test_modern_sso_session(self):
        ctx = MockSetupContext(commands={
            ("aws", "configure", "get", "sso_session", "--profile", "dev"):
                CmdResult(0, "my-sso-session\n"),
        })
        r = check_sso_configuration(ctx, "dev")
        assert r.configured is True
        assert r.style == "modern"
        assert r.session_name == "my-sso-session"

    def test_legacy_sso(self):
        ctx = MockSetupContext(commands={
            ("aws", "configure", "get", "sso_session", "--profile", "dev"):
                CmdResult(1),
            ("aws", "configure", "get", "sso_account_id", "--profile", "dev"):
                CmdResult(0, "123456789012\n"),
        })
        r = check_sso_configuration(ctx, "dev")
        assert r.configured is True
        assert r.style == "legacy"

    def test_no_sso(self):
        ctx = MockSetupContext(commands={
            ("aws", "configure", "get", "sso_session", "--profile", "dev"):
                CmdResult(1),
            ("aws", "configure", "get", "sso_account_id", "--profile", "dev"):
                CmdResult(1),
        })
        r = check_sso_configuration(ctx, "dev")
        assert r.configured is False
        assert r.style == "none"

    def test_modern_empty_output(self):
        ctx = MockSetupContext(commands={
            ("aws", "configure", "get", "sso_session", "--profile", "dev"):
                CmdResult(0, "\n"),
            ("aws", "configure", "get", "sso_account_id", "--profile", "dev"):
                CmdResult(0, "123456789012\n"),
        })
        r = check_sso_configuration(ctx, "dev")
        # Empty stdout → falls through to legacy
        assert r.configured is True
        assert r.style == "legacy"


# ===================================================================
# TestConfigureSso
# ===================================================================

class TestConfigureSso:

    def _not_configured(self):
        """Commands that indicate profile has no SSO config."""
        return {
            ("aws", "configure", "get", "sso_session", "--profile", "dev"):
                CmdResult(1),
            ("aws", "configure", "get", "sso_account_id", "--profile", "dev"):
                CmdResult(1),
        }

    def test_already_configured_modern(self):
        ctx = MockSetupContext(commands={
            ("aws", "configure", "get", "sso_session", "--profile", "dev"):
                CmdResult(0, "my-session\n"),
        })
        assert configure_sso(ctx, "dev") is True
        out = output_text(ctx)
        assert "sso-session" in out

    def test_already_configured_legacy(self):
        ctx = MockSetupContext(commands={
            ("aws", "configure", "get", "sso_session", "--profile", "dev"):
                CmdResult(1),
            ("aws", "configure", "get", "sso_account_id", "--profile", "dev"):
                CmdResult(0, "123456789012\n"),
        })
        assert configure_sso(ctx, "dev") is True
        out = output_text(ctx)
        assert "legacy" in out

    def test_not_configured_user_says_yes_writes_config(self):
        home = str(Path.home())
        config_path = f"{home}/.aws/config"
        ctx = MockSetupContext(
            commands=self._not_configured(),
            three_way=["yes"],
            prompts=[
                "https://myorg.awsapps.com/start",
                "us-east-1",
                "123456789012",
                "MyRole",
            ],
            files={config_path: ""},
        )
        assert configure_sso(ctx, "dev") is True
        content = ctx._files[config_path]
        assert "[profile dev]" in content
        assert "sso_account_id = 123456789012" in content
        assert "sso_role_name = MyRole" in content
        assert "sso_session = dev" in content
        assert "[sso-session dev]" in content
        assert "sso_start_url = https://myorg.awsapps.com/start" in content
        assert "sso_region = us-east-1" in content
        assert "sso_registration_scopes = sso:account:access" in content

    def test_not_configured_user_says_no(self):
        ctx = MockSetupContext(
            commands=self._not_configured(),
            three_way=["no"],
        )
        assert configure_sso(ctx, "dev") is False

    def test_not_configured_user_says_skip(self):
        ctx = MockSetupContext(
            commands=self._not_configured(),
            three_way=["skip"],
        )
        assert configure_sso(ctx, "dev") is False

    def test_missing_account_id_returns_false(self):
        ctx = MockSetupContext(
            commands=self._not_configured(),
            three_way=["yes"],
            prompts=[
                "https://myorg.awsapps.com/start",
                "us-east-1",
                "",   # empty account ID
                "MyRole",
            ],
        )
        assert configure_sso(ctx, "dev") is False
        out = output_text(ctx)
        assert "account id" in out.lower()

    def test_missing_role_name_returns_false(self):
        ctx = MockSetupContext(
            commands=self._not_configured(),
            three_way=["yes"],
            prompts=[
                "https://myorg.awsapps.com/start",
                "us-east-1",
                "123456789012",
                "",   # empty role name
            ],
        )
        assert configure_sso(ctx, "dev") is False
        out = output_text(ctx)
        assert "role name" in out.lower()

    def test_appends_to_existing_config(self):
        home = str(Path.home())
        config_path = f"{home}/.aws/config"
        existing = "[profile other]\nregion = us-west-2\n"
        ctx = MockSetupContext(
            commands=self._not_configured(),
            three_way=["yes"],
            prompts=[
                "https://myorg.awsapps.com/start",
                "us-east-1",
                "123456789012",
                "MyRole",
            ],
            files={config_path: existing},
        )
        assert configure_sso(ctx, "dev") is True
        content = ctx._files[config_path]
        # Old content preserved
        assert "[profile other]" in content
        # New content appended
        assert "[profile dev]" in content
        assert "[sso-session dev]" in content

    def test_duplicate_profile_not_overwritten(self):
        home = str(Path.home())
        config_path = f"{home}/.aws/config"
        existing = "[profile dev]\nsso_account_id = old\n"
        ctx = MockSetupContext(
            commands=self._not_configured(),
            three_way=["yes"],
            prompts=[
                "https://myorg.awsapps.com/start",
                "us-east-1",
                "123456789012",
                "MyRole",
            ],
            files={config_path: existing},
        )
        assert configure_sso(ctx, "dev") is False
        out = output_text(ctx)
        assert "already exists" in out.lower()

    def test_creates_config_file_if_missing(self):
        home = str(Path.home())
        config_path = f"{home}/.aws/config"
        ctx = MockSetupContext(
            commands=self._not_configured(),
            three_way=["yes"],
            prompts=[
                "https://myorg.awsapps.com/start",
                "us-east-1",
                "123456789012",
                "MyRole",
            ],
            # no files — config doesn't exist
        )
        assert configure_sso(ctx, "dev") is True
        content = ctx._files[config_path]
        assert "[profile dev]" in content
        assert "sso_registration_scopes = sso:account:access" in content


# ===================================================================
# TestCheckCredentialsValid
# ===================================================================

class TestCheckCredentialsValid:
    def test_valid(self):
        ctx = MockSetupContext(commands={
            ("aws", "sts", "get-caller-identity", "--profile", "dev"):
                CmdResult(0, '{"Account":"123"}\n'),
        })
        assert check_credentials_valid(ctx, "dev") is True

    def test_invalid(self):
        ctx = MockSetupContext(commands={
            ("aws", "sts", "get-caller-identity", "--profile", "dev"):
                CmdResult(1, "", "expired"),
        })
        assert check_credentials_valid(ctx, "dev") is False


# ===================================================================
# TestFirstLoginAndValidate
# ===================================================================

class TestFirstLoginAndValidate:
    def _sts_cmd(self, profile="default"):
        return ("aws", "sts", "get-caller-identity", "--profile", profile)

    def _s3_cmd(self, bucket, profile="default"):
        return ("aws", "s3", "ls", f"s3://{bucket}/", "--profile", profile)

    def test_sso_not_configured_skips(self):
        ctx = MockSetupContext()
        cfg = EnvConfig()
        r = first_login_and_validate(ctx, cfg, sso_configured=False)
        assert r["skipped"] is True

    def test_credentials_already_valid(self):
        ctx = MockSetupContext(commands={
            self._sts_cmd(): CmdResult(0, '{"Account":"123"}\n'),
        })
        cfg = EnvConfig(s3_bucket="")
        r = first_login_and_validate(ctx, cfg, sso_configured=True)
        assert r["credentials_valid"] is True
        assert r["login_attempted"] is False

    def test_login_succeeds(self):
        # sts fails first, then login succeeds
        call_count = {"sts": 0}
        def sts_handler(cmd):
            call_count["sts"] += 1
            if call_count["sts"] == 1:
                return CmdResult(1, "", "expired")
            return CmdResult(0, '{"Account":"123"}\n')

        ctx = MockSetupContext(commands={
            self._sts_cmd(): sts_handler,
            "python3": CmdResult(0),  # do_sso_login
        })
        cfg = EnvConfig(s3_bucket="")
        r = first_login_and_validate(ctx, cfg, sso_configured=True)
        assert r["login_attempted"] is True
        assert r["login_ok"] is True
        assert r["credentials_valid"] is True

    def test_login_fails(self):
        ctx = MockSetupContext(commands={
            self._sts_cmd(): CmdResult(1, "", "expired"),
            "python3": CmdResult(1, "", "login failed"),
        })
        cfg = EnvConfig(s3_bucket="")
        r = first_login_and_validate(ctx, cfg, sso_configured=True)
        assert r["login_attempted"] is True
        assert r["login_ok"] is False

    def test_mode_file_saved_and_restored(self):
        mode_path = str(Path.home() / ".aws" / "sso-renewer" / "mode")
        ctx = MockSetupContext(
            files={mode_path: "auto"},
            commands={
                self._sts_cmd(): CmdResult(1, "", "expired"),
                "python3": CmdResult(0),  # login succeeds
            },
        )
        cfg = EnvConfig(s3_bucket="")
        first_login_and_validate(ctx, cfg, sso_configured=True)
        # Mode should be restored to "auto"
        assert ctx._files[mode_path] == "auto"

    def test_mode_file_removed_when_none_existed(self):
        mode_path = str(Path.home() / ".aws" / "sso-renewer" / "mode")
        ctx = MockSetupContext(
            commands={
                self._sts_cmd(): CmdResult(1, "", "expired"),
                "python3": CmdResult(0),  # login succeeds
            },
        )
        cfg = EnvConfig(s3_bucket="")
        first_login_and_validate(ctx, cfg, sso_configured=True)
        # Mode file should be removed
        assert mode_path in ctx._removed

    def test_s3_validation_succeeds(self):
        ctx = MockSetupContext(commands={
            self._sts_cmd(): CmdResult(0, '{"Account":"123"}\n'),
            self._s3_cmd("my-bucket"): CmdResult(0, "listing\n"),
        })
        cfg = EnvConfig(s3_bucket="my-bucket")
        r = first_login_and_validate(ctx, cfg, sso_configured=True)
        assert r["s3_accessible"] is True

    def test_s3_validation_fails(self):
        ctx = MockSetupContext(commands={
            self._sts_cmd(): CmdResult(0, '{"Account":"123"}\n'),
            self._s3_cmd("my-bucket"): CmdResult(1, "", "access denied"),
        })
        cfg = EnvConfig(s3_bucket="my-bucket")
        r = first_login_and_validate(ctx, cfg, sso_configured=True)
        assert r["s3_accessible"] is False

    def test_s3_skipped_placeholder_bucket(self):
        ctx = MockSetupContext(commands={
            self._sts_cmd(): CmdResult(0, '{"Account":"123"}\n'),
        })
        cfg = EnvConfig(s3_bucket="your-maven-bucket")
        r = first_login_and_validate(ctx, cfg, sso_configured=True)
        assert r["s3_skipped"] is True

    def test_s3_skipped_empty_bucket(self):
        ctx = MockSetupContext(commands={
            self._sts_cmd(): CmdResult(0, '{"Account":"123"}\n'),
        })
        cfg = EnvConfig(s3_bucket="")
        r = first_login_and_validate(ctx, cfg, sso_configured=True)
        assert r["s3_skipped"] is True

    def test_login_failure_restores_mode(self):
        mode_path = str(Path.home() / ".aws" / "sso-renewer" / "mode")
        ctx = MockSetupContext(
            files={mode_path: "notify"},
            commands={
                self._sts_cmd(): CmdResult(1, "", "expired"),
                "python3": CmdResult(1, "", "login failed"),
            },
        )
        cfg = EnvConfig(s3_bucket="")
        first_login_and_validate(ctx, cfg, sso_configured=True)
        # Mode still restored even on failure
        assert ctx._files[mode_path] == "notify"


# ===================================================================
# TestStartContainers
# ===================================================================

class TestStartContainers:
    def test_user_says_yes(self):
        ctx = MockSetupContext(
            confirms=[True],
            commands={("mise", "run", "containers:up"): CmdResult(0)},
        )
        assert start_containers(ctx) is True

    def test_user_says_no(self):
        ctx = MockSetupContext(confirms=[False])
        assert start_containers(ctx) is False

    def test_start_fails(self):
        ctx = MockSetupContext(
            confirms=[True],
            commands={("mise", "run", "containers:up"): CmdResult(1, "", "error")},
        )
        assert start_containers(ctx) is False
        assert any("failed" in m.lower() for m in ctx.get_output())


# ===================================================================
# TestPrintSummary
# ===================================================================

class TestPrintSummary:
    def test_includes_port(self):
        ctx = MockSetupContext()
        print_summary(ctx, EnvConfig(proxy_port="9090"))
        out = output_text(ctx)
        assert "9090" in out

    def test_includes_commands(self):
        ctx = MockSetupContext()
        print_summary(ctx, EnvConfig())
        out = output_text(ctx)
        assert "mise run containers:logs" in out
        assert "mise run sso-status" in out
        assert "healthz" in out


# ===================================================================
# TestRunSetup
# ===================================================================

class TestRunSetup:
    def _all_tools_ctx(self, *, extra_commands=None, extra_files=None,
                       prompts=None, confirms=None, three_way=None,
                       tools=None, env=None):
        """Build a ctx with all prereqs satisfied by default."""
        base_tools = {"mise", "aws", "podman", "swiftc"}
        if tools is not None:
            base_tools = tools

        base_commands = {
            ("mise", "--version"): CmdResult(0, "2024.1.0\n"),
            ("aws", "--version"): CmdResult(0, "aws-cli/2.15.0 Python/3.11\n"),
            ("podman", "--version"): CmdResult(0, "podman version 4.9.0\n"),
            ("mise", "install", "--yes"): CmdResult(0),
            ("python3", "--version"): CmdResult(0, "Python 3.11.5\n"),
            ("mise", "run", "sso-install"): CmdResult(0),
            ("pgrep", "-q", "WindowServer"): CmdResult(1),  # headless
        }
        if extra_commands:
            base_commands.update(extra_commands)

        base_files = {}
        if extra_files:
            base_files.update(extra_files)

        return MockSetupContext(
            tools=base_tools,
            commands=base_commands,
            files=base_files,
            prompts=prompts or ["", "", "", "", ""],
            confirms=confirms or [],
            three_way=three_way or [],
            env=env or {},
        )

    def test_happy_path(self):
        ctx = self._all_tools_ctx(
            extra_commands={
                # SSO configured
                ("aws", "configure", "get", "sso_session", "--profile", "default"):
                    CmdResult(0, "my-session\n"),
                # Creds valid
                ("aws", "sts", "get-caller-identity", "--profile", "default"):
                    CmdResult(0, '{"Account":"123"}\n'),
            },
            confirms=[True],  # start containers
        )
        assert run_setup(ctx) == 0
        out = output_text(ctx)
        assert "Setup complete" in out

    def test_prereqs_fail_early_exit(self):
        ctx = MockSetupContext(
            tools={"mise", "podman", "swiftc"},  # no aws
            commands={
                ("mise", "--version"): CmdResult(0, "2024.1.0\n"),
                ("podman", "--version"): CmdResult(0, "podman version 4.9.0\n"),
            },
        )
        assert run_setup(ctx) == 1
        out = output_text(ctx)
        assert "prerequisite" in out.lower()

    def test_full_flow_no_sso(self):
        ctx = self._all_tools_ctx(
            extra_commands={
                # No SSO
                ("aws", "configure", "get", "sso_session", "--profile", "default"):
                    CmdResult(1),
                ("aws", "configure", "get", "sso_account_id", "--profile", "default"):
                    CmdResult(1),
            },
            three_way=["skip"],  # skip SSO config
            confirms=[False],  # don't start containers
        )
        assert run_setup(ctx) == 0
        out = output_text(ctx)
        assert "Setup complete" in out

    def test_full_flow_login_needed(self):
        call_count = {"sts": 0}
        def sts_handler(cmd):
            call_count["sts"] += 1
            if call_count["sts"] == 1:
                return CmdResult(1, "", "expired")
            return CmdResult(0, '{"Account":"123"}\n')

        ctx = self._all_tools_ctx(
            extra_commands={
                # SSO configured
                ("aws", "configure", "get", "sso_session", "--profile", "default"):
                    CmdResult(0, "my-session\n"),
                # Creds: first fail, then ok
                ("aws", "sts", "get-caller-identity", "--profile", "default"):
                    sts_handler,
                # Login
                "python3": CmdResult(0),
            },
            confirms=[True],  # start containers
        )
        assert run_setup(ctx) == 0

    def test_permissions_denied_exits_1(self):
        """GUI session + permission denied → setup fails."""
        osascript_se = (
            "osascript", "-e",
            'tell application "System Events" to return name of current user'
        )
        ctx = self._all_tools_ctx(
            extra_commands={
                ("pgrep", "-q", "WindowServer"): CmdResult(0),  # GUI
                osascript_se: CmdResult(1, "", "not allowed"),
            },
            env={"TERM_PROGRAM": "Terminal"},
        )
        assert run_setup(ctx) == 1

    def test_permissions_timeout_exits_1(self):
        """GUI session + dialog timeout → setup fails."""
        osascript_se = (
            "osascript", "-e",
            'tell application "System Events" to return name of current user'
        )
        osascript_dlg = (
            "osascript", "-e",
            'display dialog "Setup complete — SSO watcher permissions verified." '
            'buttons {"OK"} default button "OK"'
        )
        ctx = self._all_tools_ctx(
            extra_commands={
                ("pgrep", "-q", "WindowServer"): CmdResult(0),  # GUI
                osascript_se: CmdResult(0, "user\n"),
                osascript_dlg: CmdResult(-1, "", "timeout"),
            },
            env={"TERM_PROGRAM": "Terminal"},
        )
        assert run_setup(ctx) == 1
