#!/bin/sh
# Idempotent installer / upgrader for newsrelay. Safe to run repeatedly:
#  - never overwrites secrets, config or the database
#  - builds the new virtualenv beside the old one and rolls back if the health check fails
# Usage: sudo scripts/install.sh            (from the repository root)
set -eu

APP=/opt/newsrelay
ETC=/etc/newsrelay
SECRETS=$ETC/secrets
STATE=/var/lib/newsrelay
USER_NAME=newsrelay
UNITS="newsrelay-api.service newsrelay-worker.service newsrelay-presence.service newsrelay-reader.service newsrelay-maintenance.service newsrelay-maintenance.timer newsrelay-healthcheck.service newsrelay-healthcheck.timer"
SRC=$(cd "$(dirname "$0")/.." && pwd)
TS=$(date -u +%Y%m%dT%H%M%SZ)

say() { printf '[install] %s\n' "$*"; }
die() { printf '[install] ERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run as root (sudo)"
[ -f "$SRC/pyproject.toml" ] && [ -f "$SRC/requirements.lock" ] || die "run from the newsrelay repository"

# --- 1. platform checks -------------------------------------------------------------------------
. /etc/os-release
case "${ID:-}:${ID_LIKE:-}" in
  debian:*|raspbian:*|*:*debian*) ;;
  *) die "unsupported OS ${PRETTY_NAME:-unknown} (Debian/Raspberry Pi OS required)" ;;
esac
command -v systemctl >/dev/null || die "systemd required"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || die "Python >= 3.11 required"
python3 -c 'import sqlite3; c=sqlite3.connect(":memory:"); c.execute("create virtual table t using fts5(x)")' \
  || die "SQLite FTS5 support required"
VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' "$SRC/pyproject.toml")
say "installing newsrelay $VERSION on ${PRETTY_NAME}"

# --- 2. packages (only what is missing) ---------------------------------------------------------
NEED=""
python3 -c 'import venv, ensurepip' 2>/dev/null || NEED="$NEED python3-venv"
command -v sqlite3 >/dev/null || NEED="$NEED sqlite3"
if [ -n "$NEED" ]; then
  say "apt install:$NEED"
  DEBIAN_FRONTEND=noninteractive apt-get install -y -q $NEED >/dev/null
fi

# --- 3. service account + directories -----------------------------------------------------------
if ! id "$USER_NAME" >/dev/null 2>&1; then
  useradd --system --user-group --home-dir "$STATE" --no-create-home --shell /usr/sbin/nologin "$USER_NAME"
  say "created system user $USER_NAME"
fi
install -d -m 0755 -o root -g root "$APP" "$ETC"
install -d -m 0700 -o root -g root "$SECRETS"
install -d -m 0750 -o "$USER_NAME" -g "$USER_NAME" "$STATE" "$STATE/backups"

# --- 4. code + virtualenv in a fresh release directory (old releases stay for rollback) --------
REL=$APP/releases/$VERSION-$TS
NEWBIN=$REL/venv/bin
install -d -m 0755 "$APP/releases"
mkdir -p "$REL/src"
tar -C "$SRC" --exclude=.git --exclude=__pycache__ --exclude='*.db' --exclude=.pytest_cache \
    --exclude=.mypy_cache --exclude=.ruff_cache --exclude=.venv -cf - . | tar -C "$REL/src" -xf -
python3 -m venv "$REL/venv"
# PyPI only, hashes enforced: ignore system pip config (Raspberry Pi OS adds piwheels).
export PIP_CONFIG_FILE=/dev/null PIP_INDEX_URL=https://pypi.org/simple
unset PIP_EXTRA_INDEX_URL
# Only hash-pinned wheels from the lock are installed. The application itself is not "built"
# (that would fetch an unpinned build backend); it is put on the path via a .pth file.
"$NEWBIN/pip" install -q --disable-pip-version-check --no-cache-dir --require-hashes --only-binary=:all: \
    -r "$REL/src/requirements.lock"
SITE=$("$NEWBIN/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
echo "$REL/src/src" > "$SITE/newsrelay-src.pth"
cat > "$NEWBIN/newsrelay" <<EOF
#!$NEWBIN/python3
import sys
from newsrelay.cli import main
sys.exit(main())
EOF
chmod 0755 "$NEWBIN/newsrelay"
"$NEWBIN/python" -m compileall -q "$REL/src/src" >/dev/null || die "new build does not compile"
[ "$("$NEWBIN/newsrelay" version)" = "$VERSION" ] || die "new build does not start"
"$NEWBIN/pip" check >/dev/null || die "dependency conflict in new build"
chown -R root:root "$REL"
chmod -R go-w "$REL"

# --- 5. configuration (never overwrite) ---------------------------------------------------------
if [ ! -f "$ETC/config.toml" ]; then
  DNS=$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)
  [ -n "$DNS" ] && URL="https://$DNS" || URL="http://127.0.0.1:8787"
  sed "s#@PUBLIC_BASE_URL@#$URL#" "$REL/src/config/config.toml.example" > "$ETC/config.toml"
  chmod 0644 "$ETC/config.toml"
  say "wrote $ETC/config.toml (public_base_url=$URL)"
else
  say "keeping existing $ETC/config.toml"
fi
for s in discord_webhook discord_bot_token owner_passphrase_hash; do
  [ -e "$SECRETS/$s" ] || { install -m 0600 -o root -g root /dev/null "$SECRETS/$s"; say "created empty placeholder $SECRETS/$s"; }
done
if [ ! -s "$SECRETS/owner_passphrase_hash" ]; then
  # Generate the OAuth owner passphrase; plaintext kept root-only so it never hits logs/terminals.
  PASS=$("$NEWBIN/python" -c 'import secrets; t=secrets.token_urlsafe(24); print("-".join(t[i:i+8] for i in range(0,24,8)))')
  umask 077
  printf '%s\n' "$PASS" > "$SECRETS/owner_passphrase.txt"
  printf '%s' "$PASS" | "$NEWBIN/python" -c 'import sys; from newsrelay.api.auth import hash_passphrase; print(hash_passphrase(sys.stdin.read()))' > "$SECRETS/owner_passphrase_hash"
  chmod 0600 "$SECRETS/owner_passphrase.txt" "$SECRETS/owner_passphrase_hash"
  unset PASS
  say "generated OAuth owner passphrase -> read it with: sudo cat $SECRETS/owner_passphrase.txt"
fi

# --- 6. safe schema upgrade ----------------------------------------------------------------------
DB=$STATE/newsrelay.db
PREV=$(readlink "$APP/current" 2>/dev/null || true)
as_svc() { runuser -u "$USER_NAME" -- env NEWSRELAY_CONFIG="$ETC/config.toml" "$@"; }
schema_of() { as_svc sqlite3 -readonly "$1" "SELECT COALESCE(MAX(version),0) FROM schema_migrations" 2>/dev/null || echo 0; }
SNAP=""
if [ -f "$DB" ]; then
  OLD_SCHEMA=$(schema_of "$DB")
  # 6a. rehearse the migration on a consistent copy with the NEW code; production untouched
  TRY=$STATE/backups/upgrade-rehearsal-$TS.db
  as_svc sqlite3 "$DB" ".backup '$TRY'"
  if ! as_svc "$NEWBIN/newsrelay" upgrade-check "$TRY" >/dev/null; then
    rm -f "$TRY"; rm -rf "$REL"
    die "migration rehearsal FAILED on a copy; production untouched, still running ${PREV:-nothing}"
  fi
  rm -f "$TRY"
  # 6b. short maintenance window: stop writers, take the final consistent snapshot
  systemctl stop newsrelay-api.service newsrelay-worker.service newsrelay-maintenance.service 2>/dev/null || true
  SNAP=$STATE/backups/pre-upgrade-$TS.db
  as_svc sqlite3 "$DB" ".backup '$SNAP'"
  as_svc sqlite3 "$SNAP" "PRAGMA journal_mode=DELETE" >/dev/null
  [ "$(as_svc sqlite3 -readonly "$SNAP" 'PRAGMA integrity_check')" = "ok" ] || die "pre-upgrade snapshot not intact; aborting"
  say "pre-upgrade snapshot $SNAP (schema $OLD_SCHEMA)"
fi
# 6c. migrate production with the new code
if ! as_svc "$NEWBIN/newsrelay" migrate >/dev/null; then
  say "production migration FAILED"
  ROLLBACK_REASON=migration
else
  ROLLBACK_REASON=""
fi

# --- 7. switch release, units, wrapper; start ----------------------------------------------------
restore_previous() {
  say "ROLLING BACK ($1)"
  systemctl stop newsrelay-api.service newsrelay-worker.service 2>/dev/null || true
  if [ -n "$SNAP" ] && [ "$(schema_of "$DB")" != "$OLD_SCHEMA" -o "$1" = migration ]; then
    install -o "$USER_NAME" -g "$USER_NAME" -m 0600 "$SNAP" "$DB"
    rm -f "$DB-wal" "$DB-shm"
    say "restored database snapshot (schema $OLD_SCHEMA)"
  fi
  if [ -n "$PREV" ] && [ -d "$PREV" ]; then
    ln -sfn "$PREV" "$APP/current.tmp" && mv -T "$APP/current.tmp" "$APP/current"
    for u in $UNITS; do
      [ -f "$PREV/src/systemd/$u" ] && install -m 0644 "$PREV/src/systemd/$u" "/etc/systemd/system/$u"
    done
    systemctl daemon-reload
    systemctl reset-failed newsrelay-api.service newsrelay-worker.service 2>/dev/null || true
    systemctl start newsrelay-api.service newsrelay-worker.service       && say "previous release $PREV restarted" || say "WARNING: previous release failed to start"
  fi
  journalctl -u newsrelay-api -u newsrelay-worker -n 30 --no-pager >&2 || true
  exit 1
}
[ -z "$ROLLBACK_REASON" ] || restore_previous migration
ln -sfn "$REL" "$APP/current.tmp"
mv -T "$APP/current.tmp" "$APP/current"
echo "$VERSION $TS" > "$APP/VERSION"
for u in $UNITS; do
  dst=/etc/systemd/system/$u
  if [ -f "$dst" ] && ! cmp -s "$APP/current/src/systemd/$u" "$dst"; then
    cp -p "$dst" "$dst.bak.$TS"
    say "backed up changed unit $dst -> $dst.bak.$TS"
  fi
  install -m 0644 -o root -g root "$APP/current/src/systemd/$u" "$dst"
done
install -m 0755 -o root -g root "$APP/current/src/scripts/newsrelay-wrapper.sh" /usr/local/bin/newsrelay
systemctl daemon-reload
systemctl enable --quiet newsrelay-api.service newsrelay-worker.service newsrelay-presence.service newsrelay-reader.service     newsrelay-maintenance.timer newsrelay-healthcheck.timer
# Clear start-rate limits left by earlier restarts (repeated installs/rollbacks), then start.
systemctl reset-failed newsrelay-api.service newsrelay-worker.service 2>/dev/null || true
systemctl restart newsrelay-api.service newsrelay-worker.service || restore_previous "service start failed"
systemctl start newsrelay-maintenance.timer newsrelay-healthcheck.timer
systemctl reset-failed newsrelay-presence.service 2>/dev/null || true
systemctl restart newsrelay-presence.service || say "WARNING: presence service did not start (cosmetic only)"
systemctl reset-failed newsrelay-reader.service 2>/dev/null || true
systemctl restart newsrelay-reader.service || say "WARNING: reader service did not start (optional)"

# --- 8. verify (API ready + worker heartbeat), otherwise full rollback ---------------------------
PORT=$(sed -n 's/^listen_port *= *\([0-9]*\).*/\1/p' "$ETC/config.toml"); PORT=${PORT:-8787}
ok=0
i=0
while [ $i -lt 45 ]; do
  if "$NEWBIN/python" -c "import json,urllib.request,sys; r=json.load(urllib.request.urlopen('http://127.0.0.1:$PORT/healthz', timeout=5)); sys.exit(0 if r.get('ok') else 1)" 2>/dev/null; then
    ok=1; break
  fi
  i=$((i + 1)); sleep 2
done
[ $ok -eq 1 ] || restore_previous "health check failed"
# keep the 3 newest pre-upgrade snapshots
ls -1t "$STATE"/backups/pre-upgrade-*.db 2>/dev/null | tail -n +4 | while read -r old; do rm -f "$old" "$old-wal" "$old-shm"; done
rm -f "$STATE"/backups/upgrade-rehearsal-*.db*
# keep the 3 newest releases (never the active or previous one)
ls -1dt "$APP"/releases/*/ 2>/dev/null | tail -n +4 | while read -r old; do
  case "${old%/}" in "$(readlink "$APP/current")"|"$PREV") ;; *) rm -rf "$old" ;; esac
done
say "healthy: newsrelay $VERSION (api, worker, timers enabled; previous: ${PREV:-none})"
