#!/usr/bin/env bash
# Push the current code (and local .env / plugs.json / tinytuya.json) to the Oracle VM and restart the bot.
#   ./deploy/oracle.sh          deploy
#   ./deploy/oracle.sh logs     follow logs
set -euo pipefail
HOST="${BUTLER_HOST:?set BUTLER_HOST=ubuntu@<server-ip> (e.g. in your shell profile)}"
KEY="${BUTLER_KEY:-$HOME/.ssh/butler_vm}"
SSH=(ssh -i "$KEY" "$HOST")
cd "$(dirname "$0")/.."

if [[ "${1:-}" == "logs" ]]; then
  exec "${SSH[@]}" 'journalctl -u butler -f -n 50 -o cat'
fi

rsync -az -e "ssh -i $KEY" --delete \
  --exclude .venv --exclude .git --exclude logs --exclude __pycache__ --exclude .pytest_cache \
  --exclude snapshot.json --exclude devices.json --exclude tuya-raw.json \
  ./ "$HOST:~/butler/"
"${SSH[@]}" 'cd ~/butler && chmod 600 .env tinytuya.json plugs.json && ~/.local/bin/uv sync --no-dev -q && sudo systemctl restart butler && sleep 5 && systemctl is-active butler'
