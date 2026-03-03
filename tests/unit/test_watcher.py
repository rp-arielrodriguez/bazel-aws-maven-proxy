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

    mode_file = state_dir / "mode"

    with patch.object(watcher, 'STATE_DIR', state_dir), \
         patch.object(watcher, 'SIGNAL_FILE', signal_file), \
         patch.object(watcher, 'LOCK_DIR', lock_dir), \
         patch.object(watcher, 'LAST_RUN_FILE', last_run), \
         patch.object(watcher, 'MODE_FILE', mode_file):
        yield {
            "state_dir": state_dir,
            "signal_file": signal_file,
            "lock_dir": lock_dir,
            "last_run": last_run,
            "mode_file": mode_file,
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

    def test_clear_permission_error_does_not_crash(self, watcher_state):
        """PermissionError on unlink is caught, not propagated."""
        mock_signal = MagicMock()
        mock_signal.unlink.side_effect = PermissionError("denied")
        with patch.object(watcher, 'SIGNAL_FILE', mock_signal):
            watcher.clear_signal()  # should not raise


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

    def test_empty_output_with_success_rc(self):
        """osascript returns empty string with rc=0 → dismiss."""
        proc = _mock_proc("", 0)
        with patch.object(watcher.subprocess, 'run', return_value=proc):
            assert watcher.show_notification("default") == "dismiss"

    def test_garbage_output_with_success_rc(self):
        """osascript returns unexpected output → dismiss."""
        proc = _mock_proc("something unexpected", 0)
        with patch.object(watcher.subprocess, 'run', return_value=proc):
            assert watcher.show_notification("default") == "dismiss"

    def test_profile_with_quotes_escaped(self):
        """Profile with double quotes is escaped for AppleScript."""
        proc = _mock_proc("Refresh", 0)
        with patch.object(watcher.subprocess, 'run', return_value=proc) as mock_run:
            watcher.show_notification('my"profile')
            script = mock_run.call_args[0][0][2]
            assert 'my\\"profile' in script
            assert 'my"profile' not in script

    def test_profile_with_backslash_escaped(self):
        """Profile with backslash is escaped for AppleScript."""
        proc = _mock_proc("Refresh", 0)
        with patch.object(watcher.subprocess, 'run', return_value=proc) as mock_run:
            watcher.show_notification('my\\profile')
            script = mock_run.call_args[0][0][2]
            assert 'my\\\\profile' in script


# ---------------------------------------------------------------------------
# handle_login
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestHandleLogin:

    def test_notify_mode_refresh(self):
        with          patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'try_silent_refresh', return_value=False), \
             patch.object(watcher, '_run_notify_login', return_value="success") as mock_notify:
            assert watcher.handle_login("default") == "success"
            mock_notify.assert_called_once_with("default")

    def test_notify_mode_refresh_login_fails(self):
        with          patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'try_silent_refresh', return_value=False), \
             patch.object(watcher, '_run_notify_login', return_value="failed"):
            assert watcher.handle_login("default") == "failed"

    def test_notify_mode_dismiss(self):
        with          patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'try_silent_refresh', return_value=False), \
             patch.object(watcher, '_run_notify_login', return_value="dismiss"):
            assert watcher.handle_login("default") == "dismiss"

    def test_notify_mode_snooze(self):
        with          patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'try_silent_refresh', return_value=False), \
             patch.object(watcher, '_run_notify_login', return_value="snooze:900"):
            assert watcher.handle_login("default") == "snooze:900"

    def test_notify_mode_suppress(self):
        with          patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'try_silent_refresh', return_value=False), \
             patch.object(watcher, '_run_notify_login', return_value="suppress"):
            assert watcher.handle_login("default") == "suppress"

    def test_auto_mode_runs_directly(self):
        with          patch.object(watcher, 'read_mode', return_value='auto'), \
             patch.object(watcher, 'try_silent_refresh', return_value=False), \
             patch.object(watcher, '_run_notify_login') as mock_notify, \
             patch.object(watcher, 'run_aws_sso_login', return_value=0):
            assert watcher.handle_login("default") == "success"
            mock_notify.assert_not_called()

    def test_auto_mode_login_failure(self):
        with          patch.object(watcher, 'read_mode', return_value='auto'), \
             patch.object(watcher, 'try_silent_refresh', return_value=False), \
             patch.object(watcher, 'run_aws_sso_login', return_value=1):
            assert watcher.handle_login("default") == "failed"

    def test_notify_mode_uses_provided_profile(self):
        with          patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'try_silent_refresh', return_value=False), \
             patch.object(watcher, '_run_notify_login', return_value="dismiss") as mock_notify:
            watcher.handle_login("staging")
            mock_notify.assert_called_once_with("staging")

    def test_auto_mode_timeout_returns_failed(self):
        """Auto mode: run_aws_sso_login returns -1 (timeout) → 'failed'."""
        with patch.object(watcher, 'read_mode', return_value='auto'), \
             patch.object(watcher, 'try_silent_refresh', return_value=False), \
             patch.object(watcher, 'run_aws_sso_login', return_value=-1):
            assert watcher.handle_login("default") == "failed"

    def test_notify_mode_exception_propagates(self):
        """Exception in _run_notify_login propagates to caller."""
        with patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'try_silent_refresh', return_value=False), \
             patch.object(watcher, '_run_notify_login', side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError, match="boom"):
                watcher.handle_login("default")

    def test_silent_refresh_exception_propagates(self):
        """Exception in try_silent_refresh propagates to caller."""
        with patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'try_silent_refresh', side_effect=RuntimeError("crash")):
            with pytest.raises(RuntimeError, match="crash"):
                watcher.handle_login("default")


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
        with          patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'COOLDOWN_SECONDS', 0), \
             patch.object(watcher, '_run_notify_login', return_value="success") as mock_notify, \
             patch('time.sleep', side_effect=_stop_after(2)):
            watcher.main()
        mock_notify.assert_called_once_with("dev")
        assert not watcher_state["signal_file"].exists()

    def test_dismiss_keeps_signal(self, watcher_state):
        write_signal(watcher_state["signal_file"], profile="prod")
        with          patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'COOLDOWN_SECONDS', 600), \
             patch.object(watcher, '_run_notify_login', return_value="dismiss"), \
             patch('time.sleep', side_effect=_stop_after(1)):
            watcher.main()
        assert watcher_state["signal_file"].exists()

    def test_login_failure_keeps_signal(self, watcher_state):
        write_signal(watcher_state["signal_file"])
        with          patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'COOLDOWN_SECONDS', 0), \
             patch.object(watcher, '_run_notify_login', return_value="failed"), \
             patch.object(watcher, '_check_credentials_valid', return_value=False), \
             patch('time.sleep', side_effect=_stop_after(2)):
            watcher.main()
        assert watcher_state["signal_file"].exists()

    def test_login_failure_but_credentials_valid_clears_signal(self, watcher_state):
        """When login times out but auth actually succeeded (post-sleep), clear signal."""
        write_signal(watcher_state["signal_file"])
        with          patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'COOLDOWN_SECONDS', 0), \
             patch.object(watcher, '_run_notify_login', return_value="failed"), \
             patch.object(watcher, '_check_credentials_valid', return_value=True), \
             patch('time.sleep', side_effect=_stop_after(2)):
            watcher.main()
        assert not watcher_state["signal_file"].exists()

    def test_snooze_writes_next_attempt(self, watcher_state):
        write_signal(watcher_state["signal_file"])
        with          patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'COOLDOWN_SECONDS', 600), \
             patch.object(watcher, '_run_notify_login', return_value="snooze:900"), \
             patch('time.sleep', side_effect=_stop_after(1)):
            watcher.main()
        data = json.loads(watcher_state["signal_file"].read_text())
        assert data["nextAttemptAfter"] > time.time()

    def test_suppress_clears_signal(self, watcher_state):
        write_signal(watcher_state["signal_file"])
        with          patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'COOLDOWN_SECONDS', 600), \
             patch.object(watcher, '_run_notify_login', return_value="suppress"), \
             patch('time.sleep', side_effect=_stop_after(1)):
            watcher.main()
        assert not watcher_state["signal_file"].exists()

    def test_lock_released_after_dismiss(self, watcher_state):
        write_signal(watcher_state["signal_file"])
        with          patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'COOLDOWN_SECONDS', 0), \
             patch.object(watcher, '_run_notify_login', return_value="dismiss"), \
             patch('time.sleep', side_effect=_stop_after(2)):
            watcher.main()
        assert not watcher_state["lock_dir"].exists()

    def test_lock_released_after_exception(self, watcher_state):
        write_signal(watcher_state["signal_file"])
        with          patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'COOLDOWN_SECONDS', 0), \
             patch.object(watcher, '_run_notify_login', side_effect=RuntimeError("boom")):
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
        with patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'PROFILE', 'fallback-profile'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'COOLDOWN_SECONDS', 600), \
             patch.object(watcher, '_run_notify_login', return_value="success") as mock_notify, \
             patch('time.sleep', side_effect=_stop_after(1)):
            watcher.main()
        mock_notify.assert_called_once_with("fallback-profile")

    def test_unexpected_result_does_not_crash(self, watcher_state):
        """Unknown result string from handle_login → logs but loop continues."""
        write_signal(watcher_state["signal_file"])
        with patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'COOLDOWN_SECONDS', 0), \
             patch.object(watcher, '_run_notify_login', return_value="unknown_action"), \
             patch('time.sleep', side_effect=_stop_after(2)):
            watcher.main()  # should not raise
        # Signal file should still exist (no action taken)
        assert watcher_state["signal_file"].exists()
        # Lock should be released
        assert not watcher_state["lock_dir"].exists()

    def test_snooze_bad_seconds_uses_default(self, watcher_state):
        """Snooze with non-integer seconds falls back to 900s."""
        write_signal(watcher_state["signal_file"])
        with patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'COOLDOWN_SECONDS', 0), \
             patch.object(watcher, '_run_notify_login', return_value="snooze:abc"), \
             patch('time.sleep', side_effect=_stop_after(2)):
            watcher.main()  # should not crash
        data = json.loads(watcher_state["signal_file"].read_text())
        # 900s default fallback
        assert data["nextAttemptAfter"] > time.time() + 800
        assert data["nextAttemptAfter"] <= time.time() + 901

    def test_exception_in_handle_login_continues_loop(self, watcher_state):
        """RuntimeError during handle_login → caught by generic handler, loop continues."""
        write_signal(watcher_state["signal_file"])
        call_count = 0

        def flaky_login(profile):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("transient error")
            return "dismiss"

        with patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'COOLDOWN_SECONDS', 0), \
             patch.object(watcher, '_run_notify_login', side_effect=flaky_login), \
             patch('time.sleep', side_effect=_stop_after(3)):
            watcher.main()  # should not crash
        # Lock released even after exception
        assert not watcher_state["lock_dir"].exists()

    def test_signal_deleted_between_trigger_and_load(self, watcher_state):
        """Signal file deleted after should_trigger_login but before load_signal."""
        write_signal(watcher_state["signal_file"])
        original_load = watcher.load_signal

        def load_then_delete():
            # Delete the signal file, simulating a race
            watcher_state["signal_file"].unlink(missing_ok=True)
            return original_load()

        with patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'COOLDOWN_SECONDS', 0), \
             patch.object(watcher, 'PROFILE', 'fallback-prof'), \
             patch.object(watcher, 'load_signal', side_effect=load_then_delete), \
             patch.object(watcher, '_run_notify_login', return_value="success") as mock_notify, \
             patch('time.sleep', side_effect=_stop_after(2)):
            watcher.main()
        # Profile should fall back to env var PROFILE
        mock_notify.assert_called_with("fallback-prof")


# ---------------------------------------------------------------------------
# Mode management (read_mode / write_mode)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestModeManagement:

    def test_read_mode_from_file(self, watcher_state):
        watcher_state["mode_file"].write_text("auto\n")
        assert watcher.read_mode() == "auto"

    def test_read_mode_standalone(self, watcher_state):
        watcher_state["mode_file"].write_text("standalone\n")
        assert watcher.read_mode() == "standalone"

    def test_read_mode_falls_back_to_env(self, watcher_state):
        # No mode file, falls back to env
        with patch.object(watcher, '_ENV_MODE', 'auto'):
            assert watcher.read_mode() == "auto"

    def test_read_mode_default_notify(self, watcher_state):
        # No mode file, no env
        with patch.object(watcher, '_ENV_MODE', 'notify'):
            assert watcher.read_mode() == "notify"

    def test_read_mode_ignores_invalid_file(self, watcher_state):
        watcher_state["mode_file"].write_text("bogus\n")
        with patch.object(watcher, '_ENV_MODE', 'notify'):
            assert watcher.read_mode() == "notify"

    def test_read_mode_ignores_invalid_env(self, watcher_state):
        with patch.object(watcher, '_ENV_MODE', 'bogus'):
            assert watcher.read_mode() == "notify"

    def test_write_mode(self, watcher_state):
        watcher.write_mode("standalone")
        assert watcher_state["mode_file"].read_text().strip() == "standalone"

    def test_write_mode_invalid(self, watcher_state):
        with pytest.raises(ValueError):
            watcher.write_mode("bogus")

    def test_standalone_mode_skips_signal(self, watcher_state):
        """In standalone mode, watcher should not process signals."""
        write_signal(watcher_state["signal_file"])
        with patch.object(watcher, 'read_mode', return_value='standalone'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, '_run_notify_login') as mock_notify_login, \
             patch.object(watcher, 'run_aws_sso_login') as mock_login, \
             patch('time.sleep', side_effect=_stop_after(2)):
            watcher.main()
        mock_notify_login.assert_not_called()
        mock_login.assert_not_called()
        # Signal should still exist (not cleared)
        assert watcher_state["signal_file"].exists()

    def test_handle_login_standalone_returns_dismiss(self):
        with patch.object(watcher, 'read_mode', return_value='standalone'), \
             patch.object(watcher, '_run_notify_login') as mock_notify_login, \
             patch.object(watcher, 'run_aws_sso_login') as mock_login:
            assert watcher.handle_login("default") == "dismiss"
            mock_notify_login.assert_not_called()
            mock_login.assert_not_called()


# ---------------------------------------------------------------------------
# State machine transition tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestStateMachineTransitions:
    """
    Tests verifying all transitions from docs/state-machine.md.
    Each test maps to a row in the transition table.
    """

    # -- polling → polling (lock held by another instance) --

    def test_lock_held_skips_login(self, watcher_state):
        """polling + signal + ready + lock held → skip, stay polling."""
        write_signal(watcher_state["signal_file"])
        watcher_state["lock_dir"].mkdir()  # simulate held lock
        with patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'COOLDOWN_SECONDS', 0), \
             patch.object(watcher, '_run_notify_login') as mock_notify_login, \
             patch.object(watcher, 'run_aws_sso_login') as mock_login, \
             patch('time.sleep', side_effect=_stop_after(2)):
            watcher.main()
        mock_notify_login.assert_not_called()
        mock_login.assert_not_called()
        assert watcher_state["signal_file"].exists()

    # -- auto mode: main loop integration --

    def test_auto_mode_success_clears_signal(self, watcher_state):
        """auto: signal → login success → signal cleared + cooldown written."""
        write_signal(watcher_state["signal_file"], profile="auto-prof")
        with patch.object(watcher, 'read_mode', return_value='auto'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'COOLDOWN_SECONDS', 0), \
             patch.object(watcher, 'run_aws_sso_login', return_value=0) as mock_login, \
             patch('time.sleep', side_effect=_stop_after(2)):
            watcher.main()
        mock_login.assert_called_once_with("auto-prof")
        assert not watcher_state["signal_file"].exists()
        assert watcher_state["last_run"].exists()

    def test_auto_mode_failure_writes_30s_snooze(self, watcher_state):
        """auto: signal → login failed → signal kept + 30s snooze."""
        write_signal(watcher_state["signal_file"])
        with patch.object(watcher, 'read_mode', return_value='auto'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'COOLDOWN_SECONDS', 0), \
             patch.object(watcher, 'run_aws_sso_login', return_value=1), \
             patch('time.sleep', side_effect=_stop_after(2)):
            watcher.main()
        assert watcher_state["signal_file"].exists()
        data = json.loads(watcher_state["signal_file"].read_text())
        assert data["nextAttemptAfter"] <= time.time() + 31
        assert data["nextAttemptAfter"] > time.time() + 20

    # -- notify mode: verify cooldown file written --

    def test_success_writes_cooldown(self, watcher_state):
        """notify: refresh success → last-login-at.txt written."""
        write_signal(watcher_state["signal_file"])
        with patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'COOLDOWN_SECONDS', 0), \
             patch.object(watcher, '_run_notify_login', return_value="success"), \
             patch('time.sleep', side_effect=_stop_after(2)):
            watcher.main()
        assert watcher_state["last_run"].exists()
        ts = float(watcher_state["last_run"].read_text().strip())
        assert time.time() - ts < 5

    def test_dismiss_writes_cooldown(self, watcher_state):
        """notify: dismiss → last-login-at.txt written (cooldown to prevent spam)."""
        write_signal(watcher_state["signal_file"])
        with patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'COOLDOWN_SECONDS', 600), \
             patch.object(watcher, '_run_notify_login', return_value="dismiss"), \
             patch('time.sleep', side_effect=_stop_after(1)):
            watcher.main()
        assert watcher_state["last_run"].exists()

    def test_suppress_writes_cooldown(self, watcher_state):
        """notify: suppress → last-login-at.txt written."""
        write_signal(watcher_state["signal_file"])
        with patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'COOLDOWN_SECONDS', 600), \
             patch.object(watcher, '_run_notify_login', return_value="suppress"), \
             patch('time.sleep', side_effect=_stop_after(1)):
            watcher.main()
        assert watcher_state["last_run"].exists()

    def test_failure_writes_30s_snooze_not_cooldown(self, watcher_state):
        """notify: refresh fail → 30s snooze written, NO cooldown file."""
        write_signal(watcher_state["signal_file"])
        with patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'COOLDOWN_SECONDS', 0), \
             patch.object(watcher, '_run_notify_login', return_value="failed"), \
             patch('time.sleep', side_effect=_stop_after(2)):
            watcher.main()
        # 30s snooze written
        data = json.loads(watcher_state["signal_file"].read_text())
        assert "nextAttemptAfter" in data
        # No cooldown file (failure should not write cooldown)
        assert not watcher_state["last_run"].exists()

    # -- mode switch mid-loop --

    def test_mode_switch_takes_effect_next_cycle(self, watcher_state):
        """Mode is re-read every poll cycle; switching from standalone → notify mid-loop."""
        write_signal(watcher_state["signal_file"])
        call_count = 0

        def mode_switching_sleep(_):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # After first poll (standalone), switch to notify
                watcher_state["mode_file"].write_text("notify\n")
            elif call_count >= 3:
                raise KeyboardInterrupt

        with patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'COOLDOWN_SECONDS', 0), \
             patch.object(watcher, '_run_notify_login', return_value="success") as mock_notify_login, \
             patch('time.sleep', side_effect=mode_switching_sleep):
            # Start in standalone
            watcher_state["mode_file"].write_text("standalone\n")
            watcher.main()

        # Should have eventually processed the signal after mode switch
        mock_notify_login.assert_called_once()
        assert not watcher_state["signal_file"].exists()

    # -- lock released after all outcomes --

    def test_lock_released_after_auto_success(self, watcher_state):
        """Lock must be released after auto mode success."""
        write_signal(watcher_state["signal_file"])
        with patch.object(watcher, 'read_mode', return_value='auto'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'COOLDOWN_SECONDS', 0), \
             patch.object(watcher, 'run_aws_sso_login', return_value=0), \
             patch('time.sleep', side_effect=_stop_after(2)):
            watcher.main()
        assert not watcher_state["lock_dir"].exists()

    def test_lock_released_after_auto_failure(self, watcher_state):
        """Lock must be released after auto mode failure."""
        write_signal(watcher_state["signal_file"])
        with patch.object(watcher, 'read_mode', return_value='auto'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'COOLDOWN_SECONDS', 0), \
             patch.object(watcher, 'run_aws_sso_login', return_value=1), \
             patch('time.sleep', side_effect=_stop_after(2)):
            watcher.main()
        assert not watcher_state["lock_dir"].exists()

    def test_lock_released_after_snooze(self, watcher_state):
        """Lock must be released after snooze."""
        write_signal(watcher_state["signal_file"])
        with patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'COOLDOWN_SECONDS', 0), \
             patch.object(watcher, '_run_notify_login', return_value="snooze:900"), \
             patch('time.sleep', side_effect=_stop_after(1)):
            watcher.main()
        assert not watcher_state["lock_dir"].exists()


# ---------------------------------------------------------------------------
# _extract_authorize_url / _extract_callback_host / _launch_webview
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestExtractAuthorizeUrl:

    def test_extracts_url_from_stdout(self):
        proc = MagicMock()
        proc.stdout = iter([
            "Browser will not be automatically opened.\n",
            "Please visit the following URL:\n",
            "\n",
            "https://oidc.us-east-1.amazonaws.com/authorize?client_id=abc&redirect_uri=http%3A%2F%2F127.0.0.1%3A9999%2Fcallback\n",
        ])
        result = watcher._extract_authorize_url(proc)
        assert result == "https://oidc.us-east-1.amazonaws.com/authorize?client_id=abc&redirect_uri=http%3A%2F%2F127.0.0.1%3A9999%2Fcallback"

    def test_returns_none_when_no_url(self):
        proc = MagicMock()
        proc.stdout = iter([
            "Some error message\n",
            "No URL here\n",
        ])
        result = watcher._extract_authorize_url(proc, timeout=0.1)
        assert result is None

    def test_returns_none_when_stdout_is_none(self):
        proc = MagicMock()
        proc.stdout = None
        result = watcher._extract_authorize_url(proc)
        assert result is None

    def test_returns_none_on_empty_stdout(self):
        proc = MagicMock()
        proc.stdout = iter([])
        result = watcher._extract_authorize_url(proc)
        assert result is None


@pytest.mark.unit
class TestExtractCallbackHost:

    def test_extracts_host_and_port(self):
        url = "https://oidc.example.com/authorize?redirect_uri=http%3A%2F%2F127.0.0.1%3A60137%2Foauth%2Fcallback"
        assert watcher._extract_callback_host(url) == "127.0.0.1:60137"

    def test_extracts_host_without_port(self):
        url = "https://oidc.example.com/authorize?redirect_uri=http%3A%2F%2Flocalhost%2Fcallback"
        assert watcher._extract_callback_host(url) == "localhost"

    def test_returns_default_on_missing_redirect_uri(self):
        url = "https://oidc.example.com/authorize?client_id=abc"
        assert watcher._extract_callback_host(url) == "127.0.0.1"

    def test_returns_default_on_invalid_url(self):
        assert watcher._extract_callback_host("not-a-url") == "127.0.0.1"


@pytest.mark.unit
class TestLaunchWebview:

    def test_returns_none_when_binary_missing(self, tmp_path):
        with patch.object(watcher, 'WEBVIEW_APP', tmp_path / "nonexistent"):
            result = watcher._launch_webview("https://example.com", "127.0.0.1:9999")
            assert result is None

    def test_launches_webview_when_binary_exists(self, tmp_path):
        # Simulate .app bundle structure: tmp/SSOLogin.app/Contents/MacOS/sso-webview
        app_dir = tmp_path / "SSOLogin.app" / "Contents" / "MacOS"
        app_dir.mkdir(parents=True)
        fake_bin = app_dir / "sso-webview"
        fake_bin.write_text("#!/bin/sh\n")
        fake_bin.chmod(0o755)
        mock_popen = MagicMock()
        with patch.object(watcher, 'WEBVIEW_APP', fake_bin), \
             patch.object(watcher.subprocess, 'Popen', return_value=mock_popen) as mock_call:
            result = watcher._launch_webview("https://example.com", "127.0.0.1:9999")
            assert result is mock_popen
            mock_call.assert_called_once()
            args = mock_call.call_args[0][0]
            assert args[0] == "open"
            assert args[1] == "-a"
            assert "SSOLogin.app" in args[2]
            assert "https://example.com" in args
            assert "127.0.0.1:9999" in args

    def test_returns_none_on_popen_failure(self, tmp_path):
        fake_bin = tmp_path / "sso-webview"
        fake_bin.write_text("#!/bin/sh\n")
        fake_bin.chmod(0o755)
        with patch.object(watcher, 'WEBVIEW_APP', fake_bin), \
             patch.object(watcher.subprocess, 'Popen', side_effect=OSError("exec failed")):
            result = watcher._launch_webview("https://example.com", "127.0.0.1:9999")
            assert result is None


@pytest.mark.unit
class TestRunAwsSsoLogin:

    def _mock_aws_proc(self, url="https://oidc.example.com/authorize?redirect_uri=http%3A%2F%2F127.0.0.1%3A9999%2Fcallback",
                        returncode=0):
        """Create a mock Popen for aws sso login --no-browser."""
        proc = MagicMock()
        proc.stdout = iter([
            "Browser will not be automatically opened.\n",
            "Please visit the following URL:\n",
            "\n",
            f"{url}\n",
        ])
        proc.returncode = returncode
        # poll() returns None first (still running), then returncode
        proc.poll = MagicMock(side_effect=[None, returncode])
        # After poll() returns non-None, stdout.read() is called
        proc._readable_stdout = MagicMock()
        proc._readable_stdout.read.return_value = "Successfully logged in\n"
        original_poll = proc.poll.side_effect
        _call_count = [0]
        def poll_side_effect():
            _call_count[0] += 1
            if _call_count[0] <= 1:
                return None
            # Replace stdout with readable mock before returning
            proc.stdout = proc._readable_stdout
            return returncode
        proc.poll = MagicMock(side_effect=poll_side_effect)
        return proc

    def test_success_with_webview(self, tmp_path):
        proc = self._mock_aws_proc(returncode=0)
        webview = MagicMock()
        with patch.object(watcher.subprocess, 'Popen', return_value=proc), \
             patch.object(watcher, '_launch_webview', return_value=webview), \
             patch.object(watcher, '_is_webview_running', return_value=True), \
             patch.object(watcher, '_kill_webview') as mock_kill:
            rc = watcher.run_aws_sso_login("test-profile")
            assert rc == 0
            mock_kill.assert_called_once()

    def test_success_falls_back_to_browser(self, tmp_path):
        proc = self._mock_aws_proc(returncode=0)
        with patch.object(watcher.subprocess, 'Popen', return_value=proc), \
             patch.object(watcher.subprocess, 'run') as mock_run, \
             patch.object(watcher, '_launch_webview', return_value=None), \
             patch.object(watcher, '_is_webview_running', return_value=True):
            rc = watcher.run_aws_sso_login("test-profile")
            assert rc == 0
            # Verify browser fallback was called with open
            open_calls = [c for c in mock_run.call_args_list if c[0][0][0] == "open"]
            assert len(open_calls) == 1

    def test_returns_minus_1_when_no_url(self):
        proc = MagicMock()
        proc.stdout = iter(["Some error\n"])
        proc.kill = MagicMock()
        with patch.object(watcher.subprocess, 'Popen', return_value=proc):
            rc = watcher.run_aws_sso_login("test-profile")
            assert rc == -1
            proc.kill.assert_called_once()

    def test_returns_minus_1_on_timeout(self):
        proc = MagicMock()
        proc.stdout = iter([
            "Browser will not be automatically opened.\n",
            "\n",
            "https://oidc.example.com/authorize?redirect_uri=http%3A%2F%2F127.0.0.1%3A9999%2Fcallback\n",
        ])
        proc.poll = MagicMock(return_value=None)  # never finishes
        proc.kill = MagicMock()
        with patch.object(watcher.subprocess, 'Popen', return_value=proc), \
             patch.object(watcher.subprocess, 'run'), \
             patch.object(watcher, '_launch_webview', return_value=None), \
             patch.object(watcher, 'SSO_LOGIN_TIMEOUT', 0):  # immediate timeout
            rc = watcher.run_aws_sso_login("test-profile")
            assert rc == -1
            proc.kill.assert_called_once()

    def test_login_failure_returns_nonzero(self):
        proc = self._mock_aws_proc(returncode=1)
        with patch.object(watcher.subprocess, 'Popen', return_value=proc), \
             patch.object(watcher.subprocess, 'run'), \
             patch.object(watcher, '_launch_webview', return_value=None):
            rc = watcher.run_aws_sso_login("test-profile")
            assert rc == 1

    def test_webview_killed_on_timeout(self):
        proc = MagicMock()
        proc.stdout = iter([
            "Browser will not be automatically opened.\n",
            "\n",
            "https://oidc.example.com/authorize?redirect_uri=http%3A%2F%2F127.0.0.1%3A9999%2Fcallback\n",
        ])
        proc.poll = MagicMock(return_value=None)  # never finishes
        proc.kill = MagicMock()
        webview = MagicMock()
        with patch.object(watcher.subprocess, 'Popen', return_value=proc), \
             patch.object(watcher, '_launch_webview', return_value=webview), \
             patch.object(watcher, '_is_webview_running', return_value=True), \
             patch.object(watcher, '_kill_webview') as mock_kill, \
             patch.object(watcher, 'SSO_LOGIN_TIMEOUT', 0):  # immediate timeout
            watcher.run_aws_sso_login("test-profile")
            mock_kill.assert_called_once()

    def test_webview_closed_aws_finishes(self):
        """When webview closes and aws finishes within grace period, return its exit code."""
        proc = MagicMock()
        proc.stdout = iter([
            "Browser will not be automatically opened.\n",
            "\n",
            "https://oidc.example.com/authorize?redirect_uri=http%3A%2F%2F127.0.0.1%3A9999%2Fcallback\n",
        ])
        proc.poll = MagicMock(return_value=None)  # aws still running when polled
        proc.wait = MagicMock()  # but finishes during grace period
        proc.returncode = 0
        readable_stdout = MagicMock()
        readable_stdout.read.return_value = ""
        proc.stdout = iter([
            "Browser will not be automatically opened.\n",
            "\n",
            "https://oidc.example.com/authorize?redirect_uri=http%3A%2F%2F127.0.0.1%3A9999%2Fcallback\n",
        ])
        def wait_side_effect(**kwargs):
            proc.stdout = readable_stdout
        proc.wait.side_effect = wait_side_effect
        webview = MagicMock()
        with patch.object(watcher.subprocess, 'Popen', return_value=proc), \
             patch.object(watcher, '_launch_webview', return_value=webview), \
             patch.object(watcher, '_is_webview_running', return_value=False), \
             patch.object(watcher, '_kill_webview'):
            rc = watcher.run_aws_sso_login("test-profile")
            assert rc == 0
            proc.wait.assert_called_once()

    def test_webview_closed_aws_timeout_aborts(self):
        """When webview closes and aws doesn't finish in grace period, kill it."""
        proc = MagicMock()
        proc.stdout = iter([
            "Browser will not be automatically opened.\n",
            "\n",
            "https://oidc.example.com/authorize?redirect_uri=http%3A%2F%2F127.0.0.1%3A9999%2Fcallback\n",
        ])
        proc.poll = MagicMock(return_value=None)
        # First wait(timeout=10) raises, second wait() after kill succeeds
        proc.wait = MagicMock(side_effect=[subprocess.TimeoutExpired("aws", 10), None])
        proc.kill = MagicMock()
        webview = MagicMock()
        with patch.object(watcher.subprocess, 'Popen', return_value=proc), \
             patch.object(watcher, '_launch_webview', return_value=webview), \
             patch.object(watcher, '_is_webview_running', return_value=False), \
             patch.object(watcher, '_kill_webview'):
            rc = watcher.run_aws_sso_login("test-profile")
            assert rc == -1
            proc.kill.assert_called_once()

    def test_uses_no_browser_flag(self):
        proc = MagicMock()
        proc.stdout = iter(["error\n"])
        proc.kill = MagicMock()
        with patch.object(watcher.subprocess, 'Popen', return_value=proc) as mock_popen:
            watcher.run_aws_sso_login("myprofile")
            cmd = mock_popen.call_args[0][0]
            assert "--no-browser" in cmd
            assert "--profile" in cmd
            assert "myprofile" in cmd


# ---------------------------------------------------------------------------
# _get_sso_session_config
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestGetSsoSessionConfig:

    def test_reads_profile_with_sso_session(self, tmp_path):
        config_file = tmp_path / "config"
        config_file.write_text(
            "[profile bazel-proxy]\n"
            "sso_session = my-session\n"
            "sso_account_id = 123456\n"
            "\n"
            "[sso-session my-session]\n"
            "sso_start_url = https://my-org.awsapps.com/start\n"
            "sso_region = us-west-2\n"
            "sso_registration_scopes = sso:account:access\n"
        )
        with patch.object(watcher, 'AWS_CONFIG_FILE', config_file):
            result = watcher._get_sso_session_config("bazel-proxy")
        assert result == {
            "session_name": "my-session",
            "start_url": "https://my-org.awsapps.com/start",
            "region": "us-west-2",
        }

    def test_default_profile(self, tmp_path):
        config_file = tmp_path / "config"
        config_file.write_text(
            "[default]\n"
            "sso_session = default-session\n"
            "\n"
            "[sso-session default-session]\n"
            "sso_start_url = https://default.awsapps.com/start\n"
            "sso_region = eu-west-1\n"
        )
        with patch.object(watcher, 'AWS_CONFIG_FILE', config_file):
            result = watcher._get_sso_session_config("default")
        assert result["start_url"] == "https://default.awsapps.com/start"
        assert result["region"] == "eu-west-1"

    def test_profile_not_found(self, tmp_path):
        config_file = tmp_path / "config"
        config_file.write_text("[default]\nregion = us-east-1\n")
        with patch.object(watcher, 'AWS_CONFIG_FILE', config_file):
            assert watcher._get_sso_session_config("nonexistent") == {}

    def test_no_sso_session_key(self, tmp_path):
        config_file = tmp_path / "config"
        config_file.write_text("[profile myprof]\nregion = us-east-1\n")
        with patch.object(watcher, 'AWS_CONFIG_FILE', config_file):
            assert watcher._get_sso_session_config("myprof") == {}

    def test_sso_session_section_missing(self, tmp_path):
        config_file = tmp_path / "config"
        config_file.write_text(
            "[profile myprof]\n"
            "sso_session = ghost-session\n"
        )
        with patch.object(watcher, 'AWS_CONFIG_FILE', config_file):
            assert watcher._get_sso_session_config("myprof") == {}

    def test_config_file_missing(self, tmp_path):
        config_file = tmp_path / "nonexistent"
        with patch.object(watcher, 'AWS_CONFIG_FILE', config_file):
            assert watcher._get_sso_session_config("default") == {}

    def test_config_file_corrupt(self, tmp_path):
        config_file = tmp_path / "config"
        config_file.write_text("{{not valid ini}}")
        with patch.object(watcher, 'AWS_CONFIG_FILE', config_file):
            # configparser may parse this without error but find no sections
            result = watcher._get_sso_session_config("default")
            assert result == {}


# ---------------------------------------------------------------------------
# _find_sso_cache_file
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestFindSsoCacheFile:

    def _write_cache(self, cache_dir, filename, data):
        path = cache_dir / filename
        path.write_text(json.dumps(data))
        return path

    def test_finds_matching_cache(self, tmp_path):
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        self._write_cache(cache_dir, "abc123.json", {
            "startUrl": "https://my-org.awsapps.com/start",
            "accessToken": "old-token",
            "refreshToken": "refresh-tok",
            "clientId": "cid",
            "clientSecret": "csecret",
            "registrationExpiresAt": "2099-01-01T00:00:00Z",
        })
        with patch.object(watcher, 'SSO_CACHE_DIR', cache_dir):
            result = watcher._find_sso_cache_file("https://my-org.awsapps.com/start")
        assert result is not None
        assert result["refreshToken"] == "refresh-tok"
        assert result["_cache_path"] == str(cache_dir / "abc123.json")

    def test_no_matching_start_url(self, tmp_path):
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        self._write_cache(cache_dir, "abc123.json", {
            "startUrl": "https://other-org.awsapps.com/start",
            "accessToken": "tok",
            "refreshToken": "ref",
            "clientId": "cid",
            "clientSecret": "csecret",
        })
        with patch.object(watcher, 'SSO_CACHE_DIR', cache_dir):
            assert watcher._find_sso_cache_file("https://my-org.awsapps.com/start") is None

    def test_missing_refresh_token(self, tmp_path):
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        self._write_cache(cache_dir, "abc123.json", {
            "startUrl": "https://my-org.awsapps.com/start",
            "accessToken": "tok",
            # no refreshToken
            "clientId": "cid",
            "clientSecret": "csecret",
        })
        with patch.object(watcher, 'SSO_CACHE_DIR', cache_dir):
            assert watcher._find_sso_cache_file("https://my-org.awsapps.com/start") is None

    def test_missing_client_id(self, tmp_path):
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        self._write_cache(cache_dir, "abc123.json", {
            "startUrl": "https://my-org.awsapps.com/start",
            "accessToken": "tok",
            "refreshToken": "ref",
            # no clientId
            "clientSecret": "csecret",
        })
        with patch.object(watcher, 'SSO_CACHE_DIR', cache_dir):
            assert watcher._find_sso_cache_file("https://my-org.awsapps.com/start") is None

    def test_empty_cache_dir(self, tmp_path):
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        with patch.object(watcher, 'SSO_CACHE_DIR', cache_dir):
            assert watcher._find_sso_cache_file("https://any.url") is None

    def test_corrupt_json_file_skipped(self, tmp_path):
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        (cache_dir / "bad.json").write_text("not json")
        self._write_cache(cache_dir, "good.json", {
            "startUrl": "https://my-org.awsapps.com/start",
            "accessToken": "tok",
            "refreshToken": "ref",
            "clientId": "cid",
            "clientSecret": "csecret",
        })
        with patch.object(watcher, 'SSO_CACHE_DIR', cache_dir):
            result = watcher._find_sso_cache_file("https://my-org.awsapps.com/start")
        assert result is not None

    def test_cache_dir_missing(self, tmp_path):
        cache_dir = tmp_path / "nonexistent"
        with patch.object(watcher, 'SSO_CACHE_DIR', cache_dir):
            assert watcher._find_sso_cache_file("https://any.url") is None


# ---------------------------------------------------------------------------
# try_silent_refresh
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestTrySilentRefresh:

    def _make_cache_file(self, tmp_path):
        """Create a valid cache file and AWS config, return (config_path, cache_dir, cache_path)."""
        config_file = tmp_path / "config"
        config_file.write_text(
            "[profile test-prof]\n"
            "sso_session = test-session\n"
            "\n"
            "[sso-session test-session]\n"
            "sso_start_url = https://test.awsapps.com/start\n"
            "sso_region = us-west-2\n"
        )
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        cache_path = cache_dir / "token.json"
        cache_path.write_text(json.dumps({
            "startUrl": "https://test.awsapps.com/start",
            "accessToken": "old-access",
            "refreshToken": "old-refresh",
            "clientId": "cid-123",
            "clientSecret": "csecret-456",
            "registrationExpiresAt": "2099-01-01T00:00:00Z",
            "expiresAt": "2025-01-01T00:00:00Z",
        }))
        return config_file, cache_dir, cache_path

    def test_success(self, tmp_path):
        config_file, cache_dir, cache_path = self._make_cache_file(tmp_path)
        api_response = json.dumps({
            "accessToken": "new-access-token",
            "expiresIn": 28800,
            "refreshToken": "new-refresh-token",
        })
        proc = _mock_proc(api_response, 0)
        with patch.object(watcher, 'AWS_CONFIG_FILE', config_file), \
             patch.object(watcher, 'SSO_CACHE_DIR', cache_dir), \
             patch.object(watcher.subprocess, 'run', return_value=proc):
            result = watcher.try_silent_refresh("test-prof")
        assert result is True
        # Verify cache file was updated
        updated = json.loads(cache_path.read_text())
        assert updated["accessToken"] == "new-access-token"
        assert updated["refreshToken"] == "new-refresh-token"
        assert "expiresAt" in updated

    def test_success_no_new_refresh_token(self, tmp_path):
        """API may not return a new refresh token; old one should be preserved."""
        config_file, cache_dir, cache_path = self._make_cache_file(tmp_path)
        api_response = json.dumps({
            "accessToken": "new-access",
            "expiresIn": 3600,
        })
        proc = _mock_proc(api_response, 0)
        with patch.object(watcher, 'AWS_CONFIG_FILE', config_file), \
             patch.object(watcher, 'SSO_CACHE_DIR', cache_dir), \
             patch.object(watcher.subprocess, 'run', return_value=proc):
            result = watcher.try_silent_refresh("test-prof")
        assert result is True
        updated = json.loads(cache_path.read_text())
        assert updated["accessToken"] == "new-access"
        assert updated["refreshToken"] == "old-refresh"  # preserved

    def test_no_sso_config(self, tmp_path):
        config_file = tmp_path / "config"
        config_file.write_text("[default]\nregion = us-east-1\n")
        with patch.object(watcher, 'AWS_CONFIG_FILE', config_file):
            assert watcher.try_silent_refresh("no-such-profile") is False

    def test_no_cache_file(self, tmp_path):
        config_file = tmp_path / "config"
        config_file.write_text(
            "[profile myprof]\nsso_session = s\n\n"
            "[sso-session s]\nsso_start_url = https://x.com\nsso_region = us-east-1\n"
        )
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        with patch.object(watcher, 'AWS_CONFIG_FILE', config_file), \
             patch.object(watcher, 'SSO_CACHE_DIR', cache_dir):
            assert watcher.try_silent_refresh("myprof") is False

    def test_expired_registration(self, tmp_path):
        config_file, cache_dir, cache_path = self._make_cache_file(tmp_path)
        # Overwrite cache with expired registration
        data = json.loads(cache_path.read_text())
        data["registrationExpiresAt"] = "2020-01-01T00:00:00Z"
        cache_path.write_text(json.dumps(data))
        with patch.object(watcher, 'AWS_CONFIG_FILE', config_file), \
             patch.object(watcher, 'SSO_CACHE_DIR', cache_dir):
            assert watcher.try_silent_refresh("test-prof") is False

    def test_api_failure(self, tmp_path):
        config_file, cache_dir, _ = self._make_cache_file(tmp_path)
        proc = _mock_proc("InvalidGrantException", 254)
        with patch.object(watcher, 'AWS_CONFIG_FILE', config_file), \
             patch.object(watcher, 'SSO_CACHE_DIR', cache_dir), \
             patch.object(watcher.subprocess, 'run', return_value=proc):
            assert watcher.try_silent_refresh("test-prof") is False

    def test_invalid_json_response(self, tmp_path):
        config_file, cache_dir, _ = self._make_cache_file(tmp_path)
        proc = _mock_proc("not json at all", 0)
        with patch.object(watcher, 'AWS_CONFIG_FILE', config_file), \
             patch.object(watcher, 'SSO_CACHE_DIR', cache_dir), \
             patch.object(watcher.subprocess, 'run', return_value=proc):
            assert watcher.try_silent_refresh("test-prof") is False

    def test_no_access_token_in_response(self, tmp_path):
        config_file, cache_dir, _ = self._make_cache_file(tmp_path)
        proc = _mock_proc(json.dumps({"expiresIn": 3600}), 0)
        with patch.object(watcher, 'AWS_CONFIG_FILE', config_file), \
             patch.object(watcher, 'SSO_CACHE_DIR', cache_dir), \
             patch.object(watcher.subprocess, 'run', return_value=proc):
            assert watcher.try_silent_refresh("test-prof") is False

    def test_subprocess_timeout(self, tmp_path):
        config_file, cache_dir, _ = self._make_cache_file(tmp_path)
        with patch.object(watcher, 'AWS_CONFIG_FILE', config_file), \
             patch.object(watcher, 'SSO_CACHE_DIR', cache_dir), \
             patch.object(watcher.subprocess, 'run',
                          side_effect=subprocess.TimeoutExpired("aws", 30)):
            assert watcher.try_silent_refresh("test-prof") is False

    def test_aws_command_not_found(self, tmp_path):
        config_file, cache_dir, _ = self._make_cache_file(tmp_path)
        with patch.object(watcher, 'AWS_CONFIG_FILE', config_file), \
             patch.object(watcher, 'SSO_CACHE_DIR', cache_dir), \
             patch.object(watcher.subprocess, 'run',
                          side_effect=FileNotFoundError):
            assert watcher.try_silent_refresh("test-prof") is False

    def test_calls_correct_command(self, tmp_path):
        config_file, cache_dir, _ = self._make_cache_file(tmp_path)
        proc = _mock_proc(json.dumps({"accessToken": "tok", "expiresIn": 100}), 0)
        with patch.object(watcher, 'AWS_CONFIG_FILE', config_file), \
             patch.object(watcher, 'SSO_CACHE_DIR', cache_dir), \
             patch.object(watcher.subprocess, 'run', return_value=proc) as mock_run:
            watcher.try_silent_refresh("test-prof")
        cmd = mock_run.call_args[0][0]
        assert cmd[0:3] == ["aws", "sso-oidc", "create-token"]
        assert "--grant-type" in cmd
        assert "refresh_token" in cmd
        assert "--client-id" in cmd
        assert "cid-123" in cmd
        assert "--client-secret" in cmd
        assert "csecret-456" in cmd
        assert "--refresh-token" in cmd
        assert "old-refresh" in cmd
        assert "--region" in cmd
        assert "us-west-2" in cmd
        assert "--no-sign-request" in cmd

    def test_cache_write_failure(self, tmp_path):
        """If writing back to cache fails, return False."""
        config_file, cache_dir, cache_path = self._make_cache_file(tmp_path)
        api_response = json.dumps({"accessToken": "new", "expiresIn": 100})
        proc = _mock_proc(api_response, 0)
        with patch.object(watcher, 'AWS_CONFIG_FILE', config_file), \
             patch.object(watcher, 'SSO_CACHE_DIR', cache_dir), \
             patch.object(watcher.subprocess, 'run', return_value=proc), \
             patch('builtins.open', side_effect=[
                 # First open (read cache) succeeds
                 open(str(cache_path)),
                 # Second open (write cache) fails
                 PermissionError("denied"),
             ]):
            assert watcher.try_silent_refresh("test-prof") is False


# ---------------------------------------------------------------------------
# Silent mode in handle_login
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSilentModeHandleLogin:

    def test_silent_mode_success(self):
        """Silent mode: silent refresh works → success, no browser/dialog."""
        with patch.object(watcher, 'read_mode', return_value='silent'), \
             patch.object(watcher, 'try_silent_refresh', return_value=True), \
             patch.object(watcher, '_run_notify_login') as mock_notify_login, \
             patch.object(watcher, 'run_aws_sso_login') as mock_login:
            assert watcher.handle_login("default") == "success"
            mock_notify_login.assert_not_called()
            mock_login.assert_not_called()

    def test_silent_mode_failure(self):
        """Silent mode: silent refresh fails → 'failed', no browser fallback."""
        with patch.object(watcher, 'read_mode', return_value='silent'), \
             patch.object(watcher, 'try_silent_refresh', return_value=False), \
             patch.object(watcher, '_run_notify_login') as mock_notify_login, \
             patch.object(watcher, 'run_aws_sso_login') as mock_login:
            assert watcher.handle_login("default") == "failed"
            mock_notify_login.assert_not_called()
            mock_login.assert_not_called()

    def test_notify_tries_silent_first_success(self):
        """Notify mode: silent refresh succeeds → no dialog shown."""
        with patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'try_silent_refresh', return_value=True), \
             patch.object(watcher, '_run_notify_login') as mock_notify_login, \
             patch.object(watcher, 'run_aws_sso_login') as mock_login:
            assert watcher.handle_login("default") == "success"
            mock_notify_login.assert_not_called()
            mock_login.assert_not_called()

    def test_auto_tries_silent_first_success(self):
        """Auto mode: silent refresh succeeds → no browser opened."""
        with patch.object(watcher, 'read_mode', return_value='auto'), \
             patch.object(watcher, 'try_silent_refresh', return_value=True), \
             patch.object(watcher, '_run_notify_login') as mock_notify_login, \
             patch.object(watcher, 'run_aws_sso_login') as mock_login:
            assert watcher.handle_login("default") == "success"
            mock_notify_login.assert_not_called()
            mock_login.assert_not_called()

    def test_notify_falls_back_after_silent_failure(self):
        """Notify mode: silent fails → all-in-one webview handles login."""
        with patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'try_silent_refresh', return_value=False), \
             patch.object(watcher, '_run_notify_login', return_value="success") as mock_notify_login:
            assert watcher.handle_login("default") == "success"
            mock_notify_login.assert_called_once()

    def test_auto_falls_back_after_silent_failure(self):
        """Auto mode: silent fails → browser login directly."""
        with patch.object(watcher, 'read_mode', return_value='auto'), \
             patch.object(watcher, 'try_silent_refresh', return_value=False), \
             patch.object(watcher, 'run_aws_sso_login', return_value=0) as mock_login:
            assert watcher.handle_login("default") == "success"
            mock_login.assert_called_once()

    def test_read_mode_silent(self, watcher_state):
        """Mode file with 'silent' is valid."""
        watcher_state["mode_file"].write_text("silent\n")
        assert watcher.read_mode() == "silent"

    def test_write_mode_silent(self, watcher_state):
        watcher.write_mode("silent")
        assert watcher_state["mode_file"].read_text().strip() == "silent"


# ---------------------------------------------------------------------------
# Silent mode main loop integration
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSilentModeMainLoop:

    def test_silent_success_clears_signal(self, watcher_state):
        """Silent mode: refresh works → signal cleared, cooldown written."""
        write_signal(watcher_state["signal_file"], profile="dev")
        with patch.object(watcher, 'read_mode', return_value='silent'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'COOLDOWN_SECONDS', 0), \
             patch.object(watcher, 'try_silent_refresh', return_value=True), \
             patch('time.sleep', side_effect=_stop_after(2)):
            watcher.main()
        assert not watcher_state["signal_file"].exists()
        assert watcher_state["last_run"].exists()

    def test_silent_failure_writes_30s_snooze(self, watcher_state):
        """Silent mode: refresh fails → 30s snooze, signal kept."""
        write_signal(watcher_state["signal_file"])
        with patch.object(watcher, 'read_mode', return_value='silent'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'COOLDOWN_SECONDS', 0), \
             patch.object(watcher, 'try_silent_refresh', return_value=False), \
             patch('time.sleep', side_effect=_stop_after(2)):
            watcher.main()
        assert watcher_state["signal_file"].exists()
        data = json.loads(watcher_state["signal_file"].read_text())
        assert data["nextAttemptAfter"] <= time.time() + 31
        assert data["nextAttemptAfter"] > time.time() + 20

    def test_silent_lock_released(self, watcher_state):
        """Lock released after silent mode processing."""
        write_signal(watcher_state["signal_file"])
        with patch.object(watcher, 'read_mode', return_value='silent'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'COOLDOWN_SECONDS', 0), \
             patch.object(watcher, 'try_silent_refresh', return_value=True), \
             patch('time.sleep', side_effect=_stop_after(2)):
            watcher.main()
        assert not watcher_state["lock_dir"].exists()


# ---------------------------------------------------------------------------
# check_token_near_expiry
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestCheckTokenNearExpiry:

    def _make_cache(self, tmp_path, expires_at):
        config_file = tmp_path / "config"
        config_file.write_text(
            "[profile test]\n"
            "sso_session = s\n\n"
            "[sso-session s]\n"
            "sso_start_url = https://test.awsapps.com/start\n"
            "sso_region = us-east-1\n"
        )
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        (cache_dir / "tok.json").write_text(json.dumps({
            "startUrl": "https://test.awsapps.com/start",
            "accessToken": "tok",
            "refreshToken": "ref",
            "clientId": "cid",
            "clientSecret": "cs",
            "expiresAt": expires_at,
        }))
        return config_file, cache_dir

    def test_token_near_expiry(self, tmp_path):
        """Token expiring in 10min with 30min threshold → True."""
        from datetime import datetime, timezone, timedelta
        expires = (datetime.now(timezone.utc) + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        config_file, cache_dir = self._make_cache(tmp_path, expires)
        with patch.object(watcher, 'AWS_CONFIG_FILE', config_file), \
             patch.object(watcher, 'SSO_CACHE_DIR', cache_dir):
            assert watcher.check_token_near_expiry("test", 30) is True

    def test_token_not_near_expiry(self, tmp_path):
        """Token expiring in 4h with 30min threshold → False."""
        from datetime import datetime, timezone, timedelta
        expires = (datetime.now(timezone.utc) + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
        config_file, cache_dir = self._make_cache(tmp_path, expires)
        with patch.object(watcher, 'AWS_CONFIG_FILE', config_file), \
             patch.object(watcher, 'SSO_CACHE_DIR', cache_dir):
            assert watcher.check_token_near_expiry("test", 30) is False

    def test_token_already_expired(self, tmp_path):
        """Token already expired → True."""
        from datetime import datetime, timezone, timedelta
        expires = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        config_file, cache_dir = self._make_cache(tmp_path, expires)
        with patch.object(watcher, 'AWS_CONFIG_FILE', config_file), \
             patch.object(watcher, 'SSO_CACHE_DIR', cache_dir):
            assert watcher.check_token_near_expiry("test", 30) is True

    def test_no_config(self, tmp_path):
        config_file = tmp_path / "config"
        config_file.write_text("[default]\n")
        with patch.object(watcher, 'AWS_CONFIG_FILE', config_file):
            assert watcher.check_token_near_expiry("noexist", 30) is False

    def test_no_cache_file(self, tmp_path):
        config_file = tmp_path / "config"
        config_file.write_text(
            "[profile p]\nsso_session = s\n\n"
            "[sso-session s]\nsso_start_url = https://x.com\nsso_region = us-east-1\n"
        )
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        with patch.object(watcher, 'AWS_CONFIG_FILE', config_file), \
             patch.object(watcher, 'SSO_CACHE_DIR', cache_dir):
            assert watcher.check_token_near_expiry("p", 30) is False

    def test_no_expires_at_field(self, tmp_path):
        config_file = tmp_path / "config"
        config_file.write_text(
            "[profile p]\nsso_session = s\n\n"
            "[sso-session s]\nsso_start_url = https://x.com\nsso_region = us-east-1\n"
        )
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        (cache_dir / "tok.json").write_text(json.dumps({
            "startUrl": "https://x.com",
            "accessToken": "tok",
            "refreshToken": "ref",
            "clientId": "cid",
            "clientSecret": "cs",
            # no expiresAt
        }))
        with patch.object(watcher, 'AWS_CONFIG_FILE', config_file), \
             patch.object(watcher, 'SSO_CACHE_DIR', cache_dir):
            assert watcher.check_token_near_expiry("p", 30) is False

    def test_uses_default_threshold(self, tmp_path):
        """Uses PROACTIVE_REFRESH_MINUTES when threshold_minutes is None."""
        from datetime import datetime, timezone, timedelta
        expires = (datetime.now(timezone.utc) + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        config_file, cache_dir = self._make_cache(tmp_path, expires)
        with patch.object(watcher, 'AWS_CONFIG_FILE', config_file), \
             patch.object(watcher, 'SSO_CACHE_DIR', cache_dir), \
             patch.object(watcher, 'PROACTIVE_REFRESH_MINUTES', 30):
            assert watcher.check_token_near_expiry("test") is True


# ---------------------------------------------------------------------------
# Proactive refresh in main loop
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestProactiveRefreshMainLoop:

    def test_proactive_refresh_when_near_expiry(self, watcher_state):
        """No signal, token near expiry → proactive silent refresh fires."""
        with patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'PROACTIVE_REFRESH_MINUTES', 30), \
             patch.object(watcher, 'check_token_near_expiry', return_value=True) as mock_check, \
             patch.object(watcher, 'try_silent_refresh', return_value=True) as mock_refresh, \
             patch('time.sleep', side_effect=_stop_after(2)):
            watcher.main()
        mock_check.assert_called()
        mock_refresh.assert_called_once_with(watcher.PROFILE)

    def test_proactive_no_refresh_when_token_healthy(self, watcher_state):
        """No signal, token healthy → no refresh attempt."""
        with patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'PROACTIVE_REFRESH_MINUTES', 30), \
             patch.object(watcher, 'check_token_near_expiry', return_value=False), \
             patch.object(watcher, 'try_silent_refresh') as mock_refresh, \
             patch('time.sleep', side_effect=_stop_after(2)):
            watcher.main()
        mock_refresh.assert_not_called()

    def test_proactive_skipped_when_signal_exists(self, watcher_state):
        """Signal file present → proactive check skipped (signal path handles it)."""
        write_signal(watcher_state["signal_file"])
        with patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'COOLDOWN_SECONDS', 0), \
             patch.object(watcher, 'PROACTIVE_REFRESH_MINUTES', 30), \
             patch.object(watcher, 'check_token_near_expiry') as mock_check, \
             patch.object(watcher, '_run_notify_login', return_value="dismiss"), \
             patch('time.sleep', side_effect=_stop_after(1)):
            watcher.main()
        mock_check.assert_not_called()

    def test_proactive_skipped_in_standalone(self, watcher_state):
        """Standalone mode → no proactive check."""
        with patch.object(watcher, 'read_mode', return_value='standalone'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'PROACTIVE_REFRESH_MINUTES', 30), \
             patch.object(watcher, 'check_token_near_expiry') as mock_check, \
             patch('time.sleep', side_effect=_stop_after(2)):
            watcher.main()
        mock_check.assert_not_called()

    def test_proactive_disabled_when_zero(self, watcher_state):
        """PROACTIVE_REFRESH_MINUTES=0 disables proactive refresh."""
        with patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'PROACTIVE_REFRESH_MINUTES', 0), \
             patch.object(watcher, 'check_token_near_expiry') as mock_check, \
             patch('time.sleep', side_effect=_stop_after(2)):
            watcher.main()
        mock_check.assert_not_called()

    def test_proactive_refresh_failure_does_not_crash(self, watcher_state):
        """Proactive silent refresh fails → logs, continues loop."""
        with patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'PROACTIVE_REFRESH_MINUTES', 30), \
             patch.object(watcher, 'check_token_near_expiry', return_value=True), \
             patch.object(watcher, 'try_silent_refresh', return_value=False), \
             patch('time.sleep', side_effect=_stop_after(2)):
            # Should not raise
            watcher.main()


# ---------------------------------------------------------------------------
# _kill_webview
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestKillWebview:

    def test_osascript_quit_succeeds(self):
        """Graceful quit via osascript."""
        with patch.object(watcher.subprocess, 'run') as mock_run:
            watcher._kill_webview()
            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            assert cmd[0] == "osascript"
            assert "AWS SSO Login" in cmd[2]

    def test_falls_back_to_killall(self):
        """osascript fails → killall fallback."""
        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError("osascript failed")

        with patch.object(watcher.subprocess, 'run', side_effect=side_effect) as mock_run:
            watcher._kill_webview()
            assert mock_run.call_count == 2
            second_cmd = mock_run.call_args_list[1][0][0]
            assert second_cmd == ["killall", "sso-webview"]

    def test_both_methods_fail_silently(self):
        """Both osascript and killall fail → no exception raised."""
        with patch.object(watcher.subprocess, 'run', side_effect=OSError("nope")):
            watcher._kill_webview()  # should not raise

    def test_osascript_timeout(self):
        """osascript times out → killall fallback."""
        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise subprocess.TimeoutExpired("osascript", 3)

        with patch.object(watcher.subprocess, 'run', side_effect=side_effect) as mock_run:
            watcher._kill_webview()
            assert mock_run.call_count == 2


# ---------------------------------------------------------------------------
# _check_aws_cli
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestCheckAwsCli:

    def test_valid_version(self):
        proc = _mock_proc("aws-cli/2.33.2 Python/3.13.11 Darwin/24.0.0", 0)
        with patch.object(watcher.subprocess, 'run', return_value=proc):
            watcher._check_aws_cli()  # should not raise

    def test_low_version_warns(self, capsys):
        proc = _mock_proc("aws-cli/2.8.0 Python/3.11.0 Darwin/24.0.0", 0)
        with patch.object(watcher.subprocess, 'run', return_value=proc):
            watcher._check_aws_cli()
        output = capsys.readouterr().out
        assert "WARNING" in output
        assert "2.8.0" in output

    def test_missing_binary(self, capsys):
        with patch.object(watcher.subprocess, 'run', side_effect=FileNotFoundError):
            watcher._check_aws_cli()
        output = capsys.readouterr().out
        assert "WARNING" in output
        assert "not found" in output

    def test_generic_exception(self, capsys):
        with patch.object(watcher.subprocess, 'run', side_effect=RuntimeError("boom")):
            watcher._check_aws_cli()
        output = capsys.readouterr().out
        assert "WARNING" in output
        assert "could not check" in output

    def test_exact_minimum_version(self):
        proc = _mock_proc("aws-cli/2.9.0 Python/3.11.0 Darwin/24.0.0", 0)
        with patch.object(watcher.subprocess, 'run', return_value=proc):
            watcher._check_aws_cli()  # should not warn


# ---------------------------------------------------------------------------
# _check_credentials_valid
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestCheckCredentialsValid:

    def test_returns_true_on_success(self):
        proc = _mock_proc("", 0)
        with patch.object(watcher.subprocess, 'run', return_value=proc):
            assert watcher._check_credentials_valid("test-prof") is True

    def test_returns_false_on_failure(self):
        proc = _mock_proc("", 1)
        with patch.object(watcher.subprocess, 'run', return_value=proc):
            assert watcher._check_credentials_valid("test-prof") is False

    def test_returns_false_on_exception(self):
        with patch.object(watcher.subprocess, 'run', side_effect=Exception("boom")):
            assert watcher._check_credentials_valid("test-prof") is False

    def test_returns_false_on_timeout(self):
        with patch.object(watcher.subprocess, 'run', side_effect=subprocess.TimeoutExpired("aws", 10)):
            assert watcher._check_credentials_valid("test-prof") is False

    def test_passes_profile_to_command(self):
        proc = _mock_proc("", 0)
        with patch.object(watcher.subprocess, 'run', return_value=proc) as mock_run:
            watcher._check_credentials_valid("my-profile")
            cmd = mock_run.call_args[0][0]
            assert "--profile" in cmd
            assert "my-profile" in cmd


# ---------------------------------------------------------------------------
# _launch_notify_webview
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestLaunchNotifyWebview:

    def test_returns_none_when_binary_missing(self):
        with patch.object(watcher, 'WEBVIEW_APP', Path("/nonexistent/binary")):
            result = watcher._launch_notify_webview("test-prof", "127.0.0.1")
            assert result is None

    def test_returns_none_on_mkdtemp_failure(self):
        """mkdtemp fails → returns None, cleans up."""
        with patch.object(watcher, 'WEBVIEW_APP', Path("/fake/SSOLogin.app/Contents/MacOS/sso-webview")), \
             patch.object(Path, 'exists', return_value=True), \
             patch.object(watcher.tempfile, 'mkdtemp', side_effect=OSError("no space")):
            result = watcher._launch_notify_webview("prof", "127.0.0.1")
            assert result is None

    def test_returns_none_on_popen_failure(self):
        """Popen fails → returns None, cleans up temp dir."""
        with patch.object(watcher, 'WEBVIEW_APP', Path("/fake/SSOLogin.app/Contents/MacOS/sso-webview")), \
             patch.object(Path, 'exists', return_value=True), \
             patch.object(watcher.tempfile, 'mkdtemp', return_value="/tmp/test-fifo"), \
             patch.object(watcher.os, 'mkfifo'), \
             patch.object(watcher.subprocess, 'Popen', side_effect=OSError("exec failed")), \
             patch('shutil.rmtree') as mock_rmtree:
            result = watcher._launch_notify_webview("prof", "127.0.0.1")
            assert result is None
            mock_rmtree.assert_called_once_with("/tmp/test-fifo", ignore_errors=True)


# ---------------------------------------------------------------------------
# _run_notify_login
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestRunNotifyLogin:

    def test_fallback_to_dialog_when_webview_missing(self):
        """No webview → falls back to show_notification + run_aws_sso_login."""
        with patch.object(watcher, '_launch_notify_webview', return_value=None), \
             patch.object(watcher, 'show_notification', return_value="refresh"), \
             patch.object(watcher, 'run_aws_sso_login', return_value=0) as mock_login:
            result = watcher._run_notify_login("test-prof")
            assert result == "success"
            mock_login.assert_called_once_with("test-prof")

    def test_fallback_dialog_login_failure(self):
        """No webview, dialog refresh, login fails → 'failed'."""
        with patch.object(watcher, '_launch_notify_webview', return_value=None), \
             patch.object(watcher, 'show_notification', return_value="refresh"), \
             patch.object(watcher, 'run_aws_sso_login', return_value=1):
            assert watcher._run_notify_login("prof") == "failed"

    def test_fallback_dialog_snooze(self):
        """No webview, dialog snooze → snooze result."""
        with patch.object(watcher, '_launch_notify_webview', return_value=None), \
             patch.object(watcher, 'show_notification', return_value="snooze:900"):
            assert watcher._run_notify_login("prof") == "snooze:900"

    def test_fallback_dialog_suppress(self):
        with patch.object(watcher, '_launch_notify_webview', return_value=None), \
             patch.object(watcher, 'show_notification', return_value="suppress"):
            assert watcher._run_notify_login("prof") == "suppress"

    def test_fallback_dialog_dismiss(self):
        with patch.object(watcher, '_launch_notify_webview', return_value=None), \
             patch.object(watcher, 'show_notification', return_value="dismiss"):
            assert watcher._run_notify_login("prof") == "dismiss"

    def test_webview_snooze_action(self):
        """Webview sends snooze → returns snooze result."""
        mock_webview = MagicMock()
        mock_webview.stdout = iter(["SSO_ACTION:snooze:1800\n"])
        mock_webview.poll.return_value = 0
        mock_webview.stdin = MagicMock()
        with patch.object(watcher, '_launch_notify_webview', return_value=mock_webview), \
             patch.object(watcher, '_kill_webview'):
            assert watcher._run_notify_login("prof") == "snooze:1800"

    def test_webview_suppress_action(self):
        mock_webview = MagicMock()
        mock_webview.stdout = iter(["SSO_ACTION:suppress\n"])
        mock_webview.poll.return_value = 0
        mock_webview.stdin = MagicMock()
        with patch.object(watcher, '_launch_notify_webview', return_value=mock_webview), \
             patch.object(watcher, '_kill_webview'):
            assert watcher._run_notify_login("prof") == "suppress"

    def test_webview_window_closed_returns_dismiss(self):
        """Webview sends SSO_WINDOW_CLOSED → dismiss."""
        mock_webview = MagicMock()
        mock_webview.stdout = iter(["SSO_WINDOW_CLOSED\n"])
        mock_webview.poll.return_value = 0
        mock_webview.stdin = MagicMock()
        with patch.object(watcher, '_launch_notify_webview', return_value=mock_webview), \
             patch.object(watcher, '_kill_webview'):
            assert watcher._run_notify_login("prof") == "dismiss"

    def test_webview_no_output_returns_dismiss(self):
        """Webview closes without any output → dismiss."""
        mock_webview = MagicMock()
        mock_webview.stdout = iter([])
        mock_webview.poll.return_value = 0
        mock_webview.stdin = MagicMock()
        with patch.object(watcher, '_launch_notify_webview', return_value=mock_webview), \
             patch.object(watcher, '_kill_webview'):
            assert watcher._run_notify_login("prof") == "dismiss"

    def test_webview_refresh_extracts_url_and_succeeds(self):
        """Webview refresh → aws sso login → extract URL → send to webview → success."""
        mock_webview = MagicMock()
        mock_webview.stdout = iter(["SSO_ACTION:refresh\n"])
        mock_webview.stdin = MagicMock()
        # Webview stays alive during auth, already exited at cleanup
        mock_webview.poll.return_value = 0

        # aws sso login completes successfully on first poll
        mock_aws = MagicMock()
        mock_aws.poll.return_value = 0
        mock_aws.stdout = MagicMock()
        mock_aws.stdout.read.return_value = ""
        mock_aws.returncode = 0

        url = "https://device.sso.us-east-1.amazonaws.com/?user_code=ABCD-EFGH&redirect_uri=http://127.0.0.1:2456"

        with patch.object(watcher, '_launch_notify_webview', return_value=mock_webview), \
             patch.object(watcher.subprocess, 'Popen', return_value=mock_aws), \
             patch.object(watcher, '_extract_authorize_url', return_value=url), \
             patch.object(watcher, '_extract_callback_host', return_value="127.0.0.1:2456"), \
             patch.object(watcher, '_kill_webview'), \
             patch('time.sleep'):
            result = watcher._run_notify_login("prof")
            assert result == "success"
            # Verify URL was sent to webview stdin
            mock_webview.stdin.write.assert_called_once_with(url + "\n")

    def test_webview_refresh_no_url_returns_failed(self):
        """Webview refresh → aws sso login → no URL extracted → failed."""
        mock_webview = MagicMock()
        mock_webview.stdout = iter(["SSO_ACTION:refresh\n"])
        mock_webview.stdin = MagicMock()

        mock_aws = MagicMock()

        with patch.object(watcher, '_launch_notify_webview', return_value=mock_webview), \
             patch.object(watcher.subprocess, 'Popen', return_value=mock_aws), \
             patch.object(watcher, '_extract_authorize_url', return_value=None), \
             patch.object(watcher, '_kill_webview'):
            mock_webview.poll.return_value = None
            result = watcher._run_notify_login("prof")
            assert result == "failed"
            mock_aws.kill.assert_called()

    def test_webview_refresh_stdin_write_fails(self):
        """URL extracted but writing to webview stdin fails → failed."""
        mock_webview = MagicMock()
        mock_webview.stdout = iter(["SSO_ACTION:refresh\n"])
        mock_webview.stdin = MagicMock()
        mock_webview.stdin.write.side_effect = BrokenPipeError("pipe closed")

        mock_aws = MagicMock()
        url = "https://device.sso.us-east-1.amazonaws.com/?user_code=ABCD"

        with patch.object(watcher, '_launch_notify_webview', return_value=mock_webview), \
             patch.object(watcher.subprocess, 'Popen', return_value=mock_aws), \
             patch.object(watcher, '_extract_authorize_url', return_value=url), \
             patch.object(watcher, '_extract_callback_host', return_value="127.0.0.1"), \
             patch.object(watcher, '_kill_webview'):
            mock_webview.poll.return_value = None
            result = watcher._run_notify_login("prof")
            assert result == "failed"
            mock_aws.kill.assert_called()

    def test_webview_timeout_signal_returns_dismiss(self):
        """SSO_TIMEOUT signal from webview → dismiss."""
        mock_webview = MagicMock()
        mock_webview.stdout = iter(["SSO_TIMEOUT\n"])
        mock_webview.poll.return_value = 3
        mock_webview.stdin = MagicMock()
        with patch.object(watcher, '_launch_notify_webview', return_value=mock_webview), \
             patch.object(watcher, '_kill_webview'):
            assert watcher._run_notify_login("prof") == "dismiss"

    def test_webview_error_signal_returns_dismiss(self):
        """SSO_ERROR:detail signal → dismiss (tests startswith fix)."""
        mock_webview = MagicMock()
        mock_webview.stdout = iter(["SSO_ERROR:network failure\n"])
        mock_webview.poll.return_value = 1
        mock_webview.stdin = MagicMock()
        with patch.object(watcher, '_launch_notify_webview', return_value=mock_webview), \
             patch.object(watcher, '_kill_webview'):
            assert watcher._run_notify_login("prof") == "dismiss"

    def test_webview_unknown_action_returns_dismiss(self):
        """SSO_ACTION:unknown → dismiss (not refresh/snooze/suppress)."""
        mock_webview = MagicMock()
        mock_webview.stdout = iter(["SSO_ACTION:unknown_action\n"])
        mock_webview.poll.return_value = 0
        mock_webview.stdin = MagicMock()
        with patch.object(watcher, '_launch_notify_webview', return_value=mock_webview), \
             patch.object(watcher, '_kill_webview'):
            assert watcher._run_notify_login("prof") == "dismiss"

    def test_webview_skips_non_signal_lines(self):
        """Debug output lines before action line are skipped."""
        mock_webview = MagicMock()
        mock_webview.stdout = iter([
            "DEBUG: loading webview\n",
            "INFO: ready\n",
            "SSO_ACTION:suppress\n",
        ])
        mock_webview.poll.return_value = 0
        mock_webview.stdin = MagicMock()
        with patch.object(watcher, '_launch_notify_webview', return_value=mock_webview), \
             patch.object(watcher, '_kill_webview'):
            assert watcher._run_notify_login("prof") == "suppress"

    def test_auth_timeout_during_poll_returns_failed(self):
        """SSO_LOGIN_TIMEOUT reached while polling aws process → failed."""
        mock_webview = MagicMock()
        mock_webview.stdout = iter(["SSO_ACTION:refresh\n"])
        mock_webview.stdin = MagicMock()
        mock_webview.poll.return_value = None  # webview stays alive

        mock_aws = MagicMock()
        mock_aws.poll.return_value = None  # never finishes
        url = "https://device.sso.us-east-1.amazonaws.com/?user_code=ABCD&redirect_uri=http://127.0.0.1:2456"

        with patch.object(watcher, '_launch_notify_webview', return_value=mock_webview), \
             patch.object(watcher.subprocess, 'Popen', return_value=mock_aws), \
             patch.object(watcher, '_extract_authorize_url', return_value=url), \
             patch.object(watcher, '_extract_callback_host', return_value="127.0.0.1:2456"), \
             patch.object(watcher, '_check_credentials_valid', return_value=False), \
             patch.object(watcher, '_kill_webview'), \
             patch('time.time') as mock_time, \
             patch('time.sleep'):
            # First call: deadline = 1000 + SSO_LOGIN_TIMEOUT
            # Subsequent calls: past the deadline
            mock_time.side_effect = [1000, 1000 + watcher.SSO_LOGIN_TIMEOUT + 1,
                                     1000 + watcher.SSO_LOGIN_TIMEOUT + 2]
            result = watcher._run_notify_login("prof")
            assert result == "failed"
            mock_aws.kill.assert_called()

    def test_credential_check_during_poll_returns_success(self):
        """_check_credentials_valid finds valid creds during poll → success."""
        mock_webview = MagicMock()
        mock_webview.stdout = iter(["SSO_ACTION:refresh\n"])
        mock_webview.stdin = MagicMock()
        mock_webview.poll.return_value = None

        mock_aws = MagicMock()
        mock_aws.poll.return_value = None  # aws hangs
        url = "https://device.sso.us-east-1.amazonaws.com/?user_code=ABCD&redirect_uri=http://127.0.0.1:2456"

        call_count = [0]

        def fake_time():
            call_count[0] += 1
            # Return values that keep us within deadline but trigger cred check
            return 1000 + call_count[0]

        with patch.object(watcher, '_launch_notify_webview', return_value=mock_webview), \
             patch.object(watcher.subprocess, 'Popen', return_value=mock_aws), \
             patch.object(watcher, '_extract_authorize_url', return_value=url), \
             patch.object(watcher, '_extract_callback_host', return_value="127.0.0.1:2456"), \
             patch.object(watcher, '_check_credentials_valid', return_value=True), \
             patch.object(watcher, '_kill_webview'), \
             patch('time.time', side_effect=fake_time), \
             patch('time.sleep'):
            result = watcher._run_notify_login("prof")
            assert result == "success"
            mock_aws.kill.assert_called()

    def test_webview_exits_during_auth_aws_finishes(self):
        """Webview exits during auth, aws finishes within grace period → success."""
        mock_webview = MagicMock()
        mock_webview.stdout = iter(["SSO_ACTION:refresh\n"])
        mock_webview.stdin = MagicMock()

        mock_aws = MagicMock()
        mock_aws.returncode = 0
        mock_aws.stdout = MagicMock()
        mock_aws.stdout.read.return_value = ""
        url = "https://device.sso.us-east-1.amazonaws.com/?user_code=ABCD&redirect_uri=http://127.0.0.1:2456"

        # First poll: aws not done yet (return None). Second: webview exited.
        poll_calls = [0]
        def aws_poll():
            poll_calls[0] += 1
            if poll_calls[0] <= 1:
                return None
            return 0

        def webview_poll():
            if poll_calls[0] >= 1:
                return 0  # webview exits after first aws poll
            return None

        mock_aws.poll.side_effect = aws_poll
        mock_aws.wait.return_value = 0
        mock_webview.poll.side_effect = webview_poll

        with patch.object(watcher, '_launch_notify_webview', return_value=mock_webview), \
             patch.object(watcher.subprocess, 'Popen', return_value=mock_aws), \
             patch.object(watcher, '_extract_authorize_url', return_value=url), \
             patch.object(watcher, '_extract_callback_host', return_value="127.0.0.1:2456"), \
             patch.object(watcher, '_kill_webview'), \
             patch('time.time', return_value=1000), \
             patch('time.sleep'):
            result = watcher._run_notify_login("prof")
            assert result == "success"

    def test_webview_exits_during_auth_aws_hangs(self):
        """Webview exits during auth, aws doesn't finish in 10s → dismiss."""
        mock_webview = MagicMock()
        mock_webview.stdout = iter(["SSO_ACTION:refresh\n"])
        mock_webview.stdin = MagicMock()

        mock_aws = MagicMock()
        mock_aws.stdout = MagicMock()
        mock_aws.stdout.read.return_value = ""
        url = "https://device.sso.us-east-1.amazonaws.com/?user_code=ABCD&redirect_uri=http://127.0.0.1:2456"

        # aws poll always None, webview exits after first loop iteration
        mock_aws.poll.return_value = None
        # wait(timeout=10) raises, subsequent wait() after kill succeed
        mock_aws.wait.side_effect = [subprocess.TimeoutExpired("aws", 10), None, None]
        # poll calls: loop check(None), webview check(0=exited), finally check(0)
        mock_webview.poll.side_effect = [None, 0, 0]

        with patch.object(watcher, '_launch_notify_webview', return_value=mock_webview), \
             patch.object(watcher.subprocess, 'Popen', return_value=mock_aws), \
             patch.object(watcher, '_extract_authorize_url', return_value=url), \
             patch.object(watcher, '_extract_callback_host', return_value="127.0.0.1:2456"), \
             patch.object(watcher, '_kill_webview'), \
             patch('time.time', return_value=1000), \
             patch('time.sleep'):
            result = watcher._run_notify_login("prof")
            assert result == "dismiss"
            mock_aws.kill.assert_called()

    def test_cleanup_when_aws_proc_is_none(self):
        """Exception before aws_proc assigned → finally block safe."""
        mock_webview = MagicMock()
        mock_webview.stdout = iter(["SSO_ACTION:refresh\n"])
        mock_webview.stdin = MagicMock()
        mock_webview.poll.return_value = None

        with patch.object(watcher, '_launch_notify_webview', return_value=mock_webview), \
             patch.object(watcher.subprocess, 'Popen', side_effect=OSError("exec failed")), \
             patch.object(watcher, '_kill_webview') as mock_kill:
            with pytest.raises(OSError):
                watcher._run_notify_login("prof")
            mock_kill.assert_called()

    def test_cleanup_closes_stdout_and_calls_cleanup(self):
        """Finally block closes stdout and calls webview.cleanup()."""
        mock_webview = MagicMock()
        mock_stdout = MagicMock()
        mock_stdout.__iter__ = MagicMock(return_value=iter(["SSO_WINDOW_CLOSED\n"]))
        mock_webview.stdout = mock_stdout
        mock_webview.poll.return_value = 2
        mock_webview.stdin = MagicMock()
        mock_webview.cleanup = MagicMock()

        with patch.object(watcher, '_launch_notify_webview', return_value=mock_webview), \
             patch.object(watcher, '_kill_webview'):
            watcher._run_notify_login("prof")
            mock_stdout.close.assert_called()
            mock_webview.cleanup.assert_called_once()


# ---------------------------------------------------------------------------
# _is_webview_running
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestIsWebviewRunning:

    def test_returns_true_when_running(self):
        """pgrep rc=0 → webview is running."""
        proc = _mock_proc("", 0)
        with patch.object(watcher.subprocess, 'run', return_value=proc):
            assert watcher._is_webview_running() is True

    def test_returns_false_when_not_running(self):
        """pgrep rc=1 → webview not running."""
        proc = _mock_proc("", 1)
        with patch.object(watcher.subprocess, 'run', return_value=proc):
            assert watcher._is_webview_running() is False

    def test_returns_false_on_timeout(self):
        """subprocess.TimeoutExpired → False."""
        with patch.object(watcher.subprocess, 'run', side_effect=subprocess.TimeoutExpired("pgrep", 3)):
            assert watcher._is_webview_running() is False

    def test_returns_false_on_exception(self):
        """Generic exception → False."""
        with patch.object(watcher.subprocess, 'run', side_effect=OSError("no pgrep")):
            assert watcher._is_webview_running() is False


# ---------------------------------------------------------------------------
# Stale lock recovery
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestStaleLockRecovery:

    def test_stale_lock_reclaimed(self, watcher_state):
        """Lock older than LOCK_STALE_SECONDS is reclaimed."""
        lock = watcher_state["lock_dir"]
        lock.mkdir()
        # Backdate mtime to make it stale
        stale_time = time.time() - watcher.LOCK_STALE_SECONDS - 60
        os.utime(str(lock), (stale_time, stale_time))
        assert watcher.try_acquire_lock() is True
        assert lock.exists()

    def test_non_stale_lock_not_reclaimed(self, watcher_state):
        """Lock younger than LOCK_STALE_SECONDS is NOT reclaimed."""
        lock = watcher_state["lock_dir"]
        lock.mkdir()
        assert watcher.try_acquire_lock() is False

    def test_stale_lock_rmdir_fails(self, watcher_state):
        """If rmdir fails on stale lock, returns False."""
        lock = watcher_state["lock_dir"]
        lock.mkdir()
        stale_time = time.time() - watcher.LOCK_STALE_SECONDS - 60
        os.utime(str(lock), (stale_time, stale_time))
        # Put a file inside so rmdir fails
        (lock / "blocker").touch()
        assert watcher.try_acquire_lock() is False


# ---------------------------------------------------------------------------
# load_signal
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestLoadSignal:

    def test_loads_valid_signal(self, watcher_state):
        data = {"profile": "test", "reason": "expired"}
        watcher_state["signal_file"].write_text(json.dumps(data))
        result = watcher.load_signal()
        assert result == data

    def test_missing_file_returns_empty(self, watcher_state):
        assert watcher.load_signal() == {}

    def test_corrupt_json_returns_empty(self, watcher_state):
        watcher_state["signal_file"].write_text("not valid json {{{")
        assert watcher.load_signal() == {}

    def test_empty_file_returns_empty(self, watcher_state):
        watcher_state["signal_file"].write_text("")
        assert watcher.load_signal() == {}
