# Changelog

## 1.9.0 — 2026-10-07

- **Public reader via Cloudflare Tunnel** (frontend only; the MCP backend stays on Tailscale Funnel):
  `newsrelay-tunnel.service` + `scripts/expose-reader-public.sh` (installs cloudflared from
  Cloudflare's apt repository). Quick tunnel without an account, or a named tunnel with a fixed
  hostname via `/etc/newsrelay/tunnel.env`.
- The link under the PDF uses `reader_url`, else the current quick-tunnel address from cloudflared's
  loopback metrics port (`reader_tunnel_metrics`); only `*.trycloudflare.com` answers are accepted.

## 1.8.1 — 2026-10-06

- Briefing PDF: smaller type (body 8.8 pt, headlines 12.5 pt) — more text per phone screen.
- Speed reader: stories published before 1.8.0 (no stored JSON) are listed and readable again, from
  headline, body and sources; the day list was empty for them.

## 1.8.0 — 2026-10-06

- **Briefing PDF** before each run's stories: phone-sized pages (108 x 192 mm), IBM Plex Serif, no
  emoji, linked contents, importance scale, Impact/Outlook sections, clickable sources and **Bionic
  Reading** body text. Uploaded as an attachment (bot and webhook); a failed render never blocks the
  stories. Settings `briefing_pdf`, `briefing_dir`.
- **RSVP speed reader** (`newsrelay reader`, `newsrelay-reader.service`): one page per day,
  100–2000 words per minute, pivot-letter alignment, punctuation pauses, context line, keyboard and
  touch controls, remembers speed and position, links the day's PDFs. Read-only, localhost only;
  `scripts/expose-reader.sh` publishes it inside the tailnet (`tailscale serve`, not Funnel).
- Long stories are split without the `(2/2) continued` line; `max_message_chars` default 1990.
- Schema 3: `stories.story_json` keeps the full validated story for PDF and reader.
- New dependency: fpdf2 2.8.9 (with Pillow, fonttools, defusedxml).

## 1.7.0 — 2026-10-06

- Story footer, in order: **⭐ Importance X/10** with a one-sentence reason (`importance` is now
  1–10, `importance_reason` required), **Impact <country>** (`impact`) and **🌍 Impact global**
  (`impact_global`, required), then Outlook and sources.
- `reader_country` (config, default `Austria`) labels the country impact line; keep it equal to
  `build_prompt.py --country`.
- Prompt: "What happened" is 3–6 sentences with the needed background, strictly informative — no
  clickbait, teasers, rhetorical questions or dramatising wording; body guideline ~2,000 characters.

## 1.6.0 — 2026-10-05

- **Daily channels** (bot mode, `discord_daily_channels = true`): every day's briefing gets its own
  text channel `📅-mo-05-10` in a category the server owner creates (`discord_daily_category_id`;
  the bot never creates or moves categories), newest on top. Days older than
  `discord_daily_keep` (7) are copied chronologically into `🗄-archiv` (day header + the exact posted
  messages from the outbox), and only then is their channel deleted; the copy is resumable.
  Without the "Manage Channels" permission or a valid category id posts fall back to
  `discord_channel_id`.
- **Country filter:** `build_prompt.py --country` (default `Austria`; `--region` still works) is the
  country the readers live in: only global developments, events abroad with real consequences there,
  and its own consequential decisions qualify.
- `impact_region` is required and shown as its own line, `🌍 Region: Global` / `📍 Region: USA`.
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
