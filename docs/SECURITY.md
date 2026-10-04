# Security model

## Exposure

| Surface | Reachable from | Auth |
|---|---|---|
| `https://<pi-name>.<tailnet>.ts.net/mcp` | Internet (Tailscale Funnel) | OAuth 2.1 bearer, audience-checked |
| `/register`, `/authorize`, `/token`, `/revoke`, `/.well-known/*` | Internet | OAuth protocol endpoints (DCR limited to ChatGPT redirect URIs) |
| `/oauth/consent` | Internet | owner passphrase (scrypt, 5 failures / 15 min lockout) |
| `/healthz`, `/readyz`, `/livez` | Internet | none; return only `{"ok": bool}` |
| 127.0.0.1:8787 | Pi only | — |
| SSH, everything else | unchanged (tailnet) | — |

## Relay is not a proxy

- The only outbound HTTP code path is the Discord transport. Bot mode posts only to the channel id
  in config.toml with a token from a root-owned systemd credential (webhook mode: URL credential).
  Give the bot only *View Channel* + *Send Messages* in that channel and disable "Public Bot". No tool accepts URLs to fetch, paths, commands, or destinations.
- The API unit has `IPAddressDeny=any` + `IPAddressAllow=localhost`: even a bug could not make
  outbound connections. Source URLs in news metadata are stored and printed, never requested
  (tested with a TCP canary).
- All inputs: pydantic models with `extra="forbid"`, length/count bounds, https-only URLs without
  credentials, tz-aware timestamps, no control characters. Unknown top-level tool arguments are
  rejected by an MCP middleware (the SDK would silently ignore them). Bodies > 256 KiB are refused
  with 413 for every route, including chunked uploads without Content-Length.
- Source allowlist (`sources.toml`): stories may only cite listed outlets (exact domain or
  subdomain; lookalike domains rejected), so a prompt-injected page cannot get itself linked.
- Discord output: `allowed_mentions: {"parse": []}` plus textual neutralizing of `@everyone`,
  `@here`, `<@…>`, `<@&…>`, so injected text can never ping. Link embeds suppressed.

## Secrets

| Secret | Location | Reaches |
|---|---|---|
| Discord bot token | `/etc/newsrelay/secrets/discord_bot_token` (root 0600) | worker only, via `LoadCredential=` |
| Discord webhook URL (fallback, optional) | `/etc/newsrelay/secrets/discord_webhook` (root 0600) | worker only, via `LoadCredential=` |
| Owner passphrase hash | `/etc/newsrelay/secrets/owner_passphrase_hash` (root 0600) | API only, via `LoadCredential=` |
| Owner passphrase (plaintext) | `/etc/newsrelay/secrets/owner_passphrase.txt` (root 0600) | nobody; delete after saving it |
| OAuth codes/tokens | SQLite, SHA-256 hashes only | — |

Never in: git, argv, environment, logs, tool responses. Logs pass through a redacting formatter
(webhook URLs, bot tokens, bearer tokens, `token=`/`code=`/`password=` params, cookies) and HTTP client request
logging is disabled at the transport module.

## OAuth details

- DCR accepted only for redirect URIs that parse to `https://chatgpt.com/connector_platform_oauth_redirect`
  exactly or `https://chatgpt.com/connector/oauth/<one safe segment>` (no userinfo, port, query,
  fragment, dot-segments or escapes). Validated again at `/authorize`.
- PKCE S256 (SDK), single-use codes (5 min), access tokens 1 h, refresh tokens 180 d rotating (old
  pair revoked on use), tokens bound to the resource `…/mcp`, scope `newsrelay` required.
- RFC 9207: the authorization response carries `iss`, and the metadata advertises it, which lets
  ChatGPT use its stable callback and prevents authorization-server mix-up.
- Consent: owner passphrase (scrypt N=2^15), constant-time compare, global lockout after 5 failures
  in 15 min (an attacker can delay — not bypass — your consent; DoS accepted for a single-owner app),
  CSP + `X-Frame-Options: DENY` against clickjacking, output HTML-escaped.
- SDK quirk: `/revoke` demands `client_secret`, so public clients cannot self-revoke; the owner
  revokes everything with `sudo newsrelay revoke-tokens`.

## Dependencies

- Direct runtime deps: `mcp` (official MCP SDK), `uvicorn`, `starlette`, `pydantic`, `httpx2`,
  `rapidfuzz`, `websockets` (bot presence). `httpx2`/`httpcore2` are Pydantic's continuation of httpx (github.com/pydantic/httpx2)
  and a required dependency of `mcp` 2.3; `mcp-types` is published by the MCP project itself.
  Provenance checked 2026-10-03 (see VERIFICATION.md).
- `requirements.lock`: pip-tools, exact pins, SHA-256 for every distribution on PyPI. The installer
  forces PyPI only (`PIP_CONFIG_FILE=/dev/null`; Raspberry Pi OS configures piwheels as an extra index
  by default), binary wheels only, `--require-hashes`, and does not build the app (no unpinned build
  backend download).

## Service hardening

Dedicated `newsrelay` system user (no login shell, no home), code root-owned and read-only,
`ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `PrivateDevices`, `NoNewPrivileges`, empty
capability set, `MemoryDenyWriteExecute`, `SystemCallFilter=@system-service`, namespaces/realtime/SUID
restricted, `UMask=0077`, memory/task limits. `systemd-analyze security`: api 1.1, worker 1.3 (OK).

The presence service holds the bot token only in memory, sends it only in the Gateway IDENTIFY /
RESUME frames over TLS, requests no intents (cannot read messages) and cannot access the database.

## Prompt injection

Web content is treated as data. Even a fully compromised ChatGPT session can only: read compact
history, and post text to the one configured channel (no pings, no embeds, bounded size, idempotent
per run). It cannot read secrets, change destinations, run code, or make the Pi fetch anything.

## Rotation

- Webhook: `sudo newsrelay set-webhook` then `sudo systemctl restart newsrelay-worker`.
- Owner passphrase: `sudo newsrelay set-passphrase --generate --force`, restart API.
- Revoke all ChatGPT tokens: `sudo newsrelay revoke-tokens` (ChatGPT must re-authorize).
- Close the public endpoint: `sudo tailscale funnel --https=443 off`.
