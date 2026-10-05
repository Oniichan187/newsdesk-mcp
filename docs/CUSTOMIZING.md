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

## 2. Language, region, topics, time

```sh
python3 scripts/build_prompt.py \
  --language German \
  --region Germany \
  --topics "AI, science, climate, energy" \
  --timezone Europe/Berlin --time 07:00
```

| Option | Default | Effect |
|---|---|---|
| `--sources` | `src/newsrelay/sources.toml` | allowlist file the outlets are read from |
| `--language` | English | language of headlines and post bodies |
| `--region` | Austria | where the readers live: only stories that affect it are posted (global news, events abroad with consequences there, its own decisions) |
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
| Message length | `max_message_chars` (default 1850; Discord's hard limit is 2000) |
| Link previews | `suppress_link_embeds = false` to show previews |
| Online status | `discord_presence = true/false`, `discord_presence_status` (online/idle/dnd), `discord_presence_activity_type` (watching/playing/listening/competing), `discord_presence_text`; then `sudo systemctl restart newsrelay-presence` |
| One channel per day + archive | Create a category yourself (e.g. `📰 Tagesbriefing`; the bot never creates or moves it), copy its ID (Developer Mode → right-click → Copy ID), then `discord_daily_channels = true`, `discord_daily_category_id = "…"`; optional `discord_daily_keep` (7), `discord_daily_archive` (`🗄-archiv`); bot needs **Manage Channels**; restart `newsrelay-worker` |
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
