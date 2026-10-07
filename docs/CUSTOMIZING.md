# Customizing your news desk

The allowed outlets live in **`src/newsrelay/sources.toml`**, the editorial instructions in the
**ChatGPT task prompt**, everything operational in **`/etc/newsrelay/config.toml`** on the Pi.

## 1. Sources (allowlist)

The outlets that may be used are defined in **[`src/newsrelay/sources.toml`](../src/newsrelay/sources.toml)**
— one entry per outlet with its domains:

```toml
[[source]]
name = "Der Standard"
domains = ["derstandard.at", "derstandard.de"]   # subdomains (www., ...) are included automatically
```

This one file drives both sides:

- **The relay enforces it.** Candidates citing other sites come back as `SOURCE_NOT_ALLOWED`, and a
  story with any non-listed source URL is rejected at publish time. Lookalikes such as
  `taz.de.evil.com` or `eviltaz.de` do not match.
- **The prompt lists it.** `scripts/build_prompt.py` reads the same file, so ChatGPT is told to go
  through exactly these outlets.

To change it:

| Where | When |
|---|---|
| edit `src/newsrelay/sources.toml`, then `sudo sh scripts/update.sh` | your fork / your repo |
| copy it to `/etc/newsrelay/sources.toml`, edit, `sudo systemctl restart newsrelay-api` | one installation, no code change |

Afterwards regenerate the prompt (`python3 scripts/build_prompt.py --sources <file> > my-task-prompt.md`)
and replace it in your ChatGPT task. 15-30 outlets is a good range; long lists make runs slower.

## 2. Language, country, topics, time

```sh
python3 scripts/build_prompt.py \
  --language German \
  --country Germany \
  --topics "AI, science, climate, energy" \
  --timezone Europe/Berlin --time 07:00
```

| Option | Default | Effect |
|---|---|---|
| `--sources` | `src/newsrelay/sources.toml` | allowlist file the outlets are read from |
| `--language` | English | language of headlines and post bodies |
| `--country` (alias `--region`) | Austria | the country you live in: only stories that affect it are posted — global developments, events abroad with real consequences there, and its own consequential decisions. Set the same name as `reader_country` in `/etc/newsrelay/config.toml` (labels the "Impact <country>" line) |
| `--topics` | broad list | narrow or widen the scope |
| `--timezone` / `--time` | Europe/Vienna / 18:00 | date of the daily run key; reminder in the header |

The schedule itself is set in ChatGPT when you create the task ("every day at 07:00"). For several
runs per day, change the run key in the prompt to `news/YYYY-MM-DDTHH` — no backend change needed.

## 3. Discord

| What | Where |
|---|---|
| Channel | `discord_channel_id = "…"` in `/etc/newsrelay/config.toml`, then `sudo systemctl restart newsrelay-worker` |
| Bot name / avatar | Discord Developer Portal → your application → Bot |
| Bot token | `sudo newsrelay set-bot-token` (hidden input, stored as a root-only systemd credential) |
| Message length | `max_message_chars` (default 1990; Discord's hard limit is 2000); longer stories are split at paragraph boundaries without markers |
| Link previews | `suppress_link_embeds = false` to show previews |
| Online status | `discord_presence = true/false`, `discord_presence_status` (online/idle/dnd), `discord_presence_activity_type` (watching/playing/listening/competing), `discord_presence_text`; then `sudo systemctl restart newsrelay-presence` |
| One channel per day + archive | Create a category yourself (e.g. `📰 Tagesbriefing`; the bot never creates or moves it), copy its ID (Developer Mode → right-click → Copy ID), then `discord_daily_channels = true`, `discord_daily_category_id = "…"`; optional `discord_daily_keep` (7), `discord_daily_archive` (`🗄-archiv`); bot needs **Manage Channels**; restart `newsrelay-worker` |
| Briefing PDF | `briefing_pdf = true/false`. Each published run gets a phone-sized PDF (IBM Plex Serif, Bionic Reading, linked contents and sources), posted before the stories and stored under `/var/lib/newsrelay/briefings/<day>/`. Design: [`src/newsrelay/briefing/pdf.py`](../src/newsrelay/briefing/pdf.py) |
| Speed reader (RSVP) | `newsrelay-reader.service` serves one page per day on `127.0.0.1:8788` (100–2000 words per minute, keyboard/touch controls, Google Translate link, PDF embedded below the reader). Each day has Read, Read PDF, and MP3 actions. The Pi generates and caches MP3s locally with Kokoro-82M (`af_heart`, int8 CPU model); article read-aloud follows the reader's WPM. Setup downloads the roughly 140 MB voice model and needs 64-bit Raspberry Pi OS plus `ffmpeg`. Publish it **inside the tailnet only** with `sudo sh scripts/expose-reader.sh` (https://<pi>.<tailnet>.ts.net:8443, not reachable from the internet); set `reader_url` to link it under the PDF |
| Public reader (Cloudflare) | `sudo sh scripts/expose-reader-public.sh` installs cloudflared and `newsrelay-tunnel.service`: the reader becomes public at `https://<random>.trycloudflare.com` (address changes when the tunnel restarts; the link under the PDF always uses the current one, read from `reader_tunnel_metrics`). For a fixed address put `TUNNEL_TOKEN=…` of a named tunnel (public hostname → `http://127.0.0.1:8788`) into `/etc/newsrelay/tunnel.env` (mode 600) and set `reader_url`. Only the reader goes through Cloudflare; the MCP backend stays on Tailscale Funnel |
| Webhook instead of bot | `sudo newsrelay set-webhook`, leave `discord_channel_id` empty; `discord_username` sets the name |

Post layout (heading, category emoji, source line) is in
[`src/newsrelay/publishing/formatter.py`](../src/newsrelay/publishing/formatter.py); the body layout
("What happened / Key facts / …") is in the prompt template
[`prompts/scheduled-task.template.md`](../prompts/scheduled-task.template.md).

## 4. Memory, outages, retention

All in `/etc/newsrelay/config.toml` (restart both services after changes):

| Key | Default | Meaning |
|---|---|---|
| `overlap_hours` | 6 | each run re-checks the last N hours (late-indexed articles) |
| `initial_lookback_hours` | 24 | window of the very first run (memory is empty then) |
| `first_run_max_stories` | 8 | the first run posts at most this many stories (most important first) |
| `catchup_window_days` | 7 | after an outage, catch up in windows of this size |
| `dormant_after_days` / `archive_after_days` | 21 / 120 | topic aging (archived topics are still matched) |
| `body_retention_days` | 180 | rendered texts of *delivered* posts are pruned after this |
| `backup_keep_daily/weekly/monthly` | 14 / 8 / 12 | backup rotation |

## 5. Deeper changes (code)

- **Categories:** the `Category` list in [`src/newsrelay/schemas.py`](../src/newsrelay/schemas.py) plus
  labels/emoji in `formatter.py`.
- **Duplicate sensitivity:** thresholds at the top of
  [`src/newsrelay/dedup/matcher.py`](../src/newsrelay/dedup/matcher.py). Run the tests after changes —
  the adversarial dedup tests document the intended behaviour.
- **Exposure:** Tailscale Funnel by default; OpenAI's Secure MCP Tunnel is an alternative
  (see [CHATGPT_SETUP.md](CHATGPT_SETUP.md)).
