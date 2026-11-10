#!/usr/bin/env bash
# Copy the job bot and your profile to an Ubuntu server, schedule it, and serve the web app. Safe to run repeatedly.
# It needs sudo, nginx and certbot on the server. Other apps' crontab entries and nginx sites are left alone,
# and server data is never deleted.
set -euo pipefail
source "$(dirname "$0")/common.sh"

for required in .env .secrets/google_client.json .secrets/google_token.json profile/settings.toml profile/target_role.md; do
  [[ -f "$required" ]] || { echo "Missing $required (see README.md)"; exit 1; }
done
grep -Eq '^OPENAI_KEY=.+' .env || { echo "OPENAI_KEY is empty in .env"; exit 1; }
if ! grep -Eq '^WEB_PASSWORD=.+' .env; then
  echo "WEB_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-24)" >> .env
  echo "Generated a password for the web app and saved it in .env as WEB_PASSWORD"
fi
# Without a domain of your own, sslip.io gives the server a free hostname from its IP, such as 203-0-113-10.sslip.io.
if ! grep -Eq '^APP_HOSTNAME=.+' .env; then
  SERVER_IP="${DEPLOY_HOST##*@}"
  [[ "$SERVER_IP" =~ ^[0-9]+(\.[0-9]+){3}$ ]] || { echo "Set APP_HOSTNAME in .env to the server's hostname"; exit 1; }
  echo "APP_HOSTNAME=${SERVER_IP//./-}.sslip.io" >> .env
fi
APP_HOSTNAME="$(env_value APP_HOSTNAME)"
WEB_USERNAME="$(env_value WEB_USERNAME)"
WEB_USERNAME="${WEB_USERNAME:-jobbot}"

SSH_COMMAND="ssh ${SSH_OPTS[*]}"
HOST="$DEPLOY_HOST"

echo "Copying code"
ssh "${SSH_OPTS[@]}" "$HOST" "mkdir -p ~/$APP_NAME/.secrets ~/$APP_NAME/data/backups && chmod 700 ~/$APP_NAME/.secrets"
rsync -az --delete -e "$SSH_COMMAND" \
  --exclude .git --exclude .venv --exclude data --exclude corpus --exclude profile/cv-tailoring \
  --exclude .secrets --exclude .env --exclude __pycache__ --exclude .DS_Store \
  ./ "$HOST:~/$APP_NAME/"

echo "Copying secrets"
scp -q "${SSH_OPTS[@]}" .env "$HOST:~/$APP_NAME/.env"
scp -q "${SSH_OPTS[@]}" .secrets/google_client.json .secrets/google_token.json "$HOST:~/$APP_NAME/.secrets/"

echo "Installing on the server"
ssh "${SSH_OPTS[@]}" "$HOST" APP_NAME="$APP_NAME" HOST_NAME="$APP_HOSTNAME" WEB_USERNAME="$WEB_USERNAME" bash -s <<'REMOTE'
set -euo pipefail
APP_DIR="$HOME/$APP_NAME"
cd "$APP_DIR"
chmod 600 .env .secrets/*

if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv || { echo "Run: sudo apt-get install -y python3-venv"; exit 1; }
fi
.venv/bin/pip install --quiet --upgrade -r requirements.txt

MARKER="# job-application-bot"
RUN="cd $APP_DIR && PYTHONPATH=src .venv/bin/python -m jobbot run >> data/cron.log 2>&1"
BACKUP="sqlite3 $APP_DIR/data/jobbot.db \".backup '$APP_DIR/data/backups/jobbot-\$(date -u +\\%Y\\%m\\%d).db'\" && find $APP_DIR/data/backups -name 'jobbot-*.db' -mtime +14 -delete"
{
  crontab -l 2>/dev/null | grep -vF "$MARKER" || true
  echo "*/30 * * * * flock -n /tmp/jobbot.lock sh -c '$RUN' $MARKER"
  echo "25 3 * * * $BACKUP $MARKER"
} | crontab -
echo "Scheduled. Current job bot entries:"
crontab -l | grep -F "$MARKER"

# Web app: a systemd service on localhost, behind nginx with HTTPS and a password.
sudo tee /etc/systemd/system/jobbot-web.service > /dev/null <<UNIT
[Unit]
Description=Job tracker web app
After=network.target

[Service]
User=$USER
WorkingDirectory=$APP_DIR
Environment=PYTHONPATH=src
ExecStart=$APP_DIR/.venv/bin/python -m jobbot web
Restart=always
RestartSec=5
MemoryMax=100M

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --quiet jobbot-web
sudo systemctl restart jobbot-web

# The password file is rewritten from .env on every deploy, so changing WEB_PASSWORD there changes the login.
WEB_PASSWORD="$(grep -E '^WEB_PASSWORD=' .env | cut -d= -f2-)"
printf '%s:%s\n' "$WEB_USERNAME" "$(printf '%s' "$WEB_PASSWORD" | openssl passwd -6 -stdin)" | sudo tee /etc/nginx/job-tracker.htpasswd > /dev/null
sudo chown root:www-data /etc/nginx/job-tracker.htpasswd
sudo chmod 640 /etc/nginx/job-tracker.htpasswd

# The site is created once. Certbot then adds HTTPS to it and renews the certificate on its existing timer.
if [[ ! -f /etc/nginx/sites-available/job-tracker ]]; then
  sudo tee /etc/nginx/sites-available/job-tracker > /dev/null <<SITE
server {
    listen 80;
    server_name $HOST_NAME;

    location / {
        auth_basic "Job tracker";
        auth_basic_user_file /etc/nginx/job-tracker.htpasswd;
        proxy_pass http://127.0.0.1:8090;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
SITE
  sudo ln -sf /etc/nginx/sites-available/job-tracker /etc/nginx/sites-enabled/job-tracker
  sudo nginx -t
  sudo systemctl reload nginx
  sudo certbot --nginx -d "$HOST_NAME" --non-interactive --agree-tos --redirect
fi
# Adding a pasted job can take a minute while the AI reads it, longer than nginx's default 60 seconds.
if ! sudo grep -q proxy_read_timeout /etc/nginx/sites-available/job-tracker; then
  sudo sed -i 's|proxy_pass http://127.0.0.1:8090;|proxy_pass http://127.0.0.1:8090;\n        proxy_read_timeout 180s;|' /etc/nginx/sites-available/job-tracker
fi
sudo nginx -t && sudo systemctl reload nginx
echo "Web app: https://$HOST_NAME"
REMOTE

echo "Deployed. Run the first search with ./deploy/run-now.sh, and check it with ./deploy/status.sh"
echo "Log in to https://$APP_HOSTNAME as $WEB_USERNAME, with WEB_PASSWORD from .env"
