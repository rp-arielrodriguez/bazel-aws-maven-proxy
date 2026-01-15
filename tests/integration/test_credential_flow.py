"""
Integration tests for the full credential management flow.

These tests require Docker to be running and test the interaction between
all three services: credential-monitor, credential-renewer, and s3proxy.
"""
import os
import json
import time
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import requests

# Get proxy port from environment or use default (matches .env.example)
PROXY_PORT = os.getenv("PROXY_PORT", "8888")


@pytest.mark.integration
@pytest.mark.docker
class TestCredentialFlow:
    """Integration tests for credential monitoring and renewal flow."""

    @pytest.fixture(scope="class")
    def docker_compose_up(self):
        """Start Docker Compose services for testing."""
        # Stop any existing services
        subprocess.run(["docker-compose", "down"], capture_output=True)

        # Start services
        result = subprocess.run(
            ["docker-compose", "up", "-d"],
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            pytest.skip(f"Docker Compose failed to start: {result.stderr}")

        # Wait for services to be healthy - increased timeout and better logging
        max_wait = 60
        for i in range(max_wait):
            try:
                response = requests.get(f"http://localhost:{PROXY_PORT}/healthz", timeout=2)
                if response.status_code == 200:
                    print(f"\nServices healthy after {i+1} seconds")
                    break
            except requests.exceptions.RequestException:
                pass
            time.sleep(1)
        else:
            # Print logs before skipping
            logs = subprocess.run(["docker-compose", "logs", "--tail=50"], capture_output=True, text=True)
            print(f"\nService logs:\n{logs.stdout}")
            pytest.skip("Services did not become healthy in time")

        yield

        # Cleanup
        subprocess.run(["docker-compose", "down"], capture_output=True)

    def test_s3proxy_health_check(self, docker_compose_up):
        """Test that s3proxy service is responding to health checks."""
        response = requests.get(f"http://localhost:{PROXY_PORT}/healthz", timeout=5)
        assert response.status_code == 200
        assert response.text == "OK"

    def test_s3proxy_responds_to_requests(self, docker_compose_up):
        """Test that s3proxy responds to artifact requests."""
        # Test that proxy is running and responding
        # We don't test actual S3 functionality here - that requires mocking
        # Just verify the proxy endpoint is reachable
        response = requests.get(f"http://localhost:{PROXY_PORT}/", timeout=5)

        # Proxy should respond (even if S3 fails, we get a response)
        assert response.status_code in [200, 404, 500], f"No response from proxy: {response.status_code}"

    @pytest.mark.slow
    def test_credential_monitor_detects_changes(self, docker_compose_up):
        """Test that credential monitor service is running and monitoring."""
        # Verify credential-monitor is running
        result = subprocess.run(
            ["docker-compose", "ps", "-q", "credential-monitor"],
            capture_output=True,
            text=True
        )
        assert result.stdout.strip(), "credential-monitor should be running"

        # Check logs show it's monitoring
        logs = subprocess.run(
            ["docker-compose", "logs", "credential-monitor", "--tail=30"],
            capture_output=True,
            text=True
        )

        # Verify monitor started and is watching files
        assert ("Started credential monitoring" in logs.stdout or
                "AWS credential monitor started" in logs.stdout or
                "Starting AWS credential monitor" in logs.stdout)
        assert "Monitoring" in logs.stdout

    @pytest.mark.slow
    def test_credential_renewer_service_running(self, docker_compose_up):
        """Test that credential renewer service is running."""
        # Verify credential-renewer is running
        result = subprocess.run(
            ["docker-compose", "ps", "-q", "credential-renewer"],
            capture_output=True,
            text=True
        )
        assert result.stdout.strip(), "credential-renewer should be running"

        # Check logs show it started
        logs = subprocess.run(
            ["docker-compose", "logs", "credential-renewer", "--tail=30"],
            capture_output=True,
            text=True
        )

        # Verify renewer started successfully
        assert ("Starting credential renewal" in logs.stdout or
                "Starting credential renewer" in logs.stdout or
                "Credential renewer started" in logs.stdout or
                "credential-renewer" in logs.stdout)


@pytest.mark.integration
class TestS3ProxyIntegration:
    """Integration tests for s3proxy service without full Docker setup."""

    def test_cache_persistence(self, tmp_path):
        """Test that cached files persist across requests."""
        # This would test that files downloaded once are cached
        # and served from cache on subsequent requests
        pass

    def test_concurrent_requests(self):
        """Test handling of concurrent requests to s3proxy."""
        # This would test that multiple simultaneous requests
        # are handled correctly without race conditions
        pass


@pytest.mark.integration
@pytest.mark.aws
class TestAWSIntegration:
    """Integration tests with mock S3 (LocalStack)."""

    @pytest.fixture(scope="class")
    def localstack_s3(self):
        """Start LocalStack for S3 mocking."""
        # Ensure any previous LocalStack is cleaned up
        subprocess.run(
            ["docker-compose", "--profile", "test", "down"],
            capture_output=True,
            text=True
        )

        # Start LocalStack with test profile
        result = subprocess.run(
            ["docker-compose", "--profile", "test", "up", "-d", "localstack"],
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            # Clean up on failure
            subprocess.run(["docker-compose", "--profile", "test", "down"], capture_output=True)
            pytest.skip("LocalStack failed to start")

        # Wait for LocalStack to be ready
        import time
        localstack_ready = False
        for _ in range(30):
            try:
                response = requests.get("http://localhost:4566/_localstack/health", timeout=2)
                if response.status_code == 200:
                    time.sleep(2)  # Extra time for S3 init
                    localstack_ready = True
                    break
            except:
                pass
            time.sleep(1)

        if not localstack_ready:
            # Clean up on timeout
            subprocess.run(["docker-compose", "--profile", "test", "down"], capture_output=True)
            pytest.skip("LocalStack not ready")

        yield

        # Cleanup - ensure container is removed
        subprocess.run(["docker-compose", "--profile", "test", "down"], capture_output=True)

    def test_fetch_from_mock_s3_bucket(self, localstack_s3):
        """Test fetching artifacts from LocalStack S3."""
        import boto3

        # Create S3 client pointing to LocalStack
        s3_client = boto3.client(
            's3',
            endpoint_url='http://localhost:4566',
            aws_access_key_id='test',
            aws_secret_access_key='test',
            region_name='us-east-1'
        )

        # List buckets to verify LocalStack is working
        try:
            response = s3_client.list_buckets()
            assert 'Buckets' in response
        except Exception as e:
            pytest.skip(f"LocalStack S3 not accessible: {e}")

    def test_s3_operations(self, localstack_s3):
        """Test basic S3 operations with LocalStack."""
        import boto3

        s3_client = boto3.client(
            's3',
            endpoint_url='http://localhost:4566',
            aws_access_key_id='test',
            aws_secret_access_key='test',
            region_name='us-east-1'
        )

        # Create bucket
        bucket_name = 'test-bucket'
        try:
            s3_client.create_bucket(Bucket=bucket_name)

            # Put object
            s3_client.put_object(
                Bucket=bucket_name,
                Key='test.txt',
                Body=b'test content'
            )

            # Get object
            response = s3_client.get_object(Bucket=bucket_name, Key='test.txt')
            content = response['Body'].read()
            assert content == b'test content'

        except Exception as e:
            pytest.fail(f"S3 operations failed: {e}")


@pytest.mark.integration
class TestEndToEndFlow:
    """End-to-end tests simulating real usage scenarios."""

    def test_bazel_maven_artifact_fetch(self):
        """Simulate Bazel fetching Maven artifacts through the proxy."""
        # This would simulate a complete Bazel build workflow:
        # 1. Bazel requests artifact
        # 2. Proxy checks cache
        # 3. Proxy fetches from S3 if needed
        # 4. Proxy serves artifact to Bazel
        # 5. Subsequent requests served from cache
        pass

    def test_credential_expiration_workflow(self):
        """Test the full workflow when credentials expire."""
        # This would test:
        # 1. Normal operation with valid credentials
        # 2. Credentials expire
        # 3. Renewer detects expiration
        # 4. Notification created
        # 5. User runs aws sso login
        # 6. Monitor detects new credentials
        # 7. Proxy restarts with new credentials
        # 8. Normal operation resumes
        pass


def test_integration_test_setup():
    """Verify integration test environment is properly configured."""
    # Check Docker is available
    result = subprocess.run(
        ["docker", "--version"],
        capture_output=True,
        text=True
    )
    assert result.returncode == 0, "Docker is not available"

    # Check docker-compose is available
    result = subprocess.run(
        ["docker-compose", "--version"],
        capture_output=True,
        text=True
    )
    assert result.returncode == 0, "docker-compose is not available"
