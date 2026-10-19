#!/usr/bin/env bash
# Runs ON the VM (piped by deploy/https.sh). Needs $DOMAIN.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
: "${DOMAIN:?DOMAIN not set}"

if ! command -v caddy >/dev/null; then
  sudo apt-get update -qq
  sudo apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl >/dev/null
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | sudo gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  sudo apt-get update -qq
  sudo apt-get install -y -qq caddy >/dev/null
fi

printf '%s {\n\treverse_proxy 127.0.0.1:8787\n}\n' "$DOMAIN" | sudo tee /etc/caddy/Caddyfile >/dev/null

for port in 80 443; do
  sudo iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null \
    || sudo iptables -I INPUT 5 -p tcp --dport "$port" -j ACCEPT
done
if command -v netfilter-persistent >/dev/null; then
  sudo netfilter-persistent save >/dev/null 2>&1
else
  sudo sh -c 'iptables-save > /etc/iptables/rules.v4'
fi

sudo systemctl enable caddy >/dev/null 2>&1
sudo systemctl restart caddy
echo "https://$DOMAIN -> 127.0.0.1:8787 ($(caddy version | cut -d' ' -f1))"
