# Changelog

## 1.6.0 — 2026-10-05

- **Daily channels** (bot mode, `discord_daily_channels = true`): every day's briefing gets its own
  text channel `📅-mo-05-10` in the category `📰 Tagesbriefing`, newest on top. Days older than
  `discord_daily_keep` (7) are copied chronologically into `🗄-archiv` (day header + the exact posted
  messages from the outbox), and only then is their channel deleted; the copy is resumable.
  Without the "Manage Channels" permission posts fall back to `discord_channel_id`.
- Schema 2: table `discord_day_channels`, column `outbox.channel_id`.

## 1.5.0 — 2026-10-04

- **Reader-impact test** in the task prompt: only stories that can change something for the reader
  (money, laws, safety, health, services, security, spillover from wars …); local tragedies without
  wider consequences are dropped.
- Stories carry a required `impact` (+ optional `impact_region`), rendered as "🎯 Impact" — replaces
  "Why it matters" in the body.
- Optional `outlook` (1–3 items) for forecasts/estimates: event, likelihood
  (very_likely … very_unlikely), optional percentage (must fit the level) and basis; rendered as
  "🔮 Outlook".
- **Several sources:** when several allowed outlets cover an event, the story cites each of them
  (one real article per outlet, up to 6) — never an outlet that was not read. The relay rejects
  duplicate source URLs and homepage links.
- Prompt rule 9: the task must never pause or edit itself after relay errors (it used to pause
  itself when the relay was unreachable); it reports `relay error: …` and the next run catches up.
  Re-paste the regenerated prompt into the Scheduled Task.

## 1.4.1 — 2026-10-04

- First run: 24 h window, `begin_run` explains that memory is empty, and at most `first_run_max_stories` (8) stories are accepted, most important first. Later runs post only the delta.

## 1.4.0 — 2026-10-04

- `newsrelay-presence.service`: keeps the bot **online** in Discord (Gateway session, intents 0,
  configurable status/activity), self-healing, no DB access. New dependency `websockets` 17.1.
- `status` shows the presence state.

## 1.3.0 — 2026-10-04

- **Source allowlist** in `src/newsrelay/sources.toml` (30 outlets), enforced by the relay: candidates
  citing other sites are flagged `SOURCE_NOT_ALLOWED`, stories citing them are rejected. Optional
  per-installation override `/etc/newsrelay/sources.toml`.
- `scripts/build_prompt.py` reads the same file; the separate example lists were removed.

## 1.2.2 — 2026-10-04

- Prompt template + `scripts/build_prompt.py` (sources, language, region, topics, time).
- New README, CUSTOMIZING guide, CONTRIBUTING, security policy, CI workflow.

## 1.2.1 — 2026-10-04

- Discord layout: `##` heading, category emoji line, structured body, "📰 Sources" with named links.
- Task prompt: daily 18:00, every listed source checked, fixed markdown body layout.

## 1.2.0 — 2026-10-04

- Discord **bot** transport (default): posts to a fixed channel id with a token from a systemd
  credential; uses `nonce` + `enforce_nonce` so ambiguous sends are retried safely (max. 4× within
  the de-duplication window) instead of being parked as uncertain. Webhook remains as fallback.
- `set-bot-token`, `test-discord`; status shows `discord_mode`.
- Log redaction covers Discord bot tokens.

## 1.1.0 — 2026-10-04

Production audit release.

- Lock regenerated with pip-tools: hashes for every distribution (was: only the Pi's wheels, so
  installs failed elsewhere); installs from PyPI only (Raspberry Pi OS piwheels extra index ignored);
  the app is no longer built at install time (that fetched an unpinned setuptools).
- Worker: READY only after lock, DB, webhook parsing and crash recovery; watchdog pinged by the loop
  itself (the old helper thread treated a missing heartbeat as fresh); clean resource shutdown.
- API: READY after `/readyz` through the real listener; `/livez` drives the watchdog; healthcheck
  restarts the unit that actually failed.
- Research windows: bounded 7-day catch-up, checkpoint clamped to the window — long outages are no
  longer silently skipped (the old 14-day cap could jump over unresearched time).
- Outbox: no age-based expiry; failed items recoverable (`outbox requeue`, `requeue-failed`);
  retention never prunes payloads/bodies of unresolved items.
- Dedup: different numbers/years and opposite negations are never the same fact (fixed false
  duplicates like "Prize 2025" vs "2026" and "not confirmed" vs "confirmed"); subset facts; topic
  vocabulary signal; topics older than 30 days are never auto-duplicates without URL/fingerprint.
- HTTP: chunked oversized bodies return 413 (returned 500 on `/register`); unknown top-level tool
  arguments rejected (were silently ignored).
- OAuth: parsed redirect URI validation; RFC 9207 `iss`; metadata advertises public clients.
- Discord webhook URL validation is structural (token length is not documented as fixed).
- Installer: migration rehearsal on a copy, maintenance window, pre-upgrade snapshot, DB + code
  rollback; `upgrade-check` command; richer `status`.
- `begin_run` no longer returns recent headlines (tokens); returns `research_until`/`catch_up` and a
  delivery warning only when degraded.

## 1.0.0 — 2026-10-03

Initial release.

- MCP server (official `mcp` 2.3 SDK, Streamable HTTP, stateless JSON) with six tools; four are
  genuinely read-only, so each run needs exactly one write call.
- Built-in OAuth 2.1 authorization server: DCR restricted to ChatGPT callbacks, PKCE S256, owner
  passphrase consent with lockout, hashed rotating tokens, 180-day refresh.
- Layered local dedup (canonical URL, fact-set/title fingerprints, aliases, FTS5, RapidFuzz).
- Checkpoint with 6 h overlap, catch-up after outages, idempotent run keys.
- Durable SQLite outbox, single worker, Discord `wait=true`, 429/5xx/unsent/uncertain handling.
- Semantic, grapheme-safe message splitting; mention-proof output.
- Verified backups with rotation, integrity checks, retention/aging, restore test.
- Hardened systemd units with watchdog; health timer; idempotent installer with release rollback.
