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
def test_module_constants():
    """Test that module constants are properly defined."""
    assert hasattr(renewer, 'AWS_PROFILE')
    assert hasattr(renewer, 'SSO_CACHE_DIR')
    assert hasattr(renewer, 'CHECK_INTERVAL')
    assert hasattr(renewer, 'RENEWAL_THRESHOLD')
    assert hasattr(renewer, 'LOGIN_NOTIFICATION_FILE')
