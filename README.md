# Bazel AWS Maven Proxy

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A seamless solution for integrating Bazel's Maven artifact system with private AWS S3 buckets, supporting modern AWS authentication methods including SSO. This project eliminates the need to restart build environments when AWS credentials are refreshed.

## The Problem

Engineers using Bazel with private Maven repositories in AWS S3 face several challenges:

- Legacy tools like bazels3cache only work with static AWS credentials
- Credential rotation requires daily restarts of build environments
- AWS SSO login credential refreshes aren't automatically detected
- Security best practices (temporary credentials, SSO) clash with developer productivity

This project resolves these issues by providing a transparent layer between Bazel and your S3-hosted Maven repository.

## Architecture

The system consists of two main components:

1. **Credential Monitor Service** - A Node.js application that efficiently detects AWS credential changes using filesystem events and triggers updates when needed.

2. **S3 Proxy Service** - A service that presents a stable HTTP endpoint to Bazel while handling dynamic authentication with AWS S3 behind the scenes.

```
┌─────────────────────────────────────────────┐
│                  Developer                   │
└───────────────────┬─────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────┐
│              AWS SSO Login                   │
│        $ aws sso login --profile dev         │
└───────────────────┬─────────────────────────┘
                    │ writes
                    ▼
┌─────────────────────────────────────────────┐
│          ~/.aws/credentials                  │
│          ~/.aws/sso/cache/*.json            │
└───────────────────┬─────────────────────────┘
                    │ monitors
                    ▼
┌─────────────────────────────────────────────┐
│        Credential Monitor Service            │
│     (Event-based filesystem watcher)         │
└───────────────────┬─────────────────────────┘
                    │ triggers restart
                    ▼
┌─────────────────────────────────────────────┐
│            S3 Proxy Service                  │
│    (Refreshes AWS auth & serves artifacts)   │
└───────────────────┬─────────────────────────┘
                    │
        ┌───────────┴───────────┐
        │                       │
        ▼                       ▼
┌─────────────────┐    ┌─────────────────────┐
│  Local Cache    │    │    AWS S3 Bucket    │
│  (Docker Vol)   │    │  (Maven Repository) │
└─────────────────┘    └─────────────────────┘
                    ▲
                    │ requests
┌─────────────────────────────────────────────┐
│              Bazel Build                     │
│      (maven_install configured to use        │
│       http://localhost:9000)                 │
└─────────────────────────────────────────────┘
```

## Features

- **Zero-configuration AWS authentication**: Works with AWS SSO, IAM roles, environment variables, and static credentials
- **Real-time credential monitoring**: Uses filesystem events (not polling) for immediate detection of credential changes
- **Proactive token management**: Detects expiring SSO tokens and refreshes them before they cause build failures
- **Container-based implementation**: Easy to deploy and integrate with existing workflows
- **Compatible with all Bazel versions**: No custom Bazel plugins or patching required
- **Support for modern AWS CLI**: Works with AWS CLI v2, including the latest versions (tested with 2.22.31+)

## Prerequisites

- Docker and Docker Compose
- AWS CLI v2 configured with SSO or other authentication method
- Bazel-based project with Maven dependencies

## Quick Start

### 1. Clone this repository

```bash
git clone https://github.com/yourusername/bazel-aws-maven-proxy.git
cd bazel-aws-maven-proxy
```

### 2. Configure your S3 bucket settings

```bash
cp .env.example .env
# Edit .env with your bucket details
```

### 3. Start the services

```bash
docker-compose up -d
```

### 4. Configure your Bazel project

**Add to your `.bazelrc`:**

```
# Use local proxy for Maven artifacts
build --define=maven_repo=http://localhost:9000/
```

**Update your `WORKSPACE` file:**

```python
load("@rules_jvm_external//:defs.bzl", "maven_install")

maven_install(
    name = "maven",
    artifacts = [
        # Your Maven dependencies here
    ],
    repositories = [
        "http://localhost:9000/",  # Our S3 proxy
        "https://repo1.maven.org/maven2",  # Fallback to Maven Central
    ],
)
```

That's it! Your Bazel builds will now use the proxy, which handles all the AWS credential management for you.

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `AWS_PROFILE` | AWS CLI profile to use | `default` |
| `AWS_REGION` | AWS region for S3 bucket | `us-west-2` |
| `S3_BUCKET_NAME` | Name of your Maven S3 bucket | (required) |
| `PROXY_PORT` | Local port for the proxy service | `9000` |
| `REFRESH_INTERVAL` | Token expiration check interval (ms) | `60000` |
| `LOG_LEVEL` | Logging verbosity | `info` |

## Troubleshooting

### Viewing Logs

```bash
# All services
docker-compose logs

# Specific service
docker-compose logs credential-monitor
docker-compose logs s3proxy
```

### Common Issues

**Q: Bazel can't connect to the proxy**
A: Check that Docker Compose is running (`docker-compose ps`) and that port 9000 is not being used by another service.

**Q: Proxy shows authentication errors**
A: Verify that your AWS authentication is working with `aws s3 ls s3://your-bucket-name/`.

**Q: Artifacts aren't being found**
A: Ensure your S3 bucket is correctly configured in `.env` and that the path structure matches what Bazel expects.

## Testing

This project includes comprehensive unit and integration tests to ensure reliability.

### Running Tests

#### Install Test Dependencies

```bash
pip install -r tests/requirements.txt
```

#### Run All Tests

```bash
# Run all tests with coverage
pytest

# Run only unit tests (fast)
pytest -m unit

# Run only integration tests (slower, requires Docker)
pytest -m integration

# Run with verbose output
pytest -v

# Run specific test file
pytest tests/unit/test_credential_renewer.py

# Run specific test function
pytest tests/unit/test_credential_renewer.py::TestFindSSOTokenFile::test_find_token_file_with_single_file
```

#### Test Coverage

```bash
# Generate coverage report
pytest --cov-report=html

# Open coverage report in browser
open htmlcov/index.html
```

### Test Organization

Tests are organized into:

- **Unit Tests** (`tests/unit/`): Fast tests that mock external dependencies
  - `test_credential_renewer.py` - Tests for credential renewal logic
  - `test_s3proxy.py` - Tests for S3 proxy and caching
  - `test_credential_monitor.py` - Tests for file monitoring

- **Integration Tests** (`tests/integration/`): Tests that verify service interaction
  - `test_credential_flow.py` - End-to-end credential management tests

- **Test Fixtures** (`tests/fixtures/`): Sample data files used by tests

### Test Markers

Tests are marked with categories for selective execution:

- `@pytest.mark.unit` - Fast unit tests (no external dependencies)
- `@pytest.mark.integration` - Integration tests (may use Docker)
- `@pytest.mark.slow` - Tests that take significant time
- `@pytest.mark.aws` - Tests that interact with AWS (mocked or real)
- `@pytest.mark.docker` - Tests that require Docker

Example:

```bash
# Run only fast unit tests
pytest -m unit

# Skip slow tests
pytest -m "not slow"

# Run AWS-related tests
pytest -m aws
```

### Writing New Tests

When adding new functionality:

1. Write unit tests for individual functions
2. Add integration tests for service interactions
3. Use fixtures from `tests/conftest.py` for common test setup
4. Ensure test coverage remains above 70%

Example test structure:

```python
import pytest

def test_my_function():
    """Test description."""
    # Arrange
    input_data = "test"

    # Act
    result = my_function(input_data)

    # Assert
    assert result == expected_output
```

### Continuous Testing

For development, you can run tests automatically on file changes:

```bash
# Install pytest-watch
pip install pytest-watch

# Run tests on change
ptw -- -m unit
```

## How It Works

### Credential Monitor Service

This service uses the Node.js `chokidar` package to efficiently monitor your AWS credentials files for changes. When it detects that credentials have been updated (e.g., after running `aws sso login`), it triggers the S3 proxy service to refresh its authentication.

### S3 Proxy Service

The S3 proxy provides a simple HTTP server that Bazel can use to fetch Maven artifacts. Behind the scenes, it:

1. Obtains AWS credentials from your current AWS CLI session
2. Authenticates to your S3 bucket
3. Retrieves Maven artifacts and caches them locally
4. Serves them to Bazel through a stable HTTP endpoint

The proxy is designed to refresh its AWS authentication when triggered by the credential monitor, ensuring continuous access to your S3 bucket even as credentials expire and are refreshed.

## Security Considerations

- AWS credentials are never copied or stored outside your local `.aws` directory
- The containers only have read-only access to credential files
- All credential handling follows AWS best practices

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
