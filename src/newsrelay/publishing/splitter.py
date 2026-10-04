"""Split long text into Discord-safe messages on semantic boundaries.

Order of preference: paragraphs -> lines -> sentences -> words/links -> grapheme-safe hard split.
URLs and Markdown links are treated as atomic tokens. Length is measured in UTF-16 code units,
which is never smaller than the code-point count, so the limit holds however Discord counts.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable

_SENTENCE_RE = re.compile(r"(?<=[.!?…])\s+(?=\S)")
_ATOM_RE = re.compile(
    r"\[[^\]\n]{0,300}\]\(<?https?://[^\s)>]+>?\)"  # markdown link, optionally <url>
    r"|<https?://[^\s>]+>"  # <url>
    r"|\S+"  # any other word (bare URLs included)
)


def ulen(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _paragraphs(t: str) -> list[str]:
    return [p for p in re.split(r"\n{2,}", t) if p.strip()]


def _lines(t: str) -> list[str]:
    return [p for p in t.split("\n") if p.strip()]


def _sentences(t: str) -> list[str]:
    return [p for p in _SENTENCE_RE.split(t) if p.strip()]


def _atoms(t: str) -> list[str]:
    return _ATOM_RE.findall(t)


_LEVELS: list[tuple[Callable[[str], list[str]], str]] = [
    (_paragraphs, "\n\n"),
    (_lines, "\n"),
    (_sentences, " "),
    (_atoms, " "),
]


def _is_extend(ch: str) -> bool:
    cp = ord(ch)
    return (
        unicodedata.combining(ch) != 0
        or unicodedata.category(ch) in ("Mn", "Me", "Mc")
        or cp == 0x200D
        or 0xFE00 <= cp <= 0xFE0F
        or 0x1F3FB <= cp <= 0x1F3FF
        or 0xE0020 <= cp <= 0xE007F
    )


def _hard_split(text: str, limit: int) -> list[str]:
    out: list[str] = []
    rest = text
    while ulen(rest) > limit:
        cut = 0
        used = 0
        for i, ch in enumerate(rest):
            w = 2 if ord(ch) > 0xFFFF else 1
            if used + w > limit:
                cut = i
                break
            used += w
        # Never split inside a grapheme cluster (combining marks, ZWJ sequences, modifiers).
        while cut > 1 and (_is_extend(rest[cut]) or rest[cut - 1] == "‍"):
            cut -= 1
        if cut <= 0:
            cut = 1
        out.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        out.append(rest)
    return out


def _split(text: str, limit: int, level: int) -> list[str]:
    if ulen(text) <= limit:
        return [text]
    if level >= len(_LEVELS):
        return _hard_split(text, limit)
    splitter, joiner = _LEVELS[level]
    parts = splitter(text)
    if len(parts) <= 1:
        return _split(text, limit, level + 1)
    chunks: list[str] = []
    current = ""
    for part in parts:
        if ulen(part) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_split(part, limit, level + 1))
            continue
        candidate = f"{current}{joiner}{part}" if current else part
        if ulen(candidate) <= limit:
            current = candidate
        else:
            chunks.append(current)
            current = part
    if current:
        chunks.append(current)
    return chunks


def split_message(text: str, limit: int) -> list[str]:
    if limit < 50:
        raise ValueError("limit too small")
    text = text.strip()
    return [c.strip() for c in _split(text, limit, 0) if c.strip()]
