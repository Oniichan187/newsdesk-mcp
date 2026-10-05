"""Daily briefing channels (bot mode, optional).

Layout inside the category `discord_daily_category_id` (created by the server owner; the bot never
creates or moves the category itself):

    📰 Tagesbriefing        category
      📅-mo-05-10           one text channel per local day, newest on top
      📅-so-04-10           ... the last `discord_daily_keep` days
      🗄-archiv             older days, copied here chronologically before their channel is deleted

Delivery never depends on this: when a channel cannot be resolved (no category id configured,
category deleted, missing "Manage Channels" permission, API error), `channel_for` returns None and the message goes to the fixed
`discord_channel_id` as before. The archive copy comes from the outbox payloads (the exact text that
was posted), not from reading Discord, and is resumable: `header_sent` / `archived_upto` record
progress, and a channel is deleted only after everything was copied.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Callable
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx2 as httpx

from .. import timeutil
from ..config import Config
from ..database import tx
from ..logutil import log
from .discord import _TIMEOUT, _UA, API_BASE, Outcome, SendResult

logger = logging.getLogger("newsrelay.daychannels")

WEEKDAY_SHORT = ("mo", "di", "mi", "do", "fr", "sa", "so")
WEEKDAY_LONG = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")
GUILD_TEXT = 0
GUILD_CATEGORY = 4
LAYOUT_TTL_S = 600.0  # re-read the guild's channel list at most this often
FAILURE_BACKOFF_S = 600.0  # after an API failure, fall back to the fixed channel for this long


class DiscordAPIError(RuntimeError):
    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(f"HTTP {status} {message}".strip())
        self.status = status


class DiscordREST:
    """The few guild/channel endpoints this feature needs. Honors 429 with a bounded retry."""

    def __init__(self, token: str, *, client: httpx.Client | None = None) -> None:
        self._auth = {"Authorization": f"Bot {token.strip()}"}
        self._client = client or httpx.Client(timeout=_TIMEOUT, follow_redirects=False, headers=_UA)

    def __repr__(self) -> str:
        return "DiscordREST(<redacted>)"

    def close(self) -> None:
        self._client.close()

    def _call(self, method: str, path: str, body: Any = None) -> Any:
        for _ in range(4):
            resp = self._client.request(method, API_BASE + path, json=body, headers=self._auth)
            if resp.status_code == 429:
                try:
                    wait = float(resp.json().get("retry_after", 2.0))
                except (ValueError, AttributeError):
                    wait = 2.0
                time.sleep(min(10.0, max(0.5, wait)))
                continue
            if resp.status_code >= 400:
                raise DiscordAPIError(resp.status_code, resp.text[:200])
            return resp.json() if resp.content else None
        raise DiscordAPIError(429, "rate limited")

    def get_channel(self, channel_id: str) -> dict[str, Any]:
        result: dict[str, Any] = self._call("GET", f"/channels/{channel_id}")
        return result

    def list_channels(self, guild_id: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = self._call("GET", f"/guilds/{guild_id}/channels")
        return result

    def create_channel(self, guild_id: str, body: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = self._call("POST", f"/guilds/{guild_id}/channels", body)
        return result

    def set_positions(self, guild_id: str, positions: list[dict[str, Any]]) -> None:
        self._call("PATCH", f"/guilds/{guild_id}/channels", positions)

    def delete_channel(self, channel_id: str) -> None:
        try:
            self._call("DELETE", f"/channels/{channel_id}")
        except DiscordAPIError as exc:
            if exc.status != 404:  # already gone is fine
                raise


def local_day(iso: str, tz: str) -> date:
    return timeutil.parse_iso(iso).astimezone(ZoneInfo(tz)).date()


def channel_name(day: date) -> str:
    return f"📅-{WEEKDAY_SHORT[day.weekday()]}-{day:%d-%m}"


def day_title(day: date) -> str:
    return f"{WEEKDAY_LONG[day.weekday()]}, {day:%d.%m.%Y}"


Sender = Callable[[dict[str, Any], str, str], SendResult]  # (payload, nonce key, channel id)


class DayChannels:
    def __init__(self, cfg: Config, conn: sqlite3.Connection, api: Any) -> None:
        self.cfg = cfg
        self.conn = conn
        self.api = api
        self._channels: dict[str, dict[str, Any]] = {}
        self._layout_at: float | None = None  # None = never read (monotonic() starts at boot)
        self._failed_until = 0.0

    # -- layout --------------------------------------------------------------------------------

    def _meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row else None

    def _set_meta(self, key: str, value: str) -> None:
        with tx(self.conn):
            self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))

    def _refresh(self, guild_id: str) -> None:
        self._channels = {str(c["id"]): c for c in self.api.list_channels(guild_id)}
        self._layout_at = time.monotonic()

    def _archive_channel(self, guild_id: str, category: str) -> str:
        """The archive text channel in the category: remembered id, else by name, else created."""
        known = self._meta("daily_archive_id")
        if known and known in self._channels:
            return known
        name = self.cfg.discord_daily_archive
        for c in self._channels.values():
            if (
                c.get("type") == GUILD_TEXT
                and str(c.get("parent_id")) == category
                and str(c.get("name", "")).lower() == name.lower()
            ):
                cid = str(c["id"])
                break
        else:
            body = {
                "name": name,
                "type": GUILD_TEXT,
                "parent_id": category,
                "topic": "Archiv der Tagesbriefings, chronologisch.",
            }
            created = self.api.create_channel(guild_id, body)
            cid = str(created["id"])
            self._channels[cid] = created
            log(logger, logging.INFO, "archive channel created", channel_id=cid)
        self._set_meta("daily_archive_id", cid)
        return cid

    def _layout(self) -> tuple[str, str, str]:
        category = self.cfg.discord_daily_category_id.strip()
        if not category.isdigit():
            raise ValueError("discord_daily_category_id is not set")
        guild_id = self._meta("daily_guild_id")
        if not guild_id or self._meta("daily_guild_category") != category:
            guild_id = str(self.api.get_channel(category)["guild_id"])
            self._set_meta("daily_guild_id", guild_id)
            self._set_meta("daily_guild_category", category)
        stale = self._layout_at is None or time.monotonic() - self._layout_at > LAYOUT_TTL_S
        if stale or category not in self._channels:
            self._refresh(guild_id)
        if self._channels.get(category, {}).get("type") != GUILD_CATEGORY:
            raise ValueError("discord_daily_category_id is not a category of this server")
        return guild_id, category, self._archive_channel(guild_id, category)

    def _order(self, guild_id: str, archive: str) -> None:
        """Newest day on top, archive at the bottom."""
        days = [
            r["channel_id"]
            for r in self.conn.execute(
                "SELECT channel_id FROM discord_day_channels WHERE state != 'archived' ORDER BY day DESC"
            )
        ]
        ids = [c for c in days if c in self._channels] + [archive]
        self.api.set_positions(guild_id, [{"id": cid, "position": i} for i, cid in enumerate(ids)])

    # -- routing -------------------------------------------------------------------------------

    def channel_for(self, created_at: str) -> str | None:
        """Channel for a message enqueued at `created_at`; None = use the fixed channel."""
        if time.monotonic() < self._failed_until:
            return None
        try:
            return self._channel_for(local_day(created_at, self.cfg.display_timezone))
        except (DiscordAPIError, httpx.HTTPError, KeyError, ValueError) as exc:
            self._failed_until = time.monotonic() + FAILURE_BACKOFF_S
            log(logger, logging.WARNING, "day channel unavailable; using fixed channel", error=str(exc)[:200])
            return None

    def _channel_for(self, day: date) -> str:
        guild_id, category, archive = self._layout()
        row = self.conn.execute(
            "SELECT channel_id, state FROM discord_day_channels WHERE day = ?", (day.isoformat(),)
        ).fetchone()
        if row is not None and row["state"] != "active":
            return archive  # late message for a day that is already archived
        if row is not None and row["channel_id"] in self._channels:
            return str(row["channel_id"])
        created = self.api.create_channel(
            guild_id,
            {
                "name": channel_name(day),
                "type": GUILD_TEXT,
                "parent_id": category,
                "topic": f"News-Briefing vom {day_title(day)}",
            },
        )
        cid = str(created["id"])
        self._channels[cid] = created
        with tx(self.conn):
            self.conn.execute(
                "INSERT INTO discord_day_channels(day, channel_id, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(day) DO UPDATE SET channel_id = excluded.channel_id",
                (day.isoformat(), cid, timeutil.now_iso()),
            )
        log(logger, logging.INFO, "day channel created", day=day.isoformat(), channel_id=cid)
        try:
            self._order(guild_id, archive)
        except (DiscordAPIError, httpx.HTTPError) as exc:  # cosmetic only
            log(logger, logging.WARNING, "could not reorder channels", error=str(exc)[:200])
        return cid

    # -- archiving -----------------------------------------------------------------------------

    def due_for_archive(self, today: date) -> list[sqlite3.Row]:
        rows = self.conn.execute(
            "SELECT * FROM discord_day_channels WHERE state != 'archived' ORDER BY day DESC"
        ).fetchall()
        keep = max(1, self.cfg.discord_daily_keep)
        old = [r for r in rows[keep:] if r["day"] < today.isoformat()]
        return sorted(old, key=lambda r: r["day"])  # oldest first = chronological archive

    def archive_old(self, send: Sender, pause: Callable[[float], None], now: datetime | None = None) -> int:
        """Copy retired days to the archive, then delete their channels. Returns days finished."""
        today = (now or timeutil.now()).astimezone(ZoneInfo(self.cfg.display_timezone)).date()
        due = self.due_for_archive(today)
        if not due:
            return 0
        _, _, archive = self._layout()
        done = 0
        for row in due:
            if not self._archive_day(row, archive, send, pause):
                break  # keep chronological order: never skip ahead of an unfinished day
            done += 1
        return done

    def _archive_day(
        self, row: sqlite3.Row, archive: str, send: Sender, pause: Callable[[float], None]
    ) -> bool:
        day, channel = row["day"], row["channel_id"]
        busy = self.conn.execute(
            "SELECT 1 FROM outbox WHERE channel_id = ? AND state IN ('pending','dispatching') LIMIT 1",
            (channel,),
        ).fetchone()
        if busy:
            return False
        with tx(self.conn):
            self.conn.execute("UPDATE discord_day_channels SET state = 'archiving' WHERE day = ?", (day,))
        items = self.conn.execute(
            "SELECT id, payload FROM outbox WHERE channel_id = ? AND state = 'delivered' AND id > ? "
            "ORDER BY id",
            (channel, row["archived_upto"]),
        ).fetchall()
        if not row["header_sent"]:
            total = self.conn.execute(
                "SELECT count(*) FROM outbox WHERE channel_id = ? AND state = 'delivered'", (channel,)
            ).fetchone()[0]
            header = {
                "content": f"# 📅 {day_title(date.fromisoformat(day))}\n-# {total} Nachricht(en) aus "
                f"dem Tageskanal",
                "allowed_mentions": {"parse": []},
            }
            if not self._send(send, header, f"arch-h-{day}", archive):
                return False
            with tx(self.conn):
                self.conn.execute("UPDATE discord_day_channels SET header_sent = 1 WHERE day = ?", (day,))
            pause(self.cfg.min_send_interval_s)
        for item in items:
            if item["payload"]:
                if not self._send(send, json.loads(item["payload"]), f"arch-{item['id']}", archive):
                    return False
                pause(self.cfg.min_send_interval_s)
            with tx(self.conn):
                self.conn.execute(
                    "UPDATE discord_day_channels SET archived_upto = ? WHERE day = ?", (item["id"], day)
                )
        self.api.delete_channel(channel)
        self._channels.pop(channel, None)
        with tx(self.conn):
            self.conn.execute(
                "UPDATE discord_day_channels SET state = 'archived', archived_at = ? WHERE day = ?",
                (timeutil.now_iso(), day),
            )
        log(logger, logging.INFO, "day channel archived", day=day, messages=len(items))
        return True

    @staticmethod
    def _send(send: Sender, payload: dict[str, Any], key: str, channel: str) -> bool:
        result = send(payload, key, channel)
        if result.outcome is not Outcome.DELIVERED:
            log(
                logger,
                logging.WARNING,
                "archive copy paused",
                outcome=result.outcome.value,
                http_status=result.http_status,
            )
            return False
        return True
