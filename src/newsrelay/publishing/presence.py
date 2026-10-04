"""Discord Gateway presence: keeps the bot shown as *online* while the Pi runs.

Posting never depends on this — messages go through the REST API (BotTransport). This service
only holds one Gateway session (intents=0, no message content) with a status/activity, and
recovers on its own: heartbeat with ACK check (zombie detection), resume on reconnect requests,
re-identify on invalid sessions, bounded exponential backoff with jitter, and a long pause on
fatal close codes (bad token, disallowed intents) so Discord is never hammered.

systemd contract (newsrelay-presence.service, Type=notify): READY=1 after the first READY or
RESUMED event; WATCHDOG=1 from the receive loop itself, so a hung loop gets restarted.
"""

from __future__ import annotations

import json
import logging
import random
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..logutil import log, redact
from ..sdnotify import notify as sd_notify

logger = logging.getLogger("newsrelay.presence")

GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"
# Close codes after which retrying soon is pointless (auth failed, sharding/intents/version errors).
FATAL_CLOSE_CODES = {4004, 4010, 4011, 4012, 4013, 4014}
FATAL_PAUSE_S = 3600.0
# Close codes after which a RESUME is not allowed (session invalid) -> fresh IDENTIFY.
NO_RESUME_CODES = {4007, 4009}

ACTIVITY_TYPES = {"playing": 0, "listening": 2, "watching": 3, "competing": 5}


class _Reconnect(Exception):
    """Leave the current connection; resume if possible."""

    def __init__(self, reason: str, resumable: bool = True) -> None:
        super().__init__(reason)
        self.resumable = resumable


@dataclass
class Session:
    session_id: str | None = None
    resume_url: str | None = None
    seq: int | None = None
    fails: int = 0
    events: list[str] = field(default_factory=list)  # test/diagnostic trail (bounded)

    def note(self, event: str) -> None:
        self.events.append(event)
        del self.events[:-50]

    def can_resume(self) -> bool:
        return bool(self.session_id and self.resume_url)

    def clear(self) -> None:
        self.session_id = self.resume_url = None
        self.seq = None


Connector = Callable[[str], Any]


def _default_connector(url: str) -> Any:
    from websockets.sync.client import connect

    # Discord payloads are small with intents=0; keep library pings off (the Gateway heartbeat is ours).
    return connect(url, open_timeout=20, close_timeout=5, ping_interval=None, max_size=2**22)


class Presence:
    def __init__(
        self,
        token: str,
        *,
        status: str = "online",
        activity_type: str = "watching",
        activity_name: str = "the news",
        runtime_dir: Path | None = None,
        connector: Connector = _default_connector,
        notify: Callable[[str], object] = sd_notify,
        stop: threading.Event | None = None,
        gateway_url: str = GATEWAY_URL,
    ) -> None:
        self._token = token.strip()
        self.status = status
        self.activity = {"name": activity_name[:128], "type": ACTIVITY_TYPES.get(activity_type, 3)}
        self.runtime_dir = runtime_dir
        self.connector = connector
        self.notify = notify
        self.stop = stop or threading.Event()
        self.gateway_url = gateway_url
        self.session = Session()
        self._ready_sent = False

    def __repr__(self) -> str:
        return "Presence(<redacted>)"

    # ---------------------------------------------------------------- payloads
    def _identify(self) -> dict[str, Any]:
        return {
            "op": 2,
            "d": {
                "token": self._token,
                "intents": 0,
                "properties": {"os": "linux", "browser": "newsrelay", "device": "newsrelay"},
                "presence": {
                    "status": self.status,
                    "activities": [self.activity],
                    "since": None,
                    "afk": False,
                },
            },
        }

    def _resume(self) -> dict[str, Any]:
        return {
            "op": 6,
            "d": {"token": self._token, "session_id": self.session.session_id, "seq": self.session.seq},
        }

    # ---------------------------------------------------------------- state file (tmpfs)
    def _state(self, value: str) -> None:
        if self.runtime_dir is None:
            return
        try:
            (self.runtime_dir / "presence.state").write_text(
                f"{value} {int(time.time())}\n", encoding="utf-8"
            )
        except OSError:
            pass

    # ---------------------------------------------------------------- one connection
    def _connection(self) -> None:
        resuming = self.session.can_resume()
        url = (self.session.resume_url or self.gateway_url) if resuming else self.gateway_url
        if resuming and "?" not in url:
            url += "/?v=10&encoding=json"
        ws = self.connector(url)
        try:
            hello = json.loads(ws.recv(timeout=30))
            if hello.get("op") != 10:
                raise _Reconnect("expected HELLO", resumable=resuming)
            interval = float(hello["d"]["heartbeat_interval"]) / 1000.0
            ws.send(json.dumps(self._resume() if resuming else self._identify()))
            self.session.note("resume" if resuming else "identify")
            next_beat = time.monotonic() + interval * random.random()
            acked = True
            while not self.stop.is_set():
                self.notify("WATCHDOG=1")
                now = time.monotonic()
                if now >= next_beat:
                    if not acked:
                        raise _Reconnect("no heartbeat ACK (zombie connection)")
                    ws.send(json.dumps({"op": 1, "d": self.session.seq}))
                    acked = False
                    next_beat = now + interval
                try:
                    raw = ws.recv(timeout=max(0.05, min(next_beat - time.monotonic(), 20.0)))
                except TimeoutError:
                    continue
                msg = json.loads(raw)
                op = msg.get("op")
                if msg.get("s") is not None:
                    self.session.seq = int(msg["s"])
                if op == 11:
                    acked = True
                elif op == 1:
                    ws.send(json.dumps({"op": 1, "d": self.session.seq}))
                elif op == 7:
                    raise _Reconnect("server requested reconnect")
                elif op == 9:
                    resumable = bool(msg.get("d"))
                    if not resumable:
                        self.session.clear()
                    self.stop.wait(random.uniform(1, 5))
                    raise _Reconnect("invalid session", resumable=resumable)
                elif op == 0:
                    t = msg.get("t")
                    if t == "READY":
                        d = msg.get("d") or {}
                        self.session.session_id = d.get("session_id")
                        self.session.resume_url = d.get("resume_gateway_url")
                        self._online("ready")
                    elif t == "RESUMED":
                        self._online("resumed")
        finally:
            try:
                ws.close()
            except Exception:  # noqa: S110 - closing a broken socket must not mask the real error
                pass

    def _online(self, how: str) -> None:
        self.session.fails = 0
        self.session.note(how)
        self._state("online")
        log(logger, logging.INFO, "presence online", how=how, activity=self.activity["name"])
        if not self._ready_sent:
            self.notify("READY=1")
            self._ready_sent = True

    # ---------------------------------------------------------------- supervisor loop
    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while not self.stop.is_set() and time.monotonic() < end:
            self.notify("WATCHDOG=1")
            self.stop.wait(min(20.0, end - time.monotonic()))

    def run_forever(self) -> None:
        if not self._ready_sent:
            # Being offline (no internet yet at boot) is a normal state, not a failed start.
            self.notify("READY=1")
            self._ready_sent = True
        while not self.stop.is_set():
            try:
                self._connection()
            except _Reconnect as exc:
                if not exc.resumable:
                    self.session.clear()
                self.session.note(f"reconnect:{exc}")
                log(logger, logging.INFO, "presence reconnecting", reason=str(exc))
                self._state("reconnecting")
                self.stop.wait(random.uniform(0.5, 2.0))
                continue
            except Exception as exc:
                code = getattr(getattr(exc, "rcvd", None), "code", None)
                self._state("offline")
                if code in NO_RESUME_CODES:
                    self.session.clear()
                if code in FATAL_CLOSE_CODES:
                    self.session.note(f"fatal:{code}")
                    log(logger, logging.ERROR, "presence: Discord refused the session (check bot token)",
                        close_code=code)  # fmt: skip
                    self._sleep(FATAL_PAUSE_S)
                    continue
                self.session.fails += 1
                delay = min(300.0, 2.0 * 2 ** min(self.session.fails, 8)) * random.uniform(0.7, 1.3)
                self.session.note(f"error:{type(exc).__name__}")
                log(logger, logging.WARNING, "presence connection lost", error=redact(f"{type(exc).__name__}: {exc}")[:200],
                    retry_in_s=round(delay))  # fmt: skip
                self._sleep(delay)
        self._state("offline")


def main(cfg: Any) -> int:
    from ..config import BOT_TOKEN_CREDENTIAL, read_secret
    from .discord import validate_bot_token

    token = read_secret(cfg, BOT_TOKEN_CREDENTIAL)
    if not cfg.discord_presence or not token or not validate_bot_token(token):
        log(logger, logging.INFO, "presence disabled (no bot token or discord_presence = false)")
        sd_notify("READY=1")
        sd_notify("STOPPING=1")
        return 0
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    p = Presence(
        token,
        status=cfg.discord_presence_status,
        activity_type=cfg.discord_presence_activity_type,
        activity_name=cfg.discord_presence_text,
        runtime_dir=cfg.runtime_dir,
        stop=stop,
    )
    p.run_forever()
    sd_notify("STOPPING=1")
    return 0
