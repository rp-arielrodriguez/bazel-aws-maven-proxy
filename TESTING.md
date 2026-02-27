# Testing Guide

Quick guide for testing bazel-aws-maven-proxy.

## Quick Start

```bash
# Install test dependencies
pip install -r tests/requirements.txt

# Run all tests
pytest

# Run with coverage
pytest --cov=s3proxy --cov-report=html

# View coverage report
open htmlcov/index.html
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
    ├── test_s3proxy.py      # S3 proxy unit tests (19 tests)
    └── test_watcher.py      # SSO watcher unit tests (48 tests)
```

## Test Coverage

Current: **67 passing tests** (19 s3proxy + 48 watcher)

### S3 Proxy Tests (`tests/unit/test_s3proxy.py`)

**Cache Operations** (4 tests):
- File path conversion
- Parent directory creation
- Cache initialization

**S3 Client Management** (4 tests):
- Client creation and initialization
- Credential handling
- Client reuse and refresh

**S3 Fetch Operations** (3 tests):
- Successful file fetch
- File not found handling
- Leading slash normalization

**Flask Endpoints** (5 tests):
- Health check (healthy/unhealthy)
- File serving from cache
- Cache miss fetches from S3
- 404 handling

**Directory Listing** (1 test):
- Shows cached entries

**Decorator Tests** (2 tests):
- `@with_s3_client` decorator
- Error handling

### SSO Watcher Tests (`tests/unit/test_watcher.py`)

**Signal File Operations** (6 tests):
- Load valid/missing/malformed signal files
- Clear signal files
- Snooze update with `nextAttemptAfter`
- Signal with future `nextAttemptAfter` skipped

**Notification Dialog** (7 tests):
- Refresh action (exit code 0, stdout "refresh")
- Snooze action with duration parsing
- Suppress action ("Don't Remind")
- Dismiss action (dialog closed/timeout)
- osascript error handling

**Login Flow** (7 tests):
- Successful login clears signal
- Failed login returns failure
- Snooze/suppress/dismiss pass-through from notification
- Auto mode skips notification dialog
- Profile from signal file used in `aws sso login`

**Cooldown & Locking** (6 tests):
- Cooldown skip when recent login
- Lock acquisition and release
- Lock cleanup on error

**Last Run Tracking** (3 tests):
- Write and read timestamps
- Missing/corrupt file handling

**Main Loop (Notify Mode)** (9 tests):
- Refresh success clears signal
- Dismiss keeps signal
- Login failure keeps signal
- Snooze writes `nextAttemptAfter` to signal
- Suppress clears signal
- Lock released after dismiss/exception
- Profile fallback to env var

**Configuration** (10 tests):
- Environment variable parsing (poll interval, cooldown, login mode)
- Default values
- Signal/state directory paths

## Running Tests

### All tests
```bash
pytest
```

### Watcher tests only
```bash
pytest -m unit tests/unit/test_watcher.py -v --no-cov
```

### S3 proxy tests only
```bash
pytest tests/unit/test_s3proxy.py -v
```

### With verbose output
```bash
pytest -v
```

### With coverage
```bash
pytest --cov=s3proxy --cov-report=term-missing
```

### Run using helper script
```bash
./run_tests.sh
```

## Test Fixtures

Available fixtures in `conftest.py`:

- `temp_aws_dir` - Temporary AWS directory structure
- `mock_aws_config` - Mock AWS config file
- `mock_aws_credentials` - Mock credentials file
- `create_sso_token_file` - Factory for SSO token files
- `valid_sso_token` - Valid token (expires in 2 hours)
- `expiring_sso_token` - Expiring token (30 minutes)
- `expired_sso_token` - Already expired token
- `mock_env_vars` - Mock environment variables
- `mock_s3_bucket` - Mock S3 bucket with moto
- `sample_maven_artifact` - Sample Maven artifact data

## Adding New Tests

Example s3proxy test:

```python
@pytest.mark.unit
def test_new_feature(flask_app, mock_s3_client):
    """Test description."""
    with patch.object(app, 'get_s3_client', return_value=mock_s3_client):
        response = flask_app.get('/path')

    assert response.status_code == 200
```

Example watcher test:

```python
@pytest.mark.unit
def test_signal_handling(tmp_path):
    """Test description."""
    signal_file = tmp_path / "login-required.json"
    signal_file.write_text('{"profile": "test", "reason": "expired"}')

    result = load_signal(str(signal_file))
    assert result["profile"] == "test"
```

## Test Requirements

Core dependencies:
- pytest - Test framework
- pytest-cov - Coverage reporting
- pytest-mock - Mocking support
- flask, boto3 - Required by s3proxy

See `tests/requirements.txt` for full list.
