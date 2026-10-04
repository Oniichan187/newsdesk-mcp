#!/bin/sh
# /usr/local/bin/newsrelay — runs DB-touching commands as the service user, secret commands as root.
BIN=/opt/newsrelay/current/venv/bin/newsrelay
export NEWSRELAY_CONFIG=${NEWSRELAY_CONFIG:-/etc/newsrelay/config.toml}
case "${1:-}" in
  set-webhook|set-bot-token|set-passphrase|test-webhook|test-discord|healthcheck)
    [ "$(id -u)" = 0 ] || exec sudo NEWSRELAY_CONFIG="$NEWSRELAY_CONFIG" "$BIN" "$@"
    exec "$BIN" "$@" ;;
esac
if [ "$(id -u)" = 0 ]; then
  exec runuser -u newsrelay -- env NEWSRELAY_CONFIG="$NEWSRELAY_CONFIG" "$BIN" "$@"
fi
if [ "$(id -un)" != newsrelay ]; then
  exec sudo -u newsrelay NEWSRELAY_CONFIG="$NEWSRELAY_CONFIG" "$BIN" "$@"
fi
exec "$BIN" "$@"
