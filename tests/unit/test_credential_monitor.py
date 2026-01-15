"""
Unit tests for credential-monitor service.
"""
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock, call
import subprocess

import pytest
from watchdog.events import FileModifiedEvent

# Add credential-monitor to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../credential-monitor'))

# Import module under test
import monitor


@pytest.mark.unit
class TestCredentialEventHandler:
    """Tests for CredentialEventHandler class."""

    def test_handler_initialization(self):
        """Test event handler initializes with correct defaults."""
        handler = monitor.CredentialEventHandler()

        assert handler.last_event_time == 0
        assert handler.cooldown_period == 5

    def test_on_modified_ignores_directory_events(self):
        """Test that directory modification events are ignored."""
        handler = monitor.CredentialEventHandler()

        # Create directory event
        event = MagicMock()
        event.is_directory = True
        event.src_path = "/path/to/directory"

        with patch.object(handler, '_restart_s3proxy') as mock_restart:
            handler.on_modified(event)

        mock_restart.assert_not_called()

    def test_on_modified_triggers_restart_for_credentials_file(self, temp_aws_dir):
        """Test that credentials file modification triggers restart."""
        handler = monitor.CredentialEventHandler()
        credentials_file = temp_aws_dir / "credentials"
        credentials_file.write_text("fake credentials")

        event = FileModifiedEvent(str(credentials_file))

        with patch.object(handler, '_restart_s3proxy') as mock_restart:
            with patch.object(monitor, 'CREDENTIAL_FILE', str(credentials_file)):
                handler.on_modified(event)

        mock_restart.assert_called_once()

    def test_on_modified_triggers_restart_for_config_file(self, temp_aws_dir):
        """Test that config file modification triggers restart."""
        handler = monitor.CredentialEventHandler()
        config_file = temp_aws_dir / "config"
        config_file.write_text("fake config")

        event = FileModifiedEvent(str(config_file))

        with patch.object(handler, '_restart_s3proxy') as mock_restart:
            with patch.object(monitor, 'CONFIG_FILE', str(config_file)):
                handler.on_modified(event)

        mock_restart.assert_called_once()

    def test_on_modified_triggers_restart_for_sso_cache_file(self, temp_aws_dir):
        """Test that SSO cache file modification does NOT trigger restart (s3proxy polls instead)."""
        handler = monitor.CredentialEventHandler()
        sso_cache_dir = temp_aws_dir / "sso" / "cache"
        sso_cache_dir.mkdir(parents=True, exist_ok=True)
        token_file = sso_cache_dir / "token.json"
        token_file.write_text("{}")

        event = FileModifiedEvent(str(token_file))

        with patch.object(handler, '_restart_s3proxy') as mock_restart:
            with patch.object(monitor, 'SSO_CACHE_DIR', str(sso_cache_dir)):
                handler.on_modified(event)

        # SSO cache changes don't trigger restart - s3proxy polls every REFRESH_INTERVAL
        mock_restart.assert_not_called()

    def test_on_modified_respects_cooldown_period(self, temp_aws_dir):
        """Test that cooldown period prevents multiple restarts."""
        handler = monitor.CredentialEventHandler()
        credentials_file = temp_aws_dir / "credentials"
        credentials_file.write_text("fake credentials")

        event = FileModifiedEvent(str(credentials_file))

        with patch.object(handler, '_restart_s3proxy') as mock_restart:
            with patch.object(monitor, 'CREDENTIAL_FILE', str(credentials_file)):
                # First event
                handler.on_modified(event)
                assert mock_restart.call_count == 1

                # Second event immediately after (within cooldown)
                handler.on_modified(event)
                assert mock_restart.call_count == 1  # Still only 1

    def test_on_modified_allows_restart_after_cooldown(self, temp_aws_dir):
        """Test that restart is allowed after cooldown expires."""
        handler = monitor.CredentialEventHandler()
        handler.cooldown_period = 1  # 1 second cooldown
        credentials_file = temp_aws_dir / "credentials"
        credentials_file.write_text("fake credentials")

        event = FileModifiedEvent(str(credentials_file))

        with patch.object(handler, '_restart_s3proxy') as mock_restart:
            with patch.object(monitor, 'CREDENTIAL_FILE', str(credentials_file)):
                # First event
                handler.on_modified(event)
                assert mock_restart.call_count == 1

                # Wait for cooldown
                time.sleep(1.1)

                # Second event after cooldown
                handler.on_modified(event)
                assert mock_restart.call_count == 2

    def test_restart_s3proxy_success(self):
        """Test successful s3proxy container stop (compose auto-restarts)."""
        handler = monitor.CredentialEventHandler()

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            handler._restart_s3proxy()

        mock_run.assert_called_once_with(
            ["docker", "stop", "bazel-s3-proxy"],
            check=True
        )

    def test_restart_s3proxy_failure(self):
        """Test handling restart failure."""
        handler = monitor.CredentialEventHandler()

        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(1, 'docker-compose')

            # Should not raise, just log error
            handler._restart_s3proxy()

    def test_on_modified_ignores_unrelated_files(self, temp_aws_dir):
        """Test that unrelated files don't trigger restart."""
        handler = monitor.CredentialEventHandler()
        unrelated_file = temp_aws_dir / "random_file.txt"
        unrelated_file.write_text("random content")

        event = FileModifiedEvent(str(unrelated_file))

        with patch.object(handler, '_restart_s3proxy') as mock_restart:
            with patch.object(monitor, 'CREDENTIAL_FILE', str(temp_aws_dir / "credentials")):
                with patch.object(monitor, 'CONFIG_FILE', str(temp_aws_dir / "config")):
                    with patch.object(monitor, 'SSO_CACHE_DIR', str(temp_aws_dir / "sso" / "cache")):
                        handler.on_modified(event)

        mock_restart.assert_not_called()


@pytest.mark.unit
class TestStartMonitoring:
    """Tests for start_monitoring() function."""

    def test_start_monitoring_sets_up_observers(self, temp_aws_dir):
        """Test that monitoring correctly sets up file observers."""
        credentials_file = temp_aws_dir / "credentials"
        config_file = temp_aws_dir / "config"
        sso_cache_dir = temp_aws_dir / "sso" / "cache"

        credentials_file.write_text("creds")
        config_file.write_text("config")
        sso_cache_dir.mkdir(parents=True, exist_ok=True)

        with patch.object(monitor, 'CREDENTIAL_FILE', str(credentials_file)):
            with patch.object(monitor, 'CONFIG_FILE', str(config_file)):
                with patch.object(monitor, 'SSO_CACHE_DIR', str(sso_cache_dir)):
                    with patch('monitor.Observer') as mock_observer_class:
                        mock_observer = MagicMock()
                        mock_observer_class.return_value = mock_observer

                        # Mock the while loop to exit immediately
                        with patch('time.sleep', side_effect=KeyboardInterrupt()):
                            try:
                                monitor.start_monitoring()
                            except KeyboardInterrupt:
                                pass

        # Verify observer was started
        mock_observer.start.assert_called_once()
        # Verify schedules were added (at least for SSO cache dir)
        assert mock_observer.schedule.call_count >= 1

    def test_start_monitoring_creates_sso_cache_dir_if_missing(self, temp_aws_dir):
        """Test that monitor skips non-existent parent dirs (doesn't create them)."""
        sso_cache_dir = temp_aws_dir / "sso" / "cache_new"

        with patch.object(monitor, 'SSO_CACHE_DIR', str(sso_cache_dir)):
            with patch('monitor.Observer') as mock_observer_class:
                mock_observer = MagicMock()
                mock_observer_class.return_value = mock_observer

                with patch('time.sleep', side_effect=KeyboardInterrupt()):
                    try:
                        monitor.start_monitoring()
                    except KeyboardInterrupt:
                        pass

        # Monitor doesn't create directories - just skips them with warning
        assert not sso_cache_dir.exists()

    def test_start_monitoring_handles_missing_files_gracefully(self, temp_aws_dir):
        """Test that monitoring works even if some files don't exist."""
        # Only create SSO cache dir, not credential or config files
        sso_cache_dir = temp_aws_dir / "sso" / "cache"
        sso_cache_dir.mkdir(parents=True, exist_ok=True)

        credentials_file = temp_aws_dir / "credentials_nonexistent"
        config_file = temp_aws_dir / "config_nonexistent"

        with patch.object(monitor, 'CREDENTIAL_FILE', str(credentials_file)):
            with patch.object(monitor, 'CONFIG_FILE', str(config_file)):
                with patch.object(monitor, 'SSO_CACHE_DIR', str(sso_cache_dir)):
                    with patch('monitor.Observer') as mock_observer_class:
                        mock_observer = MagicMock()
                        mock_observer_class.return_value = mock_observer

                        with patch('time.sleep', side_effect=KeyboardInterrupt()):
                            try:
                                monitor.start_monitoring()
                            except KeyboardInterrupt:
                                pass

        # Should still start observer and schedule at least SSO cache dir
        mock_observer.start.assert_called_once()
        assert mock_observer.schedule.call_count >= 1


@pytest.mark.unit
def test_module_constants():
    """Test that module constants are properly defined."""
    assert hasattr(monitor, 'AWS_PROFILE')
    assert hasattr(monitor, 'AWS_DIR')
    assert hasattr(monitor, 'CREDENTIAL_FILE')
    assert hasattr(monitor, 'CONFIG_FILE')
    assert hasattr(monitor, 'SSO_CACHE_DIR')


@pytest.mark.unit
def test_event_handler_cooldown_calculation():
    """Test cooldown timing calculation."""
    handler = monitor.CredentialEventHandler()
    handler.cooldown_period = 5

    # Set last event time to 10 seconds ago
    handler.last_event_time = time.time() - 10

    # Event should be processed (outside cooldown)
    event = MagicMock()
    event.is_directory = False
    event.src_path = "/some/file"

    with patch.object(handler, '_restart_s3proxy') as mock_restart:
        with patch.object(monitor, 'CREDENTIAL_FILE', "/some/file"):
            handler.on_modified(event)

    mock_restart.assert_called_once()
