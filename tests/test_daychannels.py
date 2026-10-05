"""Daily briefing channels: routing, archive copy before deletion, fallback, resumability."""

from __future__ import annotations

import dataclasses
from datetime import date
from typing import Any

import pytest

from conftest import publish_input, story
from newsrelay import service
from newsrelay.publishing.daychannels import DayChannels, DiscordAPIError, channel_name, day_title
from newsrelay.publishing.discord import BotTransport, Outcome, SendResult
from newsrelay.publishing.worker import Worker

FIXED = "100000000000000001"
CATEGORY = "100000000000000002"
GUILD = "900000000000000009"


class FakeGuild:
    """In-memory stand-in for the guild/channel REST endpoints."""

    def __init__(self, fail_create: bool = False) -> None:
        self.channels: dict[str, dict[str, Any]] = {
            FIXED: {"id": FIXED, "type": 0, "name": "news"},
            CATEGORY: {"id": CATEGORY, "type": 4, "name": "📰 Tagesbriefing"},
        }
        self.deleted: list[str] = []
        self.positions: list[list[dict[str, Any]]] = []
        self.fail_create = fail_create
        self._next = 200000000000000000

    def get_channel(self, cid: str) -> dict[str, Any]:
        if cid not in self.channels:
            raise DiscordAPIError(404, "Unknown Channel")
        return {**self.channels[cid], "guild_id": GUILD}

    def list_channels(self, guild_id: str) -> list[dict[str, Any]]:
        return list(self.channels.values())

    def create_channel(self, guild_id: str, body: dict[str, Any]) -> dict[str, Any]:
        if self.fail_create:
            raise DiscordAPIError(403, "Missing Permissions")
        self._next += 1
        ch = {"id": str(self._next), **body}
        self.channels[ch["id"]] = ch
        return ch

    def set_positions(self, guild_id: str, positions: list[dict[str, Any]]) -> None:
        self.positions.append(positions)

    def delete_channel(self, cid: str) -> None:
        self.deleted.append(cid)
        self.channels.pop(cid, None)

    def by_name(self, name: str) -> str:
        return next(c["id"] for c in self.channels.values() if c["name"] == name)


class FakeBot(BotTransport):
    def __init__(self, fail_after: int | None = None) -> None:
        self.sent: list[tuple[str | None, str]] = []
        self.fail_after = fail_after
        self._n = 0

    def send(
        self, payload: dict[str, Any], key: str | None = None, channel_id: str | None = None
    ) -> SendResult:
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            return SendResult(Outcome.SERVER_ERROR, 500, error="HTTP 500")
        self._n += 1
        self.sent.append((channel_id, str(payload["content"])))
        return SendResult(Outcome.DELIVERED, 200, message_id=str(300000000000000000 + self._n))

    def close(self) -> None:
        pass


def _cfg(cfg, category: str = CATEGORY):
    return dataclasses.replace(
        cfg,
        discord_daily_channels=True,
        discord_channel_id=FIXED,
        discord_daily_category_id=category,
        discord_daily_keep=2,
    )


def _publish(conn, cfg, day: int, n: int = 1) -> None:
    stories = [
        story(
            f"c{i}",
            headline=f"Story {i} of day {day} about something important",
            key_facts=[f"Fact {i} unique to day {day}", f"Second fact {i} for day {day}"],
            entities=[f"Entity{day}x{i}"],
        )
        for i in range(n)
    ]
    service.publish_digest(conn, cfg, publish_input(f"daily-news/2026-10-{day:02d}", stories))


def _drain(w: Worker) -> None:
    while w.run_once().startswith("sent"):
        pass


def test_names():
    assert channel_name(date(2026, 10, 5)) == "📅-mo-05-10"
    assert channel_name(date(2026, 10, 4)) == "📅-so-04-10"
    assert day_title(date(2026, 10, 5)) == "Montag, 05.10.2026"


def test_messages_go_to_a_channel_per_day(cfg, conn, clock):
    cfg = _cfg(cfg)
    guild, bot = FakeGuild(), FakeBot()
    w = Worker(cfg, conn, bot, days=DayChannels(cfg, conn, guild))
    _publish(conn, cfg, 3, n=2)
    _drain(w)
    sat = guild.by_name("📅-sa-03-10")
    assert [c for c, _ in bot.sent] == [sat, sat]
    assert guild.channels[sat]["parent_id"] == CATEGORY
    assert sum(c["type"] == 4 for c in guild.channels.values()) == 1  # the bot never creates a category
    assert guild.channels[guild.by_name("🗄-archiv")]["parent_id"] == CATEGORY
    assert guild.channels[sat]["topic"] == "News-Briefing vom Samstag, 03.10.2026"
    archive = guild.by_name("🗄-archiv")
    assert guild.positions[-1] == [{"id": sat, "position": 0}, {"id": archive, "position": 1}]

    clock.advance(days=1)
    _publish(conn, cfg, 4)
    _drain(w)
    sun = guild.by_name("📅-so-04-10")
    assert bot.sent[-1][0] == sun
    assert [p["id"] for p in guild.positions[-1]] == [sun, sat, archive]  # newest on top
    assert {r[0] for r in conn.execute("SELECT channel_id FROM outbox")} == {sat, sun}


def test_old_days_are_copied_to_archive_then_deleted(cfg, conn, clock):
    cfg = _cfg(cfg)  # keep 2 days
    guild, bot = FakeGuild(), FakeBot()
    w = Worker(cfg, conn, bot, days=DayChannels(cfg, conn, guild))
    for day in (3, 4, 5):
        _publish(conn, cfg, day, n=2)
        _drain(w)
        clock.advance(days=1)
    sat = guild.by_name("📅-sa-03-10")
    day_posts = [text for c, text in bot.sent if c == sat]

    assert w.maintain_days() == 1
    archive = guild.by_name("🗄-archiv")
    copied = [text for c, text in bot.sent if c == archive]
    assert copied[0].startswith("# 📅 Samstag, 03.10.2026")
    assert copied[1:] == day_posts  # identical text, original order
    assert guild.deleted == [sat]
    assert (
        conn.execute("SELECT state FROM discord_day_channels WHERE day='2026-10-03'").fetchone()[0]
        == "archived"
    )
    assert "📅-so-04-10" in {c["name"] for c in guild.channels.values()}
    assert w.maintain_days() == 0  # rate-limited check, and nothing else is due


def test_archive_copy_resumes_without_duplicates(cfg, conn, clock):
    cfg = _cfg(cfg)
    guild = FakeGuild()
    bot = FakeBot()
    days = DayChannels(cfg, conn, guild)
    w = Worker(cfg, conn, bot, days=days)
    for day in (3, 4, 5):
        _publish(conn, cfg, day, n=3)
        _drain(w)
        clock.advance(days=1)
    sat = guild.by_name("📅-sa-03-10")
    before = len(bot.sent)
    bot.fail_after = before + 2  # header + first story, then Discord errors
    assert days.archive_old(lambda p, k, c: bot.send(p, k, c), lambda s: None) == 0
    assert guild.deleted == []  # nothing deleted while the copy is incomplete
    bot.fail_after = None
    assert days.archive_old(lambda p, k, c: bot.send(p, k, c), lambda s: None) == 1
    archive = guild.by_name("🗄-archiv")
    copied = [text for c, text in bot.sent[before:] if c == archive]
    assert len(copied) == 1 + 3 and sum(t.startswith("# 📅") for t in copied) == 1
    assert guild.deleted == [sat]


def test_missing_permission_falls_back_to_fixed_channel(cfg, conn, clock):
    cfg = _cfg(cfg)
    guild, bot = FakeGuild(fail_create=True), FakeBot()
    w = Worker(cfg, conn, bot, days=DayChannels(cfg, conn, guild))
    _publish(conn, cfg, 3)
    _drain(w)
    assert bot.sent and bot.sent[0][0] is None  # fixed channel, nothing lost
    assert conn.execute("SELECT state FROM outbox").fetchone()[0] == "delivered"


@pytest.mark.parametrize("category", ["", "100000000000000077", FIXED])  # unset, unknown, not a category
def test_without_valid_category_posts_go_to_fixed_channel(cfg, conn, clock, category):
    cfg = _cfg(cfg, category)
    guild, bot = FakeGuild(), FakeBot()
    w = Worker(cfg, conn, bot, days=DayChannels(cfg, conn, guild))
    _publish(conn, cfg, 3)
    _drain(w)
    assert bot.sent[0][0] is None
    assert len(guild.channels) == 2  # nothing was created


def test_feature_off_by_default(cfg):
    assert cfg.discord_daily_channels is False
    assert cfg.discord_daily_category_id == ""
