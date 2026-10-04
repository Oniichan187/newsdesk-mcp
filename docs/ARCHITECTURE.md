# Architecture

```
ChatGPT Scheduled Task ──HTTPS (OAuth 2.1 bearer)──> Tailscale Funnel :443 (<pi-name>.<tailnet>.ts.net)
                                                         │ tailscaled → 127.0.0.1:8787
                                                         ▼
                 newsrelay-api.service  (uvicorn + official `mcp` 2.3 SDK, Streamable HTTP, stateless JSON)
                   ├─ edge layer      256 KiB body cap (Content-Length and chunked), AS metadata (+RFC 9207)
                   ├─ /mcp            6 tools; strict top-level + nested argument validation
                   ├─ /register /authorize /token /revoke /.well-known/*   (OAuth AS, SDK handlers)
                   ├─ /oauth/consent  owner-passphrase page
                   └─ /livez /readyz /healthz   {"ok": bool}
                                                         │ SQLite (WAL, synchronous=FULL, FK on)
                                                         ▼
                   /var/lib/newsrelay/newsrelay.db  ── outbox ──>  newsrelay-worker.service ──> Discord bot (fixed channel)
```

## Modules

| Module | Responsibility |
|---|---|
| `schemas.py` | Strict input models (extra=forbid, bounds, https-only URLs, tz-aware times) |
| `service.py` | begin_run / match / publish / noop / status / health; transactions, idempotency, research windows |
| `dedup/` | URL canonicalization, Unicode normalization, fingerprints, layered matcher |
| `publishing/` | formatter, splitter, webhook transport, outbox state machine, worker |
| `api/` | MCP server, OAuth provider (SQLite, hashed tokens), consent page, edge layer |
| `maintenance/` | backups, integrity, retention |
| `cli.py` | service entry points (incl. systemd readiness/watchdog) and operator commands |

## Run lifecycle (token-minimal)

1. `begin_run(run_key)` — **read-only**. Returns `research_from`, `research_until`, `catch_up`, and a
   `delivery` warning only when delivery is degraded. No history is sent.
2. `match_candidates` — **read-only**. Per candidate: `NO_MATCH` / `EXACT_DUPLICATE` /
   `LIKELY_DUPLICATE` / `POSSIBLE_EXISTING_TOPIC` (+ topic id, state summary, ≤6 prior facts, indexes of
   new-looking facts). Duplicates come back without prior facts.
3. Exactly one write: `publish_digest` or `complete_noop`, idempotent per `run_key`
   (`UNIQUE(run_key)` + request hash; same payload → same answer, different payload → refused, nothing
   changed). Publish re-checks exact duplicates inside the same `BEGIN IMMEDIATE` transaction that writes
   stories, topics, FTS rows and outbox messages.

### Research windows and the checkpoint

```
from  = checkpoint − 6 h            (first run: now − 36 h)
until = min(now, checkpoint + 7 d)
```
Completing a run sets the checkpoint to `min(research_through, until)` (monotonic). After a long outage
the relay therefore hands out consecutive ≤7-day windows; the checkpoint never jumps over time that was
not researched. The completion reply contains `next` while time is still unresearched; the task prompt
does up to two extra catch-up runs per execution, later days continue. Tested for 3/20/90-day outages.

### Checkpoint vs. delivery

The research checkpoint advances when ChatGPT's research is **durably committed** (stories + Discord
payloads in SQLite), not when Discord confirms. A Discord outage therefore never causes re-research,
and the accepted content cannot be lost: queued items never expire, failed/uncertain items keep their
payload until an operator resolves them, and retention only prunes delivered/cancelled content.

## Outbox state machine

```
pending ─claim─> dispatching ─2xx──────────────────────────> delivered
                             ─connect/DNS/TLS error (unsent)─> pending  (backoff+jitter ≤30 min)
                             ─429──────────────────────────> pending  (Retry-After; >15× → failed)
                             ─5xx──────────────────────────> pending  (backoff; >5× → failed)
                             ─401/403/404 (webhook gone)───> pending  (backoff ≤6 h; status "invalid")
                             ─other 4xx────────────────────> failed
                             ─read timeout / reset / write error after send─> uncertain
dispatching at worker start (crash, power loss) ───────────> uncertain
failed|uncertain ─operator─> pending (resend/requeue) | delivered | cancelled
```
- Claim commits before the HTTP call; the result is committed in a second transaction.
- Strict FIFO preserves message order; the head item waiting for retry holds the queue.
- One worker: `flock` + conditional claim in SQL.
- Bot mode (default): every message carries a deterministic `nonce` with `enforce_nonce: true`, so
  Discord returns the existing message instead of posting twice. An ambiguous send is therefore
  retried with the same nonce after 15 s (max. 4 times, well inside Discord's "few minutes" window);
  after a crash, an item interrupted < 60 s ago is re-queued the same way. Only beyond that it
  becomes `uncertain`.
- Webhook mode (fallback): no server-side de-duplication; ambiguous sends are **never** resent
  automatically.

## Bot presence

`newsrelay-presence.service` keeps one Discord Gateway session (intents 0, no message content) so the
bot is shown online with an activity. It is cosmetic and isolated: no database access
(`InaccessiblePaths=/var/lib/newsrelay`), posting never depends on it. Heartbeats with ACK check
(zombie detection), RESUME after reconnect requests, re-IDENTIFY after invalid sessions, exponential
backoff with jitter, and a one-hour pause on fatal close codes (e.g. 4004 bad token).

## Liveness and readiness

| Unit | READY=1 when | WATCHDOG=1 while |
|---|---|---|
| api | config, DB, schema, OAuth initialised **and** `/readyz` answers 200 through the real listener | `/livez` answers (event loop responsive) |
| worker | lock held, DB opened/migrated, webhook parsed, crash recovery done | the processing loop iterates (pinged from the loop itself) |

`newsrelay-healthcheck.timer` (5 min) additionally checks `/readyz` and the worker's tmpfs heartbeat
(missing or stale = unhealthy) and restarts the failing unit after 3 consecutive failures.
Verified on the Pi: SIGSTOP of either process → killed by the watchdog and restarted.

## Storage & growth

Permanent (small): topics, aliases, FTS rows, story titles/facts/fingerprints, canonical source URLs,
runs, outbox idempotency keys. After 180 days, only for resolved items: story bodies, Discord payloads.
Delivery attempts of resolved items after 30 days. Topics: active → dormant (21 d) → archived (120 d),
still matched locally. `auto_vacuum=INCREMENTAL` + bounded incremental vacuum; no daily VACUUM.

## Upgrades

`scripts/install.sh` (also used by `update.sh`):
1. build a new release dir from hash-pinned wheels (PyPI only); compile check; `pip check`
2. rehearse the migration on a consistent copy with the new code (`newsrelay upgrade-check`) — abort
   with production untouched if it fails
3. stop api/worker, take a verified pre-upgrade snapshot, migrate production
4. switch the `current` symlink, install units, start, wait for `/healthz`
5. on any failure: stop, restore the snapshot if the schema changed, switch back to the previous
   release and its units, start it

Compatibility contract: migrations only move forward; code refuses a database whose schema is newer
than it knows (`database.SCHEMA_VERSION`), so an old release can never run on a newer schema.
