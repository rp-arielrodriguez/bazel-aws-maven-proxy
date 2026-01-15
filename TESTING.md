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
    └── test_s3proxy.py      # S3 proxy unit tests (19 tests)
```

## Test Coverage

Current coverage: **79%** (19 passing tests)

### S3 Proxy Tests

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

## Running Tests

### All tests
```bash
pytest
```

### Specific test file
```bash
pytest tests/unit/test_s3proxy.py
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

Example test:

```python
@pytest.mark.unit
def test_new_feature(flask_app, mock_s3_client):
    """Test description."""
    with patch.object(app, 'get_s3_client', return_value=mock_s3_client):
        response = flask_app.get('/path')

    assert response.status_code == 200
```

## Test Requirements

Core dependencies:
- pytest - Test framework
- pytest-cov - Coverage reporting
- pytest-mock - Mocking support
- flask, boto3 - Required by s3proxy

See `tests/requirements.txt` for full list.
