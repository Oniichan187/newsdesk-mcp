#!/bin/sh
# Make the RSVP reader public via Cloudflare Tunnel (frontend only; the MCP backend keeps using
# Tailscale Funnel). Installs cloudflared from Cloudflare's apt repository if needed, then enables
# newsrelay-tunnel.service. Idempotent. Run as root: sudo sh scripts/expose-reader-public.sh
#
# Without /etc/newsrelay/tunnel.env a quick tunnel is used (no account; address changes when the
# tunnel restarts — the relay always links the current one). For a fixed address create a tunnel in
# the Cloudflare dashboard (public hostname -> http://127.0.0.1:8788), then:
#   printf 'TUNNEL_TOKEN=%s\n' '<token>' > /etc/newsrelay/tunnel.env && chmod 600 /etc/newsrelay/tunnel.env
# and set reader_url = "https://<your hostname>" in /etc/newsrelay/config.toml.
set -eu
[ "$(id -u)" = 0 ] || { echo "run as root (sudo sh $0)" >&2; exit 1; }
APP=/opt/newsrelay/current/src

if ! command -v cloudflared >/dev/null; then
  mkdir -p --mode=0755 /usr/share/keyrings
  curl -fsSL https://pkg.cloudflare.com/cloudflare-public-v2.gpg -o /usr/share/keyrings/cloudflare-public-v2.gpg
  echo "deb [signed-by=/usr/share/keyrings/cloudflare-public-v2.gpg] https://pkg.cloudflare.com/cloudflared any main" \
    > /etc/apt/sources.list.d/cloudflared.list
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq cloudflared
fi
cloudflared --version

install -m 0644 -o root -g root "$APP/systemd/newsrelay-tunnel.service" /etc/systemd/system/newsrelay-tunnel.service
systemctl daemon-reload
systemctl enable --quiet newsrelay-tunnel.service
systemctl restart newsrelay-tunnel.service

i=0
while [ $i -lt 30 ]; do
  if curl -fsS -m 3 http://127.0.0.1:20241/ready >/dev/null 2>&1; then break; fi
  i=$((i + 1)); sleep 2
done
HOST=$(curl -fsS -m 3 http://127.0.0.1:20241/quicktunnel 2>/dev/null \
  | python3 -c 'import json,sys; print(json.load(sys.stdin).get("hostname",""))' 2>/dev/null || true)
if [ -n "$HOST" ]; then
  echo "public reader: https://$HOST"
else
  echo "named tunnel (or not ready yet): see 'journalctl -u newsrelay-tunnel' and your Cloudflare hostname"
fi
