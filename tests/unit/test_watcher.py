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
        with          patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'show_notification', return_value="refresh"), \
             patch.object(watcher, 'run_aws_sso_login', return_value=0) as mock_login:
            assert watcher.handle_login("default") == "success"
            mock_login.assert_called_once_with("default")

    def test_notify_mode_refresh_login_fails(self):
        with          patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'show_notification', return_value="refresh"), \
             patch.object(watcher, 'run_aws_sso_login', return_value=1):
            assert watcher.handle_login("default") == "failed"

    def test_notify_mode_dismiss(self):
        with          patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'show_notification', return_value="dismiss"), \
             patch.object(watcher, 'run_aws_sso_login') as mock_login:
            assert watcher.handle_login("default") == "dismiss"
            mock_login.assert_not_called()

    def test_notify_mode_snooze(self):
        with          patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'show_notification', return_value="snooze:900"), \
             patch.object(watcher, 'run_aws_sso_login') as mock_login:
            assert watcher.handle_login("default") == "snooze:900"
            mock_login.assert_not_called()

    def test_notify_mode_suppress(self):
        with          patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'show_notification', return_value="suppress"), \
             patch.object(watcher, 'run_aws_sso_login') as mock_login:
            assert watcher.handle_login("default") == "suppress"
            mock_login.assert_not_called()

    def test_auto_mode_runs_directly(self):
        with          patch.object(watcher, 'read_mode', return_value='auto'), \
             patch.object(watcher, 'show_notification') as mock_notify, \
             patch.object(watcher, 'run_aws_sso_login', return_value=0):
            assert watcher.handle_login("default") == "success"
            mock_notify.assert_not_called()

    def test_auto_mode_login_failure(self):
        with          patch.object(watcher, 'read_mode', return_value='auto'), \
             patch.object(watcher, 'run_aws_sso_login', return_value=1):
            assert watcher.handle_login("default") == "failed"

    def test_notify_mode_uses_provided_profile(self):
        with          patch.object(watcher, 'read_mode', return_value='notify'), \
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
        with          patch.object(watcher, 'read_mode', return_value='notify'), \
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
        with          patch.object(watcher, 'read_mode', return_value='notify'), \
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
        with          patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'COOLDOWN_SECONDS', 0), \
             patch.object(watcher, 'show_notification', return_value="refresh"), \
             patch.object(watcher, 'run_aws_sso_login', return_value=1), \
             patch('time.sleep', side_effect=_stop_after(2)):
            watcher.main()
        assert watcher_state["signal_file"].exists()

    def test_snooze_writes_next_attempt(self, watcher_state):
        write_signal(watcher_state["signal_file"])
        with          patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'COOLDOWN_SECONDS', 600), \
             patch.object(watcher, 'show_notification', return_value="snooze:900"), \
             patch('time.sleep', side_effect=_stop_after(1)):
            watcher.main()
        data = json.loads(watcher_state["signal_file"].read_text())
        assert data["nextAttemptAfter"] > time.time()

    def test_suppress_clears_signal(self, watcher_state):
        write_signal(watcher_state["signal_file"])
        with          patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'COOLDOWN_SECONDS', 600), \
             patch.object(watcher, 'show_notification', return_value="suppress"), \
             patch('time.sleep', side_effect=_stop_after(1)):
            watcher.main()
        assert not watcher_state["signal_file"].exists()

    def test_lock_released_after_dismiss(self, watcher_state):
        write_signal(watcher_state["signal_file"])
        with          patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'COOLDOWN_SECONDS', 0), \
             patch.object(watcher, 'show_notification', return_value="dismiss"), \
             patch('time.sleep', side_effect=_stop_after(2)):
            watcher.main()
        assert not watcher_state["lock_dir"].exists()

    def test_lock_released_after_exception(self, watcher_state):
        write_signal(watcher_state["signal_file"])
        with          patch.object(watcher, 'read_mode', return_value='notify'), \
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
        with patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'PROFILE', 'fallback-profile'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'COOLDOWN_SECONDS', 600), \
             patch.object(watcher, 'show_notification', return_value="refresh") as mock_notify, \
             patch.object(watcher, 'run_aws_sso_login', return_value=0), \
             patch('time.sleep', side_effect=_stop_after(1)):
            watcher.main()
        mock_notify.assert_called_once_with("fallback-profile")


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
             patch.object(watcher, 'show_notification') as mock_notify, \
             patch.object(watcher, 'run_aws_sso_login') as mock_login, \
             patch('time.sleep', side_effect=_stop_after(2)):
            watcher.main()
        mock_notify.assert_not_called()
        mock_login.assert_not_called()
        # Signal should still exist (not cleared)
        assert watcher_state["signal_file"].exists()

    def test_handle_login_standalone_returns_dismiss(self):
        with patch.object(watcher, 'read_mode', return_value='standalone'), \
             patch.object(watcher, 'show_notification') as mock_notify, \
             patch.object(watcher, 'run_aws_sso_login') as mock_login:
            assert watcher.handle_login("default") == "dismiss"
            mock_notify.assert_not_called()
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
             patch.object(watcher, 'show_notification') as mock_notify, \
             patch.object(watcher, 'run_aws_sso_login') as mock_login, \
             patch('time.sleep', side_effect=_stop_after(2)):
            watcher.main()
        mock_notify.assert_not_called()
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
             patch.object(watcher, 'show_notification', return_value="refresh"), \
             patch.object(watcher, 'run_aws_sso_login', return_value=0), \
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
             patch.object(watcher, 'show_notification', return_value="dismiss"), \
             patch('time.sleep', side_effect=_stop_after(1)):
            watcher.main()
        assert watcher_state["last_run"].exists()

    def test_suppress_writes_cooldown(self, watcher_state):
        """notify: suppress → last-login-at.txt written."""
        write_signal(watcher_state["signal_file"])
        with patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'COOLDOWN_SECONDS', 600), \
             patch.object(watcher, 'show_notification', return_value="suppress"), \
             patch('time.sleep', side_effect=_stop_after(1)):
            watcher.main()
        assert watcher_state["last_run"].exists()

    def test_failure_writes_30s_snooze_not_cooldown(self, watcher_state):
        """notify: refresh fail → 30s snooze written, NO cooldown file."""
        write_signal(watcher_state["signal_file"])
        with patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'POLL_SECONDS', 0), \
             patch.object(watcher, 'COOLDOWN_SECONDS', 0), \
             patch.object(watcher, 'show_notification', return_value="refresh"), \
             patch.object(watcher, 'run_aws_sso_login', return_value=1), \
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
             patch.object(watcher, 'show_notification', return_value="refresh") as mock_notify, \
             patch.object(watcher, 'run_aws_sso_login', return_value=0), \
             patch('time.sleep', side_effect=mode_switching_sleep):
            # Start in standalone
            watcher_state["mode_file"].write_text("standalone\n")
            watcher.main()

        # Should have eventually processed the signal after mode switch
        mock_notify.assert_called_once()
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
             patch.object(watcher, 'show_notification', return_value="snooze:900"), \
             patch('time.sleep', side_effect=_stop_after(1)):
            watcher.main()
        assert not watcher_state["lock_dir"].exists()


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
# open_url_with_tab_reuse
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestOpenUrlWithTabReuse:

    def test_chrome_reuse(self):
        proc = _mock_proc("reused", 0)
        with patch.object(watcher.subprocess, 'run', return_value=proc):
            assert watcher.open_url_with_tab_reuse("https://example.com") == "reused"

    def test_chrome_new_tab(self):
        proc = _mock_proc("new", 0)
        with patch.object(watcher.subprocess, 'run', return_value=proc):
            assert watcher.open_url_with_tab_reuse("https://example.com") == "new"

    def test_no_chrome_falls_to_safari(self):
        chrome_proc = _mock_proc("no-chrome", 0)
        safari_proc = _mock_proc("reused", 0)
        with patch.object(watcher.subprocess, 'run',
                          side_effect=[chrome_proc, safari_proc]):
            assert watcher.open_url_with_tab_reuse("https://example.com") == "reused"

    def test_no_browsers_falls_to_open(self):
        chrome_proc = _mock_proc("no-chrome", 0)
        safari_proc = _mock_proc("no-safari", 0)
        open_proc = _mock_proc("", 0)
        with patch.object(watcher.subprocess, 'run',
                          side_effect=[chrome_proc, safari_proc, open_proc]):
            assert watcher.open_url_with_tab_reuse("https://example.com") == "fallback"

    def test_osascript_exception_falls_through(self):
        with patch.object(watcher.subprocess, 'run',
                          side_effect=[
                              RuntimeError("chrome fail"),
                              RuntimeError("safari fail"),
                              _mock_proc("", 0),  # open fallback
                          ]):
            assert watcher.open_url_with_tab_reuse("https://example.com") == "fallback"

    def test_url_embedded_in_script(self):
        proc = _mock_proc("new", 0)
        with patch.object(watcher.subprocess, 'run', return_value=proc) as mock_run:
            watcher.open_url_with_tab_reuse("https://device.sso.us-west-2.amazonaws.com/?code=ABC")
            script = mock_run.call_args[0][0][2]  # osascript -e <script>
            assert "https://device.sso.us-west-2.amazonaws.com/?code=ABC" in script


# ---------------------------------------------------------------------------
# Silent mode in handle_login
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSilentModeHandleLogin:

    def test_silent_mode_success(self):
        """Silent mode: silent refresh works → success, no browser/dialog."""
        with patch.object(watcher, 'read_mode', return_value='silent'), \
             patch.object(watcher, 'try_silent_refresh', return_value=True), \
             patch.object(watcher, 'show_notification') as mock_notify, \
             patch.object(watcher, 'run_aws_sso_login') as mock_login:
            assert watcher.handle_login("default") == "success"
            mock_notify.assert_not_called()
            mock_login.assert_not_called()

    def test_silent_mode_failure(self):
        """Silent mode: silent refresh fails → 'failed', no browser fallback."""
        with patch.object(watcher, 'read_mode', return_value='silent'), \
             patch.object(watcher, 'try_silent_refresh', return_value=False), \
             patch.object(watcher, 'show_notification') as mock_notify, \
             patch.object(watcher, 'run_aws_sso_login') as mock_login:
            assert watcher.handle_login("default") == "failed"
            mock_notify.assert_not_called()
            mock_login.assert_not_called()

    def test_notify_tries_silent_first_success(self):
        """Notify mode: silent refresh succeeds → no dialog shown."""
        with patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'try_silent_refresh', return_value=True), \
             patch.object(watcher, 'show_notification') as mock_notify, \
             patch.object(watcher, 'run_aws_sso_login') as mock_login:
            assert watcher.handle_login("default") == "success"
            mock_notify.assert_not_called()
            mock_login.assert_not_called()

    def test_auto_tries_silent_first_success(self):
        """Auto mode: silent refresh succeeds → no browser opened."""
        with patch.object(watcher, 'read_mode', return_value='auto'), \
             patch.object(watcher, 'try_silent_refresh', return_value=True), \
             patch.object(watcher, 'show_notification') as mock_notify, \
             patch.object(watcher, 'run_aws_sso_login') as mock_login:
            assert watcher.handle_login("default") == "success"
            mock_notify.assert_not_called()
            mock_login.assert_not_called()

    def test_notify_falls_back_after_silent_failure(self):
        """Notify mode: silent fails → dialog shown → refresh → login."""
        with patch.object(watcher, 'read_mode', return_value='notify'), \
             patch.object(watcher, 'try_silent_refresh', return_value=False), \
             patch.object(watcher, 'show_notification', return_value="refresh"), \
             patch.object(watcher, 'run_aws_sso_login', return_value=0) as mock_login:
            assert watcher.handle_login("default") == "success"
            mock_login.assert_called_once()

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
