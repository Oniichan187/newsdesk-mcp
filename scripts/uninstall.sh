#!/bin/sh
# Remove services and code. Database, backups, config and secrets are KEPT unless
# --purge --yes-delete-data is given. Funnel exposure is removed only with --unexpose.
set -eu
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
PURGE=0; CONFIRM=0; UNEXPOSE=0
for a in "$@"; do
  case "$a" in
    --purge) PURGE=1 ;;
    --yes-delete-data) CONFIRM=1 ;;
    --unexpose) UNEXPOSE=1 ;;
    *) echo "unknown option $a" >&2; exit 2 ;;
  esac
done
UNITS="newsrelay-api.service newsrelay-worker.service newsrelay-maintenance.timer newsrelay-maintenance.service newsrelay-healthcheck.timer newsrelay-healthcheck.service"
systemctl disable --now $UNITS 2>/dev/null || true
for u in $UNITS; do rm -f "/etc/systemd/system/$u"; done
systemctl daemon-reload
rm -f /usr/local/bin/newsrelay
rm -rf /opt/newsrelay
if [ $UNEXPOSE -eq 1 ]; then tailscale funnel --https=443 off 2>/dev/null || true; fi
if [ $PURGE -eq 1 ]; then
  if [ $CONFIRM -ne 1 ]; then
    echo "--purge needs --yes-delete-data as well (deletes DB, backups, config, secrets)" >&2; exit 2
  fi
  rm -rf /var/lib/newsrelay /etc/newsrelay
  userdel newsrelay 2>/dev/null || true
  echo "purged all newsrelay data"
else
  echo "services removed; kept /var/lib/newsrelay (DB + backups) and /etc/newsrelay (config + secrets)"
fi
