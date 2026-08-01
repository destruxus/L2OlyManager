#!/usr/bin/env bash
#
# One-shot deploy for the L2 Grand Olympiad bot on a DigitalOcean droplet
# (Ubuntu/Debian). Run it FROM INSIDE the project directory, as root or with
# sudo:
#
#   cd ~/L2OlyManager        # wherever you put the project on the droplet
#   sudo bash deploy.sh
#
# It installs the bot to /opt/l2-olympiad-bot, creates a locked-down service
# user, sets up the Python venv, installs dependencies and registers the
# systemd service so the bot restarts on crash/reboot.

set -euo pipefail

APP_DIR="/opt/l2-olympiad-bot"
SERVICE_USER="olympiad"
SRC_DIR="$(pwd)"

echo "==> Source: $SRC_DIR"

# 0. Must have config.json (holds your token) before we start.
if [[ ! -f "$SRC_DIR/config.json" ]]; then
  echo "ERROR: config.json not found in $SRC_DIR."
  echo "       Copy config.example.json to config.json and fill in token + guild_id first."
  exit 1
fi

# 1. System packages.
echo "==> Installing python3-venv..."
apt-get update -y >/dev/null
apt-get install -y python3-venv python3-pip >/dev/null

# 2. Service user (no login shell, no home clutter).
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  echo "==> Creating service user '$SERVICE_USER'..."
  useradd -r -s /usr/sbin/nologin -d "$APP_DIR" "$SERVICE_USER"
fi

# 3. Copy project into place (exclude venv/db/cache from the source).
echo "==> Copying project to $APP_DIR..."
mkdir -p "$APP_DIR"
rsync -a --delete \
  --exclude '.venv' --exclude '__pycache__' --exclude '*.db' --exclude '.git' \
  "$SRC_DIR"/ "$APP_DIR"/

# 4. Python virtual environment + dependencies.
echo "==> Building virtualenv + installing dependencies..."
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip >/dev/null
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

# 5. Ownership: the service user owns its dir (so it can write olympiad.db).
chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR"

# 6. systemd service.
echo "==> Installing systemd service..."
cp "$APP_DIR/l2-olympiad.service" /etc/systemd/system/l2-olympiad.service
systemctl daemon-reload
systemctl enable --now l2-olympiad

echo
echo "==> Done. Bot service is running."
echo "    Status: systemctl status l2-olympiad --no-pager"
echo "    Logs:   journalctl -u l2-olympiad -f"
echo
echo "Next: in Discord, run /setup once, then post a score in a class points thread."
