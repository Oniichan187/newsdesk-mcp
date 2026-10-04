# Connecting ChatGPT

Verified against OpenAI documentation on 2026-10-03. OpenAI changes these products often: re-check the
linked pages if a label differs.

## What the relay offers

- MCP server (Streamable HTTP): `https://<pi-name>.<tailnet>.ts.net/mcp`
- Authentication: OAuth 2.1 (authorization code + PKCE S256, Dynamic Client Registration,
  refresh tokens/`offline_access`, RFC 9207 `iss`). Only ChatGPT's documented callbacks may register:
  `https://chatgpt.com/connector_platform_oauth_redirect` (stable; used because the server returns
  `iss`) and `https://chatgpt.com/connector/oauth/{callback_id}`. ChatGPT sends `resource=…/mcp`; tokens
  are bound to it. OpenAI says ChatGPT prefers CIMD when a server supports it; this server does not
  advertise CIMD, so ChatGPT uses DCR (documented fallback) — UNVERIFIED against the live product.
- Consent requires the **owner passphrase**: `sudo cat /etc/newsrelay/secrets/owner_passphrase.txt`
  on the Pi. Put it in your password manager, then you may delete that file (the hash stays).
- Tools: `newsrelay_begin_run`, `newsrelay_match_candidates`, `newsrelay_publish_status`,
  `newsrelay_health` (all `readOnlyHint: true`), and `newsrelay_publish_digest`,
  `newsrelay_complete_noop` (write actions). Each run makes exactly **one** write call.

## Steps (ChatGPT web)

1. Settings → **Apps** (formerly Connectors) → **Advanced settings** → enable **Developer mode**.
2. **Create app**: name `News Relay`, MCP server URL as above, authentication **OAuth**. Leave client
   ID/secret empty (ChatGPT registers itself via DCR).
3. Approve on the News Relay page that opens with the owner passphrase.
4. In a normal chat, enable the app and test: *"Use News Relay: call newsrelay_health."*
   Then: *"Call newsrelay_begin_run with run_key daily-news/<today>."*
5. Create the Scheduled Task with the prompt from `SCHEDULED_TASK_PROMPT.md` (daily, 07:00 Vienna)
   and make sure the News Relay app is enabled for it.
6. Trigger the task once ("run now" if available) and check `sudo newsrelay status` on the Pi:
   `last_runs` must show the run.

## Known OpenAI limitations (from official docs, 2026-10-03)

- Developer mode: "Available to Pro, Plus, Business, Enterprise, and Education accounts on the web"
  with "full MCP client support for all tools, both read and write"
  (developers.openai.com/api/docs/guides/developer-mode). The help center instead says full MCP incl.
  write actions is rolling out to Business/Enterprise/Edu and "Pro users can connect MCPs with
  read/fetch permissions" — the pages conflict; Plus behaviour must be tested.
- "Tools without [readOnlyHint] are treated as write actions… Write actions by default require
  confirmation." Remembered approvals apply per conversation only.
- Scheduled tasks: "can use supported apps … An action that sends a message or changes external data
  may require approval. If approval is required, the task pauses until you review it."
  (help.openai.com/en/articles/10291617-scheduled-tasks-in-chatgpt). Whether a *custom developer-mode*
  app runs in a scheduled task is not documented.
- App permissions (help.openai.com/en/articles/20001495) mention "Allow all actions" / "Always allow"
  for eligible connected apps; whether this applies to custom developer-mode MCP apps is not
  documented. If ChatGPT offers it for News Relay, enabling it for the two write tools is what makes
  unattended runs possible.

**Consequence:** if the scheduled run pauses at `newsrelay_publish_digest` / `newsrelay_complete_noop`
waiting for approval, unattended operation is blocked by ChatGPT, not by the relay. The relay already
minimizes this to one approval per run; approving it from the ChatGPT notification still works.
Marking the write tools read-only to dodge the prompt would misrepresent them and is deliberately not
done.

## Alternative: OpenAI Secure MCP Tunnel

OpenAI documents an outbound-only tunnel (developers.openai.com/api/docs/guides/secure-mcp-tunnels,
client: github.com/openai/tunnel-client) that needs a tunnel ID + API key from the OpenAI Platform. It
would let you turn off Funnel (`sudo tailscale funnel --https=443 off`). It does not change the
write-approval behaviour above. Its plan availability for ChatGPT developer mode was not verifiable.
