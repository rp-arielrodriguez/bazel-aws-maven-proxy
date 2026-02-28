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
    └── test_watcher.py      # SSO watcher tests (69 tests)
```

## Test Coverage

**88 passing tests** (19 s3proxy + 69 watcher)

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
- `mock_aws_config` — Mock AWS config file
- `mock_aws_credentials` — Mock credentials file
- `create_sso_token_file` — Factory for SSO token files
- `valid_sso_token` — Valid token (expires in 2 hours)
- `expiring_sso_token` — Expiring token (30 minutes)
- `expired_sso_token` — Already expired token
- `mock_env_vars` — Mock environment variables
- `mock_s3_bucket` — Mock S3 bucket with moto
- `sample_maven_artifact` — Sample Maven artifact data

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
