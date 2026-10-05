#!/bin/sh
# Publish the RSVP reader inside the tailnet only (tailscale serve, NOT Funnel): reachable from your
# own devices at https://<pi-name>.<tailnet>.ts.net:8443, invisible from the internet.
# The config persists in tailscaled and survives reboots. Idempotent.
set -eu
PORT=$(sed -n 's/^reader_listen_port *= *\([0-9]*\).*/\1/p' /etc/newsrelay/config.toml 2>/dev/null)
PORT=${PORT:-8788}
tailscale serve --bg --https=8443 "http://127.0.0.1:$PORT"
tailscale serve status
echo
echo "Tip: set reader_url = \"https://<this host>:8443\" in /etc/newsrelay/config.toml to link it under the PDF."
