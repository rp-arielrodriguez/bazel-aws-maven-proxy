#!/bin/bash
# check_login.sh - Check if login is required and prompt the user

# Configuration
LOGIN_NOTIFICATION_FILE="./data/login_required.txt"
CHECK_INTERVAL=60  # seconds

echo "Starting AWS SSO login notification checker..."
echo "This script will check periodically if AWS SSO login is required."
echo "Press Ctrl+C to exit."

while true; do
    if [ -f "$LOGIN_NOTIFICATION_FILE" ] && [ -s "$LOGIN_NOTIFICATION_FILE" ]; then
        echo -e "\n===== AWS SSO LOGIN REQUIRED ====="
        cat "$LOGIN_NOTIFICATION_FILE"

        read -p "Would you like to login now? (y/n): " choice
        if [[ "$choice" =~ ^[Yy]$ ]]; then
            ./login.sh
        else
            echo "You can run './login.sh' later when you're ready."
        fi
    fi

    sleep $CHECK_INTERVAL
done
