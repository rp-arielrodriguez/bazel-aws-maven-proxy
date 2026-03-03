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
    ├── test_s3proxy.py      # S3 proxy tests (19 tests)
    └── test_watcher.py      # SSO watcher tests (177 tests)
```

## Test Coverage

**221 passing tests** (19 s3proxy + 202 watcher)

### S3 Proxy Tests (`tests/unit/test_s3proxy.py`)

**Cache Operations** (4 tests):
- File path conversion, parent directory creation, cache initialization

**S3 Client Management** (4 tests):
- Client creation, credential handling, client reuse and refresh

**S3 Fetch Operations** (3 tests):
- Successful fetch, file not found, leading slash normalization

**Flask Endpoints** (5 tests):
- Health check (healthy/unhealthy), file serving, cache miss, 404

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

**Proactive Refresh Main Loop — `TestProactiveRefreshMainLoop`** (6 tests):
- Fires when near expiry, skips when healthy, skips with signal present
- Skips in standalone, disabled when zero, failure doesn't crash

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
