"""Deterministic fingerprints for titles, fact sets and URLs."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from .normalize import canonical_url, normalize_text, tokens


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def url_hash(url: str) -> str:
    return _h(canonical_url(url))


def title_fingerprint(title: str) -> str:
    """Order-insensitive bag of significant title words."""
    return _h(" ".join(sorted(set(tokens(title)))))


def facts_fingerprint(facts: Iterable[str]) -> str:
    """Order-insensitive hash of the normalized fact set."""
    norm = sorted({normalize_text(f) for f in facts if normalize_text(f)})
    return _h("\n".join(norm))
