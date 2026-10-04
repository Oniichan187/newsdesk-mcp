# Customizing your news desk

Almost everything editorial lives in the **ChatGPT task prompt**; everything operational lives in
**`/etc/newsrelay/config.toml`** on the Pi. You rarely need to touch code.

## 1. Sources

Source lists are plain text files, one outlet per line (`#` = comment):

| File | Focus |
|---|---|
| [`examples/sources/dach-quality-press.txt`](../examples/sources/dach-quality-press.txt) | German-language quality press & public broadcasters (DE/AT/CH) — **default** |
| [`examples/sources/international.txt`](../examples/sources/international.txt) | Wire services, BBC/DW/NPR, Guardian, FT, Economist … |
| [`examples/sources/tech-security.txt`](../examples/sources/tech-security.txt) | Tech, AI, security, digital rights |

Copy one, edit it, then build your prompt:

```sh
cp examples/sources/dach-quality-press.txt my-sources.txt     # add/remove outlets
python3 scripts/build_prompt.py --sources my-sources.txt > my-task-prompt.md
```

Paste `my-task-prompt.md` into your Scheduled Task (edit the existing task in ChatGPT and replace the
prompt). Names are enough; add a domain in brackets if a name is ambiguous, e.g. `Profil (profil.at)`.
Long lists make each run slower and more expensive for ChatGPT — 15–30 sources is a good range.

## 2. Language, region, topics, time

```sh
python3 scripts/build_prompt.py \
  --sources examples/sources/international.txt \
  --language German \
  --region "Germany and the EU" \
  --topics "AI, science, climate, energy" \
  --timezone Europe/Berlin --time 07:00
```

| Option | Default | Effect |
|---|---|---|
| `--sources` | DACH list | which outlets ChatGPT goes through |
| `--language` | English | language of headlines and post bodies |
| `--region` | Austria/Europe | whose perspective decides what is important |
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
| Webhook instead of bot | `sudo newsrelay set-webhook`, leave `discord_channel_id` empty; `discord_username` sets the name |

Post layout (heading, category emoji, source line) is in
[`src/newsrelay/publishing/formatter.py`](../src/newsrelay/publishing/formatter.py); the body layout
("What happened / Why it matters / …") is in the prompt template
[`prompts/scheduled-task.template.md`](../prompts/scheduled-task.template.md).

## 4. Memory, outages, retention

All in `/etc/newsrelay/config.toml` (restart both services after changes):

| Key | Default | Meaning |
|---|---|---|
| `overlap_hours` | 6 | each run re-checks the last N hours (late-indexed articles) |
| `initial_lookback_hours` | 36 | window of the very first run |
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
