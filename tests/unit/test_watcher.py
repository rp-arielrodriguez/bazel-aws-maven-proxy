"""
Unit tests for sso-watcher service.
"""
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Add sso-watcher to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../sso-watcher'))

import watcher


@pytest.fixture
def watcher_state(tmp_path):
    """Set up isolated watcher state directory."""
    state_dir = tmp_path / "sso-renewer"
    state_dir.mkdir()
    signal_file = state_dir / "login-required.json"
    lock_dir = state_dir / "login.lock"
    last_run = state_dir / "last-login-at.txt"

    with patch.object(watcher, 'STATE_DIR', state_dir), \
         patch.object(watcher, 'SIGNAL_FILE', signal_file), \
         patch.object(watcher, 'LOCK_DIR', lock_dir), \
         patch.object(watcher, 'LAST_RUN_FILE', last_run):
        yield {
            "state_dir": state_dir,
            "signal_file": signal_file,
            "lock_dir": lock_dir,
            "last_run": last_run,
        }


def write_signal(signal_file: Path, profile: str = "default", reason: str = "expired"):
    """Helper to write a signal file."""
    signal_file.write_text(json.dumps({
        "profile": profile,
        "reason": reason,
        "timestamp": "2025-01-01T00:00:00Z",
    }))


@pytest.mark.unit
class TestShouldTriggerLogin:
    """Tests for should_trigger_login logic."""

    def test_no_signal_file(self, watcher_state):
        """No trigger when signal file missing."""
        assert watcher.should_trigger_login() is False

    def test_signal_file_exists_no_cooldown(self, watcher_state):
        """Trigger when signal exists and no prior run."""
        write_signal(watcher_state["signal_file"])
        assert watcher.should_trigger_login() is True

    def test_within_cooldown(self, watcher_state):
        """No trigger when within cooldown period."""
        write_signal(watcher_state["signal_file"])
        watcher_state["last_run"].write_text(f"{time.time()}\n")

        with patch.object(watcher, 'COOLDOWN_SECONDS', 600):
            assert watcher.should_trigger_login() is False

    def test_cooldown_expired(self, watcher_state):
        """Trigger when cooldown has passed."""
        write_signal(watcher_state["signal_file"])
        watcher_state["last_run"].write_text(f"{time.time() - 700}\n")

        with patch.object(watcher, 'COOLDOWN_SECONDS', 600):
            assert watcher.should_trigger_login() is True

    def test_next_attempt_after_not_reached(self, watcher_state):
        """No trigger when nextAttemptAfter is in the future."""
        signal_data = {
            "profile": "default",
            "reason": "expired",
            "nextAttemptAfter": time.time() + 9999,
        }
        watcher_state["signal_file"].write_text(json.dumps(signal_data))
        assert watcher.should_trigger_login() is False


@pytest.mark.unit
class TestLocking:
    """Tests for lock acquire/release."""

    def test_acquire_lock(self, watcher_state):
        """Lock can be acquired when not held."""
        assert watcher.try_acquire_lock() is True
        assert watcher_state["lock_dir"].exists()

    def test_acquire_lock_already_held(self, watcher_state):
        """Lock fails when already held."""
        watcher_state["lock_dir"].mkdir()
        assert watcher.try_acquire_lock() is False

    def test_release_lock(self, watcher_state):
        """Lock can be released."""
        watcher_state["lock_dir"].mkdir()
        watcher.release_lock()
        assert not watcher_state["lock_dir"].exists()

    def test_release_nonexistent_lock(self, watcher_state):
        """Releasing nonexistent lock is safe."""
        watcher.release_lock()  # should not raise


@pytest.mark.unit
class TestClearSignal:
    """Tests for signal file clearing."""

    def test_clear_existing_signal(self, watcher_state):
        """Signal file is removed."""
        write_signal(watcher_state["signal_file"])
        watcher.clear_signal()
        assert not watcher_state["signal_file"].exists()

    def test_clear_nonexistent_signal(self, watcher_state):
        """Clearing missing signal is safe."""
        watcher.clear_signal()  # should not raise


@pytest.mark.unit
class TestShowNotification:
    """Tests for macOS notification dialog."""

    def test_user_clicks_refresh(self):
        """User accepts -> returns True."""
        mock_proc = MagicMock()
        mock_proc.stdout = 'button returned:Refresh, gave up:false'
        mock_proc.returncode = 0

        with patch.object(watcher.subprocess, 'run', return_value=mock_proc):
            assert watcher.show_notification("default") is True

    def test_user_clicks_dismiss(self):
        """User dismisses -> returns False."""
        mock_proc = MagicMock()
        mock_proc.stdout = 'button returned:Dismiss, gave up:false'
        mock_proc.returncode = 0

        with patch.object(watcher.subprocess, 'run', return_value=mock_proc):
            assert watcher.show_notification("default") is False

    def test_dialog_times_out(self):
        """Dialog gave up -> returns False."""
        mock_proc = MagicMock()
        mock_proc.stdout = 'button returned:Refresh, gave up:true'
        mock_proc.returncode = 0

        with patch.object(watcher.subprocess, 'run', return_value=mock_proc):
            assert watcher.show_notification("default") is False

    def test_user_closes_dialog(self):
        """User closes dialog (rc != 0) -> returns False."""
        mock_proc = MagicMock()
        mock_proc.stdout = ''
        mock_proc.returncode = 1

        with patch.object(watcher.subprocess, 'run', return_value=mock_proc):
            assert watcher.show_notification("default") is False

    def test_osascript_not_found(self):
        """Non-macOS fallback -> returns True (auto mode)."""
        with patch.object(watcher.subprocess, 'run', side_effect=FileNotFoundError):
            assert watcher.show_notification("default") is True

    def test_subprocess_timeout(self):
        """Subprocess timeout -> returns False."""
        import subprocess
        with patch.object(watcher.subprocess, 'run', side_effect=subprocess.TimeoutExpired("osascript", 130)):
            assert watcher.show_notification("default") is False


@pytest.mark.unit
class TestHandleLogin:
    """Tests for handle_login flow."""

    def test_notify_mode_user_accepts(self):
        """Notify mode: user accepts, login runs."""
        with patch.object(watcher, 'LOGIN_MODE', 'notify'), \
             patch.object(watcher, 'show_notification', return_value=True), \
             patch.object(watcher, 'run_aws_sso_login', return_value=0) as mock_login:
            assert watcher.handle_login("default") is True
            mock_login.assert_called_once_with("default")

    def test_notify_mode_user_dismisses(self):
        """Notify mode: user dismisses, login skipped."""
        with patch.object(watcher, 'LOGIN_MODE', 'notify'), \
             patch.object(watcher, 'show_notification', return_value=False), \
             patch.object(watcher, 'run_aws_sso_login') as mock_login:
            assert watcher.handle_login("default") is False
            mock_login.assert_not_called()

    def test_auto_mode_runs_directly(self):
        """Auto mode: login runs without notification."""
        with patch.object(watcher, 'LOGIN_MODE', 'auto'), \
             patch.object(watcher, 'show_notification') as mock_notify, \
             patch.object(watcher, 'run_aws_sso_login', return_value=0):
            assert watcher.handle_login("default") is True
            mock_notify.assert_not_called()

    def test_login_failure_returns_false(self):
        """Login failure returns False regardless of mode."""
        with patch.object(watcher, 'LOGIN_MODE', 'auto'), \
             patch.object(watcher, 'run_aws_sso_login', return_value=1):
            assert watcher.handle_login("default") is False


@pytest.mark.unit
class TestLastRun:
    """Tests for last-run timestamp persistence."""

    def test_write_and_read(self, watcher_state):
        """Timestamp round-trips correctly."""
        ts = time.time()
        watcher.write_last_run(ts)
        assert watcher.read_last_run() == ts

    def test_read_missing_file(self, watcher_state):
        """Missing file returns None."""
        assert watcher.read_last_run() is None

    def test_read_corrupt_file(self, watcher_state):
        """Corrupt file returns None."""
        watcher_state["last_run"].write_text("not-a-number\n")
        assert watcher.read_last_run() is None
