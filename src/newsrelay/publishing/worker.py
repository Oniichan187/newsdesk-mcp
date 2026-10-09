"""Single Discord delivery worker (systemd: newsrelay-worker.service, Type=notify).

Readiness/liveness contract with systemd:
- READY=1 is sent only after the singleton lock is held, config + DB are open, the schema is
  validated, the webhook credential is parsed and crash recovery of the outbox has completed.
- WATCHDOG=1 is sent *from the processing loop itself* on every iteration (and while it sleeps
  between iterations). There is no helper thread, so a hung loop stops pinging and systemd
  (WatchdogSec=) kills and restarts the worker.
- The loop also refreshes a tmpfs heartbeat file; a missing or stale heartbeat is reported as
  unhealthy by /healthz and `newsrelay status`.
"""

from __future__ import annotations

import fcntl
import json
import logging
import signal
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO

from .. import __version__, timeutil
from ..config import BOT_TOKEN_CREDENTIAL, WEBHOOK_CREDENTIAL, Config, read_secret
from ..database import DiskFullError, check_disk, open_db
from ..logutil import log
from ..sdnotify import notify as sd_notify
from . import outbox
from .daychannels import DayChannels, DiscordREST
from .discord import BotTransport, SendResult, Transport, WebhookTransport

logger = logging.getLogger("newsrelay.worker")

# Longest uninterrupted wait inside the loop; must stay well below WatchdogSec (180 s).
MAX_SLEEP_SLICE_S = 20.0
# How often an idle worker checks whether old day channels must be archived.
ARCHIVE_CHECK_S = 300.0


class SingleInstanceError(RuntimeError):
    pass


def acquire_lock(runtime_dir: Path) -> IO[str]:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    fh = (runtime_dir / "worker.lock").open("w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        fh.close()
        raise SingleInstanceError("another worker holds the lock") from exc
    return fh


def heartbeat(runtime_dir: Path) -> None:
    # tmpfs file (/run): liveness without SD-card writes.
    (runtime_dir / "worker.heartbeat").touch()


def heartbeat_age(runtime_dir: Path) -> float | None:
    """Seconds since the loop last ran, or None if it never ran (= unhealthy)."""
    try:
        return time.time() - (runtime_dir / "worker.heartbeat").stat().st_mtime
    except FileNotFoundError:
        return None


class Worker:
    def __init__(
        self,
        cfg: Config,
        conn: sqlite3.Connection,
        transport: Transport | None,
        stop: threading.Event | None = None,
        notify: Callable[[str], object] = sd_notify,
        days: DayChannels | None = None,
    ) -> None:
        self.cfg = cfg
        self.conn = conn
        self.transport = transport
        self.stop = stop or threading.Event()
        self.notify = notify
        self.days = days
        self._warned_no_webhook = False
        # monotonic() counts from boot, so 0.0 would look "recent" on a freshly started host
        self._archive_checked: float | None = None
        self._autopublish_checked: float | None = None

    def _alive(self) -> None:
        heartbeat(self.cfg.runtime_dir)
        self.notify("WATCHDOG=1")

    def sleep(self, seconds: float) -> None:
        """Interruptible sleep that keeps proving liveness from the loop thread."""
        deadline = time.monotonic() + max(0.0, seconds)
        while not self.stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self.stop.wait(min(remaining, MAX_SLEEP_SLICE_S))
            self._alive()

    def run_once(self) -> str:
        """Process at most one due item. Returns 'sent:<state>', 'idle', or 'blocked:<reason>'."""
        self._alive()
        if self.transport is None:
            if not self._warned_no_webhook:
                log(logger, logging.WARNING, "no Discord webhook configured; queue is held")
                self._warned_no_webhook = True
            return "blocked:no-webhook"
        try:
            check_disk(self.cfg.db_path, self.cfg.min_free_disk_mb)
        except DiskFullError as exc:
            log(logger, logging.ERROR, "disk nearly full; worker paused", error=str(exc))
            return "blocked:disk"
        due, _ = outbox.next_due(self.conn)
        if due is None:
            return "idle"
        item = outbox.claim(self.conn, due["id"])
        if item is None:
            return "idle"
        assert item.payload is not None
        channel = self._route(item.id)
        # no DB transaction is open during the network call
        if channel:
            result = self._send_to(_load(item.payload), item.idempotency_key, channel)
        else:
            result = self.transport.send(_load(item.payload), key=item.idempotency_key)
        state = outbox.finish(
            self.conn, item, result, self.cfg, getattr(self.transport, "dedup_window_s", 0.0)
        )
        self.sleep(max(self.cfg.min_send_interval_s, result.bucket_wait or 0.0))
        return f"sent:{state}"

    def _route(self, outbox_id: int) -> str | None:
        """Day channel for an item (sticky across retries so the nonce stays in one channel)."""
        if self.days is None:
            return None
        row = self.conn.execute(
            "SELECT created_at, channel_id FROM outbox WHERE id = ?", (outbox_id,)
        ).fetchone()
        if row["channel_id"]:
            return str(row["channel_id"])
        channel = self.days.channel_for(row["created_at"])
        if channel:
            self.conn.execute("UPDATE outbox SET channel_id = ? WHERE id = ?", (channel, outbox_id))
        return channel

    def _send_to(self, payload: dict[str, object], key: str, channel: str) -> SendResult:
        assert isinstance(self.transport, BotTransport)
        return self.transport.send(payload, key=key, channel_id=channel)

    def autopublish(self) -> int:
        """Publish staged runs whose final publish call never arrived (checked at most once a minute)."""
        if self._autopublish_checked is not None and time.monotonic() - self._autopublish_checked < 60:
            return 0
        self._autopublish_checked = time.monotonic()
        from ..service import autopublish_staged

        try:
            return autopublish_staged(self.conn, self.cfg)
        except Exception as exc:  # never let this take down delivery
            log(logger, logging.ERROR, "auto-publish of staged stories failed", error=str(exc)[:300])
            return 0

    def maintain_days(self) -> int:
        """Archive old day channels when the queue is idle (at most every ARCHIVE_CHECK_S)."""
        if self.days is None or (
            self._archive_checked is not None and time.monotonic() - self._archive_checked < ARCHIVE_CHECK_S
        ):
            return 0
        self._archive_checked = time.monotonic()
        try:
            return self.days.archive_old(lambda p, k, c: self._send_to(p, k, c), self.sleep)
        except Exception as exc:  # never let archiving take down delivery
            log(logger, logging.WARNING, "day channel archiving failed", error=str(exc)[:200])
            return 0

    def wait_time(self) -> float:
        _, wake_at = outbox.next_due(self.conn)
        if wake_at is None:
            return self.cfg.worker_poll_s
        delta = (timeutil.parse_iso(wake_at) - timeutil.now()).total_seconds()
        return max(1.0, min(self.cfg.worker_poll_s, delta))

    def prepare(self) -> int:
        """Crash recovery; must complete before READY=1."""
        window = float(getattr(self.transport, "dedup_window_s", 0.0))
        recovered = outbox.recover_after_crash(self.conn, window)
        log(logger, logging.INFO, "worker prepared", version=__version__, recovered_uncertain=recovered)
        return recovered

    def loop(self) -> None:
        consecutive_errors = 0
        while not self.stop.is_set():
            try:
                res = self.run_once()
                consecutive_errors = 0
            except sqlite3.OperationalError as exc:  # e.g. database locked / disk I/O / read-only FS
                consecutive_errors += 1
                log(logger, logging.ERROR, "database error in worker loop", error=str(exc))
                self.sleep(min(120.0, 5.0 * 2 ** min(consecutive_errors, 5)))
                continue
            if res == "idle":
                self.autopublish()
                self.maintain_days()
            if not res.startswith("sent"):
                self.sleep(self.wait_time())
        log(logger, logging.INFO, "worker stopping")

    def run_forever(self) -> None:
        self.prepare()
        self.notify("READY=1")
        self.loop()


def _load(payload: str) -> dict[str, object]:
    data = json.loads(payload)
    assert isinstance(data, dict)
    return data


def make_transport(cfg: Config) -> BotTransport | WebhookTransport | None:
    """Bot (token credential + discord_channel_id) if configured, else webhook, else None."""
    token = read_secret(cfg, BOT_TOKEN_CREDENTIAL)
    if token and cfg.discord_channel_id:
        try:
            return BotTransport(token, cfg.discord_channel_id)
        except ValueError as exc:
            log(logger, logging.ERROR, "bot configuration invalid", error=str(exc))
            return None
    url = read_secret(cfg, WEBHOOK_CREDENTIAL)
    if not url:
        return None
    try:
        return WebhookTransport(url)
    except ValueError:
        log(logger, logging.ERROR, "configured webhook credential has invalid format")
        return None


def transport_mode(transport: object | None) -> str:
    return {BotTransport: "bot", WebhookTransport: "webhook"}.get(type(transport), "none")


def main(cfg: Config, notify: Callable[[str], object] = sd_notify) -> int:
    """Service entry point. Any failure before READY=1 exits non-zero (systemd sees a failed start)."""
    stop = threading.Event()

    def _sig(signum: int, _frame: object) -> None:
        log(logger, logging.INFO, "signal received", signal=signum)
        stop.set()

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    lock: IO[str] | None = None
    conn: sqlite3.Connection | None = None
    transport: BotTransport | WebhookTransport | None = None
    try:
        lock = acquire_lock(cfg.runtime_dir)
        conn = open_db(cfg.db_path)  # migrates / refuses a newer schema
        transport = make_transport(cfg)
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('webhook_configured', ?)",
            ("yes" if transport else "no",),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('discord_mode', ?)", (transport_mode(transport),)
        )
        days = None
        if cfg.discord_daily_channels and isinstance(transport, BotTransport):
            days = DayChannels(cfg, conn, DiscordREST(transport.token))
        Worker(cfg, conn, transport, stop, notify, days).run_forever()
    except SingleInstanceError as exc:
        log(logger, logging.ERROR, "worker not started", error=str(exc))
        return 1
    finally:
        notify("STOPPING=1")
        if transport is not None:
            transport.close()
        if conn is not None:
            conn.close()
        if lock is not None:
            lock.close()
    return 0
