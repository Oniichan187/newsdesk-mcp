"""Briefing files on disk and the stories of a day, read back from the database."""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import timeutil
from ..config import Config
from ..logutil import log
from ..schemas import Story
from .text import CATEGORY_LABEL, CONFIDENCE_LABEL, StoryText, clean, parse_body, story_text

logger = logging.getLogger("newsrelay.briefing")
_SAFE = re.compile(r"[^a-z0-9-]+")


def local_day(when: datetime, tz: str) -> date:
    return when.astimezone(ZoneInfo(tz)).date()


def pdf_path(cfg: Config, day: date, run_key: str) -> Path:
    slug = _SAFE.sub("-", run_key.split("/", 1)[0].lower()).strip("-") or "run"
    return cfg.briefing_dir / day.isoformat() / f"briefing-{day.isoformat()}-{slug}.pdf"


def write_pdf(cfg: Config, run_key: str, stories: list[Story]) -> Path | None:
    """Render and store the run's PDF; None (logged) if rendering fails — posting goes on without it."""
    from .pdf import render_pdf

    day = local_day(timeutil.now(), cfg.display_timezone)
    path = pdf_path(cfg, day, run_key)
    try:
        texts = [story_text(s, _label(s, cfg.display_timezone)) for s in stories]
        data = render_pdf(day, texts, cfg.reader_country)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
    except Exception as exc:  # a broken PDF must never block the news itself
        log(logger, logging.ERROR, "briefing pdf failed", run_key=run_key, error=str(exc)[:300])
        return None
    log(logger, logging.INFO, "briefing pdf written", run_key=run_key, path=str(path), bytes=len(data))
    return path


def _label(story: Story, tz: str) -> str:
    when = story.event_time or timeutil.now()
    d = when.astimezone(ZoneInfo(tz))
    return f"{d.day} {d:%b %Y}"


def day_pdfs(cfg: Config, day: date) -> list[Path]:
    folder = cfg.briefing_dir / day.isoformat()
    return sorted(folder.glob("*.pdf")) if folder.is_dir() else []


_READABLE = "(story_json IS NOT NULL OR body IS NOT NULL)"


def days(conn: sqlite3.Connection, cfg: Config, limit: int = 60) -> list[tuple[date, int]]:
    """Local days that have stories, newest first, with their story counts."""
    counts: dict[date, int] = {}
    for (created,) in conn.execute(
        f"SELECT created_at FROM stories WHERE {_READABLE} ORDER BY created_at DESC LIMIT 2000"
    ):
        d = local_day(timeutil.parse_iso(created), cfg.display_timezone)
        counts[d] = counts.get(d, 0) + 1
    return sorted(counts.items(), reverse=True)[:limit]


def day_stories(conn: sqlite3.Connection, cfg: Config, day: date) -> list[StoryText]:
    out: list[StoryText] = []
    for row in conn.execute(f"SELECT * FROM stories WHERE {_READABLE} ORDER BY created_at, id"):
        if local_day(timeutil.parse_iso(row["created_at"]), cfg.display_timezone) != day:
            continue
        if row["story_json"]:
            story = Story.model_validate_json(row["story_json"])
            out.append(story_text(story, _label(story, cfg.display_timezone)))
        else:
            out.append(_legacy(conn, row, cfg.display_timezone))
    return sorted(out, key=lambda s: -s.importance)  # stable: older stories keep their order


def _legacy(conn: sqlite3.Connection, row: sqlite3.Row, tz: str) -> StoryText:
    """Stories published before schema 3 only kept headline, body and sources."""
    when = timeutil.parse_iso(row["event_time"] or row["created_at"]).astimezone(ZoneInfo(tz))
    sources = [
        (r["source_name"] or r["domain"], r["canonical_url"])
        for r in conn.execute(
            "SELECT source_name, domain, canonical_url FROM story_sources WHERE story_id = ?", (row["id"],)
        )
    ]
    return StoryText(
        kind=row["kind"],
        category=CATEGORY_LABEL.get(row["category"], "News"),
        headline=clean(row["headline"]),
        region="",
        importance=0,
        importance_reason="",
        confidence=CONFIDENCE_LABEL.get(row["confidence"], ""),
        date=f"{when.day} {when:%b %Y}",
        sections=parse_body(row["body"] or ""),
        impact_country="",
        impact_global="",
        outlook=[],
        sources=sources,
    )
