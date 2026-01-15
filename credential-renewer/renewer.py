# credential-renewer/renewer.py
import os
import time
import json
import logging
import subprocess
import configparser
import hashlib
import boto3
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Dict
from botocore.exceptions import ClientError

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('credential-renewer')

# Configuration
AWS_PROFILE = os.environ.get('AWS_PROFILE', 'default')
SSO_CACHE_DIR = os.path.expanduser('~/.aws/sso/cache')
CHECK_INTERVAL = int(os.environ.get('CHECK_INTERVAL', '900'))  # 15 minutes by default
RENEWAL_THRESHOLD = int(os.environ.get('RENEWAL_THRESHOLD', '3600'))  # 1 hour by default
LOGIN_NOTIFICATION_FILE = os.environ.get('LOGIN_NOTIFICATION_FILE', '/data/login_required.txt')


def find_sso_token_file():
    """Find the latest SSO token file in the cache directory."""
    if not os.path.exists(SSO_CACHE_DIR):
        logger.warning(f"SSO cache directory does not exist: {SSO_CACHE_DIR}")
        return None

    # Find all JSON files in the SSO cache directory
    json_files = list(Path(SSO_CACHE_DIR).glob('*.json'))
    if not json_files:
        logger.warning("No SSO token files found in cache directory")
        return None

    # Filter to only token files (have accessToken), exclude client registration files
    token_files = []
    for f in json_files:
        try:
            with open(f, 'r') as file:
                data = json.load(file)
                # Token files have accessToken, client registration files don't
                if 'accessToken' in data:
                    token_files.append(f)
        except:
            continue

    if not token_files:
        logger.warning("No valid token files found in cache directory")
        return None

    # Get the most recently modified token file
    latest_file = max(token_files, key=lambda f: f.stat().st_mtime)
    mod_time = datetime.fromtimestamp(latest_file.stat().st_mtime)
    logger.info(f"Found latest token file: {latest_file.name}, modified: {mod_time}")
    return latest_file


def get_sso_session_config(profile_name: str) -> Dict[str, str]:
    """
    Extract SSO session configuration from AWS config.

    Args:
        profile_name: AWS profile name to look up

    Returns:
        dict with keys: sso_session, sso_region, sso_start_url,
                       sso_account_id, sso_role_name, sso_registration_scopes

    Raises:
        Exception if config missing or incomplete
    """
    config = configparser.ConfigParser()
    config.read(os.path.expanduser('~/.aws/config'))

    # Find profile section
    profile_section = f"profile {profile_name}"
    if profile_section not in config:
        profile_section = profile_name

    if profile_section not in config:
        raise Exception(f"Profile '{profile_name}' not found in ~/.aws/config")

    # Extract SSO session name
    sso_session = config[profile_section].get('sso_session')
    if not sso_session:
        raise Exception(f"Profile '{profile_name}' missing 'sso_session' field")

    # Find SSO session section
    sso_session_section = f"sso-session {sso_session}"
    if sso_session_section not in config:
        raise Exception(f"SSO session '{sso_session}' not found in ~/.aws/config")

    # Extract SSO session configuration
    sso_region = config[sso_session_section].get('sso_region')
    sso_start_url = config[sso_session_section].get('sso_start_url')
    sso_registration_scopes = config[sso_session_section].get('sso_registration_scopes', 'sso:account:access')

    if not sso_region or not sso_start_url:
        raise Exception(f"SSO session '{sso_session}' missing required fields (sso_region, sso_start_url)")

    # Extract account ID and role name
    sso_account_id = config[profile_section].get('sso_account_id')
    sso_role_name = config[profile_section].get('sso_role_name')

    return {
        'sso_session': sso_session,
        'sso_region': sso_region,
        'sso_start_url': sso_start_url,
        'sso_account_id': sso_account_id,
        'sso_role_name': sso_role_name,
        'sso_registration_scopes': sso_registration_scopes.split(',') if isinstance(sso_registration_scopes, str) else [sso_registration_scopes]
    }


def find_client_registration(sso_region: str, sso_start_url: str) -> Optional[Dict]:
    """
    Find existing client registration in botocore cache.

    AWS CLI stores client registration as:
    ~/.aws/sso/cache/botocore-client-id-{region}-{hash}.json

    Args:
        sso_region: AWS region for SSO
        sso_start_url: SSO portal URL

    Returns:
        dict with clientId, clientSecret, registrationExpiresAt or None
    """
    # Compute hash of SSO start URL (same as AWS CLI)
    url_hash = hashlib.sha1(sso_start_url.encode('utf-8')).hexdigest()

    # Look for registration file
    registration_file = Path(SSO_CACHE_DIR) / f"botocore-client-id-{sso_region}-{url_hash}.json"

    if not registration_file.exists():
        logger.info(f"No client registration found at {registration_file}")
        return None

    try:
        with open(registration_file, 'r') as f:
            registration_data = json.load(f)

        # Check if registration is expired
        if 'registrationExpiresAt' in registration_data:
            expires_at = datetime.fromisoformat(registration_data['registrationExpiresAt'].replace('Z', '+00:00'))
            if datetime.now(expires_at.tzinfo) >= expires_at:
                logger.info("Client registration expired")
                return None

        # Validate required fields
        if 'clientId' not in registration_data or 'clientSecret' not in registration_data:
            logger.warning("Client registration missing required fields")
            return None

        logger.info("Found valid client registration")
        return registration_data

    except Exception as e:
        logger.warning(f"Error reading client registration: {str(e)}")
        return None


def register_sso_client(sso_region: str, sso_start_url: str, scopes: list) -> Dict:
    """
    Register SSO-OIDC client with AWS.

    Args:
        sso_region: AWS region for SSO
        sso_start_url: SSO portal URL
        scopes: List of OAuth scopes (e.g., ['sso:account:access'])

    Returns:
        dict with clientId, clientSecret, clientIdIssuedAt,
             clientSecretExpiresAt, registrationExpiresAt

    Raises:
        Exception on API or network errors
    """
    try:
        logger.info(f"Registering new SSO-OIDC client in region {sso_region}")

        # Create SSO-OIDC client
        oidc_client = boto3.client('sso-oidc', region_name=sso_region)

        # Register client
        response = oidc_client.register_client(
            clientName='botocore-client-bazel-proxy',
            clientType='public',
            scopes=scopes
        )

        # Calculate registration expiration
        client_secret_expires_at = response.get('clientSecretExpiresAt', 0)
        registration_expires_at = datetime.fromtimestamp(client_secret_expires_at).isoformat() + 'Z'

        registration_data = {
            'clientId': response['clientId'],
            'clientSecret': response['clientSecret'],
            'clientIdIssuedAt': response.get('clientIdIssuedAt'),
            'clientSecretExpiresAt': client_secret_expires_at,
            'registrationExpiresAt': registration_expires_at
        }

        # Cache registration
        try:
            url_hash = hashlib.sha1(sso_start_url.encode('utf-8')).hexdigest()
            registration_file = Path(SSO_CACHE_DIR) / f"botocore-client-id-{sso_region}-{url_hash}.json"
            Path(SSO_CACHE_DIR).mkdir(parents=True, exist_ok=True)

            with open(registration_file, 'w') as f:
                json.dump(registration_data, f)
            logger.info(f"Cached client registration to {registration_file}")
        except Exception as e:
            logger.warning(f"Could not cache client registration: {str(e)}")

        return registration_data

    except ClientError as e:
        error_msg = f"AWS API error during client registration: {e.response['Error']['Message']}"
        logger.error(error_msg)
        raise Exception(error_msg)
    except Exception as e:
        error_msg = f"Network error during client registration: {str(e)}"
        logger.error(error_msg)
        raise Exception(error_msg)


def clear_notification_file():
    """Remove login notification file if it exists."""
    try:
        notification_path = Path(LOGIN_NOTIFICATION_FILE)
        if notification_path.exists():
            notification_path.unlink()
            logger.info("Cleared login notification file")
    except Exception as e:
        logger.warning(f"Could not clear notification file: {str(e)}")


def refresh_sso_token() -> bool:
    """
    Attempt to refresh SSO access token using refresh token.

    Returns:
        True if refresh successful, False otherwise
    """
    try:
        # Find current SSO token file
        token_file = find_sso_token_file()
        if not token_file:
            logger.info("No token file found for refresh")
            return False

        logger.info(f"Using token file: {token_file}")

        # Read token data
        with open(token_file, 'r') as f:
            token_data = json.load(f)

        # Log token file contents (excluding sensitive data)
        token_keys = list(token_data.keys())
        logger.info(f"Token file keys: {token_keys}")

        # Log expiration for debugging
        if 'expiresAt' in token_data:
            logger.info(f"Access token expires at: {token_data['expiresAt']}")

        # Check if refresh token exists
        refresh_token = token_data.get('refreshToken')
        if not refresh_token:
            logger.info(f"No refresh token available in token file: {token_file}")
            logger.info("This may indicate SSO session not configured properly")
            return False

        # Get SSO session config
        try:
            sso_config = get_sso_session_config(AWS_PROFILE)
        except Exception as e:
            logger.error(f"Error getting SSO configuration: {str(e)}")
            return False

        # Check if token file has embedded client credentials
        token_client_id = token_data.get('clientId')
        token_client_secret = token_data.get('clientSecret')

        # Find or register client
        client_registration = find_client_registration(
            sso_config['sso_region'],
            sso_config['sso_start_url']
        )

        # Prefer token's embedded client if available (AWS CLI does this)
        if token_client_id and token_client_secret:
            logger.info(f"Using client credentials from token file: {token_client_id}")
            client_registration = {
                'clientId': token_client_id,
                'clientSecret': token_client_secret
            }
        elif not client_registration:
            logger.info("No client registration found, registering new client")
            client_registration = register_sso_client(
                sso_config['sso_region'],
                sso_config['sso_start_url'],
                sso_config['sso_registration_scopes']
            )
        else:
            logger.info(f"Using cached client registration: {client_registration['clientId']}")

        # Create SSO-OIDC client and refresh token
        logger.info("Attempting to refresh SSO token")
        oidc_client = boto3.client('sso-oidc', region_name=sso_config['sso_region'])

        response = oidc_client.create_token(
            grantType='refresh_token',
            clientId=client_registration['clientId'],
            clientSecret=client_registration['clientSecret'],
            refreshToken=refresh_token
        )

        # Extract new tokens
        new_access_token = response['accessToken']
        new_refresh_token = response.get('refreshToken', refresh_token)  # May be same or rotated
        expires_in = response['expiresIn']

        # Calculate new expiration time
        new_expires_at = (datetime.now() + timedelta(seconds=expires_in)).isoformat() + 'Z'

        # Update token file with new values
        token_data['accessToken'] = new_access_token
        token_data['refreshToken'] = new_refresh_token
        token_data['expiresAt'] = new_expires_at

        try:
            with open(token_file, 'w') as f:
                json.dump(token_data, f, indent=2)
            logger.info(f"Token refresh successful, new expiration: {new_expires_at}")
        except Exception as e:
            error_msg = f"Failed to write updated token to cache: {str(e)}"
            logger.error(error_msg)
            raise Exception(error_msg)

        # Clear notification file
        clear_notification_file()

        return True

    except ClientError as e:
        error_code = e.response['Error']['Code']
        if error_code == 'InvalidGrantException':
            logger.warning("Refresh token invalid or expired")
        else:
            logger.warning(f"AWS API error during token refresh: {e.response['Error']['Message']}")
        return False
    except Exception as e:
        logger.warning(f"Error during token refresh: {str(e)}")
        return False


def perform_sso_login():
    """Create a notification file that informs the user to run AWS SSO login."""
    try:
        logger.info("Creating login notification file")
        
        # Get SSO configuration
        config = configparser.ConfigParser()
        config.read(os.path.expanduser('~/.aws/config'))
        
        profile_section = f"profile {AWS_PROFILE}"
        if profile_section not in config:
            profile_section = AWS_PROFILE  # Try without the "profile " prefix
        
        if profile_section in config:
            sso_start_url = config[profile_section].get("sso_start_url", "unknown")
            sso_region = config[profile_section].get("sso_region", "unknown")
        else:
            sso_start_url = "unknown"
            sso_region = "unknown"
            
        # Create notification message
        message = f"""
===========================================================================
AWS SSO LOGIN REQUIRED

Your AWS SSO refresh token has expired. Please run:

    aws sso login --profile {AWS_PROFILE}

After successful login, the system will automatically refresh tokens
for approximately {RENEWAL_THRESHOLD // 3600} hours before this is needed again.

SSO Start URL: {sso_start_url}
SSO Region: {sso_region}
===========================================================================
"""
        
        # Write notification file
        Path(os.path.dirname(LOGIN_NOTIFICATION_FILE)).mkdir(parents=True, exist_ok=True)
        with open(LOGIN_NOTIFICATION_FILE, 'w') as f:
            f.write(message)
            
        logger.info(f"Login notification created at {LOGIN_NOTIFICATION_FILE}")
        return True
    except Exception as e:
        logger.error(f"Error creating login notification: {str(e)}")
        return False

def check_token_expiration():
    """Check token expiration and attempt refresh if needed."""
    token_file = find_sso_token_file()
    if not token_file:
        logger.info("No token file found, manual login required")
        return True

    try:
        with open(token_file, 'r') as f:
            token_data = json.load(f)

        # Check if the token has an expiration time
        if 'expiresAt' not in token_data:
            logger.warning("Token file missing expiresAt")
            return True

        expires_at = datetime.fromisoformat(token_data['expiresAt'].replace('Z', '+00:00'))
        now = datetime.now(expires_at.tzinfo)
        time_until_expiry = (expires_at - now).total_seconds()

        logger.info(f"Token expires in {time_until_expiry:.0f} seconds")

        if time_until_expiry < RENEWAL_THRESHOLD:
            logger.info(f"Token expiring in {time_until_expiry:.0f}s, attempting auto-refresh")

            # ATTEMPT AUTO-REFRESH BEFORE NOTIFICATION
            if refresh_sso_token():
                logger.info("Token refresh successful, no manual login needed")
                return False  # No notification
            else:
                logger.warning("Token refresh failed, manual login required")
                return True  # Create notification
        else:
            logger.info(f"Token valid for {time_until_expiry:.0f}s")
            return False

    except Exception as e:
        logger.error(f"Error checking token expiration: {str(e)}")
        return True

def main():
    """Main function to periodically check and renew AWS SSO credentials."""
    logger.info(f"Starting credential renewal service for profile: {AWS_PROFILE}")
    logger.info(f"Checking every {CHECK_INTERVAL} seconds")
    logger.info(f"Renewal threshold: {RENEWAL_THRESHOLD} seconds before expiration")
    
    while True:
        try:
            # Check if token needs renewal
            if check_token_expiration():
                perform_sso_login()
            
            # Sleep until next check
            time.sleep(CHECK_INTERVAL)
        except Exception as e:
            logger.error(f"Error in renewal cycle: {str(e)}")
            time.sleep(60)  # Sleep for a minute on error

if __name__ == "__main__":
    main()
