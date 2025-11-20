"""
Shared pytest fixtures for bazel-aws-maven-proxy tests.
"""
import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any

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
def mock_aws_config(temp_aws_dir):
    """Create a mock AWS config file."""
    config_content = """[profile default]
region = us-west-2
output = json

[profile bazel-cache]
sso_session = my-sso
sso_account_id = 123456789012
sso_role_name = DeveloperRole
region = sa-east-1
output = json

[sso-session my-sso]
sso_region = sa-east-1
sso_start_url = https://my-sso-portal.awsapps.com/start
sso_registration_scopes = sso:account:access
"""
    config_file = temp_aws_dir / "config"
    config_file.write_text(config_content)
    return config_file


@pytest.fixture
def mock_aws_credentials(temp_aws_dir):
    """Create a mock AWS credentials file."""
    credentials_content = """[default]
aws_access_key_id = AKIAIOSFODNN7EXAMPLE
aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY

[bazel-cache]
aws_access_key_id = AKIAIOSFODNN7EXAMPLE2
aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY2
"""
    credentials_file = temp_aws_dir / "credentials"
    credentials_file.write_text(credentials_content)
    return credentials_file


@pytest.fixture
def create_sso_token_file(temp_aws_dir):
    """Factory fixture to create SSO token files with custom expiration."""
    def _create_token(expires_in_seconds: int, access_token: str = "fake-access-token") -> Path:
        """
        Create a mock SSO token file.

        Args:
            expires_in_seconds: Seconds until token expires (negative for already expired)
            access_token: The access token value

        Returns:
            Path to the created token file
        """
        sso_cache_dir = temp_aws_dir / "sso" / "cache"
        token_file = sso_cache_dir / "d033e22ae348aeb5660fc2140aec35850c4da997.json"

        expires_at = datetime.utcnow() + timedelta(seconds=expires_in_seconds)

        token_data = {
            "startUrl": "https://my-sso-portal.awsapps.com/start",
            "region": "sa-east-1",
            "accessToken": access_token,
            "expiresAt": expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        }

        token_file.write_text(json.dumps(token_data, indent=2))
        return token_file

    return _create_token


@pytest.fixture
def valid_sso_token(create_sso_token_file):
    """Create a valid SSO token that expires in 2 hours."""
    return create_sso_token_file(expires_in_seconds=7200)


@pytest.fixture
def expiring_sso_token(create_sso_token_file):
    """Create an SSO token that expires in 30 minutes (within renewal threshold)."""
    return create_sso_token_file(expires_in_seconds=1800)


@pytest.fixture
def expired_sso_token(create_sso_token_file):
    """Create an already-expired SSO token."""
    return create_sso_token_file(expires_in_seconds=-3600)


@pytest.fixture
def mock_env_vars(temp_aws_dir, monkeypatch):
    """Set up mock environment variables for testing."""
    env_vars = {
        "AWS_PROFILE": "bazel-cache",
        "AWS_REGION": "sa-east-1",
        "S3_BUCKET_NAME": "test-maven-bucket",
        "PROXY_PORT": "9000",
        "CHECK_INTERVAL": "60",
        "RENEWAL_THRESHOLD": "3600",
        "LOG_LEVEL": "DEBUG",
        "HOME": str(temp_aws_dir.parent)
    }

    for key, value in env_vars.items():
        monkeypatch.setenv(key, value)

    return env_vars


@pytest.fixture
def mock_s3_bucket():
    """Create a mock S3 bucket for testing."""
    from moto import mock_s3
    import boto3

    with mock_s3():
        # Create mock S3 bucket
        conn = boto3.resource("s3", region_name="sa-east-1")
        bucket = conn.create_bucket(
            Bucket="test-maven-bucket",
            CreateBucketConfiguration={"LocationConstraint": "sa-east-1"}
        )

        # Add some test artifacts
        test_artifacts = [
            "com/example/artifact/1.0.0/artifact-1.0.0.jar",
            "com/example/artifact/1.0.0/artifact-1.0.0.pom",
            "com/example/other/2.0.0/other-2.0.0.jar",
        ]

        for artifact_path in test_artifacts:
            bucket.put_object(Key=artifact_path, Body=b"fake artifact content")

        yield bucket


@pytest.fixture
def sample_maven_artifact():
    """Create a sample Maven artifact for testing."""
    return {
        "group_id": "com.example",
        "artifact_id": "test-artifact",
        "version": "1.0.0",
        "packaging": "jar",
        "content": b"fake jar content"
    }


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
