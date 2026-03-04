# Testing Guide

## Quick Start

```bash
pip install -r tests/requirements.txt
pytest
```

## Test Structure

```
tests/
├── conftest.py              # Shared pytest fixtures
├── requirements.txt         # Test dependencies
├── fixtures/                # Sample data files
│   ├── sample_sso_token_valid.json
│   ├── sample_sso_token_expired.json
│   └── sample_aws_config.ini
└── unit/
    ├── test_s3proxy.py      # S3 proxy tests (22 tests)
    └── test_watcher.py      # SSO watcher tests (202 tests)
```

## Test Coverage

**378 passing tests** (22 s3proxy + 211 watcher + 52 monitor + 93 setup)

### S3 Proxy Tests (`tests/unit/test_s3proxy.py`)

**Cache Operations** (6 tests):
- File path conversion, parent directory creation, cache initialization, path traversal blocking

**S3 Client Management** (4 tests):
- Client creation, credential handling, client reuse and refresh

**S3 Fetch Operations** (3 tests):
- Successful fetch, file not found, leading slash normalization

**Flask Endpoints** (6 tests):
- Health check (healthy/unhealthy), file serving, cache miss, 404, path traversal 403

**Directory Listing** (1 test):
- Shows cached entries

**Decorator Tests** (2 tests):
- `@with_s3_client` decorator and error handling

### SSO Watcher Tests (`tests/unit/test_watcher.py`)

**Signal Trigger Logic — `TestShouldTriggerLogin`** (6 tests):
- No signal file, signal exists, cooldown active/expired, snooze active/expired

**Locking — `TestLocking`** (4 tests):
- Acquire lock, already held, release, release nonexistent

**Signal Clearing — `TestClearSignal`** (2 tests):
- Clear existing and nonexistent signal

**Snooze Update — `TestUpdateSignalSnooze`** (2 tests):
- Write and overwrite `nextAttemptAfter`

**Notification Dialog — `TestShowNotification`** (14 tests):
- Refresh, snooze (15m/30m/1h/4h), suppress, dismiss, dialog timeout
- Subprocess timeout, osascript not found, generic exception
- Profile in script, unknown snooze label, snooze/suppress cancel

**Login Flow — `TestHandleLogin`** (8 tests):
- Notify mode: refresh (success/fail), dismiss, snooze, suppress
- Auto mode: direct login (success/fail), profile from signal

**Last Run Tracking — `TestLastRun`** (3 tests):
- Write and read timestamps, missing/corrupt file

**Main Loop — `TestMainLoopNotifyMode`** (8 tests):
- Refresh success clears signal, dismiss keeps signal
- Login failure keeps signal, snooze writes `nextAttemptAfter`
- Suppress clears signal, lock released after dismiss/exception
- Profile fallback to env var

**Mode Management — `TestModeManagement`** (10 tests):
- Read mode from file (notify/standalone), fallback to env, default
- Ignore invalid file/env, write mode, write invalid mode
- Standalone mode skips signal, standalone handle_login returns dismiss

**State Machine Transitions — `TestStateMachineTransitions`** (11 tests):
- Lock held skips login (main loop integration)
- Auto mode: success clears signal, failure writes 30s snooze
- Cooldown file written on success, dismiss, suppress
- Failure writes 30s snooze but NOT cooldown
- Mode switch mid-loop takes effect next cycle
- Lock released after auto success, auto failure, snooze

**AWS Config Parsing — `TestGetSsoSessionConfig`** (7 tests):
- Read profile with sso-session, default profile
- Profile not found, no sso_session key, session section missing
- Config file missing, corrupt config file

**SSO Cache Discovery — `TestFindSsoCacheFile`** (7 tests):
- Find matching cache, no matching startUrl, missing refreshToken
- Missing clientId, empty cache dir, corrupt JSON skipped, cache dir missing

**Silent Token Refresh — `TestTrySilentRefresh`** (12 tests):
- Success (with/without new refresh token), correct command args
- No SSO config, no cache file, expired registration
- API failure, invalid JSON, no accessToken in response
- Subprocess timeout, aws not found, cache write failure

**URL Extraction — `TestExtractAuthorizeUrl`** (4 tests):
- Extracts URL from stdout, returns None on missing/empty/no URL

**Callback Host — `TestExtractCallbackHost`** (4 tests):
- Extracts host:port from redirect_uri, handles missing/invalid URLs

**Webview Launch — `TestLaunchWebview`** (3 tests):
- Binary missing returns None, successful launch, Popen failure

**SSO Login Flow — `TestRunAwsSsoLogin`** (8 tests):
- Success with webview, fallback to browser, no URL returns -1
- Timeout returns -1, login failure, webview killed on timeout
- Webview close aborts login, uses --no-browser flag

**Silent Mode Handle Login — `TestSilentModeHandleLogin`** (8 tests):
- Silent mode success/failure, notify/auto try silent first
- Notify/auto fall back after silent failure
- Read/write silent mode

**Silent Mode Main Loop — `TestSilentModeMainLoop`** (3 tests):
- Success clears signal, failure writes 30s snooze, lock released

**Token Expiry Check — `TestCheckTokenNearExpiry`** (7 tests):
- Near expiry, not near, already expired, no config, no cache
- No expiresAt field, uses default threshold

**Proactive Refresh Main Loop — `TestProactiveRefreshMainLoop`** (10 tests):
- Fires when near expiry, skips when healthy, skips with signal present
- Skips in standalone, disabled when zero, failure doesn't crash
- 3 consecutive failures writes signal, stops proactive after max failures
- Failure counter resets on success, resets after login

**Webview Kill — `TestKillWebview`** (4 tests):
- osascript quit, killall fallback, both fail silently, osascript timeout

**AWS CLI Check — `TestCheckAwsCli`** (5 tests):
- Valid version, low version warning, missing binary, generic exception, exact minimum

**Credential Check — `TestCheckCredentialsValid`** (5 tests):
- Returns true/false on success/failure, exception handling, timeout, profile passed to command

**Notify Webview Launch — `TestLaunchNotifyWebview`** (3 tests):
- Binary missing returns None, mkdtemp failure, Popen failure cleanup

**Notify Login Flow — `TestRunNotifyLogin`** (22 tests):
- Fallback to dialog when webview missing (refresh/fail/snooze/suppress/dismiss)
- Webview actions: snooze, suppress, dismiss, window closed, no output
- Webview refresh: URL extracted and sent, no URL returns failed, stdin write fails
- Webview signals: SSO_TIMEOUT, SSO_ERROR:detail, unknown action, non-signal debug lines
- Auth polling: timeout returns failed, credential check returns success
- Webview exits during auth: aws finishes (success), aws hangs (dismiss)
- Cleanup: aws_proc is None, finally closes stdout and calls cleanup

**Webview Running Check — `TestIsWebviewRunning`** (4 tests):
- pgrep returns running/not running, timeout handling, generic exception

**Stale Lock Recovery — `TestStaleLockRecovery`** (3 tests):
- Stale lock reclaimed, non-stale not reclaimed, rmdir failure on stale lock

**Signal Loading — `TestLoadSignal`** (4 tests):
- Valid signal, missing file, corrupt JSON, empty file

**Signal Write (Watcher) — `TestWriteSignal`** (5 tests):
- Writes valid signal file with correct structure, creates parent dir
- Overwrites existing signal, atomic write cleanup on failure, logs error

### SSO Monitor Tests (`tests/unit/test_monitor.py`)

**Module Config — `TestModuleConfig`** (7 tests):
- CHECK_INTERVAL defaults, env override, clamped minimum, non-integer fallback
- SIGNAL_FILE and AWS_PROFILE from env

**Credential Checking — `TestCheckCredentials`** (12 tests):
- Valid credentials, NoCredentialsError, TokenRetrievalError, CredentialRetrievalError
- ClientError (expired token, invalid token, other codes), EndpointConnectionError
- ProfileNotFound, generic exception, no-session fallback

**Signal File Write — `TestWriteSignalFile`** (7 tests):
- JSON content/fields, default reason, parent dir creation, atomic write
- Tempfile cleanup on error, permission error handling, overwrite

**Signal File Clear — `TestClearSignalFile`** (3 tests):
- Remove existing, no error when missing, permission error suppressed

**SIGTERM Handling — `TestSigtermHandling`** (1 test):
- SIGTERM handler registered, calls sys.exit(0)

**Session Reuse — `TestSessionReuse`** (2 tests):
- Session created once, passed to check_credentials

**State Transitions — `TestMainLoopStateTransitions`** (6 tests):
- valid→expired writes signal, expired→valid clears, no spam on same-state
- None→valid, None→expired transitions

**Loop Interval — `TestMainLoopInterval`** (2 tests):
- Sleeps CHECK_INTERVAL, sleeps after error

**Error Recovery — `TestMainLoopErrorRecovery`** (3 tests):
- Exception resets last_state, KeyboardInterrupt clean exit, loop continues

**ProfileNotFound — `TestMainLoopProfileNotFound`** (5 tests):
- No crash, writes signal with reason, no spam on repeated failures
- Retries session each iteration, recovers when profile appears

**Startup — `TestMainLoopStartup`** (2 tests):
- Main calls check_credentials, creates session with profile

**Full Cycle — `TestFullCycle`** (2 tests):
- valid→expired→valid cycle, multiple transitions

### Setup Tests (`tests/unit/test_setup.py`)

Uses `MockSetupContext` — subclass of `SetupContext` with in-memory filesystem, configurable command results, and FIFO prompt queues. No real commands executed, no filesystem touched.

**AWS Version Parsing — `TestParseAwsVersion`** (3 tests):
- Standard format, no match, empty

**AWS Version Check — `TestCheckAwsVersion`** (7 tests):
- Exact minimum (2.9), above, below, major 3, major 1, invalid, empty

**Prerequisites — `TestCheckPrerequisites`** (10 tests):
- All present, missing each tool, docker fallback, all missing, ok property

**Prerequisites — Swiftc Install — `TestCheckPrerequisitesSwiftc`** (5 tests):
- User accepts install (succeeds/fails), user declines, user skips, swiftc present skips prompt

**AWS Profiles — `TestListAwsProfiles`** (4 tests):
- Multiple profiles, no config file, empty, default-section only

**Env Config Prompts — `TestPromptEnvConfig`** (4 tests):
- Defaults, custom values, invalid SSO mode, profiles shown

**Env Content Generation — `TestGenerateEnvContent`** (4 tests):
- Default config, custom, hardcoded values, commented engine

**Parse Existing Env — `TestParseExistingEnv`** (5 tests):
- All fields, quoted values, comments, missing file, partial

**Configure Env — `TestConfigureEnv`** (3 tests):
- Fresh install, keep existing, overwrite

**Install Tools — `TestInstallTools`** (2 tests):
- Success, failure

**Install SSO Watcher — `TestInstallSsoWatcher`** (2 tests):
- Success, failure

**GUI Session Detection — `TestIsGuiSession`** (4 tests):
- DISPLAY set, TERM_PROGRAM set, WindowServer running, headless

**macOS Permissions — `TestCheckMacosPermissions`** (6 tests):
- All OK, System Events denied (fail), System Events timeout (fail), dialog denied (fail), dialog timeout (fail), headless skip

**SSO Configuration Check — `TestCheckSsoConfiguration`** (4 tests):
- Modern sso_session, legacy, none, empty output

**Configure SSO — `TestConfigureSso`** (6 tests):
- Already configured (modern/legacy), user yes/no/skip, failure

**Credentials Valid — `TestCheckCredentialsValid`** (2 tests):
- Valid, invalid

**First Login + Validate — `TestFirstLoginAndValidate`** (11 tests):
- SSO not configured skip, credentials valid, login success/fail
- Mode file save/restore, S3 validation success/fail/skip, failure restores mode

**Start Containers — `TestStartContainers`** (3 tests):
- User yes, no, command fails

**Print Summary — `TestPrintSummary`** (2 tests):
- Port in output, commands in output

**Full Setup Flow — `TestRunSetup`** (6 tests):
- Happy path, prereq fail early exit, no SSO flow, login needed flow, permissions denied exits 1, permissions timeout exits 1

## Running Tests

```bash
# All tests
pytest

# Verbose
pytest -v --no-cov

# Watcher tests only
pytest tests/unit/test_watcher.py -v --no-cov

# S3 proxy tests only
pytest tests/unit/test_s3proxy.py -v

# With coverage
pytest --cov=s3proxy --cov-report=term-missing

# Using helper script
./run_tests.sh
```

## Test Fixtures

Available in `conftest.py`:

- `temp_aws_dir` — Temporary AWS directory structure
- `mock_env_vars` — Mock environment variables
- `reset_logging` — Autouse fixture to reset logging between tests

Watcher-specific (in `test_watcher.py`):

- `watcher_state` — Isolated state directory with signal, lock, mode files

## Adding Tests

S3 proxy:
```python
@pytest.mark.unit
def test_new_feature(flask_app, mock_s3_client):
    with patch.object(app, 'get_s3_client', return_value=mock_s3_client):
        response = flask_app.get('/path')
    assert response.status_code == 200
```

Watcher:
```python
@pytest.mark.unit
def test_signal_handling(tmp_path):
    signal_file = tmp_path / "login-required.json"
    signal_file.write_text('{"profile": "test", "reason": "expired"}')
    result = load_signal(str(signal_file))
    assert result["profile"] == "test"
```

## Dependencies

- pytest, pytest-cov, pytest-mock
- flask, boto3 (required by s3proxy)

See `tests/requirements.txt` for full list.
