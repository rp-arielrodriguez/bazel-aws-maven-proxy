#!/usr/bin/env python3
"""
SSO credential monitor for container deployment.

Periodically checks if AWS SSO credentials are valid.
Writes signal file when credentials expire for host-side watcher to pick up.
"""
import logging
import os
import sys
import json
import time
import signal as _signal
import tempfile as _tempfile
from pathlib import Path
from datetime import datetime, timezone

import boto3
from botocore.exceptions import (
    NoCredentialsError,
    ClientError,
    TokenRetrievalError,
    CredentialRetrievalError
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [sso-monitor] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("sso-monitor")

# Configuration from environment
AWS_PROFILE = os.environ.get('AWS_PROFILE', 'default')
CHECK_INTERVAL = max(int(os.environ.get('CHECK_INTERVAL', '60')), 5)  # seconds
SIGNAL_FILE = Path(os.environ.get(
    'SIGNAL_FILE',
    '/signals/login-required.json'
))


def check_credentials(session=None) -> bool:
    """
    Check if credentials are valid using boto3 sts.get_caller_identity().

    Returns:
        True if valid, False otherwise
    """
    try:
        if session is None:
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
        logger.warning("AWS API error: %s", error_code)
        return False

    except Exception as e:
        logger.warning("Error checking credentials: %s", e)
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

        tmp_fd, tmp_path = _tempfile.mkstemp(
            dir=str(SIGNAL_FILE.parent),
            suffix='.tmp'
        )
        try:
            with os.fdopen(tmp_fd, 'w') as f:
                json.dump(signal_data, f, indent=2)
            os.replace(tmp_path, str(SIGNAL_FILE))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        logger.error("Credentials invalid — wrote signal: %s", SIGNAL_FILE)

    except Exception as e:
        logger.error("Error writing signal file: %s", e)


def clear_signal_file():
    """Remove signal file when credentials are valid."""
    try:
        SIGNAL_FILE.unlink(missing_ok=True)
        logger.info("Credentials valid — cleared signal")
    except Exception:
        pass


def main():
    """Main monitoring loop."""
    logger.info("Starting credential monitor")
    logger.info("Profile: %s", AWS_PROFILE)
    logger.info("Check interval: %ds", CHECK_INTERVAL)
    logger.info("Signal file: %s", SIGNAL_FILE)
    # Handle SIGTERM from docker stop
    _signal.signal(_signal.SIGTERM, lambda *_: sys.exit(0))

    print("", flush=True)

    last_state = None
    session = boto3.Session(profile_name=AWS_PROFILE)

    while True:
        try:
            credentials_valid = check_credentials(session=session)

            # Only log state changes
            if credentials_valid != last_state:
                if credentials_valid:
                    logger.info("Credentials valid")
                    clear_signal_file()
                else:
                    logger.error("Credentials invalid")
                    write_signal_file()

                last_state = credentials_valid

            time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            logger.info("Exiting")
            break
        except Exception as e:
            logger.error("Error in monitoring loop: %s", e)
            last_state = None  # force re-evaluation on recovery
            time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
