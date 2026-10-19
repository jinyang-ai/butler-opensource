#!/usr/bin/env bash
# One-time setup of a fresh Ubuntu 24.04 VM (Oracle Always Free or any other) to run butler as a service.
#   BUTLER_HOST=ubuntu@<ip> ./deploy/provision-oracle.sh
# Then deploy code + secrets with ./deploy/oracle.sh (same BUTLER_HOST).
set -euo pipefail
HOST="${BUTLER_HOST:?set BUTLER_HOST=ubuntu@<public-ip>}"
KEY="${BUTLER_KEY:-$HOME/.ssh/butler_vm}"
TZ_NAME="${BUTLER_TZ:-Asia/Kolkata}"

ssh -i "$KEY" -o StrictHostKeyChecking=accept-new "$HOST" "TZ_NAME=$TZ_NAME bash -s" <<'REMOTE'
set -euo pipefail
sudo timedatectl set-timezone "$TZ_NAME"
# 1 GB VMs need swap for dependency installs
if ! swapon --show | grep -q swapfile; then
  sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap -q /swapfile && sudo swapon /swapfile
  echo "/swapfile none swap sw 0 0" | sudo tee -a /etc/fstab >/dev/null
fi
command -v ~/.local/bin/uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
mkdir -p ~/butler ~/butler-data
sudo tee /etc/systemd/system/butler.service >/dev/null <<EOF
[Unit]
Description=butler Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=/home/$USER/butler
Environment=TUYA_TRANSPORT=cloud
Environment=ENERGY_POLL_SECONDS=300
Environment=DATA_DIR=/home/$USER/butler-data
ExecStart=/home/$USER/.local/bin/uv run --no-dev butler serve
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable butler -q
echo "provisioned: $(lsb_release -ds), tz $(timedatectl show -p Timezone --value)"
REMOTE
echo "Next: BUTLER_HOST=$HOST ./deploy/oracle.sh"
