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
_CONTINUATION_RESERVE = 24

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


def _impact_lines(story: Story) -> list[str]:
    where = f" ({story.impact_region})" if story.impact_region else ""
    lines = [f"🎯 **Impact{where}:** {story.impact}"]
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


def render_text(story: Story, display_tz: str) -> str:
    """Discord markdown: heading, small meta line, the body as written, linked sources."""
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
        "",
        story.body.strip(),
        "",
        *_impact_lines(story),
        "",
        _source_line(story),
    ]
    return sanitize_mentions("\n".join(parts))


def build_payloads(
    story: Story, *, username: str, limit: int, suppress_embeds: bool, display_tz: str
) -> list[dict[str, Any]]:
    text = render_text(story, display_tz)
    chunks = split_message(text, limit)
    if len(chunks) > 1:
        chunks = split_message(text, limit - _CONTINUATION_RESERVE)
        total = len(chunks)
        chunks = [c if i == 0 else f"-# ({i + 1}/{total}) continued\n{c}" for i, c in enumerate(chunks)]
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
