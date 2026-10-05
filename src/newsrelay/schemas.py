"""Strict input schemas for every operation exposed to ChatGPT.

Bounds are deliberately tight: oversized or malformed input is rejected, never silently truncated.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, model_validator

MAX_CANDIDATES = 40
MAX_STORIES = 20
MAX_FACTS = 8
MAX_SOURCES = 6
MAX_ENTITIES = 12
MAX_URL_LEN = 600
MAX_OUTLOOKS = 3

RUN_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}/\d{4}-\d{2}-\d{2}(T\d{2}(:\d{2})?)?$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _no_control(value: str) -> str:
    if _CONTROL.search(value):
        raise ValueError("control characters are not allowed")
    return value


def _https_url(value: str) -> str:
    if len(value) > MAX_URL_LEN:
        raise ValueError(f"URL longer than {MAX_URL_LEN} characters")
    parts = urlsplit(value)
    if parts.scheme != "https":
        raise ValueError("only https:// source URLs are accepted")
    host = parts.hostname or ""
    if not host or "." not in host or parts.username or parts.password:
        raise ValueError("URL must have a public hostname and no credentials")
    if any(c.isspace() for c in value):
        raise ValueError("URL must not contain whitespace")
    return value


def _article_url(value: str) -> str:
    """A source must point at an article, not at an outlet's front page."""
    parts = urlsplit(value)
    if parts.path.strip("/") == "" and not parts.query:
        raise ValueError("source URL must link to the article itself, not to the homepage")
    return value


def _distinct_urls(values: list[str]) -> None:
    if len(set(values)) != len(values):
        raise ValueError("each source URL may be listed only once")


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp must include a timezone offset (use Z for UTC)")
    return value


Short = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200), AfterValidator(_no_control)
]
Fact = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=3, max_length=280), AfterValidator(_no_control)
]
Entity = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80), AfterValidator(_no_control)
]
CandidateId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,40}$")]
TopicKey = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{1,79}$")]
HttpsUrl = Annotated[str, StringConstraints(strip_whitespace=True, min_length=12), AfterValidator(_https_url)]
ArticleUrl = Annotated[HttpsUrl, AfterValidator(_article_url)]
AwareDatetime = Annotated[datetime, AfterValidator(_aware)]
Category = Literal[
    "ai",
    "computing",
    "cybersecurity",
    "privacy",
    "internet-policy",
    "law-regulation",
    "politics",
    "war-geopolitics",
    "security",
    "science",
    "health",
    "economy",
    "energy",
    "climate",
    "disaster",
    "infrastructure",
    "civil-liberties",
    "europe",
    "austria",
    "world",
    "other",
]
Confidence = Literal["confirmed", "partially_confirmed", "unverified_claim", "disputed", "corrected"]
Likelihood = Literal["very_likely", "likely", "uncertain", "unlikely", "very_unlikely"]

# A stated percentage must fit the verbal level (bands overlap on purpose).
LIKELIHOOD_RANGE: dict[str, tuple[int, int]] = {
    "very_likely": (80, 100),
    "likely": (60, 85),
    "uncertain": (35, 65),
    "unlikely": (15, 40),
    "very_unlikely": (0, 20),
}


RunKey = Annotated[
    str,
    StringConstraints(pattern=RUN_KEY_RE.pattern),
    Field(
        description="Deterministic run key, e.g. 'daily-news/2026-10-03' (Europe/Vienna date), or "
        "'news/2026-10-03T06' for sub-daily cadences."
    ),
]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Source(Strict):
    url: ArticleUrl
    name: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=80), AfterValidator(_no_control)]
        | None
    ) = None


class Outlook(Strict):
    """A forecast or estimate the story rests on, with how likely it is to happen."""

    event: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=5, max_length=200),
        AfterValidator(_no_control),
    ]
    likelihood: Likelihood
    probability_percent: int | None = Field(default=None, ge=0, le=100)
    basis: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=5, max_length=300),
        AfterValidator(_no_control),
    ]

    @model_validator(mode="after")
    def _percent_fits_level(self) -> Outlook:
        if self.probability_percent is not None:
            lo, hi = LIKELIHOOD_RANGE[self.likelihood]
            if not lo <= self.probability_percent <= hi:
                raise ValueError(
                    f"probability_percent {self.probability_percent} does not fit likelihood "
                    f"'{self.likelihood}' ({lo}-{hi})"
                )
        return self


class BeginRunInput(Strict):
    run_key: RunKey
    scheduled_for: AwareDatetime | None = None


class Candidate(Strict):
    candidate_id: CandidateId
    title: Short
    category: Category
    event_time: AwareDatetime | None = None
    entities: list[Entity] = Field(default_factory=list, max_length=MAX_ENTITIES)
    key_facts: list[Fact] = Field(min_length=1, max_length=MAX_FACTS)
    source_urls: list[ArticleUrl] = Field(min_length=1, max_length=MAX_SOURCES)
    topic_key: TopicKey | None = None

    @model_validator(mode="after")
    def _unique_sources(self) -> Candidate:
        _distinct_urls(list(self.source_urls))
        return self


class MatchInput(Strict):
    run_key: RunKey
    candidates: list[Candidate] = Field(min_length=1, max_length=MAX_CANDIDATES)


class Story(Strict):
    candidate_id: CandidateId
    kind: Literal["NEW", "UPDATE", "CORRECTION"]
    topic_id: Annotated[str, StringConstraints(pattern=r"^top_[a-f0-9]{16}$")] | None = None
    topic_key: TopicKey | None = None
    category: Category
    headline: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=5, max_length=160),
        AfterValidator(_no_control),
    ]
    body: Annotated[str, StringConstraints(strip_whitespace=True, min_length=40, max_length=3500)]
    key_facts: list[Fact] = Field(min_length=1, max_length=MAX_FACTS)
    entities: list[Entity] = Field(default_factory=list, max_length=MAX_ENTITIES)
    impact: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=10, max_length=600),
        AfterValidator(_no_control),
    ] = Field(description="What concretely changes or could change for the reader: who, how, from when.")
    impact_region: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=2, max_length=60),
        AfterValidator(_no_control),
    ] = Field(
        description="Region of the world the story concerns: 'Global', a continent ('Europe'), a bloc "
        "('EU') or a country ('USA', 'Austria')."
    )
    outlook: list[Outlook] = Field(
        default_factory=list,
        max_length=MAX_OUTLOOKS,
        description="Only for forecasts/estimates/pending decisions: what may happen and how likely.",
    )
    material_change: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=400), AfterValidator(_no_control)]
        | None
    ) = None
    topic_state_summary: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=10, max_length=500),
        AfterValidator(_no_control),
    ]
    confidence: Confidence
    importance: int = Field(default=2, ge=1, le=3)
    event_time: AwareDatetime | None = None
    sources: list[Source] = Field(min_length=1, max_length=MAX_SOURCES)

    @model_validator(mode="after")
    def _unique_sources(self) -> Story:
        _distinct_urls([s.url for s in self.sources])
        return self


class PublishInput(Strict):
    run_key: RunKey
    research_through: AwareDatetime
    stories: list[Story] = Field(min_length=1, max_length=MAX_STORIES)


class NoopInput(Strict):
    run_key: RunKey
    research_through: AwareDatetime


class StatusInput(Strict):
    run_key: RunKey
