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

The system consists of three main components:

1. **Credential Renewer Service** - Monitors SSO token expiration and automatically refreshes tokens using AWS SSO-OIDC APIs before they expire.

2. **Credential Monitor Service** - Detects AWS credential file changes using filesystem events and triggers proxy updates when needed.

3. **S3 Proxy Service** - Presents a stable HTTP endpoint to Bazel while handling dynamic authentication with AWS S3 behind the scenes.

```
┌─────────────────────────────────────────────┐
│                  Developer                   │
└───────────────────┬─────────────────────────┘
                    │
                    ▼ (once every ~90 days)
┌─────────────────────────────────────────────┐
│         AWS SSO Login (Manual)               │
│        $ aws sso login --profile dev         │
└───────────────────┬─────────────────────────┘
                    │ creates tokens
                    ▼
┌─────────────────────────────────────────────┐
│          ~/.aws/sso/cache/*.json             │
│     (access token + refresh token)           │
└───────────────────┬─────────────────────────┘
                    │
        ┌───────────┴──────────────┐
        │                          │
        ▼ monitors                 ▼ monitors
┌────────────────────┐    ┌────────────────────┐
│ Credential Renewer │    │ Credential Monitor │
│   (Auto-refresh    │    │  (File watcher)    │
│   via SSO-OIDC)    │    │                    │
└────────┬───────────┘    └──────────┬─────────┘
         │ refreshes token           │ detects changes
         │ (every ~1 hour)           │
         └───────────┬───────────────┘
                     │ writes new token
                     ▼
         ┌─────────────────────────┐
         │  ~/.aws/sso/cache/*.json│
         └───────────┬─────────────┘
                     │ triggers restart
                     ▼
         ┌─────────────────────────┐
         │    S3 Proxy Service     │
         │  (Loads new credentials)│
         └────────┬────────────────┘
                  │
      ┌───────────┴───────────┐
      │                       │
      ▼                       ▼
┌──────────────┐    ┌─────────────────────┐
│ Local Cache  │    │   AWS S3 Bucket     │
│ (Docker Vol) │    │ (Maven Repository)  │
└──────────────┘    └─────────────────────┘
                     ▲
                     │ requests artifacts
         ┌───────────────────────┐
         │    Bazel Build        │
         │ (http://localhost:9000│
         └───────────────────────┘
```

### Token Refresh Flow

```
Credential Renewer checks token expiration (every 15 min)
           │
           ▼ Token expiring within 1 hour?
    ┌──────┴──────┐
    │             │
   YES           NO
    │             │
    │             └─> Continue monitoring
    │
    ▼ Call refresh_sso_token()
    │
    ├─> Read SSO session config from ~/.aws/config
    ├─> Find/register SSO-OIDC client
    ├─> Call boto3 create_token() with refresh token
    │
    ▼ Success?
  ┌──┴──┐
  │     │
 YES   NO
  │     │
  │     └─> Create notification file for manual login
  │
  ▼ Write new token to SSO cache
  │
  ▼ Credential Monitor detects file change
  │
  ▼ S3 Proxy restarts with new credentials
  │
  ▼ Builds continue uninterrupted
```

## Features

- **Automatic SSO token refresh**: Refreshes AWS SSO tokens automatically using SSO-OIDC APIs - login once every ~90 days instead of multiple times daily
- **Zero-configuration AWS authentication**: Works with AWS SSO, IAM roles, environment variables, and static credentials
- **Real-time credential monitoring**: Uses filesystem events (not polling) for immediate detection of credential changes
- **Proactive token management**: Monitors token expiration and refreshes before they expire to prevent build failures
- **Container-based implementation**: Easy to deploy and integrate with existing workflows
- **Compatible with all Bazel versions**: No custom Bazel plugins or patching required
- **Support for modern AWS CLI**: Works with AWS CLI v2, including the latest versions (tested with 2.22.31+)

## Prerequisites

- Docker and Docker Compose
- AWS CLI v2 (v2.0+) configured with SSO or other authentication method
- Bazel-based project with Maven dependencies

## AWS SSO Configuration

For automatic token refresh to work, you need AWS CLI v2 with SSO session support. This allows the system to automatically refresh your AWS credentials without manual intervention.

### Initial Setup

1. **Configure SSO session in `~/.aws/config`:**

```ini
[profile bazel-cache]
sso_session = my-sso
sso_account_id = 123456789012
sso_role_name = DeveloperRole
region = sa-east-1

[sso-session my-sso]
sso_region = sa-east-1
sso_start_url = https://your-sso-portal.awsapps.com/start
sso_registration_scopes = sso:account:access
```

**Critical:** The `sso_registration_scopes = sso:account:access` line is REQUIRED for automatic token refresh to work.

2. **Validate your configuration:**

```bash
./validate_sso_setup.sh bazel-cache
```

3. **Initial login:**

```bash
aws sso login --profile bazel-cache
```

This opens your browser for authentication (MFA may be required).

### How Auto-Refresh Works

With proper SSO session configuration:

1. **Initial login** (once): You authenticate via browser with MFA
2. **Automatic refresh** (hourly): The credential-renewer service automatically refreshes tokens using AWS SSO-OIDC APIs
3. **Manual login** (rarely): Only needed when refresh token expires (~90 days)

**Without SSO session:** You would need to run `aws sso login` multiple times per day when tokens expire.

### Example Configuration

See `examples/aws_config_example` for a complete, annotated configuration file with detailed explanations.

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

**Q: Token refresh failing / Still prompted for login frequently**
A: Check SSO session configuration:
- Verify `sso_registration_scopes = sso:account:access` in `~/.aws/config`
- Run `./validate_sso_setup.sh <profile>` to check configuration
- Check credential-renewer logs: `docker-compose logs credential-renewer`
- Ensure SSO session section exists (see AWS SSO Configuration section above)

**Q: "Profile missing sso_session field" error**
A: Your AWS config uses old SSO format. Update to sso-session format (see `examples/aws_config_example`). Without this, auto-refresh won't work.

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

### AWS SSO Token Auto-Refresh

The credential-renewer service uses AWS SSO-OIDC APIs to automatically refresh your SSO tokens:

1. **Initial Setup**: You login once with `aws sso login --profile bazel-cache`
   - Creates an access token (expires ~1 hour)
   - Creates a refresh token (expires ~90 days)

2. **Automatic Refresh**: Every 15 minutes, the service:
   - Checks token expiration time
   - If expiring within 1 hour, calls `refresh_sso_token()`
   - Uses boto3 SSO-OIDC client to get new tokens
   - Updates SSO cache with refreshed credentials
   - Credential monitor detects change and restarts s3proxy

3. **Manual Login Only When Needed**: When refresh token expires (~90 days):
   - Creates notification file: `./data/login_required.txt`
   - Run `./check_login.sh` or `./login.sh` to re-authenticate

**Result**: Login once every few months instead of multiple times per day.

### Credential Monitor Service

Uses Python watchdog to efficiently monitor AWS credentials files for changes. When it detects updates (from manual login or auto-refresh), triggers S3 proxy restart to load new credentials.

### S3 Proxy Service

Provides stable HTTP server for Bazel to fetch Maven artifacts:

1. Obtains AWS credentials from AWS CLI session
2. Authenticates to S3 bucket
3. Retrieves Maven artifacts and caches locally
4. Serves to Bazel through stable endpoint

Refreshes authentication when credential monitor detects changes, ensuring continuous access.

## Security Considerations

- AWS credentials are never copied or stored outside your local `.aws` directory
- The containers only have read-only access to credential files
- All credential handling follows AWS best practices

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
