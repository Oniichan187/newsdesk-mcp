#!/bin/sh
# Upgrade = verified backup first, then the idempotent installer (which rolls back if unhealthy).
# Code is only ever installed from a local, reviewed checkout — never auto-pulled.
set -eu
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
if [ -x /opt/newsrelay/current/venv/bin/newsrelay ]; then
  runuser -u newsrelay -- env NEWSRELAY_CONFIG=/etc/newsrelay/config.toml /opt/newsrelay/current/venv/bin/newsrelay backup
fi
exec "$(dirname "$0")/install.sh"
