#!/usr/bin/env python3
"""
SSO credential monitor for container deployment.

Periodically checks if AWS SSO credentials are valid.
Writes signal file when credentials expire for host-side watcher to pick up.
"""
import os
import sys
import json
import time
from pathlib import Path
from datetime import datetime, timezone

import boto3
from botocore.exceptions import (
    NoCredentialsError,
    ClientError,
    TokenRetrievalError,
    CredentialRetrievalError
)

# Configuration from environment
AWS_PROFILE = os.environ.get('AWS_PROFILE', 'default')
CHECK_INTERVAL = int(os.environ.get('CHECK_INTERVAL', '60'))  # seconds
SIGNAL_FILE = Path(os.environ.get(
    'SIGNAL_FILE',
    '/signals/login-required.json'
))


def check_credentials() -> bool:
    """
    Check if credentials are valid using boto3 sts.get_caller_identity().

    Returns:
        True if valid, False otherwise
    """
    try:
        session = boto3.Session(profile_name=AWS_PROFILE)
        sts = session.client('sts')
        sts.get_caller_identity()
        return True

    except (NoCredentialsError, TokenRetrievalError, CredentialRetrievalError):
        return False

    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', '')
        if error_code in ['ExpiredToken', 'ExpiredTokenException', 'InvalidToken']:
            return False
        print(f"[sso-monitor] Warning: AWS API error: {error_code}", flush=True)
        return False

    except Exception as e:
        print(f"[sso-monitor] Warning: Error checking credentials: {e}", flush=True)
        return False


def write_signal_file(reason: str = "Credentials expired"):
    """Write signal file for watcher to pick up."""
    try:
        # Ensure directory exists
        SIGNAL_FILE.parent.mkdir(parents=True, exist_ok=True)

        signal_data = {
            "profile": AWS_PROFILE,
            "reason": reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "sso-monitor-container"
        }

        with open(SIGNAL_FILE, 'w') as f:
            json.dump(signal_data, f, indent=2)

        print(f"[sso-monitor] ✗ Credentials invalid - wrote signal: {SIGNAL_FILE}", flush=True)

    except Exception as e:
        print(f"[sso-monitor] Error writing signal file: {e}", file=sys.stderr, flush=True)


def clear_signal_file():
    """Remove signal file when credentials are valid."""
    try:
        if SIGNAL_FILE.exists():
            SIGNAL_FILE.unlink()
            print(f"[sso-monitor] ✓ Credentials valid - cleared signal", flush=True)
    except Exception:
        pass


def main():
    """Main monitoring loop."""
    print(f"[sso-monitor] Starting credential monitor", flush=True)
    print(f"[sso-monitor] Profile: {AWS_PROFILE}", flush=True)
    print(f"[sso-monitor] Check interval: {CHECK_INTERVAL}s", flush=True)
    print(f"[sso-monitor] Signal file: {SIGNAL_FILE}", flush=True)
    print("", flush=True)

    last_state = None

    while True:
        try:
            credentials_valid = check_credentials()

            # Only log state changes
            if credentials_valid != last_state:
                if credentials_valid:
                    print(f"[sso-monitor] ✓ Credentials valid", flush=True)
                    clear_signal_file()
                else:
                    print(f"[sso-monitor] ✗ Credentials invalid", flush=True)
                    write_signal_file()

                last_state = credentials_valid

            time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            print("[sso-monitor] Exiting", flush=True)
            break
        except Exception as e:
            print(f"[sso-monitor] Error in monitoring loop: {e}", file=sys.stderr, flush=True)
            time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
