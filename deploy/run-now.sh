#!/usr/bin/env bash
# Run one search on the server immediately, even outside active hours.
set -euo pipefail
source "$(dirname "$0")/common.sh"

ssh "${SSH_OPTS[@]}" "$DEPLOY_HOST" "cd ~/$APP_NAME && PYTHONPATH=src flock -n /tmp/jobbot.lock .venv/bin/python -m jobbot run --force; echo \"Exit code: \$?\"; tail -25 data/jobbot.log"
