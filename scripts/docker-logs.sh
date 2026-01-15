#!/usr/bin/env bash

# Trap SIGINT (Ctrl+C) and SIGTERM to exit gracefully
trap 'exit 0' INT TERM

# Follow docker-compose logs
docker-compose logs -f &
wait $!
