# credential-monitor/monitor.py
import os
import time
import logging
import subprocess
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('credential-monitor')

# Configuration
AWS_PROFILE = os.environ.get('AWS_PROFILE', 'default')
AWS_DIR = os.path.expanduser('~/.aws')
CREDENTIAL_FILE = os.path.join(AWS_DIR, 'credentials')
CONFIG_FILE = os.path.join(AWS_DIR, 'config')
SSO_CACHE_DIR = os.path.join(AWS_DIR, 'sso/cache')
LOGIN_NOTIFICATION_FILE = '/app/data/login_required.txt'

class CredentialEventHandler(FileSystemEventHandler):
    """Handles file system events related to AWS credentials."""
    
    def __init__(self):
        self.last_event_time = 0
        self.cooldown_period = 5  # seconds
    
    def on_created(self, event):
        """Called when a file is created."""
        # Only restart on login notification file creation (refresh token expired)
        if event.is_directory:
            return

        if event.src_path == LOGIN_NOTIFICATION_FILE:
            logger.warning(f"Refresh token expired - manual login required")
            logger.info(f"Detected notification file: {event.src_path}")
            self._restart_s3proxy()

    def on_modified(self, event):
        """Called when a file or directory is modified."""
        # Apply cooldown to prevent multiple restarts for related changes
        current_time = time.time()
        if current_time - self.last_event_time < self.cooldown_period:
            logger.debug(f"Ignoring event due to cooldown: {event.src_path}")
            return

        self.last_event_time = current_time

        # Process the event
        if event.is_directory:
            return

        # Only restart on manual credential changes (not automatic token refresh)
        # S3proxy auto-detects refreshed tokens every REFRESH_INTERVAL (5 min)
        if event.src_path == CREDENTIAL_FILE or event.src_path == CONFIG_FILE:
            logger.info(f"Detected manual change in AWS config: {event.src_path}")
            self._restart_s3proxy()
    
    def _restart_s3proxy(self):
        """Restart the S3 proxy container via docker stop (compose will auto-restart)."""
        logger.info("Stopping s3proxy container (compose will restart)...")
        try:
            subprocess.run(
                ["docker", "stop", "bazel-s3-proxy"],
                check=True
            )
            logger.info("Successfully stopped s3proxy container")
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to stop s3proxy: {e}")

def start_monitoring():
    """Start monitoring AWS credential files for changes."""
    # Create handler and observer
    event_handler = CredentialEventHandler()
    observer = Observer()

    # Set up paths to watch
    paths_to_watch = [
        CREDENTIAL_FILE,
        CONFIG_FILE,
        LOGIN_NOTIFICATION_FILE  # Watch for refresh token expiration
    ]

    # Schedule monitoring
    for path in paths_to_watch:
        parent_dir = os.path.dirname(path)
        if os.path.exists(parent_dir):
            observer.schedule(event_handler, parent_dir, recursive=False)
            logger.info(f"Monitoring file: {path}")
        else:
            logger.warning(f"Parent directory does not exist: {parent_dir}")
    
    # Start the observer
    observer.start()
    logger.info(f"Started credential monitoring for profile: {AWS_PROFILE}")
    
    try:
        # Keep running
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()

if __name__ == "__main__":
    start_monitoring()
