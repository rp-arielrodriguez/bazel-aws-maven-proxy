# Testing Guide

This document provides comprehensive information about testing the bazel-aws-maven-proxy project.

## Quick Start

```bash
# Install test dependencies
pip install -r tests/requirements.txt

# Run all unit tests (fast)
./run_tests.sh unit

# Run all tests with coverage
./run_tests.sh all

# View coverage report
./run_tests.sh coverage && open htmlcov/index.html
```

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
    └── test_credential_flow.py      # End-to-end tests
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

# Combine markers
pytest -m "unit and not slow"
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

## Next Steps

Future testing improvements:

- [ ] Add mutation testing (mutmut)
- [ ] Add property-based testing (hypothesis)
- [ ] Increase integration test coverage
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
