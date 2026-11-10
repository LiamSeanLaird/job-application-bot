#!/usr/bin/env bash
# Show whether the job bot is installed and what it last did. Prints no secrets.
set -uo pipefail
source "$(dirname "$0")/common.sh"

ssh "${SSH_OPTS[@]}" "$DEPLOY_HOST" APP_NAME="$APP_NAME" bash -s <<'REMOTE'
cd ~/"$APP_NAME" 2>/dev/null || { echo "Not installed: ~/$APP_NAME is missing"; exit 0; }
echo "Server time: $(date '+%Y-%m-%d %H:%M %Z')"
for path in src/jobbot/cli.py .venv/bin/python .env profile/settings.toml profile/target_role.md .secrets/google_client.json .secrets/google_token.json; do
  [[ -e "$path" ]] && echo "OK       $path" || echo "MISSING  $path"
done
grep -Eq '^OPENAI_KEY=.+' .env 2>/dev/null && echo "OK       OPENAI_KEY is set" || echo "MISSING  OPENAI_KEY value"
echo "Scheduled entries: $(crontab -l 2>/dev/null | grep -c "$APP_NAME")"
echo "Web app service: $(systemctl is-active jobbot-web 2>/dev/null || echo 'not installed')"
echo "--- Job counts"
sqlite3 data/jobbot.db "select status, count(*) from jobs group by status" 2>/dev/null || echo "No database yet"
echo "--- Last log lines"
tail -25 data/jobbot.log 2>/dev/null || echo "No log yet"
echo "--- Last cron errors"
tail -15 data/cron.log 2>/dev/null || echo "None"
REMOTE
