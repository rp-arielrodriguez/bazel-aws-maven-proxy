# credential-renewer/renewer.py
import os
import time
import json
import logging
import subprocess
import configparser
from pathlib import Path
from datetime import datetime, timedelta

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
    
    # Get the most recently modified file
    latest_file = max(json_files, key=lambda f: f.stat().st_mtime)
    return latest_file


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

Your AWS SSO credentials have expired or will expire soon. Please run the 
following command in a terminal:

    aws sso login --profile {AWS_PROFILE}

This will open a browser window where you can complete the SSO login process.
After successful login, the credential monitor will automatically detect the
new credentials and update the proxy.

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
    """Check if the SSO token is nearing expiration."""
    token_file = find_sso_token_file()
    if not token_file:
        logger.info("No token file found, initiating login")
        return True
    
    try:
        with open(token_file, 'r') as f:
            token_data = json.load(f)
        
        # Check if the token has an expiration time
        if 'expiresAt' in token_data:
            expires_at = datetime.fromisoformat(token_data['expiresAt'].replace('Z', '+00:00'))
            now = datetime.now(expires_at.tzinfo)
            
            # Calculate time until expiration
            time_until_expiry = (expires_at - now).total_seconds()
            
            logger.info(f"Token expires in {time_until_expiry:.0f} seconds")
            
            # If token will expire soon, renew it
            if time_until_expiry < RENEWAL_THRESHOLD:
                logger.info(f"Token will expire soon ({time_until_expiry:.0f}s), initiating renewal")
                return True
            else:
                logger.info(f"Token still valid for {time_until_expiry:.0f} seconds")
                return False
        else:
            logger.warning("Token file does not contain expiration time")
            return True
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
