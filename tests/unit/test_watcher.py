"""
Unit tests for sso-watcher service.
"""
import json
import os
import subprocess
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


def _mock_proc(stdout: str = "", returncode: int = 0):
    proc = MagicMock()
    proc.stdout = stdout
    proc.returncode = returncode
    return proc


# ---------------------------------------------------------------------------
# should_trigger_login
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestShouldTriggerLogin:

    def test_no_signal_file(self, watcher_state):
        assert watcher.should_trigger_login() is False

    def test_signal_file_exists_no_cooldown(self, watcher_state):
        write_signal(watcher_state["signal_file"])
        assert watcher.should_trigger_login() is True

    def test_within_cooldown(self, watcher_state):
        write_signal(watcher_state["signal_file"])
        watcher_state["last_run"].write_text(f"{time.time()}\n")
        with patch.object(watcher, 'COOLDOWN_SECONDS', 600):
            assert watcher.should_trigger_login() is False

    def test_cooldown_expired(self, watcher_state):
        write_signal(watcher_state["signal_file"])
        watcher_state["last_run"].write_text(f"{time.time() - 700}\n")
        with patch.object(watcher, 'COOLDOWN_SECONDS', 600):
            assert watcher.should_trigger_login() is True

    def test_next_attempt_after_not_reached(self, watcher_state):
        signal_data = {"profile": "default", "reason": "expired",
                       "nextAttemptAfter": time.time() + 9999}
        watcher_state["signal_file"].write_text(json.dumps(signal_data))
        assert watcher.should_trigger_login() is False

    def test_next_attempt_after_passed(self, watcher_state):
        signal_data = {"profile": "default", "reason": "expired",
                       "nextAttemptAfter": time.time() - 10}
        watcher_state["signal_file"].write_text(json.dumps(signal_data))
        assert watcher.should_trigger_login() is True


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestLocking:

    def test_acquire_lock(self, watcher_state):
        assert watcher.try_acquire_lock() is True
        assert watcher_state["lock_dir"].exists()

    def test_acquire_lock_already_held(self, watcher_state):
        watcher_state["lock_dir"].mkdir()
        assert watcher.try_acquire_lock() is False

    def test_release_lock(self, watcher_state):
        watcher_state["lock_dir"].mkdir()
        watcher.release_lock()
        assert not watcher_state["lock_dir"].exists()

    def test_release_nonexistent_lock(self, watcher_state):
        watcher.release_lock()


# ---------------------------------------------------------------------------
# Signal file operations
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestClearSignal:

    def test_clear_existing_signal(self, watcher_state):
        write_signal(watcher_state["signal_file"])
        watcher.clear_signal()
        assert not watcher_state["signal_file"].exists()

    def test_clear_nonexistent_signal(self, watcher_state):
        watcher.clear_signal()


@pytest.mark.unit
class TestUpdateSignalSnooze:

    def test_writes_next_attempt_after(self, watcher_state):
        write_signal(watcher_state["signal_file"])
        watcher.update_signal_snooze(900)
        data = json.loads(watcher_state["signal_file"].read_text())
        assert data["nextAttemptAfter"] > time.time()
        assert data["profile"] == "default"

    def test_overwrites_existing_snooze(self, watcher_state):
        write_signal(watcher_state["signal_file"])
        watcher.update_signal_snooze(100)
        first = json.loads(watcher_state["signal_file"].read_text())["nextAttemptAfter"]
        time.sleep(0.01)
        watcher.update_signal_snooze(200)
        second = json.loads(watcher_state["signal_file"].read_text())["nextAttemptAfter"]
        assert second > first


# ---------------------------------------------------------------------------
# show_notification (single osascript process)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestShowNotification:
    """Tests for the unified notification dialog."""

    def test_user_clicks_refresh(self):
        proc = _mock_proc("Refresh", 0)
        with patch.object(watcher.subprocess, 'run', return_value=proc):
            assert watcher.show_notification("default") == "refresh"

    def test_user_snoozes_15_min(self):
        proc = _mock_proc("snooze:15 min", 0)
        with patch.object(watcher.subprocess, 'run', return_value=proc):
            assert watcher.show_notification("default") == "snooze:900"

    def test_user_snoozes_30_min(self):
        proc = _mock_proc("snooze:30 min", 0)
        with patch.object(watcher.subprocess, 'run', return_value=proc):
            assert watcher.show_notification("default") == "snooze:1800"

    def test_user_snoozes_1_hour(self):
        proc = _mock_proc("snooze:1 hour", 0)
        with patch.object(watcher.subprocess, 'run', return_value=proc):
            assert watcher.show_notification("default") == "snooze:3600"

    def test_user_snoozes_4_hours(self):
        proc = _mock_proc("snooze:4 hours", 0)
        with patch.object(watcher.subprocess, 'run', return_value=proc):
            assert watcher.show_notification("default") == "snooze:14400"

    def test_user_suppresses(self):
        proc = _mock_proc("suppress", 0)
        with patch.object(watcher.subprocess, 'run', return_value=proc):
            assert watcher.show_notification("default") == "suppress"

    def test_dialog_times_out(self):
        proc = _mock_proc("dismiss", 0)
        with patch.object(watcher.subprocess, 'run', return_value=proc):
            assert watcher.show_notification("default") == "dismiss"

    def test_user_closes_dialog(self):
        proc = _mock_proc("", 1)
        with patch.object(watcher.subprocess, 'run', return_value=proc):
            assert watcher.show_notification("default") == "dismiss"

    def test_subprocess_timeout(self):
        with patch.object(watcher.subprocess, 'run',
                          side_effect=subprocess.TimeoutExpired("osascript", 135)):
            assert watcher.show_notification("default") == "dismiss"

    def test_osascript_not_found(self):
        with patch.object(watcher.subprocess, 'run', side_effect=FileNotFoundError):
            assert watcher.show_notification("default") == "refresh"

    def test_generic_exception(self):
        with patch.object(watcher.subprocess, 'run', side_effect=RuntimeError("boom")):
            assert watcher.show_notification("default") == "dismiss"

    def test_profile_in_script(self):
        proc = _mock_proc("Refresh", 0)
        with patch.object(watcher.subprocess, 'run', return_value=proc) as mock_run:
            watcher.show_notification("my-custom-profile")
            script = mock_run.call_args[0][0][2]
            assert "my-custom-profile" in script

    def test_unknown_snooze_label_returns_dismiss(self):
        proc = _mock_proc("snooze:99 years", 0)
        with patch.object(watcher.subprocess, 'run', return_value=proc):
            assert watcher.show_notification("default") == "dismiss"

    def test_snooze_cancel_returns_dismiss(self):
        """User clicks Snooze then cancels the picker."""
        proc = _mock_proc("dismiss", 0)
        with patch.object(watcher.subprocess, 'run', return_value=proc):
            assert watcher.show_notification("default") == "dismiss"

    def test_suppress_cancel_returns_dismiss(self):
        """User clicks Don't Remind then cancels the warning."""
        proc = _mock_proc("dismiss", 0)
        with patch.object(watcher.subprocess, 'run', return_value=proc):
            assert watcher.show_notification("default") == "dismiss"


# ---------------------------------------------------------------------------
# handle_login
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestHandleLogin:

    def test_notify_mode_refresh(self):
        with patch.object(watcher, 'LOGIN_MODE', 'notify'), \
             patch.object(watcher, 'show_notification', return_value="refresh"), \
             patch.object(watcher, 'run_aws_sso_login', return_value=0) as mock_login:
            assert watcher.handle_login("default") == "success"
            mock_login.assert_called_once_with("default")

    def test_notify_mode_refresh_login_fails(self):
        with patch.object(watcher, 'LOGIN_MODE', 'notify'), \
             patch.object(watcher, 'show_notification', return_value="refresh"), \
             patch.object(watcher, 'run_aws_sso_login', return_value=1):
            assert watcher.handle_login("default") == "failed"

    def test_notify_mode_dismiss(self):
        with patch.object(watcher, 'LOGIN_MODE', 'notify'), \
             patch.object(watcher, 'show_notification', return_value="dismiss"), \
             patch.object(watcher, 'run_aws_sso_login') as mock_login:
            assert watcher.handle_login("default") == "dismiss"
            mock_login.assert_not_called()

    def test_notify_mode_snooze(self):
        with patch.object(watcher, 'LOGIN_MODE', 'notify'), \
             patch.object(watcher, 'show_notification', return_value="snooze:900"), \
             patch.object(watcher, 'run_aws_sso_login') as mock_login:
            assert watcher.handle_login("default") == "snooze:900"
            mock_login.assert_not_called()

    def test_notify_mode_suppress(self):
        with patch.object(watcher, 'LOGIN_MODE', 'notify'), \
             patch.object(watcher, 'show_notification', return_value="suppress"), \
             patch.object(watcher, 'run_aws_sso_login') as mock_login:
            assert watcher.handle_login("default") == "suppress"
            mock_login.assert_not_called()

    def test_auto_mode_runs_directly(self):
        with patch.object(watcher, 'LOGIN_MODE', 'auto'), \
             patch.object(watcher, 'show_notification') as mock_notify, \
             patch.object(watcher, 'run_aws_sso_login', return_value=0):
            assert watcher.handle_login("default") == "success"
            mock_notify.assert_not_called()

    def test_auto_mode_login_failure(self):
        with patch.object(watcher, 'LOGIN_MODE', 'auto'), \
             patch.object(watcher, 'run_aws_sso_login', return_value=1):
            assert watcher.handle_login("default") == "failed"

    def test_notify_mode_uses_provided_profile(self):
        with patch.object(watcher, 'LOGIN_MODE', 'notify'), \
             patch.object(watcher, 'show_notification', return_value="dismiss") as mock_notify, \
             patch.object(watcher, 'run_aws_sso_login'):
            watcher.handle_login("staging")
            mock_notify.assert_called_once_with("staging")


# ---------------------------------------------------------------------------
# Last-run timestamp
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestLastRun:

    def test_write_and_read(self, watcher_state):
        ts = time.time()
        watcher.write_last_run(ts)
        assert watcher.read_last_run() == ts

    def test_read_missing_file(self, watcher_state):
        assert watcher.read_last_run() is None

    def test_read_corrupt_file(self, watcher_state):
        watcher_state["last_run"].write_text("not-a-number\n")
        assert watcher.read_last_run() is None


# ---------------------------------------------------------------------------
# Main loop integration tests
# ---------------------------------------------------------------------------

def _stop_after(n):
    """Return a sleep side_effect that raises KeyboardInterrupt after n calls."""
    call_count = 0
    def side_effect(_):
        nonlocal call_count
        call_count += 1
        if call_count >= n:
            raise KeyboardInterrupt
    return side_effect


@pytest.mark.unit
class TestMainLoopNotifyMode:

    def test_refresh_success_clears_signal(self, watcher_state):
        write_signal(watcher_state["signal_file"], profile="dev")
        with patch.object(watcher, 'LOGIN_MODE', 'notify'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'COOLDOWN_SECONDS', 0), \
             patch.object(watcher, 'show_notification', return_value="refresh"), \
             patch.object(watcher, 'run_aws_sso_login', return_value=0) as mock_login, \
             patch('time.sleep', side_effect=_stop_after(2)):
            watcher.main()
        mock_login.assert_called_once_with("dev")
        assert not watcher_state["signal_file"].exists()

    def test_dismiss_keeps_signal(self, watcher_state):
        write_signal(watcher_state["signal_file"], profile="prod")
        with patch.object(watcher, 'LOGIN_MODE', 'notify'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'COOLDOWN_SECONDS', 600), \
             patch.object(watcher, 'show_notification', return_value="dismiss"), \
             patch.object(watcher, 'run_aws_sso_login') as mock_login, \
             patch('time.sleep', side_effect=_stop_after(1)):
            watcher.main()
        mock_login.assert_not_called()
        assert watcher_state["signal_file"].exists()

    def test_login_failure_keeps_signal(self, watcher_state):
        write_signal(watcher_state["signal_file"])
        with patch.object(watcher, 'LOGIN_MODE', 'notify'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'COOLDOWN_SECONDS', 0), \
             patch.object(watcher, 'show_notification', return_value="refresh"), \
             patch.object(watcher, 'run_aws_sso_login', return_value=1), \
             patch('time.sleep', side_effect=_stop_after(2)):
            watcher.main()
        assert watcher_state["signal_file"].exists()

    def test_snooze_writes_next_attempt(self, watcher_state):
        write_signal(watcher_state["signal_file"])
        with patch.object(watcher, 'LOGIN_MODE', 'notify'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'COOLDOWN_SECONDS', 600), \
             patch.object(watcher, 'show_notification', return_value="snooze:900"), \
             patch('time.sleep', side_effect=_stop_after(1)):
            watcher.main()
        data = json.loads(watcher_state["signal_file"].read_text())
        assert data["nextAttemptAfter"] > time.time()

    def test_suppress_clears_signal(self, watcher_state):
        write_signal(watcher_state["signal_file"])
        with patch.object(watcher, 'LOGIN_MODE', 'notify'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'COOLDOWN_SECONDS', 600), \
             patch.object(watcher, 'show_notification', return_value="suppress"), \
             patch('time.sleep', side_effect=_stop_after(1)):
            watcher.main()
        assert not watcher_state["signal_file"].exists()

    def test_lock_released_after_dismiss(self, watcher_state):
        write_signal(watcher_state["signal_file"])
        with patch.object(watcher, 'LOGIN_MODE', 'notify'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'COOLDOWN_SECONDS', 0), \
             patch.object(watcher, 'show_notification', return_value="dismiss"), \
             patch('time.sleep', side_effect=_stop_after(2)):
            watcher.main()
        assert not watcher_state["lock_dir"].exists()

    def test_lock_released_after_exception(self, watcher_state):
        write_signal(watcher_state["signal_file"])
        with patch.object(watcher, 'LOGIN_MODE', 'notify'), \
             patch.object(watcher, 'COOLDOWN_SECONDS', 0), \
             patch.object(watcher, 'show_notification', side_effect=RuntimeError("boom")):
            assert watcher.try_acquire_lock() is True
            try:
                watcher.handle_login("default")
            except RuntimeError:
                pass
            finally:
                watcher.release_lock()
        assert not watcher_state["lock_dir"].exists()

    def test_signal_profile_fallback(self, watcher_state):
        signal_data = {"reason": "expired"}
        watcher_state["signal_file"].write_text(json.dumps(signal_data))
        with patch.object(watcher, 'LOGIN_MODE', 'notify'), \
             patch.object(watcher, 'PROFILE', 'fallback-profile'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'COOLDOWN_SECONDS', 600), \
             patch.object(watcher, 'show_notification', return_value="refresh") as mock_notify, \
             patch.object(watcher, 'run_aws_sso_login', return_value=0), \
             patch('time.sleep', side_effect=_stop_after(1)):
            watcher.main()
        mock_notify.assert_called_once_with("fallback-profile")
