"""Render a validated story into one or more Discord webhook payloads."""

from __future__ import annotations

import re
from typing import Any
from zoneinfo import ZoneInfo

from .. import timeutil
from ..dedup.normalize import url_domain
from ..schemas import Story
from .splitter import split_message, ulen

_CATEGORY_LABEL = {
    "ai": "AI",
    "computing": "Computing",
    "cybersecurity": "Cybersecurity",
    "privacy": "Privacy",
    "internet-policy": "Internet policy",
    "law-regulation": "Law & regulation",
    "politics": "Politics",
    "war-geopolitics": "War & geopolitics",
    "security": "Security",
    "science": "Science",
    "health": "Health",
    "economy": "Economy",
    "energy": "Energy",
    "climate": "Climate",
    "disaster": "Disaster",
    "infrastructure": "Infrastructure",
    "civil-liberties": "Civil liberties",
    "europe": "Europe",
    "austria": "Austria",
    "world": "World",
    "other": "News",
}
_CONFIDENCE_LABEL = {
    "confirmed": None,
    "partially_confirmed": "partly confirmed",
    "unverified_claim": "⚠️ unverified claim",
    "disputed": "⚠️ disputed",
    "corrected": "correction",
}
_LIKELIHOOD_LABEL = {
    "very_likely": "very likely",
    "likely": "likely",
    "uncertain": "open (about even)",
    "unlikely": "unlikely",
    "very_unlikely": "very unlikely",
}
_KIND_PREFIX = {"NEW": "", "UPDATE": "🔄 Update: ", "CORRECTION": "✏️ Correction: "}

# Mass mentions and user/role/channel mention syntax are neutralized textually *and* via
# allowed_mentions={"parse": []}, so generated or injected text can never ping anyone.
_MENTION_RE = re.compile(r"@(everyone|here)\b", re.I)
_ID_MENTION_RE = re.compile(r"<(@[!&]?|#)(\d+)>")


def sanitize_mentions(text: str) -> str:
    text = _MENTION_RE.sub("@​\\1", text)
    return _ID_MENTION_RE.sub(lambda m: f"<​{m.group(1)}{m.group(2)}>", text)


def _source_line(story: Story) -> str:
    links = []
    for src in story.sources:
        label = (src.name or url_domain(src.url)).replace("[", "(").replace("]", ")")
        links.append(f"[{label}](<{src.url}>)")
    return "📰 **Sources:** " + " · ".join(links)


_GLOBAL = {"global", "worldwide", "world", "weltweit"}


def _region_line(story: Story) -> str:
    emoji = "🌍" if story.impact_region.lower() in _GLOBAL else "📍"
    return f"{emoji} **Region:** {story.impact_region}"


_COUNTRY_FLAG = {
    "austria": "🇦🇹",
    "österreich": "🇦🇹",
    "germany": "🇩🇪",
    "deutschland": "🇩🇪",
    "switzerland": "🇨🇭",
    "schweiz": "🇨🇭",
}


def _impact_lines(story: Story, country: str) -> list[str]:
    flag = _COUNTRY_FLAG.get(country.strip().lower(), "📍")
    lines = [
        f"⭐ **Importance:** {story.importance}/10 — {story.importance_reason}",
        "",
        f"{flag} **Impact {country}:** {story.impact}",
        "",
        f"🌍 **Impact global:** {story.impact_global}",
    ]
    if story.outlook:
        lines += ["", "🔮 **Outlook:**"]
        for o in story.outlook:
            level = _LIKELIHOOD_LABEL[o.likelihood]
            if o.probability_percent is not None:
                level += f", ~{o.probability_percent} %"
            lines.append(f"- {o.event} → **{level}** ({o.basis})")
    return lines


_CATEGORY_EMOJI = {
    "ai": "🤖",
    "computing": "💻",
    "cybersecurity": "🛡️",
    "privacy": "🔒",
    "internet-policy": "🌐",
    "law-regulation": "⚖️",
    "politics": "🏛️",
    "war-geopolitics": "🌍",
    "security": "🚨",
    "science": "🔬",
    "health": "🩺",
    "economy": "📈",
    "energy": "⚡",
    "climate": "🌡️",
    "disaster": "🌪️",
    "infrastructure": "🏗️",
    "civil-liberties": "🗽",
    "europe": "🇪🇺",
    "austria": "🇦🇹",
    "world": "🌐",
    "other": "📰",
}


def render_text(story: Story, display_tz: str, country: str = "Austria") -> str:
    """Discord markdown: heading, meta line, region, the body as written, importance, impact
    (reader country, global), outlook, sources."""
    headline = story.headline
    prefix = _KIND_PREFIX[story.kind]
    if prefix and headline.lower().startswith(prefix.split(" ", 1)[1].lower()):
        prefix = prefix.split(" ", 1)[0] + " "
    emoji = _CATEGORY_EMOJI.get(story.category, "📰")
    meta = [f"{emoji} {_CATEGORY_LABEL.get(story.category, 'News')}"]
    label = _CONFIDENCE_LABEL.get(story.confidence)
    if label:
        meta.append(label)
    when = story.event_time or timeutil.now()
    meta.append(when.astimezone(ZoneInfo(display_tz)).strftime("%d %b %Y"))
    parts = [
        f"## {prefix}{headline}",
        "-# " + " · ".join(meta),
        _region_line(story),
        "",
        story.body.strip(),
        "",
        *_impact_lines(story, country),
        "",
        _source_line(story),
    ]
    return sanitize_mentions("\n".join(parts))


def teaser_of(story: Story) -> str:
    """The index line for a story: its teaser, else the first sentence of the impact on the reader."""
    if story.teaser:
        return story.teaser
    first = re.split(r"(?<=[.!?])\s+", story.impact.strip(), maxsplit=1)[0]
    return first


def _clip(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if ulen(text) <= n else text[: max(0, n - 1)].rstrip(" ,;:–-") + "…"


def briefing_index(stories: list[Story], budget: int) -> str:
    """Numbered contents of the day: headline, importance, and one line that says why to read it.

    Fits into ``budget`` characters: teasers are shortened first, then dropped, then the list is cut.
    """
    if not stories:
        return ""
    head = "**In this briefing**"

    def build(teaser_len: int | None, count: int) -> str:
        lines = [head]
        for n, s in enumerate(stories[:count], 1):
            lines.append(f"**{n}.** {_clip(s.headline, 160)} · {s.importance}/10")
            if teaser_len:
                lines.append(f"> {_clip(teaser_of(s), teaser_len)}")
        if count < len(stories):
            lines.append(f"…and {len(stories) - count} more below")
        return "\n".join(lines)

    for teaser_len in (140, 110, 80, 60):
        text = build(teaser_len, len(stories))
        if ulen(text) <= budget:
            return text
    for count in range(len(stories), 0, -1):
        text = build(None, count)
        if ulen(text) <= budget:
            return text
    return ""


def briefing_payload(
    path: str | None,
    title: str,
    stories: int | list[Story],
    reader_url: str,
    limit: int = 1990,
) -> dict[str, Any]:
    """The first message of a run: title, index of the stories, and the day's PDF (if any)."""
    items = stories if isinstance(stories, list) else []
    total = len(items) if isinstance(stories, list) else stories
    count = f"{total} {'story' if total == 1 else 'stories'}"
    lines = [f"**{title}**", f"{count} · PDF for reading on any device" if path else count]
    if reader_url:
        lines.append(f"Speed reading: <{reader_url}>")
    text = "\n".join(lines)
    index = briefing_index(items, limit - ulen(text) - 2)
    if index:
        text += "\n\n" + index
    payload: dict[str, Any] = {
        "content": sanitize_mentions(text),
        "allowed_mentions": {"parse": []},
        "flags": 4,
    }
    if path:
        payload["file"] = {"path": path, "name": path.replace("\\", "/").rsplit("/", 1)[-1]}
    return payload


def build_payloads(
    story: Story,
    *,
    username: str,
    limit: int,
    suppress_embeds: bool,
    display_tz: str,
    country: str = "Austria",
) -> list[dict[str, Any]]:
    text = render_text(story, display_tz, country)
    chunks = split_message(text, limit)
    payloads = []
    for chunk in chunks:
        assert ulen(chunk) <= limit
        payload: dict[str, Any] = {
            "content": chunk,
            "username": username,
            "allowed_mentions": {"parse": []},
        }
        if suppress_embeds:
            payload["flags"] = 4  # SUPPRESS_EMBEDS
        payloads.append(payload)
    return payloads
