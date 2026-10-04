"""Structured journald-friendly logging with secret redaction and repeat suppression."""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from typing import Any

_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"https?://[^\s/]*discord(?:app)?\.com/api(?:/v\d+)?/webhooks/[^\s\"']+", re.I),
        "[REDACTED_WEBHOOK]",
    ),
    (re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer\s+)?[^\s,\"']+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [REDACTED]"),
    # Discord bot tokens: base64(user id) . timestamp . hmac — with or without the "Bot " prefix
    (
        re.compile(r"\b[MNO][A-Za-z0-9_-]{22,30}\.[A-Za-z0-9_-]{5,8}\.[A-Za-z0-9_-]{25,45}\b"),
        "[REDACTED_BOT_TOKEN]",
    ),
    (
        re.compile(
            r"(?i)\b(access_token|refresh_token|code|client_secret|token|password|passphrase)="
            r"[^&\s\"']+"
        ),
        r"\1=[REDACTED]",
    ),
    (re.compile(r"(?i)(cookie\s*[:=]\s*)[^\n]+"), r"\1[REDACTED]"),
]


def redact(text: str) -> str:
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    return text


class RedactingJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "lvl": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update({k: redact(str(v)) if isinstance(v, str) else v for k, v in extra.items()})
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))[-2000:]
        return json.dumps(payload, ensure_ascii=False, default=str)


class RepeatSuppressFilter(logging.Filter):
    """Drop identical WARNING+/ERROR messages repeated within `window` seconds."""

    def __init__(self, window: float = 300.0) -> None:
        super().__init__()
        self.window = window
        self._seen: dict[str, float] = {}

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.WARNING:
            return True
        key = f"{record.name}:{record.getMessage()[:200]}"
        t = time.monotonic()
        last = self._seen.get(key)
        if last is not None and t - last < self.window:
            return False
        self._seen[key] = t
        if len(self._seen) > 500:
            self._seen.clear()
        return True


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(RedactingJsonFormatter())
    handler.addFilter(RepeatSuppressFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # httpx-style libraries log full URLs at INFO; keep them quiet (URLs may contain the webhook token).
    for noisy in ("httpx", "httpx2", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def log(logger: logging.Logger, level: int, msg: str, **fields: Any) -> None:
    logger.log(level, msg, extra={"fields": fields})
