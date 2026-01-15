"""
Unit tests for credential-renewer service.
"""
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock, mock_open

import pytest
from freezegun import freeze_time

# Add credential-renewer to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../credential-renewer'))

# Import module under test
import renewer


@pytest.mark.unit
class TestFindSSOTokenFile:
    """Tests for find_sso_token_file() function."""

    def test_find_token_file_with_single_file(self, temp_aws_dir, create_sso_token_file):
        """Test finding token file when only one exists."""
        token_file = create_sso_token_file(expires_in_seconds=3600)

        with patch.object(renewer, 'SSO_CACHE_DIR', str(temp_aws_dir / "sso" / "cache")):
            result = renewer.find_sso_token_file()

        assert result == token_file
        assert result.exists()

    def test_find_token_file_with_multiple_files(self, temp_aws_dir):
        """Test finding the most recent token file when multiple exist."""
        sso_cache_dir = temp_aws_dir / "sso" / "cache"
        sso_cache_dir.mkdir(parents=True, exist_ok=True)

        # Create multiple token files with different timestamps
        old_token = sso_cache_dir / "old_token.json"
        new_token = sso_cache_dir / "new_token.json"

        old_token.write_text('{"accessToken": "old"}')
        # Sleep briefly to ensure different timestamps
        import time
        time.sleep(0.01)
        new_token.write_text('{"accessToken": "new"}')

        with patch.object(renewer, 'SSO_CACHE_DIR', str(sso_cache_dir)):
            result = renewer.find_sso_token_file()

        assert result == new_token

    def test_find_token_file_with_no_cache_dir(self, temp_aws_dir):
        """Test handling when SSO cache directory doesn't exist."""
        non_existent_dir = temp_aws_dir / "sso" / "cache_nonexistent"

        with patch.object(renewer, 'SSO_CACHE_DIR', str(non_existent_dir)):
            result = renewer.find_sso_token_file()

        assert result is None

    def test_find_token_file_with_empty_cache_dir(self, temp_aws_dir):
        """Test handling when SSO cache directory is empty."""
        sso_cache_dir = temp_aws_dir / "sso" / "cache"
        sso_cache_dir.mkdir(parents=True, exist_ok=True)

        with patch.object(renewer, 'SSO_CACHE_DIR', str(sso_cache_dir)):
            result = renewer.find_sso_token_file()

        assert result is None


@pytest.mark.unit
class TestCheckTokenExpiration:
    """Tests for check_token_expiration() function."""

    def test_check_valid_token_not_expiring_soon(self, temp_aws_dir, valid_sso_token):
        """Test that valid token (not expiring soon) returns False."""
        with patch.object(renewer, 'SSO_CACHE_DIR', str(temp_aws_dir / "sso" / "cache")):
            with patch.object(renewer, 'RENEWAL_THRESHOLD', 3600):
                result = renewer.check_token_expiration()

        assert result is False

    def test_check_token_expiring_soon(self, temp_aws_dir, expiring_sso_token):
        """Test that token expiring within threshold returns True."""
        with patch.object(renewer, 'SSO_CACHE_DIR', str(temp_aws_dir / "sso" / "cache")):
            with patch.object(renewer, 'RENEWAL_THRESHOLD', 3600):
                result = renewer.check_token_expiration()

        assert result is True

    def test_check_expired_token(self, temp_aws_dir, expired_sso_token):
        """Test that expired token returns True."""
        with patch.object(renewer, 'SSO_CACHE_DIR', str(temp_aws_dir / "sso" / "cache")):
            with patch.object(renewer, 'RENEWAL_THRESHOLD', 3600):
                result = renewer.check_token_expiration()

        assert result is True

    def test_check_no_token_file(self, temp_aws_dir):
        """Test that missing token file returns True."""
        sso_cache_dir = temp_aws_dir / "sso" / "cache"
        sso_cache_dir.mkdir(parents=True, exist_ok=True)

        with patch.object(renewer, 'SSO_CACHE_DIR', str(sso_cache_dir)):
            result = renewer.check_token_expiration()

        assert result is True

    def test_check_token_missing_expires_at(self, temp_aws_dir):
        """Test handling token file without expiresAt field."""
        sso_cache_dir = temp_aws_dir / "sso" / "cache"
        sso_cache_dir.mkdir(parents=True, exist_ok=True)

        token_file = sso_cache_dir / "invalid_token.json"
        token_file.write_text(json.dumps({
            "accessToken": "fake-token",
            "region": "sa-east-1"
            # Missing expiresAt
        }))

        with patch.object(renewer, 'SSO_CACHE_DIR', str(sso_cache_dir)):
            result = renewer.check_token_expiration()

        assert result is True

    def test_check_token_invalid_json(self, temp_aws_dir):
        """Test handling corrupted token file."""
        sso_cache_dir = temp_aws_dir / "sso" / "cache"
        sso_cache_dir.mkdir(parents=True, exist_ok=True)

        token_file = sso_cache_dir / "corrupted_token.json"
        token_file.write_text("invalid json {{{")

        with patch.object(renewer, 'SSO_CACHE_DIR', str(sso_cache_dir)):
            result = renewer.check_token_expiration()

        assert result is True

    @freeze_time("2025-06-01 12:00:00")
    def test_check_token_at_exact_threshold(self, temp_aws_dir, create_sso_token_file):
        """Test token expiring at exact renewal threshold."""
        # Create token that expires in exactly 3600 seconds (1 hour)
        token_file = create_sso_token_file(expires_in_seconds=3600)

        with patch.object(renewer, 'SSO_CACHE_DIR', str(temp_aws_dir / "sso" / "cache")):
            with patch.object(renewer, 'RENEWAL_THRESHOLD', 3600):
                result = renewer.check_token_expiration()

        # At exact threshold, should return False (not less than threshold)
        assert result is False

    @freeze_time("2025-06-01 12:00:00")
    def test_check_token_just_below_threshold(self, temp_aws_dir, create_sso_token_file):
        """Test token expiring just below renewal threshold."""
        # Create token that expires in 3599 seconds (1 second less than threshold)
        token_file = create_sso_token_file(expires_in_seconds=3599)

        with patch.object(renewer, 'SSO_CACHE_DIR', str(temp_aws_dir / "sso" / "cache")):
            with patch.object(renewer, 'RENEWAL_THRESHOLD', 3600):
                result = renewer.check_token_expiration()

        assert result is True


@pytest.mark.unit
class TestPerformSSOLogin:
    """Tests for perform_sso_login() function."""

    def test_create_notification_file_success(self, temp_aws_dir, mock_aws_config, tmp_path):
        """Test successful creation of login notification file."""
        notification_file = tmp_path / "login_required.txt"

        # Mock configparser to return expected SSO values
        mock_config = MagicMock()
        mock_config_instance = MagicMock()
        mock_config.return_value = mock_config_instance
        mock_config_instance.__contains__ = lambda self, key: key == 'profile bazel-cache'
        mock_config_instance.__getitem__ = lambda self, key: {
            'sso_start_url': 'https://my-sso-portal.awsapps.com/start',
            'sso_region': 'sa-east-1'
        }
        mock_config_instance.get = MagicMock(side_effect=lambda section: {
            'profile bazel-cache': {
                'sso_start_url': 'https://my-sso-portal.awsapps.com/start',
                'sso_region': 'sa-east-1'
            }
        }.get(section, {}))

        # Make the section behave like a dict
        profile_section = MagicMock()
        profile_section.get = lambda key, default='unknown': {
            'sso_start_url': 'https://my-sso-portal.awsapps.com/start',
            'sso_region': 'sa-east-1'
        }.get(key, default)
        mock_config_instance.__getitem__ = lambda self, key: profile_section if key == 'profile bazel-cache' else {}

        with patch.object(renewer, 'AWS_PROFILE', 'bazel-cache'):
            with patch.object(renewer, 'LOGIN_NOTIFICATION_FILE', str(notification_file)):
                with patch('renewer.configparser.ConfigParser', return_value=mock_config_instance):
                    result = renewer.perform_sso_login()

        assert result is True
        assert notification_file.exists()

        content = notification_file.read_text()
        assert "AWS SSO LOGIN REQUIRED" in content
        assert "aws sso login --profile bazel-cache" in content
        assert "https://my-sso-portal.awsapps.com/start" in content
        assert "sa-east-1" in content

    def test_create_notification_file_with_unknown_profile(self, temp_aws_dir, mock_aws_config, tmp_path):
        """Test notification creation with non-existent profile."""
        notification_file = tmp_path / "login_required.txt"

        with patch.object(renewer, 'AWS_PROFILE', 'nonexistent-profile'):
            with patch.object(renewer, 'LOGIN_NOTIFICATION_FILE', str(notification_file)):
                with patch('os.path.expanduser', return_value=str(mock_aws_config)):
                    result = renewer.perform_sso_login()

        assert result is True
        assert notification_file.exists()

        content = notification_file.read_text()
        assert "AWS SSO LOGIN REQUIRED" in content
        assert "SSO Start URL: unknown" in content
        assert "SSO Region: unknown" in content

    def test_create_notification_file_missing_config(self, temp_aws_dir, tmp_path):
        """Test notification creation when AWS config file doesn't exist."""
        notification_file = tmp_path / "login_required.txt"
        non_existent_config = temp_aws_dir / "nonexistent_config"

        with patch.object(renewer, 'AWS_PROFILE', 'default'):
            with patch.object(renewer, 'LOGIN_NOTIFICATION_FILE', str(notification_file)):
                with patch('os.path.expanduser', return_value=str(non_existent_config)):
                    result = renewer.perform_sso_login()

        assert result is True
        assert notification_file.exists()

    def test_create_notification_file_permission_error(self, temp_aws_dir, tmp_path):
        """Test handling permission errors when creating notification."""
        notification_file = tmp_path / "readonly_dir" / "login_required.txt"

        with patch.object(renewer, 'AWS_PROFILE', 'default'):
            with patch.object(renewer, 'LOGIN_NOTIFICATION_FILE', str(notification_file)):
                with patch('pathlib.Path.mkdir', side_effect=PermissionError("No permission")):
                    result = renewer.perform_sso_login()

        assert result is False


@pytest.mark.unit
class TestMainLoop:
    """Tests for main() function and overall flow."""

    def test_main_loop_triggers_login_notification(self, temp_aws_dir, expired_sso_token, tmp_path, monkeypatch):
        """Test that main loop creates notification when token is expired."""
        notification_file = tmp_path / "login_required.txt"

        monkeypatch.setattr(renewer, 'SSO_CACHE_DIR', str(temp_aws_dir / "sso" / "cache"))
        monkeypatch.setattr(renewer, 'AWS_PROFILE', 'bazel-cache')
        monkeypatch.setattr(renewer, 'LOGIN_NOTIFICATION_FILE', str(notification_file))
        monkeypatch.setattr(renewer, 'CHECK_INTERVAL', 1)
        monkeypatch.setattr(renewer, 'RENEWAL_THRESHOLD', 3600)

        # Mock time.sleep to exit after first iteration
        iteration_count = [0]

        def mock_sleep(seconds):
            iteration_count[0] += 1
            if iteration_count[0] >= 1:
                raise KeyboardInterrupt()

        with patch('time.sleep', side_effect=mock_sleep):
            with patch('os.path.expanduser', return_value=str(temp_aws_dir / ".aws" / "config")):
                try:
                    renewer.main()
                except KeyboardInterrupt:
                    pass

        # Notification should have been created
        assert notification_file.exists()

    def test_main_loop_no_notification_for_valid_token(self, temp_aws_dir, valid_sso_token, tmp_path, monkeypatch):
        """Test that main loop doesn't create notification for valid token."""
        notification_file = tmp_path / "login_required.txt"

        monkeypatch.setattr(renewer, 'SSO_CACHE_DIR', str(temp_aws_dir / "sso" / "cache"))
        monkeypatch.setattr(renewer, 'AWS_PROFILE', 'bazel-cache')
        monkeypatch.setattr(renewer, 'LOGIN_NOTIFICATION_FILE', str(notification_file))
        monkeypatch.setattr(renewer, 'CHECK_INTERVAL', 1)
        monkeypatch.setattr(renewer, 'RENEWAL_THRESHOLD', 3600)

        # Mock time.sleep to exit after first iteration
        iteration_count = [0]

        def mock_sleep(seconds):
            iteration_count[0] += 1
            if iteration_count[0] >= 1:
                raise KeyboardInterrupt()

        with patch('time.sleep', side_effect=mock_sleep):
            try:
                renewer.main()
            except KeyboardInterrupt:
                pass

        # Notification should NOT have been created
        assert not notification_file.exists()


@pytest.mark.unit
class TestGetSSOSessionConfig:
    """Tests for get_sso_session_config() function."""

    def test_get_sso_session_config_success(self, tmp_path):
        """Test extracting complete SSO session configuration."""
        config_file = tmp_path / "config"
        config_file.write_text("""
[profile bazel-cache]
sso_session = my-sso
sso_account_id = 123456789012
sso_role_name = DeveloperRole

[sso-session my-sso]
sso_region = sa-east-1
sso_start_url = https://my-sso-portal.awsapps.com/start
sso_registration_scopes = sso:account:access
""")

        with patch('os.path.expanduser', return_value=str(config_file)):
            result = renewer.get_sso_session_config('bazel-cache')

        assert result['sso_session'] == 'my-sso'
        assert result['sso_region'] == 'sa-east-1'
        assert result['sso_start_url'] == 'https://my-sso-portal.awsapps.com/start'
        assert result['sso_account_id'] == '123456789012'
        assert result['sso_role_name'] == 'DeveloperRole'
        assert 'sso:account:access' in result['sso_registration_scopes']

    def test_get_sso_session_config_missing_profile(self, tmp_path):
        """Test error when profile not found."""
        config_file = tmp_path / "config"
        config_file.write_text("[profile other]\nsso_session = test")

        with patch('os.path.expanduser', return_value=str(config_file)):
            with pytest.raises(Exception, match="Profile 'nonexistent' not found"):
                renewer.get_sso_session_config('nonexistent')

    def test_get_sso_session_config_missing_sso_session_field(self, tmp_path):
        """Test error when profile missing sso_session field."""
        config_file = tmp_path / "config"
        config_file.write_text("[profile bazel-cache]\nsso_account_id = 123")

        with patch('os.path.expanduser', return_value=str(config_file)):
            with pytest.raises(Exception, match="missing 'sso_session' field"):
                renewer.get_sso_session_config('bazel-cache')

    def test_get_sso_session_config_missing_sso_session_block(self, tmp_path):
        """Test error when sso-session block not found."""
        config_file = tmp_path / "config"
        config_file.write_text("""
[profile bazel-cache]
sso_session = my-sso
""")

        with patch('os.path.expanduser', return_value=str(config_file)):
            with pytest.raises(Exception, match="SSO session 'my-sso' not found"):
                renewer.get_sso_session_config('bazel-cache')


@pytest.mark.unit
class TestFindClientRegistration:
    """Tests for find_client_registration() function."""

    def test_find_client_registration_exists_valid(self, temp_aws_dir):
        """Test finding valid client registration."""
        sso_cache_dir = temp_aws_dir / "sso" / "cache"
        sso_cache_dir.mkdir(parents=True, exist_ok=True)

        # Create registration file
        import hashlib
        sso_start_url = "https://my-sso.awsapps.com/start"
        url_hash = hashlib.sha1(sso_start_url.encode('utf-8')).hexdigest()
        reg_file = sso_cache_dir / f"botocore-client-id-sa-east-1-{url_hash}.json"

        expires_at = (datetime.now() + timedelta(days=30)).isoformat() + 'Z'
        reg_file.write_text(json.dumps({
            "clientId": "test-client-id",
            "clientSecret": "test-secret",
            "registrationExpiresAt": expires_at
        }))

        with patch.object(renewer, 'SSO_CACHE_DIR', str(sso_cache_dir)):
            result = renewer.find_client_registration('sa-east-1', sso_start_url)

        assert result is not None
        assert result['clientId'] == 'test-client-id'
        assert result['clientSecret'] == 'test-secret'

    def test_find_client_registration_not_exists(self, temp_aws_dir):
        """Test when no registration file exists."""
        sso_cache_dir = temp_aws_dir / "sso" / "cache"
        sso_cache_dir.mkdir(parents=True, exist_ok=True)

        with patch.object(renewer, 'SSO_CACHE_DIR', str(sso_cache_dir)):
            result = renewer.find_client_registration('sa-east-1', 'https://test.com')

        assert result is None

    def test_find_client_registration_expired(self, temp_aws_dir):
        """Test expired registration returns None."""
        sso_cache_dir = temp_aws_dir / "sso" / "cache"
        sso_cache_dir.mkdir(parents=True, exist_ok=True)

        import hashlib
        sso_start_url = "https://my-sso.awsapps.com/start"
        url_hash = hashlib.sha1(sso_start_url.encode('utf-8')).hexdigest()
        reg_file = sso_cache_dir / f"botocore-client-id-sa-east-1-{url_hash}.json"

        # Expired registration
        expires_at = (datetime.now() - timedelta(days=1)).isoformat() + 'Z'
        reg_file.write_text(json.dumps({
            "clientId": "test-client-id",
            "clientSecret": "test-secret",
            "registrationExpiresAt": expires_at
        }))

        with patch.object(renewer, 'SSO_CACHE_DIR', str(sso_cache_dir)):
            result = renewer.find_client_registration('sa-east-1', sso_start_url)

        assert result is None

    def test_find_client_registration_invalid_json(self, temp_aws_dir):
        """Test corrupted registration file."""
        sso_cache_dir = temp_aws_dir / "sso" / "cache"
        sso_cache_dir.mkdir(parents=True, exist_ok=True)

        import hashlib
        sso_start_url = "https://my-sso.awsapps.com/start"
        url_hash = hashlib.sha1(sso_start_url.encode('utf-8')).hexdigest()
        reg_file = sso_cache_dir / f"botocore-client-id-sa-east-1-{url_hash}.json"
        reg_file.write_text("invalid json {{{")

        with patch.object(renewer, 'SSO_CACHE_DIR', str(sso_cache_dir)):
            result = renewer.find_client_registration('sa-east-1', sso_start_url)

        assert result is None


@pytest.mark.unit
class TestRegisterSSOClient:
    """Tests for register_sso_client() function."""

    def test_register_sso_client_success(self, temp_aws_dir):
        """Test successful client registration."""
        sso_cache_dir = temp_aws_dir / "sso" / "cache"
        sso_cache_dir.mkdir(parents=True, exist_ok=True)

        mock_client = MagicMock()
        mock_client.register_client.return_value = {
            'clientId': 'new-client-id',
            'clientSecret': 'new-secret',
            'clientIdIssuedAt': 1234567890,
            'clientSecretExpiresAt': 1234567890 + 7776000  # 90 days
        }

        with patch('boto3.client', return_value=mock_client):
            with patch.object(renewer, 'SSO_CACHE_DIR', str(sso_cache_dir)):
                result = renewer.register_sso_client(
                    'sa-east-1',
                    'https://my-sso.awsapps.com/start',
                    ['sso:account:access']
                )

        assert result['clientId'] == 'new-client-id'
        assert result['clientSecret'] == 'new-secret'
        mock_client.register_client.assert_called_once()

    def test_register_sso_client_api_error(self, temp_aws_dir):
        """Test AWS API error during registration."""
        mock_client = MagicMock()
        from botocore.exceptions import ClientError
        mock_client.register_client.side_effect = ClientError(
            {'Error': {'Code': 'AccessDenied', 'Message': 'Access denied'}},
            'RegisterClient'
        )

        with patch('boto3.client', return_value=mock_client):
            with pytest.raises(Exception, match="AWS API error"):
                renewer.register_sso_client('sa-east-1', 'https://test.com', ['sso:account:access'])

    def test_register_sso_client_network_error(self):
        """Test network error during registration."""
        mock_client = MagicMock()
        mock_client.register_client.side_effect = Exception("Network timeout")

        with patch('boto3.client', return_value=mock_client):
            with pytest.raises(Exception, match="Network error"):
                renewer.register_sso_client('sa-east-1', 'https://test.com', ['sso:account:access'])


@pytest.mark.unit
class TestClearNotificationFile:
    """Tests for clear_notification_file() function."""

    def test_clear_notification_file_exists(self, tmp_path):
        """Test clearing existing notification file."""
        notification_file = tmp_path / "login_required.txt"
        notification_file.write_text("Login required")

        with patch.object(renewer, 'LOGIN_NOTIFICATION_FILE', str(notification_file)):
            renewer.clear_notification_file()

        assert not notification_file.exists()

    def test_clear_notification_file_not_exists(self, tmp_path):
        """Test clearing non-existent notification file."""
        notification_file = tmp_path / "login_required.txt"

        with patch.object(renewer, 'LOGIN_NOTIFICATION_FILE', str(notification_file)):
            renewer.clear_notification_file()  # Should not raise error


@pytest.mark.unit
class TestRefreshSSOToken:
    """Tests for refresh_sso_token() function."""

    def test_refresh_sso_token_success(self, temp_aws_dir, tmp_path):
        """Test complete successful token refresh flow."""
        sso_cache_dir = temp_aws_dir / "sso" / "cache"
        sso_cache_dir.mkdir(parents=True, exist_ok=True)

        # Create token file with refresh token
        token_file = sso_cache_dir / "token.json"
        expires_at = (datetime.now() + timedelta(minutes=30)).isoformat() + 'Z'
        token_file.write_text(json.dumps({
            "accessToken": "old-access-token",
            "refreshToken": "old-refresh-token",
            "expiresAt": expires_at
        }))

        # Mock SSO config
        config_file = tmp_path / "config"
        config_file.write_text("""
[profile default]
sso_session = my-sso

[sso-session my-sso]
sso_region = sa-east-1
sso_start_url = https://my-sso.awsapps.com/start
sso_registration_scopes = sso:account:access
""")

        # Mock OIDC client
        mock_oidc = MagicMock()
        mock_oidc.create_token.return_value = {
            'accessToken': 'new-access-token',
            'refreshToken': 'new-refresh-token',
            'expiresIn': 3600
        }

        with patch.object(renewer, 'SSO_CACHE_DIR', str(sso_cache_dir)):
            with patch('os.path.expanduser', return_value=str(config_file)):
                with patch('boto3.client', return_value=mock_oidc):
                    with patch.object(renewer, 'find_client_registration', return_value={
                        'clientId': 'test-client',
                        'clientSecret': 'test-secret'
                    }):
                        result = renewer.refresh_sso_token()

        assert result is True
        mock_oidc.create_token.assert_called_once()

        # Verify token file updated
        updated_token = json.loads(token_file.read_text())
        assert updated_token['accessToken'] == 'new-access-token'
        assert updated_token['refreshToken'] == 'new-refresh-token'

    def test_refresh_sso_token_no_refresh_token(self, temp_aws_dir):
        """Test refresh with missing refreshToken field."""
        sso_cache_dir = temp_aws_dir / "sso" / "cache"
        sso_cache_dir.mkdir(parents=True, exist_ok=True)

        token_file = sso_cache_dir / "token.json"
        token_file.write_text(json.dumps({
            "accessToken": "access-token",
            "expiresAt": (datetime.now() + timedelta(minutes=30)).isoformat() + 'Z'
        }))

        with patch.object(renewer, 'SSO_CACHE_DIR', str(sso_cache_dir)):
            result = renewer.refresh_sso_token()

        assert result is False

    def test_refresh_sso_token_expired_refresh_token(self, temp_aws_dir, tmp_path):
        """Test refresh with expired refresh token (InvalidGrantException)."""
        sso_cache_dir = temp_aws_dir / "sso" / "cache"
        sso_cache_dir.mkdir(parents=True, exist_ok=True)

        token_file = sso_cache_dir / "token.json"
        token_file.write_text(json.dumps({
            "accessToken": "access-token",
            "refreshToken": "expired-refresh-token",
            "expiresAt": (datetime.now() + timedelta(minutes=30)).isoformat() + 'Z'
        }))

        config_file = tmp_path / "config"
        config_file.write_text("""
[profile default]
sso_session = my-sso

[sso-session my-sso]
sso_region = sa-east-1
sso_start_url = https://my-sso.awsapps.com/start
""")

        from botocore.exceptions import ClientError
        mock_oidc = MagicMock()
        mock_oidc.create_token.side_effect = ClientError(
            {'Error': {'Code': 'InvalidGrantException', 'Message': 'Invalid grant'}},
            'CreateToken'
        )

        with patch.object(renewer, 'SSO_CACHE_DIR', str(sso_cache_dir)):
            with patch('os.path.expanduser', return_value=str(config_file)):
                with patch('boto3.client', return_value=mock_oidc):
                    with patch.object(renewer, 'find_client_registration', return_value={
                        'clientId': 'test-client',
                        'clientSecret': 'test-secret'
                    }):
                        result = renewer.refresh_sso_token()

        assert result is False

    def test_refresh_sso_token_network_error(self, temp_aws_dir, tmp_path):
        """Test refresh with network error."""
        sso_cache_dir = temp_aws_dir / "sso" / "cache"
        sso_cache_dir.mkdir(parents=True, exist_ok=True)

        token_file = sso_cache_dir / "token.json"
        token_file.write_text(json.dumps({
            "accessToken": "access-token",
            "refreshToken": "refresh-token",
            "expiresAt": (datetime.now() + timedelta(minutes=30)).isoformat() + 'Z'
        }))

        config_file = tmp_path / "config"
        config_file.write_text("""
[profile default]
sso_session = my-sso

[sso-session my-sso]
sso_region = sa-east-1
sso_start_url = https://my-sso.awsapps.com/start
""")

        mock_oidc = MagicMock()
        mock_oidc.create_token.side_effect = Exception("Network timeout")

        with patch.object(renewer, 'SSO_CACHE_DIR', str(sso_cache_dir)):
            with patch('os.path.expanduser', return_value=str(config_file)):
                with patch('boto3.client', return_value=mock_oidc):
                    with patch.object(renewer, 'find_client_registration', return_value={
                        'clientId': 'test-client',
                        'clientSecret': 'test-secret'
                    }):
                        result = renewer.refresh_sso_token()

        assert result is False

    def test_refresh_sso_token_no_token_file(self, temp_aws_dir):
        """Test refresh with no token file."""
        sso_cache_dir = temp_aws_dir / "sso" / "cache"
        sso_cache_dir.mkdir(parents=True, exist_ok=True)

        with patch.object(renewer, 'SSO_CACHE_DIR', str(sso_cache_dir)):
            result = renewer.refresh_sso_token()

        assert result is False

    def test_refresh_sso_token_config_error(self, temp_aws_dir, tmp_path):
        """Test refresh with config parsing error."""
        sso_cache_dir = temp_aws_dir / "sso" / "cache"
        sso_cache_dir.mkdir(parents=True, exist_ok=True)

        token_file = sso_cache_dir / "token.json"
        token_file.write_text(json.dumps({
            "accessToken": "access-token",
            "refreshToken": "refresh-token",
            "expiresAt": (datetime.now() + timedelta(minutes=30)).isoformat() + 'Z'
        }))

        config_file = tmp_path / "config"
        config_file.write_text("[profile other]\ntest=value")

        with patch.object(renewer, 'SSO_CACHE_DIR', str(sso_cache_dir)):
            with patch('os.path.expanduser', return_value=str(config_file)):
                result = renewer.refresh_sso_token()

        assert result is False


@pytest.mark.unit
class TestCheckTokenExpirationWithRefresh:
    """Tests for updated check_token_expiration() with refresh logic."""

    def test_check_expiration_with_successful_refresh(self, temp_aws_dir, tmp_path):
        """Test token expiring but refresh succeeds, returns False."""
        sso_cache_dir = temp_aws_dir / "sso" / "cache"
        sso_cache_dir.mkdir(parents=True, exist_ok=True)

        # Token expiring in 30 minutes (less than 1 hour threshold)
        token_file = sso_cache_dir / "token.json"
        expires_at = (datetime.now() + timedelta(minutes=30)).isoformat() + 'Z'
        token_file.write_text(json.dumps({
            "accessToken": "access-token",
            "refreshToken": "refresh-token",
            "expiresAt": expires_at
        }))

        with patch.object(renewer, 'SSO_CACHE_DIR', str(sso_cache_dir)):
            with patch.object(renewer, 'RENEWAL_THRESHOLD', 3600):
                with patch.object(renewer, 'refresh_sso_token', return_value=True):
                    result = renewer.check_token_expiration()

        assert result is False

    def test_check_expiration_with_failed_refresh(self, temp_aws_dir):
        """Test token expiring and refresh fails, returns True."""
        sso_cache_dir = temp_aws_dir / "sso" / "cache"
        sso_cache_dir.mkdir(parents=True, exist_ok=True)

        token_file = sso_cache_dir / "token.json"
        expires_at = (datetime.now() + timedelta(minutes=30)).isoformat() + 'Z'
        token_file.write_text(json.dumps({
            "accessToken": "access-token",
            "expiresAt": expires_at
        }))

        with patch.object(renewer, 'SSO_CACHE_DIR', str(sso_cache_dir)):
            with patch.object(renewer, 'RENEWAL_THRESHOLD', 3600):
                with patch.object(renewer, 'refresh_sso_token', return_value=False):
                    result = renewer.check_token_expiration()

        assert result is True

    @freeze_time("2026-01-14 12:00:00")
    def test_check_expiration_token_valid_no_refresh(self, temp_aws_dir):
        """Test token not expiring, no refresh attempted."""
        sso_cache_dir = temp_aws_dir / "sso" / "cache"
        sso_cache_dir.mkdir(parents=True, exist_ok=True)

        token_file = sso_cache_dir / "token.json"
        # Token expires in 2 hours (7200 seconds > 3600 threshold)
        expires_at = (datetime.now() + timedelta(hours=2)).isoformat() + 'Z'
        token_file.write_text(json.dumps({
            "accessToken": "access-token",
            "expiresAt": expires_at
        }))

        with patch.object(renewer, 'SSO_CACHE_DIR', str(sso_cache_dir)):
            with patch.object(renewer, 'RENEWAL_THRESHOLD', 3600):
                result = renewer.check_token_expiration()

        assert result is False


@pytest.mark.unit
def test_module_constants():
    """Test that module constants are properly defined."""
    assert hasattr(renewer, 'AWS_PROFILE')
    assert hasattr(renewer, 'SSO_CACHE_DIR')
    assert hasattr(renewer, 'CHECK_INTERVAL')
    assert hasattr(renewer, 'RENEWAL_THRESHOLD')
    assert hasattr(renewer, 'LOGIN_NOTIFICATION_FILE')
