"""Plain, structured text of a story for the PDF and the RSVP reader (no Discord markup, no emoji)."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from ..schemas import Story

CATEGORY_LABEL = {
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
CONFIDENCE_LABEL = {
    "confirmed": "",
    "partially_confirmed": "Partly confirmed",
    "unverified_claim": "Unverified claim",
    "disputed": "Disputed",
    "corrected": "Correction",
}
LIKELIHOOD_LABEL = {
    "very_likely": "Very likely",
    "likely": "Likely",
    "uncertain": "Open",
    "unlikely": "Unlikely",
    "very_unlikely": "Very unlikely",
}

_LABEL_RE = re.compile(r"^\*\*(.+?):\*\*\s*(.*)$")
_MD_RE = re.compile(r"(\*\*|__|~~|`)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(<?[^)>]+>?\)")
# Emoji and pictographs: kept out of the reading formats.
_EMOJI_RE = re.compile("[\U0001f000-\U0001faff\U00002600-\U000027bf\U0001f1e6-\U0001f1ff️‍⭐⭕⏰-⏿]")
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def clean(text: str) -> str:
    """Strip Discord/markdown markup and emoji; normalise whitespace."""
    text = _LINK_RE.sub(r"\1", text)
    text = _MD_RE.sub("", text)
    text = _EMOJI_RE.sub("", text)
    return re.sub(r"[ \t]+", " ", text).strip()


@dataclass
class Section:
    label: str
    paragraphs: list[str] = field(default_factory=list)
    bullets: list[str] = field(default_factory=list)


@dataclass
class OutlookItem:
    event: str
    likelihood: str
    basis: str


@dataclass
class StoryText:
    kind: str
    category: str
    headline: str
    region: str
    importance: int
    importance_reason: str
    confidence: str
    date: str
    sections: list[Section]
    impact_country: str
    impact_global: str
    outlook: list[OutlookItem]
    sources: list[tuple[str, str]]  # (name, url)


def parse_body(body: str) -> list[Section]:
    """`**Label:** text` starts a section; `- x` lines are bullets; anything else is a paragraph."""
    sections: list[Section] = []
    current: Section | None = None
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _LABEL_RE.match(line)
        if m:
            current = Section(clean(m.group(1)))
            sections.append(current)
            if m.group(2).strip():
                current.paragraphs.append(clean(m.group(2)))
            continue
        if current is None:
            current = Section("")
            sections.append(current)
        if line[:2] in ("- ", "* ", "• "):
            current.bullets.append(clean(line[2:]))
        else:
            current.paragraphs.append(clean(line))
    return sections


def story_text(story: Story, date_label: str) -> StoryText:
    return StoryText(
        kind=story.kind,
        category=CATEGORY_LABEL.get(story.category, "News"),
        headline=clean(story.headline),
        region=clean(story.impact_region),
        importance=story.importance,
        importance_reason=clean(story.importance_reason),
        confidence=CONFIDENCE_LABEL.get(story.confidence, ""),
        date=date_label,
        sections=parse_body(story.body),
        impact_country=clean(story.impact),
        impact_global=clean(story.impact_global),
        outlook=[
            OutlookItem(
                clean(o.event),
                LIKELIHOOD_LABEL[o.likelihood]
                + (f", about {o.probability_percent} %" if o.probability_percent is not None else ""),
                clean(o.basis),
            )
            for o in story.outlook
        ],
        sources=[(s.name or _domain(s.url), s.url) for s in story.sources],
    )


def _domain(url: str) -> str:
    from urllib.parse import urlsplit

    host = urlsplit(url).hostname or url
    return host.removeprefix("www.")


def bionic_split(word: str) -> int:
    """Number of leading letters to emphasise (Bionic Reading style fixation)."""
    n = len(word)
    if n <= 3:
        return 1
    if n == 4:
        return 2
    return math.ceil(n * 0.4)


def bionic_markdown(text: str) -> str:
    """Wrap the fixation part of every word in `**`, as understood by fpdf2's markdown mode.

    fpdf2 treats `**`, `__` and `--` as markers, so existing ones are neutralised first.
    """
    text = text.replace("**", "").replace("__", "_").replace("--", "–")

    def emph(m: re.Match[str]) -> str:
        w = m.group(0)
        k = bionic_split(w)
        return f"**{w[:k]}**{w[k:]}"

    return _WORD_RE.sub(emph, text)


def reading_words(st: StoryText, country: str) -> list[tuple[str, str]]:
    """(section label, word) pairs in reading order, for the RSVP reader."""
    out: list[tuple[str, str]] = []

    def add(label: str, text: str) -> None:
        out.extend((label, w) for w in text.split())

    add("Headline", st.headline)
    for sec in st.sections:
        for p in sec.paragraphs:
            add(sec.label or "Text", p)
        for b in sec.bullets:
            add(sec.label or "Text", b)
    add("Importance", f"{st.importance} of 10. {st.importance_reason}")
    add(f"Impact {country}", st.impact_country)
    add("Impact global", st.impact_global)
    for o in st.outlook:
        add("Outlook", f"{o.event}: {o.likelihood}. {o.basis}.")
    return out
