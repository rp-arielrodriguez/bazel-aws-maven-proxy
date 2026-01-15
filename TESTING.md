# Testing Guide

This document provides comprehensive information about testing the bazel-aws-maven-proxy project.

## Quick Start

```bash
# Install test dependencies
pip install -r tests/requirements.txt

# Run all unit tests (fast, ~2 seconds)
./run_tests.sh unit

# Run integration tests (automated only, no Docker/AWS needed)
./run_tests.sh integration
# Result: 11 passed, 16 skipped

# Run all tests with coverage
./run_tests.sh all

# View coverage report
./run_tests.sh coverage && open htmlcov/index.html
```

**Note:** Integration tests skip tests requiring Docker, AWS credentials, or manual interaction. See "Understanding Skipped Tests" section below to run them.

## Test Structure

```
tests/
├── __init__.py
├── conftest.py                      # Shared pytest fixtures
├── requirements.txt                 # Test dependencies
├── fixtures/                        # Sample data files
│   ├── sample_sso_token_valid.json
│   ├── sample_sso_token_expired.json
│   └── sample_aws_config.ini
├── unit/                            # Fast unit tests
│   ├── __init__.py
│   ├── test_credential_renewer.py   # ~200 lines, 15+ tests
│   ├── test_s3proxy.py              # ~270 lines, 20+ tests
│   └── test_credential_monitor.py   # ~180 lines, 15+ tests
└── integration/                     # Slower integration tests
    ├── __init__.py
    ├── test_credential_flow.py      # End-to-end tests
    └── test_token_refresh.py        # Token auto-refresh validation
```

## What Was Fixed

### Critical Bug Fix
- **credential-renewer/renewer.py**: Added missing `import configparser` statement
  - This was causing a NameError on line 48 when `configparser.ConfigParser()` was called
  - The service would crash when trying to create login notification files

## Test Coverage

The test suite covers:

### credential-renewer (test_credential_renewer.py)
- ✓ Finding SSO token files (single, multiple, missing)
- ✓ Token expiration checking (valid, expiring, expired)
- ✓ Token validation edge cases (missing fields, invalid JSON)
- ✓ Notification file creation (success, errors, missing config)
- ✓ Main loop behavior (triggers notification correctly)
- ✓ Threshold boundary conditions

### s3proxy (test_s3proxy.py)
- ✓ Cache path conversions
- ✓ S3 client initialization and refresh
- ✓ Credential management
- ✓ File fetching from S3 (success, not found, errors)
- ✓ Cache hit/miss logic
- ✓ Flask endpoints (health check, file serving)
- ✓ Directory listings
- ✓ Error handling

### credential-monitor (test_credential_monitor.py)
- ✓ Event handler initialization
- ✓ File modification detection
- ✓ Cooldown period enforcement
- ✓ Docker restart triggering
- ✓ Observer setup and configuration
- ✓ Handling missing files gracefully

## Running Tests

### Using the test runner script

```bash
# Unit tests (fast, ~2 seconds)
./run_tests.sh unit

# Integration tests (slow, requires Docker)
./run_tests.sh integration

# All tests with coverage
./run_tests.sh all

# Generate HTML coverage report
./run_tests.sh coverage

# Watch mode (re-run on file changes)
./run_tests.sh watch

# Quick smoke test (stop on first failure)
./run_tests.sh quick
```

### Using pytest directly

```bash
# Run all tests
pytest

# Run specific test file
pytest tests/unit/test_credential_renewer.py

# Run specific test class
pytest tests/unit/test_credential_renewer.py::TestFindSSOTokenFile

# Run specific test method
pytest tests/unit/test_credential_renewer.py::TestFindSSOTokenFile::test_find_token_file_with_single_file

# Run with verbose output
pytest -v

# Run with extra verbosity
pytest -vv

# Show print statements
pytest -s

# Stop on first failure
pytest -x

# Run only tests matching pattern
pytest -k "token"
```

### Using test markers

```bash
# Run only unit tests
pytest -m unit

# Run only integration tests
pytest -m integration

# Run only AWS-related tests
pytest -m aws

# Skip slow tests
pytest -m "not slow"

# Run Docker tests
pytest -m docker

# Skip manual tests (require human interaction)
pytest -m "not manual"

# Combine markers
pytest -m "unit and not slow"

# Run integration tests excluding manual/docker/aws
pytest -m "integration and not manual and not docker and not aws"
```

## Test Fixtures

The `tests/conftest.py` file provides shared fixtures:

- `temp_aws_dir`: Temporary AWS directory structure
- `mock_aws_config`: Sample AWS config file
- `mock_aws_credentials`: Sample credentials file
- `create_sso_token_file`: Factory for creating token files
- `valid_sso_token`: Token expiring in 2 hours
- `expiring_sso_token`: Token expiring in 30 minutes
- `expired_sso_token`: Already expired token
- `mock_env_vars`: Environment variables for testing
- `mock_s3_bucket`: Mocked S3 bucket with test data

## Coverage Requirements

The project enforces a minimum of **70% code coverage**. Coverage reports show:

- Line coverage (which lines were executed)
- Branch coverage (which branches were taken)
- Missing lines (code not covered by tests)

View detailed coverage:

```bash
pytest --cov-report=term-missing
```

## Continuous Integration

To run tests in CI/CD:

```bash
# Install dependencies
pip install -r tests/requirements.txt

# Run tests with coverage
pytest --cov-report=xml --cov-report=term

# Check exit code
echo $?  # 0 = all tests passed
```

## Writing New Tests

### Test Template

```python
import pytest

@pytest.mark.unit
class TestMyFeature:
    """Tests for my new feature."""

    def test_basic_functionality(self):
        """Test basic case."""
        # Arrange
        input_data = "test"

        # Act
        result = my_function(input_data)

        # Assert
        assert result == "expected"

    def test_edge_case(self):
        """Test edge case."""
        with pytest.raises(ValueError):
            my_function(None)

    def test_with_mock(self, mocker):
        """Test with mocked dependencies."""
        mock_s3 = mocker.Mock()
        mock_s3.download_file.return_value = None

        result = my_function(mock_s3)

        assert result is not None
        mock_s3.download_file.assert_called_once()
```

### Best Practices

1. **One assertion per test** (when possible)
2. **Descriptive test names** (`test_find_token_file_with_no_cache_dir`)
3. **Arrange-Act-Assert** pattern
4. **Use fixtures** for common setup
5. **Mock external dependencies** (S3, Docker, filesystem)
6. **Test edge cases** (empty, null, missing, invalid)
7. **Add docstrings** explaining what the test validates

## Debugging Failed Tests

```bash
# Run with debugger on failure
pytest --pdb

# Show local variables on failure
pytest --showlocals

# Increase verbosity
pytest -vv

# Show print statements
pytest -s

# Run single failing test
pytest tests/unit/test_file.py::test_name -vv -s
```

## Performance

- **Unit tests**: ~2-5 seconds (all 50+ tests)
- **Integration tests**: ~30-60 seconds (requires Docker)
- **Full suite**: ~1-2 minutes with coverage

## AWS SSO Token Auto-Refresh Testing

### Integration Tests

The `test_token_refresh.py` suite validates token auto-refresh functionality:

**Test Categories:**
- `TestInitialSSOLogin`: SSO setup validation
- `TestAutomaticTokenRefresh`: Auto-refresh logic
- `TestS3ProxyCredentialPickup`: Credential pickup after refresh
- `TestNotificationCreation`: Notification system when refresh fails
- `TestCredentialMonitorDetection`: File watching and restart triggers
- `TestEndToEndWorkflow`: Complete workflows
- `TestTokenRefreshRobustness`: Error handling

**Run automated tests (no Docker/AWS needed):**
```bash
pytest tests/integration/test_token_refresh.py -v -m "integration and not manual and not docker and not aws"
# Result: 11 passed, 16 skipped
```

**Run Docker-dependent tests:**
```bash
# Tests default to port 8888 (matches .env.example)
# Override with PROXY_PORT env var if needed
pytest tests/integration/ -v --no-cov

# Or run specific test class
pytest tests/integration/test_credential_flow.py::TestCredentialFlow -v --no-cov

# If using different port (e.g., 9000), set PROXY_PORT
PROXY_PORT=9000 pytest tests/integration/ -v --no-cov
```

**LocalStack S3 Tests:**
```bash
# Tests automatically start LocalStack with docker-compose --profile test
# S3 tests validate against mock S3 (no real AWS credentials needed)
pytest tests/integration/test_credential_flow.py::TestAWSIntegration -v --no-cov
```

**Note**: Integration tests automatically:
- Default to PROXY_PORT=8888 (matches .env.example)
- Manage docker-compose lifecycle (start/stop services)
- Wait up to 60s for services to become healthy
- Start LocalStack for S3 mocking when needed
- Clean up containers after completion

**Run AWS-dependent tests (requires real AWS credentials):**
```bash
# Configure AWS credentials first
export AWS_PROFILE=your-profile-name
aws sso login --profile $AWS_PROFILE

# Run AWS tests
pytest tests/integration/test_credential_flow.py::TestAWSIntegration -v

# Or run specific AWS test
pytest tests/integration/test_credential_flow.py::TestAWSIntegration::test_fetch_from_real_s3_bucket -v
```

**Run all integration tests (including skipped):**
```bash
# Start Docker services
docker-compose up -d

# Configure AWS
export AWS_PROFILE=bazel-cache
aws sso login --profile $AWS_PROFILE

# Run all integration tests
pytest -m integration -v

# Note: Manual tests will still skip (require human interaction)
```

### Understanding Skipped Tests

When you run `pytest -m integration`, you'll see ~16 tests skipped. Here's what each skip means and how to run them:

| Skip Reason | Count | How to Run |
|-------------|-------|------------|
| "Services did not become healthy" | 4 | Start Docker: `docker-compose up -d && sleep 10` |
| "AWS credentials not configured" | 2 | Configure AWS: `aws sso login --profile bazel-cache` |
| "Manual test" | 3 | Run manually with MFA/browser (see below) |
| "Complex mocking - covered by unit tests" | 3 | Already covered by unit tests - skip is intentional |
| "Requires Docker services running" | 1 | Start Docker and run specific test |
| "Requires full Docker environment" | 1 | Start all services: `docker-compose up -d` |
| "Thread safety testing requires setup" | 1 | Future enhancement - not implemented yet |
| "Retry logic test needs implementation" | 1 | Future enhancement - not implemented yet |

**Quick commands:**

```bash
# Run Docker tests
docker-compose up -d && sleep 10
pytest tests/integration/test_credential_flow.py::TestCredentialFlow -v

# Run AWS tests
aws sso login --profile bazel-cache
pytest tests/integration/test_credential_flow.py::TestAWSIntegration -v

# Run all non-manual tests
docker-compose up -d && sleep 10
aws sso login --profile bazel-cache
pytest -m "integration and not manual" -v
```

### Manual Testing Procedures

Some tests require human interaction (MFA, browser login). These cannot be fully automated:

**1. Validate SSO Setup**
```bash
./validate_sso_setup.sh bazel-cache
# Expected: Script validates your AWS config has proper sso-session fields
```

**2. Test Initial Login with MFA**
```bash
./login.sh bazel-cache
# Expected: Browser opens, you authenticate with MFA, login succeeds
```

**3. Monitor Automatic Refresh (requires ~1 hour wait)**
```bash
docker-compose up -d
docker-compose logs -f credential-renewer
# Wait for token to approach expiration (~45 min)
# Expected: Automatic refresh occurs without user intervention
```

**4. Test Complete Workflow**
```bash
# Start services
docker-compose up -d

# Test artifact fetch
curl http://localhost:9000/healthz
# Expected: OK

# Monitor logs for automatic refresh cycles
docker-compose logs -f credential-renewer
docker-compose logs -f credential-monitor
docker-compose logs -f s3proxy
# Expected: Logs show credential refresh and s3proxy restart
```

**5. Test with Real Bazel Project**
```bash
# In your Bazel project directory
bazel build //...
# Expected: Maven artifacts fetched through proxy without authentication errors
```

### Expected Behavior

**Normal Operation:**
- Token checked every 15 minutes
- Automatic refresh when expiring within 1 hour
- No manual login needed for ~90 days
- S3Proxy restarts automatically after refresh
- No notification files created

**Refresh Failure (refresh token expired):**
- Notification file created at `/app/data/login_required.txt`
- User prompted to run `aws sso login`
- After login, notification cleared automatically

## Next Steps

Future testing improvements:

- [ ] Add mutation testing (mutmut)
- [ ] Add property-based testing (hypothesis)
- [ ] Add performance benchmarks
- [ ] CI/CD pipeline integration
- [ ] Automated test reporting

## Troubleshooting

### Common Issues

**"No module named pytest"**
```bash
pip install -r tests/requirements.txt
```

**"Docker not available" in integration tests**
```bash
# Start Docker
# Or skip integration tests: pytest -m "not integration"
```

**Import errors in tests**
```bash
# Ensure Python path includes service directories
# Tests already handle this with sys.path.insert()
```

**Coverage too low**
```bash
# View missing coverage
pytest --cov-report=term-missing

# See which lines need tests
```

## Resources

- [pytest documentation](https://docs.pytest.org/)
- [pytest-cov documentation](https://pytest-cov.readthedocs.io/)
- [Testing Best Practices](https://docs.python-guide.org/writing/tests/)
