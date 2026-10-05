#!/bin/sh
# Publish the relay via Tailscale Funnel (public HTTPS, Tailscale-managed certificate).
# Everything behind it requires OAuth except /healthz ({"ok":bool}) and OAuth metadata/consent pages.
# The config persists in tailscaled and survives reboots. Idempotent.
set -eu
PORT=$(sed -n 's/^listen_port *= *\([0-9]*\).*/\1/p' /etc/newsrelay/config.toml 2>/dev/null)
PORT=${PORT:-8787}
tailscale funnel --bg --https=443 "http://127.0.0.1:$PORT"
tailscale funnel status
