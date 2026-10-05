"""Discord webhook transport. The only outbound HTTP the relay ever performs.

The destination is fixed by local configuration (systemd credential); callers cannot supply URLs.
Outcomes are classified so the outbox can distinguish *known unsent* failures (safe to retry)
from *ambiguous* ones (request may have reached Discord -> DELIVERY_UNCERTAIN, no blind retry).
"""

from __future__ import annotations

import enum
import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx2 as httpx

from ..logutil import redact

# httpx2 logs every request URL at INFO, and the webhook URL *is* the credential. Silence it here
# (not only in setup_logging) so no code path can leak it.
for _name in ("httpx2", "httpcore2", "httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)

_WEBHOOK_HOSTS = {"discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"}
_PATH_RE = re.compile(r"^/api(?:/v\d{1,2})?/webhooks/(\d{15,25})/([A-Za-z0-9_.-]{20,300})$")


def validate_webhook_url(url: str) -> bool:
    """Structural check: https, Discord host, /api[/vN]/webhooks/<snowflake>/<token>, nothing else.

    The token length/alphabet is not documented as fixed, so only a generous sanity bound is used.
    """
    try:
        u = urlsplit(url.strip())
        port = u.port
    except ValueError:
        return False
    return (
        u.scheme == "https"
        and (u.hostname or "") in _WEBHOOK_HOSTS
        and port is None
        and not u.username
        and not u.password
        and not u.query
        and not u.fragment
        and bool(_PATH_RE.match(u.path))
    )


class Outcome(enum.Enum):
    DELIVERED = "delivered"
    RATE_LIMITED = "rate_limited"
    UNSENT = "unsent"  # failed before the request could reach Discord: safe to retry
    SERVER_ERROR = "server_error"
    AUTH_FAILED = "auth_failed"  # webhook deleted/invalid (401/403/404)
    PERMANENT = "permanent"  # other 4xx: payload rejected, do not retry
    UNCERTAIN = "uncertain"  # request may have been processed; outcome unknown


@dataclass(frozen=True)
class SendResult:
    outcome: Outcome
    http_status: int | None = None
    message_id: str | None = None
    retry_after: float | None = None
    bucket_wait: float | None = None
    error: str | None = None


class Transport(Protocol):
    # True when the transport lets Discord de-duplicate retries (bot API nonce + enforce_nonce).
    dedup_window_s: float

    def send(self, payload: dict[str, Any], key: str | None = None) -> SendResult: ...

    def close(self) -> None: ...


def _err(exc: BaseException) -> str:
    return redact(f"{type(exc).__name__}: {exc}")[:200]


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


_UA = {"User-Agent": "newsrelay (self-hosted, +https://discord.com/developers/docs)"}
_TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=5.0)


def _post(client: httpx.Client, url: str, **kw: Any) -> SendResult:
    try:
        resp = client.post(url, **kw)
    except (
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.PoolTimeout,
        httpx.UnsupportedProtocol,
        httpx.ProxyError,
    ) as exc:
        return SendResult(Outcome.UNSENT, error=_err(exc))  # request never left the Pi
    except httpx.HTTPError as exc:  # read/write timeout, reset, protocol error: may have arrived
        return SendResult(Outcome.UNCERTAIN, error=_err(exc))
    return classify_response(resp)


class WebhookTransport:
    """Execute Webhook (`?wait=true`). No idempotency support on Discord's side."""

    dedup_window_s = 0.0

    def __init__(
        self, webhook_url: str, *, client: httpx.Client | None = None, timeout: httpx.Timeout | None = None
    ) -> None:
        if not validate_webhook_url(webhook_url):
            raise ValueError("invalid Discord webhook URL format")
        self._url = webhook_url.strip()
        self._client = client or httpx.Client(
            timeout=timeout or _TIMEOUT, follow_redirects=False, headers=_UA
        )

    def __repr__(self) -> str:  # never expose the token
        return "WebhookTransport(<redacted>)"

    def close(self) -> None:
        self._client.close()

    def send(self, payload: dict[str, Any], key: str | None = None) -> SendResult:
        return _post(self._client, self._url, params={"wait": "true"}, json=payload)

    def delete_message(self, message_id: str) -> int:
        if not message_id.isdigit():
            raise ValueError("bad message id")
        return self._client.delete(f"{self._url}/messages/{message_id}").status_code


_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]{50,120}$")
API_BASE = "https://discord.com/api/v10"


def validate_bot_token(token: str) -> bool:
    return bool(_TOKEN_RE.match(token.strip())) and token.count(".") == 2


class BotTransport:
    """Create Message as a bot in one fixed channel.

    Each message carries a deterministic `nonce` with `enforce_nonce: true`; Discord then returns the
    already-created message instead of posting twice when the same nonce is re-sent within its
    de-duplication window ("the past few minutes"). The outbox uses this to retry ambiguous sends
    safely for a short time instead of parking them as uncertain.
    """

    dedup_window_s = 120.0

    def __init__(
        self,
        token: str,
        channel_id: str,
        *,
        client: httpx.Client | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> None:
        if not validate_bot_token(token):
            raise ValueError("invalid Discord bot token format")
        if not channel_id.isdigit() or not 15 <= len(channel_id) <= 25:
            raise ValueError("invalid Discord channel id")
        self._url = f"{API_BASE}/channels/{channel_id}/messages"
        self.token = token.strip()
        self._auth = {"Authorization": f"Bot {token.strip()}"}
        self._client = client or httpx.Client(
            timeout=timeout or _TIMEOUT, follow_redirects=False, headers=_UA
        )

    def __repr__(self) -> str:  # never expose the token
        return "BotTransport(<redacted>)"

    def close(self) -> None:
        self._client.close()

    def send(
        self, payload: dict[str, Any], key: str | None = None, channel_id: str | None = None
    ) -> SendResult:
        """Post to the fixed channel, or to `channel_id` (a day/archive channel of the same bot)."""
        body = {k: v for k, v in payload.items() if k in ("content", "allowed_mentions", "flags")}
        body.setdefault("allowed_mentions", {"parse": []})
        if key:
            body["nonce"] = key[:25]
            body["enforce_nonce"] = True
        url = self._url
        if channel_id:
            if not channel_id.isdigit() or not 15 <= len(channel_id) <= 25:
                raise ValueError("invalid Discord channel id")
            url = f"{API_BASE}/channels/{channel_id}/messages"
        return _post(self._client, url, json=body, headers=self._auth)

    def delete_message(self, message_id: str) -> int:
        if not message_id.isdigit():
            raise ValueError("bad message id")
        return self._client.delete(f"{self._url}/{message_id}", headers=self._auth).status_code


def classify_response(resp: httpx.Response) -> SendResult:
    status = resp.status_code
    bucket_wait = None
    if resp.headers.get("X-RateLimit-Remaining") == "0":
        bucket_wait = _float(resp.headers.get("X-RateLimit-Reset-After"))
    body: dict[str, Any] = {}
    try:
        parsed = resp.json()
        if isinstance(parsed, dict):
            body = parsed
    except ValueError:
        pass
    if 200 <= status < 300:
        msg_id = str(body.get("id")) if body.get("id") else None
        return SendResult(Outcome.DELIVERED, status, message_id=msg_id, bucket_wait=bucket_wait)
    if status == 429:
        retry = _float(body.get("retry_after")) or _float(resp.headers.get("Retry-After")) or 5.0
        return SendResult(Outcome.RATE_LIMITED, status, retry_after=retry, error="HTTP 429")
    if status in (401, 403, 404):
        code = body.get("code")
        return SendResult(
            Outcome.AUTH_FAILED, status, error=f"HTTP {status} code={code} (credential/permission/channel)"
        )
    if 400 <= status < 500:
        code = body.get("code")
        return SendResult(Outcome.PERMANENT, status, error=f"HTTP {status} code={code}")
    return SendResult(Outcome.SERVER_ERROR, status, error=f"HTTP {status}")
