#!/usr/bin/env bash
# Shared settings for the deploy scripts, read from .env. Sourced, not run directly.
cd "$(dirname "${BASH_SOURCE[0]}")/.."

env_value() {
  grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- | sed -e "s/^[\"']//" -e "s/[\"']$//"
}

APP_NAME="job-application-bot"
DEPLOY_HOST="${DEPLOY_HOST:-$(env_value DEPLOY_HOST)}"
[[ -n "$DEPLOY_HOST" ]] || { echo "Set DEPLOY_HOST in .env, for example DEPLOY_HOST=ubuntu@203.0.113.10"; exit 1; }

# The SSH key is optional. Without one, ssh uses your normal keys and ~/.ssh/config.
SSH_KEY="${DEPLOY_SSH_KEY:-$(env_value DEPLOY_SSH_KEY)}"
SSH_KEY="${SSH_KEY/#\~/$HOME}"
SSH_OPTS=()
[[ -n "$SSH_KEY" ]] && SSH_OPTS=(-i "$SSH_KEY")
