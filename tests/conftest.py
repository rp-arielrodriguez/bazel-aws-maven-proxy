"""
Shared pytest fixtures for bazel-aws-maven-proxy tests.
"""
import pytest


@pytest.fixture
def temp_aws_dir(tmp_path):
    """Create a temporary AWS directory structure."""
    aws_dir = tmp_path / ".aws"
    aws_dir.mkdir()

    sso_cache_dir = aws_dir / "sso" / "cache"
    sso_cache_dir.mkdir(parents=True)

    return aws_dir


@pytest.fixture
def mock_env_vars(temp_aws_dir, monkeypatch):
    """Set up mock environment variables for testing."""
    env_vars = {
        "AWS_PROFILE": "bazel-cache",
        "AWS_REGION": "sa-east-1",
        "S3_BUCKET_NAME": "test-maven-bucket",
        "PROXY_PORT": "8888",
        "CHECK_INTERVAL": "60",
        "LOG_LEVEL": "DEBUG",
        "HOME": str(temp_aws_dir.parent)
    }

    for key, value in env_vars.items():
        monkeypatch.setenv(key, value)

    return env_vars


@pytest.fixture(autouse=True)
def reset_logging():
    """Reset logging configuration before each test."""
    import logging
    # Remove all handlers from root logger
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)
    # Reset log level
    root.setLevel(logging.WARNING)
