"""
Unit tests for sso-monitor service.
"""
import json
import os
import signal as _signal
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock, call, mock_open

import pytest

# Add sso-monitor to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../sso-monitor'))


# ---------------------------------------------------------------------------
# Helpers
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


def _make_client_error(code='ExpiredToken'):
    """Create a botocore ClientError with given error code."""
    from botocore.exceptions import ClientError
    return ClientError(
        {'Error': {'Code': code, 'Message': 'test'}},
        'GetCallerIdentity'
    )


# ---------------------------------------------------------------------------
# Module-level config tests (must reload module to test env parsing)
# ---------------------------------------------------------------------------

import importlib


def _reload_monitor(**env_overrides):
    """Reload monitor module with given env, return it."""
    import monitor as _m
    importlib.reload(_m)
    return _m


@pytest.mark.unit
class TestModuleConfig:
    """CHECK_INTERVAL / SIGNAL_FILE env parsing at import time."""

    def setup_method(self):
        """Save module state before each test."""
        import monitor
        self._saved_attrs = {
            'AWS_PROFILE': monitor.AWS_PROFILE,
            'CHECK_INTERVAL': monitor.CHECK_INTERVAL,
            'SIGNAL_FILE': monitor.SIGNAL_FILE,
        }

    def teardown_method(self):
        """Restore module state after each reload test."""
        import monitor
        for attr, val in self._saved_attrs.items():
            setattr(monitor, attr, val)

    def test_default_check_interval(self):
        """Default CHECK_INTERVAL=60 when env unset."""
        with patch.dict(os.environ, {}, clear=True):
            m = _reload_monitor()
            assert m.CHECK_INTERVAL >= 5

    def test_check_interval_from_env(self):
        """CHECK_INTERVAL reads from env."""
        with patch.dict(os.environ, {'CHECK_INTERVAL': '120'}, clear=False):
            m = _reload_monitor()
            assert m.CHECK_INTERVAL == 120

    def test_check_interval_clamped_to_minimum(self):
        """CHECK_INTERVAL below 5 is clamped to 5."""
        with patch.dict(os.environ, {'CHECK_INTERVAL': '1'}, clear=False):
            m = _reload_monitor()
            assert m.CHECK_INTERVAL == 5

    def test_check_interval_non_integer_falls_back(self):
        """Non-integer CHECK_INTERVAL raises ValueError at import."""
        with patch.dict(os.environ, {'CHECK_INTERVAL': 'abc'}, clear=False):
            with pytest.raises(ValueError):
                _reload_monitor()

    def test_signal_file_from_env(self):
        """SIGNAL_FILE reads from env."""
        with patch.dict(os.environ, {'SIGNAL_FILE': '/tmp/test-signal.json'}, clear=False):
            m = _reload_monitor()
            assert m.SIGNAL_FILE == Path('/tmp/test-signal.json')

    def test_aws_profile_from_env(self):
        """AWS_PROFILE reads from env."""
        with patch.dict(os.environ, {'AWS_PROFILE': 'staging'}, clear=False):
            m = _reload_monitor()
            assert m.AWS_PROFILE == 'staging'

    def test_aws_profile_defaults_to_default(self):
        """AWS_PROFILE defaults to 'default'."""
        env = os.environ.copy()
        env.pop('AWS_PROFILE', None)
        with patch.dict(os.environ, env, clear=True):
            m = _reload_monitor()
            assert m.AWS_PROFILE == 'default'


import monitor


# ---------------------------------------------------------------------------
# check_credentials
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestCheckCredentials:

    def test_valid_credentials(self):
        """STS get_caller_identity succeeds → True."""
        mock_session = MagicMock()
        mock_sts = MagicMock()
        mock_session.client.return_value = mock_sts
        mock_sts.get_caller_identity.return_value = {'Account': '123'}

        assert monitor.check_credentials(session=mock_session) is True
        mock_session.client.assert_called_once_with('sts')
        mock_sts.get_caller_identity.assert_called_once()

    def test_no_credentials_error(self):
        """NoCredentialsError → False."""
        from botocore.exceptions import NoCredentialsError
        mock_session = MagicMock()
        mock_sts = MagicMock()
        mock_session.client.return_value = mock_sts
        mock_sts.get_caller_identity.side_effect = NoCredentialsError()

        assert monitor.check_credentials(session=mock_session) is False

    def test_token_retrieval_error(self):
        """TokenRetrievalError → False."""
        from botocore.exceptions import TokenRetrievalError
        mock_session = MagicMock()
        mock_sts = MagicMock()
        mock_session.client.return_value = mock_sts
        mock_sts.get_caller_identity.side_effect = TokenRetrievalError(provider='sso', error_msg='expired')

        assert monitor.check_credentials(session=mock_session) is False

    def test_credential_retrieval_error(self):
        """CredentialRetrievalError → False."""
        from botocore.exceptions import CredentialRetrievalError
        mock_session = MagicMock()
        mock_sts = MagicMock()
        mock_session.client.return_value = mock_sts
        mock_sts.get_caller_identity.side_effect = CredentialRetrievalError(provider='sso', error_msg='fail')

        assert monitor.check_credentials(session=mock_session) is False

    def test_client_error_expired_token(self):
        """ClientError with ExpiredToken → False."""
        mock_session = MagicMock()
        mock_sts = MagicMock()
        mock_session.client.return_value = mock_sts
        mock_sts.get_caller_identity.side_effect = _make_client_error('ExpiredToken')

        assert monitor.check_credentials(session=mock_session) is False

    def test_client_error_expired_token_exception(self):
        """ClientError with ExpiredTokenException → False."""
        mock_session = MagicMock()
        mock_sts = MagicMock()
        mock_session.client.return_value = mock_sts
        mock_sts.get_caller_identity.side_effect = _make_client_error('ExpiredTokenException')

        assert monitor.check_credentials(session=mock_session) is False

    def test_client_error_invalid_token(self):
        """ClientError with InvalidToken → False."""
        mock_session = MagicMock()
        mock_sts = MagicMock()
        mock_session.client.return_value = mock_sts
        mock_sts.get_caller_identity.side_effect = _make_client_error('InvalidToken')

        assert monitor.check_credentials(session=mock_session) is False

    def test_client_error_other_code(self):
        """ClientError with non-token code → False (with warning)."""
        mock_session = MagicMock()
        mock_sts = MagicMock()
        mock_session.client.return_value = mock_sts
        mock_sts.get_caller_identity.side_effect = _make_client_error('AccessDenied')

        assert monitor.check_credentials(session=mock_session) is False

    def test_generic_exception(self):
        """Unexpected exception → False (with warning)."""
        mock_session = MagicMock()
        mock_sts = MagicMock()
        mock_session.client.return_value = mock_sts
        mock_sts.get_caller_identity.side_effect = RuntimeError("network down")

        assert monitor.check_credentials(session=mock_session) is False

    def test_endpoint_connection_error(self):
        """EndpointConnectionError → False (caught by generic handler)."""
        from botocore.exceptions import EndpointConnectionError
        mock_session = MagicMock()
        mock_sts = MagicMock()
        mock_session.client.return_value = mock_sts
        mock_sts.get_caller_identity.side_effect = EndpointConnectionError(endpoint_url='https://sts.amazonaws.com')

        assert monitor.check_credentials(session=mock_session) is False

    def test_no_session_creates_default(self):
        """When session=None, creates boto3.Session with AWS_PROFILE."""
        mock_session = MagicMock()
        mock_sts = MagicMock()
        mock_session.client.return_value = mock_sts
        mock_sts.get_caller_identity.return_value = {'Account': '123'}

        with patch('boto3.Session', return_value=mock_session) as mock_cls, \
             patch.object(monitor, 'AWS_PROFILE', 'my-profile'):
            result = monitor.check_credentials(session=None)

        assert result is True
        mock_cls.assert_called_once_with(profile_name='my-profile')

    def test_profile_not_found(self):
        """ProfileNotFound → False."""
        from botocore.exceptions import ProfileNotFound
        mock_session = MagicMock()
        mock_session.client.side_effect = ProfileNotFound(profile='missing')

        assert monitor.check_credentials(session=mock_session) is False


# ---------------------------------------------------------------------------
# write_signal_file
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestWriteSignalFile:

    def test_writes_json_with_expected_fields(self, tmp_path):
        """Signal file contains profile, reason, timestamp, source."""
        signal_file = tmp_path / "signals" / "login-required.json"
        with patch.object(monitor, 'SIGNAL_FILE', signal_file), \
             patch.object(monitor, 'AWS_PROFILE', 'test-prof'):
            monitor.write_signal_file("Creds expired")

        assert signal_file.exists()
        data = json.loads(signal_file.read_text())
        assert data['profile'] == 'test-prof'
        assert data['reason'] == 'Creds expired'
        assert data['source'] == 'sso-monitor-container'
        assert 'timestamp' in data
        # Timestamp should be ISO format
        assert 'T' in data['timestamp']

    def test_default_reason(self, tmp_path):
        """Default reason is 'Credentials expired'."""
        signal_file = tmp_path / "signals" / "login-required.json"
        with patch.object(monitor, 'SIGNAL_FILE', signal_file), \
             patch.object(monitor, 'AWS_PROFILE', 'default'):
            monitor.write_signal_file()

        data = json.loads(signal_file.read_text())
        assert data['reason'] == 'Credentials expired'

    def test_creates_parent_directory(self, tmp_path):
        """Signal parent dir created if missing."""
        signal_file = tmp_path / "deep" / "nested" / "signal.json"
        with patch.object(monitor, 'SIGNAL_FILE', signal_file), \
             patch.object(monitor, 'AWS_PROFILE', 'default'):
            monitor.write_signal_file()

        assert signal_file.exists()

    def test_atomic_write_uses_tempfile_and_replace(self, tmp_path):
        """Verify atomic write: mkstemp → write → os.replace."""
        signal_file = tmp_path / "signals" / "login-required.json"
        signal_file.parent.mkdir(parents=True)

        with patch.object(monitor, 'SIGNAL_FILE', signal_file), \
             patch.object(monitor, 'AWS_PROFILE', 'default'), \
             patch.object(monitor._tempfile, 'mkstemp',
                          return_value=(99, str(tmp_path / "signals" / "tmp123.tmp"))) as mock_mkstemp, \
             patch('os.fdopen', return_value=MagicMock(__enter__=MagicMock(), __exit__=MagicMock())) as mock_fdopen, \
             patch('os.replace') as mock_replace:
            monitor.write_signal_file()

        mock_mkstemp.assert_called_once()
        mock_fdopen.assert_called_once_with(99, 'w')
        mock_replace.assert_called_once_with(
            str(tmp_path / "signals" / "tmp123.tmp"),
            str(signal_file)
        )

    def test_cleans_up_tempfile_on_write_error(self, tmp_path):
        """On write failure, tempfile is unlinked."""
        signal_file = tmp_path / "signals" / "login-required.json"
        signal_file.parent.mkdir(parents=True)

        tmp_file = str(tmp_path / "signals" / "tmp456.tmp")
        Path(tmp_file).touch()  # create it so unlink can work

        with patch.object(monitor, 'SIGNAL_FILE', signal_file), \
             patch.object(monitor, 'AWS_PROFILE', 'default'), \
             patch.object(monitor._tempfile, 'mkstemp', return_value=(99, tmp_file)), \
             patch('os.fdopen', side_effect=OSError("disk full")), \
             patch('os.unlink') as mock_unlink:
            # Should not raise (outer except catches)
            monitor.write_signal_file()

        mock_unlink.assert_called_once_with(tmp_file)

    def test_permission_error_handled_gracefully(self, tmp_path):
        """PermissionError on mkdir does not crash."""
        signal_file = tmp_path / "noperm" / "signal.json"
        mock_path = MagicMock(spec=Path)
        mock_path.parent = MagicMock()
        mock_path.parent.mkdir.side_effect = PermissionError("denied")

        with patch.object(monitor, 'SIGNAL_FILE', mock_path):
            # Should not raise
            monitor.write_signal_file()

    def test_overwrites_existing_signal(self, tmp_path):
        """Writing signal overwrites existing file."""
        signal_file = tmp_path / "signals" / "login-required.json"
        signal_file.parent.mkdir(parents=True)
        signal_file.write_text('{"old": true}')

        with patch.object(monitor, 'SIGNAL_FILE', signal_file), \
             patch.object(monitor, 'AWS_PROFILE', 'default'):
            monitor.write_signal_file("new reason")

        data = json.loads(signal_file.read_text())
        assert data['reason'] == 'new reason'
        assert 'old' not in data


# ---------------------------------------------------------------------------
# clear_signal_file
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestClearSignalFile:

    def test_removes_existing_signal(self, tmp_path):
        """Existing signal file is deleted."""
        signal_file = tmp_path / "login-required.json"
        signal_file.write_text('{"reason": "expired"}')

        with patch.object(monitor, 'SIGNAL_FILE', signal_file):
            monitor.clear_signal_file()

        assert not signal_file.exists()

    def test_no_error_when_missing(self, tmp_path):
        """No crash when signal file doesn't exist."""
        signal_file = tmp_path / "nonexistent.json"

        with patch.object(monitor, 'SIGNAL_FILE', signal_file):
            monitor.clear_signal_file()  # should not raise

    def test_permission_error_suppressed(self):
        """PermissionError on unlink is swallowed."""
        mock_path = MagicMock(spec=Path)
        mock_path.unlink.side_effect = PermissionError("denied")

        with patch.object(monitor, 'SIGNAL_FILE', mock_path):
            monitor.clear_signal_file()  # should not raise


# ---------------------------------------------------------------------------
# SIGTERM handling
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSigtermHandling:

    def test_sigterm_triggers_sys_exit(self):
        """SIGTERM handler calls sys.exit(0)."""
        with patch('boto3.Session'), \
             patch.object(monitor._signal, 'signal') as mock_signal_fn, \
             patch('time.sleep', side_effect=KeyboardInterrupt):
            monitor.main()

        # Verify signal.signal was called with SIGTERM
        sigterm_calls = [
            c for c in mock_signal_fn.call_args_list
            if c[0][0] == _signal.SIGTERM
        ]
        assert len(sigterm_calls) == 1

        # Extract the handler and verify it calls sys.exit(0)
        handler = sigterm_calls[0][0][1]
        with pytest.raises(SystemExit) as exc_info:
            handler()
        assert exc_info.value.code == 0


# ---------------------------------------------------------------------------
# Session reuse
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSessionReuse:

    def test_session_created_once(self):
        """boto3.Session created once (lazily) in main loop, reused across checks."""
        mock_session = MagicMock()
        mock_sts = MagicMock()
        mock_session.client.return_value = mock_sts
        mock_sts.get_caller_identity.return_value = {'Account': '123'}

        with patch('boto3.Session', return_value=mock_session) as mock_cls, \
             patch.object(monitor, 'AWS_PROFILE', 'default'), \
             patch('time.sleep', side_effect=_stop_after(3)):
            monitor.main()

        # Session constructor called exactly once (lazy in first iteration)
        mock_cls.assert_called_once_with(profile_name='default')

    def test_session_passed_to_check_credentials(self):
        """Session from main() is passed to check_credentials."""
        mock_session = MagicMock()
        mock_sts = MagicMock()
        mock_session.client.return_value = mock_sts
        mock_sts.get_caller_identity.return_value = {'Account': '123'}

        with patch('boto3.Session', return_value=mock_session), \
             patch.object(monitor, 'check_credentials', return_value=True) as mock_check, \
             patch('time.sleep', side_effect=_stop_after(1)):
            monitor.main()

        mock_check.assert_called_with(session=mock_session)


# ---------------------------------------------------------------------------
# Main loop: state transitions
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestMainLoopStateTransitions:

    def test_valid_to_expired_writes_signal(self, tmp_path):
        """Transition valid→expired writes signal file."""
        signal_file = tmp_path / "login-required.json"
        validity = iter([True, False])  # first valid, then expired

        with patch.object(monitor, 'SIGNAL_FILE', signal_file), \
             patch.object(monitor, 'AWS_PROFILE', 'default'), \
             patch('boto3.Session'), \
             patch.object(monitor, 'check_credentials', side_effect=lambda **kw: next(validity)), \
             patch.object(monitor, 'write_signal_file') as mock_write, \
             patch.object(monitor, 'clear_signal_file') as mock_clear, \
             patch('time.sleep', side_effect=_stop_after(2)):
            monitor.main()

        # First call: valid → clear_signal_file
        mock_clear.assert_called_once()
        # Second call: expired → write_signal_file
        mock_write.assert_called_once()

    def test_expired_to_valid_clears_signal(self, tmp_path):
        """Transition expired→valid clears signal file."""
        signal_file = tmp_path / "login-required.json"
        validity = iter([False, True])

        with patch.object(monitor, 'SIGNAL_FILE', signal_file), \
             patch.object(monitor, 'AWS_PROFILE', 'default'), \
             patch('boto3.Session'), \
             patch.object(monitor, 'check_credentials', side_effect=lambda **kw: next(validity)), \
             patch.object(monitor, 'write_signal_file') as mock_write, \
             patch.object(monitor, 'clear_signal_file') as mock_clear, \
             patch('time.sleep', side_effect=_stop_after(2)):
            monitor.main()

        mock_write.assert_called_once()
        mock_clear.assert_called_once()

    def test_expired_stays_expired_no_spam(self):
        """Consecutive expired states → signal written only once."""
        with patch('boto3.Session'), \
             patch.object(monitor, 'check_credentials', return_value=False), \
             patch.object(monitor, 'write_signal_file') as mock_write, \
             patch('time.sleep', side_effect=_stop_after(3)):
            monitor.main()

        mock_write.assert_called_once()

    def test_valid_stays_valid_no_spam(self):
        """Consecutive valid states → clear called only once."""
        with patch('boto3.Session'), \
             patch.object(monitor, 'check_credentials', return_value=True), \
             patch.object(monitor, 'clear_signal_file') as mock_clear, \
             patch('time.sleep', side_effect=_stop_after(3)):
            monitor.main()

        mock_clear.assert_called_once()

    def test_none_to_valid_clears_signal(self):
        """Initial state (None) → valid triggers clear."""
        with patch('boto3.Session'), \
             patch.object(monitor, 'check_credentials', return_value=True), \
             patch.object(monitor, 'clear_signal_file') as mock_clear, \
             patch('time.sleep', side_effect=_stop_after(1)):
            monitor.main()

        mock_clear.assert_called_once()

    def test_none_to_expired_writes_signal(self):
        """Initial state (None) → expired triggers write."""
        with patch('boto3.Session'), \
             patch.object(monitor, 'check_credentials', return_value=False), \
             patch.object(monitor, 'write_signal_file') as mock_write, \
             patch('time.sleep', side_effect=_stop_after(1)):
            monitor.main()

        mock_write.assert_called_once()


# ---------------------------------------------------------------------------
# Main loop: sleep interval
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestMainLoopInterval:

    def test_sleeps_check_interval(self):
        """Loop sleeps CHECK_INTERVAL seconds between checks."""
        with patch('boto3.Session'), \
             patch.object(monitor, 'check_credentials', return_value=True), \
             patch.object(monitor, 'CHECK_INTERVAL', 42), \
             patch('time.sleep', side_effect=_stop_after(1)) as mock_sleep:
            monitor.main()

        mock_sleep.assert_called_with(42)

    def test_sleeps_after_error(self):
        """Loop sleeps CHECK_INTERVAL even after exception."""
        call_count = [0]
        def failing_check(**kw):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("transient")
            return True

        with patch('boto3.Session'), \
             patch.object(monitor, 'check_credentials', side_effect=failing_check), \
             patch.object(monitor, 'CHECK_INTERVAL', 99), \
             patch('time.sleep', side_effect=_stop_after(2)) as mock_sleep:
            monitor.main()

        # Both calls should use CHECK_INTERVAL
        assert mock_sleep.call_args_list[0] == call(99)


# ---------------------------------------------------------------------------
# Main loop: error recovery
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestMainLoopErrorRecovery:

    def test_exception_resets_last_state(self):
        """Exception in loop sets last_state=None, forcing re-evaluation."""
        call_count = [0]
        def flaky_check(**kw):
            call_count[0] += 1
            if call_count[0] == 1:
                return True  # first: valid
            if call_count[0] == 2:
                raise RuntimeError("crash")  # resets last_state to None
            return True  # third: valid again (should trigger clear since last_state=None)

        with patch('boto3.Session'), \
             patch.object(monitor, 'check_credentials', side_effect=flaky_check), \
             patch.object(monitor, 'clear_signal_file') as mock_clear, \
             patch('time.sleep', side_effect=_stop_after(3)):
            monitor.main()

        # clear_signal_file called twice: once on first valid, once on recovery
        assert mock_clear.call_count == 2

    def test_keyboard_interrupt_exits_cleanly(self):
        """KeyboardInterrupt breaks the loop without crash."""
        with patch('boto3.Session'), \
             patch.object(monitor, 'check_credentials', return_value=True), \
             patch('time.sleep', side_effect=KeyboardInterrupt):
            # Should not raise
            monitor.main()

    def test_loop_continues_after_exception(self):
        """Generic exception doesn't kill the loop."""
        call_count = [0]
        def counting_check(**kw):
            call_count[0] += 1
            if call_count[0] <= 2:
                raise RuntimeError("fail")
            return True

        with patch('boto3.Session'), \
             patch.object(monitor, 'check_credentials', side_effect=counting_check), \
             patch('time.sleep', side_effect=_stop_after(3)):
            monitor.main()

        # Should have called check 3 times (2 failures + 1 success)
        assert call_count[0] == 3


# ---------------------------------------------------------------------------
# Main loop: ProfileNotFound handling
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestMainLoopProfileNotFound:

    def test_profile_not_found_does_not_crash(self):
        """ProfileNotFound at session creation → loop continues, no crash."""
        from botocore.exceptions import ProfileNotFound

        with patch('boto3.Session', side_effect=ProfileNotFound(profile='missing')), \
             patch.object(monitor, 'write_signal_file') as mock_write, \
             patch('time.sleep', side_effect=_stop_after(2)):
            monitor.main()

        # Should write signal once (state transition None→False)
        mock_write.assert_called_once()

    def test_profile_not_found_writes_signal_with_reason(self):
        """ProfileNotFound writes signal with descriptive reason."""
        from botocore.exceptions import ProfileNotFound

        with patch('boto3.Session', side_effect=ProfileNotFound(profile='bad-prof')), \
             patch.object(monitor, 'AWS_PROFILE', 'bad-prof'), \
             patch.object(monitor, 'write_signal_file') as mock_write, \
             patch('time.sleep', side_effect=_stop_after(1)):
            monitor.main()

        mock_write.assert_called_once_with("Profile 'bad-prof' not found")

    def test_profile_not_found_no_spam(self):
        """Repeated ProfileNotFound → signal written only once."""
        from botocore.exceptions import ProfileNotFound

        with patch('boto3.Session', side_effect=ProfileNotFound(profile='missing')), \
             patch.object(monitor, 'write_signal_file') as mock_write, \
             patch('time.sleep', side_effect=_stop_after(3)):
            monitor.main()

        mock_write.assert_called_once()

    def test_profile_not_found_retries_session(self):
        """ProfileNotFound → retries boto3.Session each iteration."""
        from botocore.exceptions import ProfileNotFound
        call_count = [0]

        def session_factory(**kw):
            call_count[0] += 1
            raise ProfileNotFound(profile='missing')

        with patch('boto3.Session', side_effect=session_factory), \
             patch.object(monitor, 'write_signal_file'), \
             patch('time.sleep', side_effect=_stop_after(3)):
            monitor.main()

        # Session creation retried each loop iteration
        assert call_count[0] == 3

    def test_profile_not_found_recovers_when_profile_appears(self):
        """ProfileNotFound initially → profile appears later → recovery."""
        from botocore.exceptions import ProfileNotFound
        call_count = [0]
        mock_session = MagicMock()
        mock_sts = MagicMock()
        mock_session.client.return_value = mock_sts
        mock_sts.get_caller_identity.return_value = {'Account': '123'}

        def session_factory(**kw):
            nonlocal call_count
            call_count[0] += 1
            if call_count[0] <= 2:
                raise ProfileNotFound(profile='missing')
            return mock_session

        with patch('boto3.Session', side_effect=session_factory), \
             patch.object(monitor, 'write_signal_file') as mock_write, \
             patch.object(monitor, 'clear_signal_file') as mock_clear, \
             patch('time.sleep', side_effect=_stop_after(4)):
            monitor.main()

        # Signal written once (ProfileNotFound), then cleared once (recovery)
        mock_write.assert_called_once()
        mock_clear.assert_called_once()


# ---------------------------------------------------------------------------
# Main loop: startup messages
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestMainLoopStartup:

    def test_main_calls_check_credentials(self):
        """main() enters loop and calls check_credentials."""
        with patch('boto3.Session'), \
             patch.object(monitor, 'check_credentials', return_value=True) as mock_check, \
             patch('time.sleep', side_effect=_stop_after(1)):
            monitor.main()

        assert mock_check.call_count >= 1

    def test_main_creates_session_with_profile(self):
        """main() creates boto3.Session with configured profile."""
        with patch('boto3.Session') as mock_cls, \
             patch.object(monitor, 'AWS_PROFILE', 'my-prof'), \
             patch.object(monitor, 'check_credentials', return_value=True), \
             patch('time.sleep', side_effect=_stop_after(1)):
            monitor.main()

        mock_cls.assert_called_once_with(profile_name='my-prof')


# ---------------------------------------------------------------------------
# Integration: full valid→expired→valid cycle
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestFullCycle:

    def test_valid_expired_valid_cycle(self, tmp_path):
        """Full cycle: valid → expired → valid."""
        signal_file = tmp_path / "login-required.json"
        states = iter([True, False, True])

        with patch.object(monitor, 'SIGNAL_FILE', signal_file), \
             patch.object(monitor, 'AWS_PROFILE', 'default'), \
             patch('boto3.Session'), \
             patch.object(monitor, 'check_credentials', side_effect=lambda **kw: next(states)), \
             patch('time.sleep', side_effect=_stop_after(3)):
            monitor.main()

        # After the cycle, signal should be cleared
        assert not signal_file.exists()

    def test_multiple_transitions(self, tmp_path):
        """Multiple state flips: valid→expired→valid→expired."""
        signal_file = tmp_path / "login-required.json"
        states = iter([True, False, True, False])

        write_count = [0]
        clear_count = [0]

        orig_write = monitor.write_signal_file
        orig_clear = monitor.clear_signal_file

        def counting_write(reason="Credentials expired"):
            write_count[0] += 1
            signal_file.parent.mkdir(parents=True, exist_ok=True)
            signal_file.write_text(json.dumps({"reason": reason}))

        def counting_clear():
            clear_count[0] += 1
            signal_file.unlink(missing_ok=True)

        with patch.object(monitor, 'SIGNAL_FILE', signal_file), \
             patch.object(monitor, 'AWS_PROFILE', 'default'), \
             patch('boto3.Session'), \
             patch.object(monitor, 'check_credentials', side_effect=lambda **kw: next(states)), \
             patch.object(monitor, 'write_signal_file', side_effect=counting_write), \
             patch.object(monitor, 'clear_signal_file', side_effect=counting_clear), \
             patch('time.sleep', side_effect=_stop_after(4)):
            monitor.main()

        assert write_count[0] == 2
        assert clear_count[0] == 2
