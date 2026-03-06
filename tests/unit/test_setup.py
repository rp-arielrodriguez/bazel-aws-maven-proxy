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
    _clear_sso_cache,
    _discover_account_and_role,
    _find_sso_access_token,
    _read_aws_config,
    _sso_list_accounts,
    _sso_list_roles,
    _write_sso_config,
    check_aws_version,
    check_credentials_valid,
    check_macos_permissions,
    check_prerequisites,
    check_sso_configuration,
    configure_env,
    configure_sso,
    detect_tls_skip,
    do_sso_login,
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
        choices: list | None = None,
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
        self._choices: deque = deque(choices or [])
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

        # Prefix match for string keys (e.g. "aws sso list-accounts" matches
        # "aws sso list-accounts --access-token ...")
        for k, val in self._commands.items():
            if isinstance(k, str) and key_joined.startswith(k + " "):
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

    def choose(self, items: list[str], label: str = "Choice") -> int:
        if self._choices:
            return self._choices.popleft()
        return 0

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

    def glob_files(self, pattern: str) -> list[str]:
        """Match virtual files against a glob-style pattern."""
        import fnmatch
        return [p for p in self._files if fnmatch.fnmatch(p, pattern)]

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
        assert config.aws_region == "sa-east-1"
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

    def test_tls_skip_false_no_engine(self):
        """No container engine → skip_tls_verify stays False."""
        ctx = MockSetupContext(prompts=["", "", "", "", ""])
        config = prompt_env_config(ctx)
        assert config.skip_tls_verify is False

    def test_tls_skip_false_pull_ok(self):
        """Podman pull succeeds → skip_tls_verify stays False."""
        ctx = MockSetupContext(
            prompts=["", "", "", "", ""],
            commands={"podman pull": CmdResult(0)},
        )
        config = prompt_env_config(ctx, container_engine="podman")
        assert config.skip_tls_verify is False

    def test_tls_skip_true_x509_error(self):
        """Podman pull fails with x509 → skip_tls_verify auto-enabled."""
        ctx = MockSetupContext(
            prompts=["", "", "", "", ""],
            commands={
                "podman pull": CmdResult(
                    1, "", "x509: certificate signed by unknown authority"
                ),
            },
        )
        config = prompt_env_config(ctx, container_engine="podman")
        assert config.skip_tls_verify is True

    def test_tls_skip_false_docker(self):
        """Docker engine → detection skipped, stays False."""
        ctx = MockSetupContext(prompts=["", "", "", "", ""])
        config = prompt_env_config(ctx, container_engine="docker")
        assert config.skip_tls_verify is False

    def test_tls_skip_false_non_tls_failure(self):
        """Podman pull fails with non-TLS error → stays False."""
        ctx = MockSetupContext(
            prompts=["", "", "", "", ""],
            commands={
                "podman pull": CmdResult(1, "", "connection refused"),
            },
        )
        config = prompt_env_config(ctx, container_engine="podman")
        assert config.skip_tls_verify is False


# ===================================================================
# TestDetectTlsSkip
# ===================================================================

class TestDetectTlsSkip:
    def test_no_engine(self):
        ctx = MockSetupContext()
        assert detect_tls_skip(ctx, "") is False

    def test_docker_skips_detection(self):
        ctx = MockSetupContext()
        assert detect_tls_skip(ctx, "docker") is False

    def test_podman_pull_ok(self):
        ctx = MockSetupContext(commands={"podman pull": CmdResult(0)})
        assert detect_tls_skip(ctx, "podman") is False
        assert any("pull OK" in m for m in ctx.get_output())

    def test_podman_x509_error(self):
        ctx = MockSetupContext(commands={
            "podman pull": CmdResult(
                1, "", "x509: certificate signed by unknown authority"
            ),
        })
        assert detect_tls_skip(ctx, "podman") is True
        out = output_text(ctx)
        assert "TLS certificate error" in out

    def test_podman_tls_error(self):
        ctx = MockSetupContext(commands={
            "podman pull": CmdResult(1, "", "tls: handshake failure"),
        })
        assert detect_tls_skip(ctx, "podman") is True

    def test_podman_certificate_keyword(self):
        ctx = MockSetupContext(commands={
            "podman pull": CmdResult(
                1, "", "certificate verify failed"
            ),
        })
        assert detect_tls_skip(ctx, "podman") is True

    def test_podman_non_tls_failure(self):
        ctx = MockSetupContext(commands={
            "podman pull": CmdResult(1, "", "connection refused"),
        })
        assert detect_tls_skip(ctx, "podman") is False
        out = output_text(ctx)
        assert "SKIP_TLS_VERIFY=true" in out

    def test_podman_error_in_stdout(self):
        """Error message in stdout (not stderr) still detected."""
        ctx = MockSetupContext(commands={
            "podman pull": CmdResult(1, "x509: certificate problem", ""),
        })
        assert detect_tls_skip(ctx, "podman") is True


# ===================================================================
# TestGenerateEnvContent
# ===================================================================

class TestGenerateEnvContent:
    def test_default_config(self):
        content = generate_env_content(EnvConfig())
        assert 'AWS_PROFILE="default"' in content
        assert 'AWS_REGION="sa-east-1"' in content
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
        assert cfg.aws_region == "sa-east-1"  # default, comment was ignored

    def test_missing_file(self):
        env_path = Path("/fake/nonexistent/.env")
        ctx = MockSetupContext()
        cfg = parse_existing_env(ctx, env_path)
        # Should return defaults
        assert cfg.aws_profile == "default"
        assert cfg.aws_region == "sa-east-1"

    def test_partial_env(self):
        env_content = 'AWS_PROFILE="custom"\n'
        env_path = Path("/fake/.env")
        ctx = MockSetupContext(files={str(env_path): env_content})
        cfg = parse_existing_env(ctx, env_path)
        assert cfg.aws_profile == "custom"
        assert cfg.aws_region == "sa-east-1"  # default
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

    def test_dialog_denied_warns(self):
        ctx = MockSetupContext(
            env={"DISPLAY": ":0"},
            commands={
                self._osascript_system_events(): CmdResult(0, "user\n"),
                self._osascript_dialog(): CmdResult(1, "", "denied"),
            },
        )
        r = check_macos_permissions(ctx)
        assert r["failed"] is False  # non-fatal
        assert r["system_events"] is True
        assert r["dialog"] is False

    def test_dialog_timeout_warns(self):
        ctx = MockSetupContext(
            env={"DISPLAY": ":0"},
            commands={
                self._osascript_system_events(): CmdResult(0, "user\n"),
                self._osascript_dialog(): CmdResult(-1, "", "timeout"),
            },
        )
        r = check_macos_permissions(ctx)
        assert r["failed"] is False  # non-fatal
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

    def _cache_file(self, start_url="https://myorg.awsapps.com/start"):
        """Return (path, content) for a mock SSO cache file."""
        import json as _json
        home = str(Path.home())
        path = f"{home}/.aws/sso/cache/test-token.json"
        content = _json.dumps({
            "accessToken": "fake-token",
            "startUrl": start_url,
            "expiresAt": "2099-01-01T00:00:00Z",
        })
        return path, content

    def _discover_commands(self, *, login_ok=True, accounts=None, roles=None,
                           ctx_ref=None):
        """Commands for auto-discover flow (login + list accounts/roles).

        ctx_ref: mutable list [ctx] — set ctx_ref[0] = ctx after creating
        MockSetupContext. The login callback uses it to write the cache
        file (simulating what aws sso login does on the real filesystem).
        """
        import json as _json
        cmds = dict(self._not_configured())

        if login_ok and ctx_ref is not None:
            cache_path, cache_content = self._cache_file()
            def _login_with_cache(cmd):
                ctx_ref[0]._files[cache_path] = cache_content
                return CmdResult(0)
            cmds["python3"] = _login_with_cache
        else:
            cmds["python3"] = (
                CmdResult(0) if login_ok else CmdResult(1, "", "login failed")
            )

        if accounts is not None:
            cmds["aws sso list-accounts"] = CmdResult(
                0, _json.dumps({"accountList": accounts})
            )
        if roles is not None:
            cmds["aws sso list-account-roles"] = CmdResult(
                0, _json.dumps({"roleList": roles})
            )
        return cmds

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

    def test_auto_discover_full_flow(self):
        """Login succeeds → list accounts → pick → list roles → pick → config written."""
        home = str(Path.home())
        config_path = f"{home}/.aws/config"
        accounts = [
            {"accountId": "111111111111", "accountName": "dev-account"},
            {"accountId": "222222222222", "accountName": "prod-account"},
        ]
        roles = [
            {"roleName": "Admin", "accountId": "222222222222"},
            {"roleName": "ReadOnly", "accountId": "222222222222"},
        ]
        ctx_ref = [None]
        ctx = MockSetupContext(
            commands=self._discover_commands(accounts=accounts, roles=roles,
                                             ctx_ref=ctx_ref),
            three_way=["yes"],
            prompts=["https://myorg.awsapps.com/start", "us-east-1"],
            choices=[1, 0],  # pick prod-account (idx 1), Admin role (idx 0)
            files={config_path: ""},
        )
        ctx_ref[0] = ctx
        assert configure_sso(ctx, "dev") is True
        content = ctx._files[config_path]
        assert "sso_account_id = 222222222222" in content
        assert "sso_role_name = Admin" in content
        assert "sso_registration_scopes = sso:account:access" in content

    def test_auto_discover_single_role_auto_selected(self):
        """Single role available → auto-selected without prompt."""
        home = str(Path.home())
        config_path = f"{home}/.aws/config"
        accounts = [{"accountId": "111111111111", "accountName": "my-account"}]
        roles = [{"roleName": "OnlyRole", "accountId": "111111111111"}]
        ctx_ref = [None]
        ctx = MockSetupContext(
            commands=self._discover_commands(accounts=accounts, roles=roles,
                                             ctx_ref=ctx_ref),
            three_way=["yes"],
            prompts=["https://myorg.awsapps.com/start", "us-east-1"],
            choices=[0],  # pick first account
            files={config_path: ""},
        )
        ctx_ref[0] = ctx
        assert configure_sso(ctx, "dev") is True
        content = ctx._files[config_path]
        assert "sso_role_name = OnlyRole" in content

    def test_auto_discover_login_fails_falls_back_to_manual(self):
        """Login fails → falls back to manual prompts for account/role."""
        home = str(Path.home())
        config_path = f"{home}/.aws/config"
        ctx = MockSetupContext(
            commands=self._discover_commands(login_ok=False),
            three_way=["yes"],
            # SSO prompts (url, region) + manual fallback (account_id, role_name)
            prompts=["https://myorg.awsapps.com/start", "us-east-1",
                     "123456789012", "MyRole"],
            files={config_path: ""},
        )
        assert configure_sso(ctx, "dev") is True
        content = ctx._files[config_path]
        assert "sso_account_id = 123456789012" in content
        assert "sso_role_name = MyRole" in content

    def test_manual_fallback_missing_account_id(self):
        """Login fails, manual fallback, empty account ID → failure."""
        ctx = MockSetupContext(
            commands=self._discover_commands(login_ok=False),
            three_way=["yes"],
            prompts=["https://myorg.awsapps.com/start", "us-east-1", "", ""],
        )
        assert configure_sso(ctx, "dev") is False

    def test_duplicate_profile_not_overwritten(self):
        """Existing profile in config → not overwritten."""
        home = str(Path.home())
        config_path = f"{home}/.aws/config"
        existing = "[profile dev]\nsso_account_id = old\n"
        accounts = [{"accountId": "111111111111", "accountName": "acc"}]
        roles = [{"roleName": "Role", "accountId": "111111111111"}]
        ctx_ref = [None]
        ctx = MockSetupContext(
            commands=self._discover_commands(accounts=accounts, roles=roles,
                                             ctx_ref=ctx_ref),
            three_way=["yes"],
            prompts=["https://myorg.awsapps.com/start", "us-east-1"],
            choices=[0],
            files={config_path: existing},
        )
        ctx_ref[0] = ctx
        assert configure_sso(ctx, "dev") is False
        out = output_text(ctx)
        assert "already exists" in out.lower()

    def test_temp_config_cleaned_up(self):
        """Temporary sso-session sections removed after discover."""
        home = str(Path.home())
        config_path = f"{home}/.aws/config"
        accounts = [{"accountId": "111", "accountName": "acc"}]
        roles = [{"roleName": "Role", "accountId": "111"}]
        ctx_ref = [None]
        ctx = MockSetupContext(
            commands=self._discover_commands(accounts=accounts, roles=roles,
                                             ctx_ref=ctx_ref),
            three_way=["yes"],
            prompts=["https://myorg.awsapps.com/start", "us-east-1"],
            choices=[0],
            files={config_path: ""},
        )
        ctx_ref[0] = ctx
        configure_sso(ctx, "dev")
        content = ctx._files[config_path]
        assert "dev-setup-tmp" not in content

    def test_sso_cache_cleared_before_login(self):
        """All SSO cache files cleared before discover login."""
        import json as _json
        home = str(Path.home())
        config_path = f"{home}/.aws/config"
        # Stale client registration
        stale_path = f"{home}/.aws/sso/cache/stale-client.json"
        stale_content = _json.dumps({
            "clientId": "old-id", "clientSecret": "old-secret",
            "expiresAt": "2099-01-01T00:00:00Z",
        })
        # Old token (wrong scope)
        old_token_path = f"{home}/.aws/sso/cache/old-token.json"
        old_token_content = _json.dumps({
            "accessToken": "old-token", "startUrl": "https://x.awsapps.com/start",
            "expiresAt": "2099-01-01T00:00:00Z",
        })
        accounts = [{"accountId": "111", "accountName": "acc"}]
        roles = [{"roleName": "Role", "accountId": "111"}]
        ctx_ref = [None]
        ctx = MockSetupContext(
            commands=self._discover_commands(accounts=accounts, roles=roles,
                                             ctx_ref=ctx_ref),
            three_way=["yes"],
            prompts=["https://myorg.awsapps.com/start", "us-east-1"],
            choices=[0],
            files={
                config_path: "",
                stale_path: stale_content,
                old_token_path: old_token_content,
            },
        )
        ctx_ref[0] = ctx
        configure_sso(ctx, "dev")
        # Stale client registration removed, access tokens preserved
        assert stale_path in ctx._removed
        assert old_token_path not in ctx._removed


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

    def test_permissions_dialog_timeout_continues(self):
        """GUI session + dialog timeout → warns but setup continues."""
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
        assert run_setup(ctx) == 0
        assert any("timed out" in l for l in ctx._output)


# ===================================================================
# TestReadAwsConfig
# ===================================================================

class TestReadAwsConfig:
    def test_reads_existing(self):
        home = str(Path.home())
        ctx = MockSetupContext(files={
            f"{home}/.aws/config": "[profile foo]\nregion=us-east-1\n"
        })
        assert "[profile foo]" in _read_aws_config(ctx)

    def test_missing_file(self):
        ctx = MockSetupContext()
        assert _read_aws_config(ctx) == ""


# ===================================================================
# TestWriteSsoConfig
# ===================================================================

class TestWriteSsoConfig:
    def test_writes_new_config(self):
        home = str(Path.home())
        config_path = f"{home}/.aws/config"
        ctx = MockSetupContext(files={config_path: ""})
        ok = _write_sso_config(ctx, "dev", "dev-session",
                               "https://org.awsapps.com/start",
                               "us-east-1", "111111111111", "Admin")
        assert ok is True
        written = ctx._files[config_path]
        assert "[profile dev]" in written
        assert "sso_account_id = 111111111111" in written
        assert "[sso-session dev-session]" in written
        assert "sso_registration_scopes = sso:account:access" in written

    def test_appends_to_existing(self):
        home = str(Path.home())
        config_path = f"{home}/.aws/config"
        ctx = MockSetupContext(files={
            config_path: "[profile old]\nregion=sa-east-1\n"
        })
        ok = _write_sso_config(ctx, "new", "new-sess",
                               "https://org.awsapps.com/start",
                               "us-east-1", "222", "ReadOnly")
        assert ok is True
        written = ctx._files[config_path]
        assert "[profile old]" in written
        assert "[profile new]" in written

    def test_duplicate_profile_returns_false(self):
        home = str(Path.home())
        config_path = f"{home}/.aws/config"
        ctx = MockSetupContext(files={
            config_path: "[profile dev]\nsso_session = dev\n"
        })
        ok = _write_sso_config(ctx, "dev", "dev-new",
                               "https://org.awsapps.com/start",
                               "us-east-1", "111", "Admin")
        assert ok is False
        assert any("already exists" in m for m in ctx.get_output())

    def test_duplicate_session_returns_false(self):
        home = str(Path.home())
        config_path = f"{home}/.aws/config"
        ctx = MockSetupContext(files={
            config_path: "[sso-session my-sess]\nsso_start_url = https://x\n"
        })
        ok = _write_sso_config(ctx, "newprof", "my-sess",
                               "https://org.awsapps.com/start",
                               "us-east-1", "111", "Admin")
        assert ok is False

    def test_no_existing_config_file(self):
        """Config file doesn't exist yet — creates it."""
        home = str(Path.home())
        config_path = f"{home}/.aws/config"
        ctx = MockSetupContext()  # no files
        ok = _write_sso_config(ctx, "dev", "dev-sess",
                               "https://org.awsapps.com/start",
                               "us-east-1", "111", "Admin")
        assert ok is True
        assert config_path in ctx._files


# ===================================================================
# TestFindSsoAccessToken
# ===================================================================

class TestFindSsoAccessToken:
    def _cache_file(self, name, **fields):
        import json
        home = str(Path.home())
        path = f"{home}/.aws/sso/cache/{name}.json"
        return path, json.dumps(fields)

    def test_finds_matching_token(self):
        path, content = self._cache_file("tok1",
            accessToken="my-token",
            startUrl="https://org.awsapps.com/start",
            expiresAt="2099-01-01T00:00:00Z")
        ctx = MockSetupContext(files={path: content})
        token = _find_sso_access_token(ctx, "https://org.awsapps.com/start")
        assert token == "my-token"

    def test_url_normalization(self):
        """start_url with trailing /# still matches."""
        path, content = self._cache_file("tok1",
            accessToken="tok",
            startUrl="https://org.awsapps.com/start/#",
            expiresAt="2099-01-01T00:00:00Z")
        ctx = MockSetupContext(files={path: content})
        assert _find_sso_access_token(ctx, "https://org.awsapps.com/start") == "tok"

    def test_picks_latest_expiry(self):
        p1, c1 = self._cache_file("old",
            accessToken="old-tok",
            startUrl="https://org.awsapps.com/start",
            expiresAt="2024-01-01T00:00:00Z")
        p2, c2 = self._cache_file("new",
            accessToken="new-tok",
            startUrl="https://org.awsapps.com/start",
            expiresAt="2099-01-01T00:00:00Z")
        ctx = MockSetupContext(files={p1: c1, p2: c2})
        assert _find_sso_access_token(ctx, "https://org.awsapps.com/start") == "new-tok"

    def test_wrong_url_no_match(self):
        path, content = self._cache_file("tok1",
            accessToken="tok",
            startUrl="https://other.awsapps.com/start",
            expiresAt="2099-01-01T00:00:00Z")
        ctx = MockSetupContext(files={path: content})
        assert _find_sso_access_token(ctx, "https://org.awsapps.com/start") == ""

    def test_malformed_json_skipped(self):
        home = str(Path.home())
        bad_path = f"{home}/.aws/sso/cache/bad.json"
        ctx = MockSetupContext(files={bad_path: "not json{"})
        assert _find_sso_access_token(ctx, "https://org.awsapps.com/start") == ""

    def test_empty_cache(self):
        ctx = MockSetupContext()
        assert _find_sso_access_token(ctx, "https://org.awsapps.com/start") == ""

    def test_client_registration_ignored(self):
        """Files without accessToken (OIDC registrations) are skipped."""
        path, content = self._cache_file("reg",
            clientId="cid", clientSecret="csec",
            expiresAt="2099-01-01T00:00:00Z")
        ctx = MockSetupContext(files={path: content})
        assert _find_sso_access_token(ctx, "https://org.awsapps.com/start") == ""


# ===================================================================
# TestSsoListAccounts
# ===================================================================

class TestSsoListAccounts:
    def test_success(self):
        import json
        accounts = [{"accountId": "111", "accountName": "dev"}]
        ctx = MockSetupContext(commands={
            "aws sso list-accounts": CmdResult(0, json.dumps({"accountList": accounts}))
        })
        result = _sso_list_accounts(ctx, "token", "us-east-1")
        assert result == accounts

    def test_command_fails(self):
        ctx = MockSetupContext(commands={
            "aws sso list-accounts": CmdResult(1, "", "unauthorized")
        })
        result = _sso_list_accounts(ctx, "token", "us-east-1")
        assert result == []
        assert any("list-accounts error" in m for m in ctx.get_output())

    def test_bad_json(self):
        ctx = MockSetupContext(commands={
            "aws sso list-accounts": CmdResult(0, "not json{")
        })
        result = _sso_list_accounts(ctx, "token", "us-east-1")
        assert result == []

    def test_missing_key(self):
        import json
        ctx = MockSetupContext(commands={
            "aws sso list-accounts": CmdResult(0, json.dumps({"other": []}))
        })
        result = _sso_list_accounts(ctx, "token", "us-east-1")
        assert result == []


# ===================================================================
# TestSsoListRoles
# ===================================================================

class TestSsoListRoles:
    def test_success(self):
        import json
        roles = [{"roleName": "Admin", "accountId": "111"}]
        ctx = MockSetupContext(commands={
            "aws sso list-account-roles": CmdResult(0, json.dumps({"roleList": roles}))
        })
        result = _sso_list_roles(ctx, "token", "111", "us-east-1")
        assert result == roles

    def test_command_fails(self):
        ctx = MockSetupContext(commands={
            "aws sso list-account-roles": CmdResult(1, "", "forbidden")
        })
        result = _sso_list_roles(ctx, "token", "111", "us-east-1")
        assert result == []
        assert any("list-roles error" in m for m in ctx.get_output())

    def test_bad_json(self):
        ctx = MockSetupContext(commands={
            "aws sso list-account-roles": CmdResult(0, "invalid")
        })
        result = _sso_list_roles(ctx, "token", "111", "us-east-1")
        assert result == []


# ===================================================================
# TestClearSsoCache
# ===================================================================

class TestClearSsoCache:
    def test_removes_client_registrations(self):
        import json
        home = str(Path.home())
        reg = f"{home}/.aws/sso/cache/reg.json"
        ctx = MockSetupContext(files={
            reg: json.dumps({"clientId": "cid", "clientSecret": "sec",
                             "expiresAt": "2099-01-01"})
        })
        _clear_sso_cache(ctx)
        assert reg in ctx._removed

    def test_preserves_access_tokens(self):
        import json
        home = str(Path.home())
        tok = f"{home}/.aws/sso/cache/token.json"
        ctx = MockSetupContext(files={
            tok: json.dumps({"accessToken": "at", "startUrl": "https://x",
                             "expiresAt": "2099-01-01"})
        })
        _clear_sso_cache(ctx)
        assert tok not in ctx._removed

    def test_skips_malformed_files(self):
        home = str(Path.home())
        bad = f"{home}/.aws/sso/cache/bad.json"
        ctx = MockSetupContext(files={bad: "not json"})
        _clear_sso_cache(ctx)
        assert bad not in ctx._removed

    def test_empty_cache(self):
        ctx = MockSetupContext()
        _clear_sso_cache(ctx)  # no error
        assert ctx._removed == []


# ===================================================================
# TestDiscoverAccountAndRole
# ===================================================================

class TestDiscoverAccountAndRole:
    def _make_ctx(self, *, login_ok=True, token="", accounts=None,
                  roles=None, ctx_ref=None):
        """Build MockSetupContext for discover tests."""
        import json
        home = str(Path.home())
        config_path = f"{home}/.aws/config"
        cmds = {}

        if login_ok and ctx_ref is not None:
            cache_path = f"{home}/.aws/sso/cache/tok.json"
            cache_content = json.dumps({
                "accessToken": token or "test-token",
                "startUrl": "https://org.awsapps.com/start",
                "expiresAt": "2099-01-01T00:00:00Z",
            })
            def _login(cmd):
                ctx_ref[0]._files[cache_path] = cache_content
                return CmdResult(0)
            cmds["python3"] = _login
        else:
            cmds["python3"] = CmdResult(0 if login_ok else 1, "", "login failed" if not login_ok else "")

        if accounts is not None:
            cmds["aws sso list-accounts"] = CmdResult(
                0, json.dumps({"accountList": accounts}))
        if roles is not None:
            cmds["aws sso list-account-roles"] = CmdResult(
                0, json.dumps({"roleList": roles}))

        return cmds, config_path

    def test_token_not_found(self):
        """Login succeeds but no token in cache → fallback."""
        home = str(Path.home())
        config_path = f"{home}/.aws/config"
        ctx = MockSetupContext(
            commands={"python3": CmdResult(0)},  # login ok but no cache written
            files={config_path: ""},
        )
        acct, role = _discover_account_and_role(
            ctx, "dev", "https://org.awsapps.com/start", "us-east-1")
        assert acct == ""
        assert role == ""
        assert any("Could not find SSO token" in m for m in ctx.get_output())

    def test_no_accounts_found(self):
        """Login + token ok but list-accounts returns empty."""
        import json
        ctx_ref = [None]
        cmds, config_path = self._make_ctx(
            login_ok=True, ctx_ref=ctx_ref, accounts=[])
        ctx = MockSetupContext(commands=cmds, files={config_path: ""})
        ctx_ref[0] = ctx
        acct, role = _discover_account_and_role(
            ctx, "dev", "https://org.awsapps.com/start", "us-east-1")
        assert acct == ""
        assert role == ""
        assert any("No accounts" in m for m in ctx.get_output())

    def test_no_roles_found(self):
        """Account selected but list-roles returns empty."""
        import json
        accounts = [{"accountId": "111", "accountName": "dev"}]
        ctx_ref = [None]
        cmds, config_path = self._make_ctx(
            login_ok=True, ctx_ref=ctx_ref, accounts=accounts, roles=[])
        ctx = MockSetupContext(
            commands=cmds, files={config_path: ""},
            choices=[0])  # pick first account
        ctx_ref[0] = ctx
        acct, role = _discover_account_and_role(
            ctx, "dev", "https://org.awsapps.com/start", "us-east-1")
        assert acct == "111"
        assert role == ""
        assert any("No roles" in m for m in ctx.get_output())

    def test_temp_config_always_cleaned(self):
        """Temp config removed even when login fails."""
        home = str(Path.home())
        config_path = f"{home}/.aws/config"
        ctx = MockSetupContext(
            commands={"python3": CmdResult(1, "", "fail")},
            files={config_path: "[profile existing]\n"},
        )
        _discover_account_and_role(
            ctx, "dev", "https://org.awsapps.com/start", "us-east-1")
        # Temp session cleaned from config
        final_config = ctx._files.get(config_path, "")
        assert "setup-tmp" not in final_config
        assert "[profile existing]" in final_config


# ===================================================================
# TestDoSsoLogin
# ===================================================================

class TestDoSsoLogin:
    def test_success(self):
        ctx = MockSetupContext(
            commands={"python3": CmdResult(0)},
            repo_root=Path("/my/repo"),
        )
        assert do_sso_login(ctx, "dev") is True

    def test_failure(self):
        ctx = MockSetupContext(
            commands={"python3": CmdResult(1, "", "error")},
            repo_root=Path("/my/repo"),
        )
        assert do_sso_login(ctx, "dev") is False

    def test_command_contains_profile(self):
        """Verify the constructed command includes the profile name."""
        called_with = []
        def capture(cmd):
            called_with.append(cmd)
            return CmdResult(0)
        ctx = MockSetupContext(
            commands={"python3": capture},
            repo_root=Path("/my/repo"),
        )
        do_sso_login(ctx, "my-profile")
        assert len(called_with) == 1
        cmd_str = " ".join(called_with[0])
        assert "my-profile" in cmd_str
        assert "run_aws_sso_login" in cmd_str
