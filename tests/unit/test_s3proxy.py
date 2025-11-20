"""
Unit tests for s3proxy service.
"""
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, Mock
import tempfile

import pytest
from botocore.exceptions import ClientError

# Add s3proxy to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../s3proxy'))

# Import after adding to path
import app


@pytest.fixture
def flask_app():
    """Create Flask test client."""
    app.app.config['TESTING'] = True
    with app.app.test_client() as client:
        yield client


@pytest.fixture
def mock_s3_client():
    """Create a mock S3 client."""
    mock_client = MagicMock()
    mock_client.list_buckets.return_value = {'Buckets': []}
    return mock_client


@pytest.fixture
def temp_cache_dir(tmp_path):
    """Create a temporary cache directory."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    return cache_dir


@pytest.mark.unit
class TestCacheOperations:
    """Tests for cache-related operations."""

    def test_get_cached_file_path(self):
        """Test converting request path to cache path."""
        with patch.object(app, 'CACHE_DIR', '/data'):
            result = app.get_cached_file_path('/com/example/artifact.jar')
            assert result == '/data/com/example/artifact.jar'

    def test_get_cached_file_path_without_leading_slash(self):
        """Test cache path conversion without leading slash."""
        with patch.object(app, 'CACHE_DIR', '/data'):
            result = app.get_cached_file_path('com/example/artifact.jar')
            assert result == '/data/com/example/artifact.jar'

    def test_ensure_parent_dir_exists(self, temp_cache_dir):
        """Test creating parent directories."""
        file_path = temp_cache_dir / "com" / "example" / "artifact.jar"
        app.ensure_parent_dir_exists(str(file_path))

        assert file_path.parent.exists()
        assert file_path.parent.is_dir()

    def test_create_cache_dir_if_not_exists(self, tmp_path):
        """Test cache directory initialization."""
        cache_dir = tmp_path / "new_cache"

        with patch.object(app, 'CACHE_DIR', str(cache_dir)):
            app.create_cache_dir_if_not_exists()

        assert cache_dir.exists()
        assert (cache_dir / 'healthz').exists()
        assert (cache_dir / 'healthz' / 'index.html').exists()


@pytest.mark.unit
class TestS3ClientInitialization:
    """Tests for S3 client creation and management."""

    def test_get_s3_client_creates_new_client(self, mock_env_vars):
        """Test creating S3 client for the first time."""
        with patch('boto3.Session') as mock_session_class:
            mock_session = MagicMock()
            mock_session_class.return_value = mock_session

            mock_creds = MagicMock()
            mock_creds.access_key = 'fake-access-key'
            mock_creds.secret_key = 'fake-secret-key'
            mock_creds.token = 'fake-token'
            mock_session.get_credentials.return_value = mock_creds

            with patch('boto3.client') as mock_client:
                mock_s3 = MagicMock()
                mock_s3.list_buckets.return_value = {'Buckets': []}
                mock_client.return_value = mock_s3

                # Reset global client
                app.s3_client = None
                app.last_credentials_check = 0

                result = app.get_s3_client()

                assert result == mock_s3
                mock_session_class.assert_called_once_with(profile_name='bazel-cache')
                mock_client.assert_called_once()

    def test_get_s3_client_no_credentials(self, mock_env_vars):
        """Test handling missing credentials."""
        with patch('boto3.Session') as mock_session_class:
            mock_session = MagicMock()
            mock_session_class.return_value = mock_session
            mock_session.get_credentials.return_value = None

            # Reset global client
            app.s3_client = None
            app.last_credentials_check = 0

            with pytest.raises(Exception, match="No credentials found"):
                app.get_s3_client()

    def test_get_s3_client_reuses_existing_client(self, mock_env_vars):
        """Test that client is reused within refresh interval."""
        mock_client = MagicMock()

        with patch.object(app, 's3_client', mock_client):
            with patch.object(app, 'last_credentials_check', 100):
                with patch('time.time', return_value=150):  # Within refresh interval
                    with patch.object(app, 'REFRESH_INTERVAL', 300):
                        result = app.get_s3_client()

        assert result == mock_client

    def test_get_s3_client_refreshes_after_interval(self, mock_env_vars):
        """Test that client is refreshed after interval expires."""
        with patch('boto3.Session') as mock_session_class:
            mock_session = MagicMock()
            mock_session_class.return_value = mock_session

            mock_creds = MagicMock()
            mock_creds.access_key = 'fake-access-key'
            mock_creds.secret_key = 'fake-secret-key'
            mock_creds.token = 'fake-token'
            mock_session.get_credentials.return_value = mock_creds

            with patch('boto3.client') as mock_client:
                mock_s3 = MagicMock()
                mock_s3.list_buckets.return_value = {'Buckets': []}
                mock_client.return_value = mock_s3

                with patch.object(app, 'last_credentials_check', 100):
                    with patch('time.time', return_value=500):  # Past refresh interval
                        with patch.object(app, 'REFRESH_INTERVAL', 300):
                            app.s3_client = MagicMock()  # Existing client
                            result = app.get_s3_client()

                # Should have created new client
                assert result == mock_s3


@pytest.mark.unit
class TestFetchFromS3:
    """Tests for fetching files from S3."""

    def test_fetch_from_s3_success(self, mock_s3_client, temp_cache_dir):
        """Test successful file fetch from S3."""
        file_path = "com/example/artifact.jar"

        # Make the mock actually create the file
        def mock_download(bucket, key, local_path):
            Path(local_path).parent.mkdir(parents=True, exist_ok=True)
            Path(local_path).write_bytes(b"fake content")

        mock_s3_client.download_file.side_effect = mock_download

        with patch.object(app, 'CACHE_DIR', str(temp_cache_dir)):
            with patch.object(app, 'S3_BUCKET_NAME', 'test-bucket'):
                result = app.fetch_from_s3(mock_s3_client, file_path)

        assert result is not None
        assert Path(result).exists()
        mock_s3_client.download_file.assert_called_once_with(
            'test-bucket',
            'com/example/artifact.jar',
            str(temp_cache_dir / 'com' / 'example' / 'artifact.jar')
        )

    def test_fetch_from_s3_file_not_found(self, mock_s3_client, temp_cache_dir):
        """Test handling file not found in S3."""
        file_path = "com/example/missing.jar"

        error = ClientError(
            {'Error': {'Code': 'NoSuchKey', 'Message': 'Not found'}},
            'download_file'
        )
        mock_s3_client.download_file.side_effect = error

        with patch.object(app, 'CACHE_DIR', str(temp_cache_dir)):
            with patch.object(app, 'S3_BUCKET_NAME', 'test-bucket'):
                result = app.fetch_from_s3(mock_s3_client, file_path)

        assert result is None

    def test_fetch_from_s3_removes_leading_slash(self, mock_s3_client, temp_cache_dir):
        """Test that leading slash is removed from S3 key."""
        file_path = "/com/example/artifact.jar"

        with patch.object(app, 'CACHE_DIR', str(temp_cache_dir)):
            with patch.object(app, 'S3_BUCKET_NAME', 'test-bucket'):
                app.fetch_from_s3(mock_s3_client, file_path)

        # Should call with key without leading slash
        call_args = mock_s3_client.download_file.call_args[0]
        assert call_args[1] == 'com/example/artifact.jar'


@pytest.mark.unit
class TestFlaskEndpoints:
    """Tests for Flask application endpoints."""

    def test_health_check_endpoint_healthy(self, flask_app, mock_s3_client):
        """Test health check when service is healthy."""
        with patch.object(app, 'get_s3_client', return_value=mock_s3_client):
            response = flask_app.get('/healthz')

        assert response.status_code == 200
        assert response.data == b'OK'

    def test_health_check_endpoint_unhealthy(self, flask_app):
        """Test health check when S3 client fails."""
        with patch.object(app, 'get_s3_client', side_effect=Exception("Connection failed")):
            response = flask_app.get('/healthz')

        assert response.status_code == 500
        assert b'Connection failed' in response.data

    def test_get_file_from_cache(self, flask_app, mock_s3_client, temp_cache_dir):
        """Test serving file from cache."""
        # Create cached file
        cached_file = temp_cache_dir / "com" / "example" / "artifact.jar"
        cached_file.parent.mkdir(parents=True)
        cached_file.write_bytes(b"cached content")

        with patch.object(app, 'CACHE_DIR', str(temp_cache_dir)):
            with patch.object(app, 'get_s3_client', return_value=mock_s3_client):
                response = flask_app.get('/com/example/artifact.jar')

        assert response.status_code == 200
        assert response.data == b"cached content"
        # Should not call S3 since file is in cache
        mock_s3_client.download_file.assert_not_called()

    def test_get_file_cache_miss_fetches_from_s3(self, flask_app, mock_s3_client, temp_cache_dir):
        """Test fetching file from S3 on cache miss."""
        file_path = "com/example/artifact.jar"

        def mock_download(bucket, key, local_path):
            Path(local_path).parent.mkdir(parents=True, exist_ok=True)
            Path(local_path).write_bytes(b"s3 content")

        mock_s3_client.download_file.side_effect = mock_download

        with patch.object(app, 'CACHE_DIR', str(temp_cache_dir)):
            with patch.object(app, 'S3_BUCKET_NAME', 'test-bucket'):
                with patch.object(app, 'get_s3_client', return_value=mock_s3_client):
                    response = flask_app.get(f'/{file_path}')

        assert response.status_code == 200
        assert response.data == b"s3 content"
        mock_s3_client.download_file.assert_called_once()

    def test_get_file_not_found(self, flask_app, mock_s3_client, temp_cache_dir):
        """Test 404 when file not in cache or S3."""
        error = ClientError(
            {'Error': {'Code': 'NoSuchKey', 'Message': 'Not found'}},
            'download_file'
        )
        mock_s3_client.download_file.side_effect = error

        with patch.object(app, 'CACHE_DIR', str(temp_cache_dir)):
            with patch.object(app, 'S3_BUCKET_NAME', 'test-bucket'):
                with patch.object(app, 'get_s3_client', return_value=mock_s3_client):
                    response = flask_app.get('/com/example/missing.jar')

        assert response.status_code == 404


@pytest.mark.unit
class TestDirectoryListing:
    """Tests for directory listing functionality."""

    def test_directory_listing_shows_cached_entries(self, flask_app, mock_s3_client, temp_cache_dir):
        """Test directory listing includes cached files."""
        # Create cached directory structure
        test_dir = temp_cache_dir / "com" / "example"
        test_dir.mkdir(parents=True)
        (test_dir / "artifact.jar").write_bytes(b"content")
        (test_dir / "artifact.pom").write_bytes(b"pom")

        # Mock S3 response (no additional entries)
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = []
        mock_s3_client.get_paginator.return_value = mock_paginator

        with patch.object(app, 'CACHE_DIR', str(temp_cache_dir)):
            with patch.object(app, 'S3_BUCKET_NAME', 'test-bucket'):
                with patch.object(app, 'get_s3_client', return_value=mock_s3_client):
                    response = flask_app.get('/com/example/')

        assert response.status_code == 200
        assert b'artifact.jar' in response.data
        assert b'artifact.pom' in response.data


@pytest.mark.unit
def test_with_s3_client_decorator(mock_s3_client):
    """Test the @with_s3_client decorator."""
    @app.with_s3_client
    def test_function(client, arg1, arg2):
        return f"{arg1}-{arg2}"

    with patch.object(app, 'get_s3_client', return_value=mock_s3_client):
        result = test_function("foo", "bar")

    assert result == "foo-bar"


@pytest.mark.unit
def test_with_s3_client_decorator_handles_errors():
    """Test decorator handles S3 client errors."""
    @app.with_s3_client
    def test_function(client):
        return "success"

    with app.app.app_context():
        with patch.object(app, 'get_s3_client', side_effect=ClientError({'Error': {'Code': 'Error'}}, 'test')):
            response = test_function()

        # Should return JSON error response
        assert isinstance(response, tuple)
        assert response[1] == 500
