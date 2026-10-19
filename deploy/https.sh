#!/usr/bin/env bash
# HTTPS for the Shortcuts/Watch API: Caddy (auto Let's Encrypt) → bot on 127.0.0.1:8787.
# Uses <ip-with-dashes>.sslip.io so no domain is needed. Also opens 80/443 in the VM's iptables.
# Oracle's VCN security list must ALSO allow TCP 80 and 443 (console step, see AGENTS.md §7).
#   BUTLER_HOST=ubuntu@<ip> ./deploy/https.sh
set -euo pipefail
HOST="${BUTLER_HOST:?set BUTLER_HOST=ubuntu@<server-ip> (e.g. in your shell profile)}"
KEY="${BUTLER_KEY:-$HOME/.ssh/butler_vm}"
IP="${HOST#*@}"
DOMAIN="${BUTLER_DOMAIN:-${IP//./-}.sslip.io}"
REMOTE_SCRIPT="$(dirname "$0")/https-remote.sh"

ssh -i "$KEY" "$HOST" "DOMAIN=$DOMAIN bash -s" < "$REMOTE_SCRIPT"
