# Disaster recovery

All commands on the Pi. `newsrelay …` is the wrapper in `/usr/local/bin` (runs as the right user).

## Quick diagnosis

```sh
sudo newsrelay status                       # health JSON, outbox counts, last runs
systemctl status newsrelay-api newsrelay-worker
journalctl -u newsrelay-api -u newsrelay-worker -n 100
sudo newsrelay outbox list                  # undelivered / uncertain items
```

## Corrupted database

The nightly job stops before pruning if `integrity_check` fails (journal: `database integrity problem`).

```sh
sudo systemctl stop newsrelay-api newsrelay-worker
sudo cp -a /var/lib/newsrelay /root/newsrelay-broken-$(date +%F)        # keep evidence
ls /var/lib/newsrelay/backups/daily/                                     # pick newest good one
sudo newsrelay restore-test /var/lib/newsrelay/backups/daily/<file>.db   # validates a temp copy
sudo install -o newsrelay -g newsrelay -m 600 /var/lib/newsrelay/backups/daily/<file>.db /var/lib/newsrelay/newsrelay.db
sudo rm -f /var/lib/newsrelay/newsrelay.db-wal /var/lib/newsrelay/newsrelay.db-shm
sudo systemctl start newsrelay-api newsrelay-worker && sudo newsrelay status
```
Loss: anything after the backup (≤ 1 day; `pre-upgrade-*` snapshots may be newer). Stories published in that window may be matched as
NO_MATCH once; outbox items from that window are gone (they were already delivered or will be
re-published by the next run).

## Lost / dead SD card

Local backups live on the same card. **Recommendation:** copy `/var/lib/newsrelay/backups/` to another
machine occasionally, e.g. `scp -r <user>@<pi-name>:/var/lib/newsrelay/backups ./` (needs sudo to read;
or mount a USB stick and set `backup_dir` in config.toml). Rebuild:

1. Flash Raspberry Pi OS, join Tailscale with the same machine name (`<pi-name>`), so the Funnel
   hostname stays `<pi-name>.<tailnet>.ts.net`.
2. Copy the repo, `sudo sh scripts/install.sh`, `sudo sh scripts/expose-funnel.sh`.
3. Restore a backup as above (or start empty — the relay works, it just forgets history).
4. `sudo newsrelay set-webhook`, then reconnect ChatGPT (new passphrase in
   `/etc/newsrelay/secrets/owner_passphrase.txt`; old tokens are invalid).

## Lost or revoked Discord bot token / missing permissions

Symptom: `discord_status: invalid since …` (HTTP 401/403/404); items stay `pending`, never dropped.
Fix: Developer Portal → Bot → Reset Token → `sudo newsrelay set-bot-token` →
`sudo systemctl restart newsrelay-worker`. For 403, give the bot *View Channel* + *Send Messages* in
the news channel. Then `sudo newsrelay test-discord`.

## Lost or revoked Discord webhook (fallback mode)

Symptom: `webhook_status: invalid since …`; items stay `pending` (retried at most every 6 h, never
dropped). Fix: Discord → channel → Integrations → Webhooks → new webhook → copy URL, then
`sudo newsrelay set-webhook`, `sudo systemctl restart newsrelay-worker`. Pending items are sent in order.
Items that had reached `failed` meanwhile (e.g. repeated 5xx): `sudo newsrelay outbox requeue-failed --yes`.

## Tailscale failure

`systemctl status tailscaled`, `tailscale status`. Funnel config persists in tailscaled; re-apply with
`sudo sh scripts/expose-funnel.sh`. If Funnel was disabled in the tailnet policy, re-enable the
`funnel` node attribute in the Tailscale admin console. Local services keep running meanwhile;
ChatGPT runs fail (checkpoint is not advanced, so the next good run catches up).

## Expired ChatGPT authorization

Refresh tokens last 180 days and rotate on every use, so a daily task never expires. If ChatGPT shows
the app as disconnected: Settings → Apps → News Relay → reconnect, approve with the passphrase.

## Broken update / dependency upgrade

`scripts/install.sh` rehearses the migration on a copy first (abort = nothing changed), takes a verified
`backups/pre-upgrade-<ts>.db` snapshot while services are stopped, and on any failure restores that
snapshot (if the schema changed) and switches back to the previous release and its units. Manual
rollback, if ever needed:
```sh
ls -1t /opt/newsrelay/releases/ ; ls -1t /var/lib/newsrelay/backups/pre-upgrade-*.db
sudo systemctl stop newsrelay-api newsrelay-worker
# only if the failed release had changed the schema:
sudo install -o newsrelay -g newsrelay -m 600 /var/lib/newsrelay/backups/pre-upgrade-<ts>.db /var/lib/newsrelay/newsrelay.db
sudo rm -f /var/lib/newsrelay/newsrelay.db-wal /var/lib/newsrelay/newsrelay.db-shm
sudo ln -sfn /opt/newsrelay/releases/<previous> /opt/newsrelay/current
sudo systemctl start newsrelay-api newsrelay-worker && sudo newsrelay status
```
Old code refuses a newer schema, so an incomplete manual rollback fails loudly instead of corrupting data.

## OpenAI capability change

The relay is transport-agnostic (`service.py`); only `api/app.py` binds it to MCP. If OpenAI changes
auth or transport, adapt that module. If ChatGPT stops supporting custom apps in tasks, the backend
keeps state and can be driven by any MCP client.

## Uncertain deliveries

`sudo newsrelay outbox list` → check the Discord channel → `sudo newsrelay outbox delivered <id>` if
the message is there, else `sudo newsrelay outbox resend <id>` (or `cancel`). Single failed items:
`sudo newsrelay outbox requeue <id>`. Nothing is resent without one of these explicit commands.
