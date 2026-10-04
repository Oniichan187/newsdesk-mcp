"""Local historical matching: "have we seen something substantially like this candidate before?"

Layers (all local, no external AI):
  1. canonical source URL hash
  2. exact fact-set fingerprint / title fingerprint
  3. topic key + alias lookup
  4. FTS5 retrieval over compact topic text (covers archived topics too)
  5. RapidFuzz title/fact similarity + entity overlap + date proximity scoring
Only the few relevant matches are returned, never bulk history.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from rapidfuzz import fuzz

from .. import timeutil
from ..schemas import Candidate
from .fingerprint import facts_fingerprint, title_fingerprint, url_hash
from .normalize import normalize_text, tokens

FACT_MATCH_THRESHOLD = 82.0
TOPIC_SCORE_THRESHOLD = 0.45
MAX_TOPICS_CONSIDERED = 12
RECENT_STORIES_PER_TOPIC = 5

NO_MATCH = "NO_MATCH"
EXACT_DUPLICATE = "EXACT_DUPLICATE"
LIKELY_DUPLICATE = "LIKELY_DUPLICATE"
POSSIBLE_EXISTING_TOPIC = "POSSIBLE_EXISTING_TOPIC"


@dataclass
class _Signals:
    url_hit: bool = False
    same_facts_fp: bool = False
    same_title_fp: bool = False
    key_or_alias: bool = False
    fts: bool = False


@dataclass
class TopicScore:
    topic_id: str
    score: float
    title_sim: float
    entity_overlap: float
    fact_coverage: float
    new_fact_idx: list[int]
    signals: _Signals
    age_days: float = 0.0
    reasons: list[str] = field(default_factory=list)


def _title_similarity(a: str, b: str) -> float:
    na, nb = " ".join(tokens(a)), " ".join(tokens(b))
    if not na or not nb:
        return 0.0
    # token_set_ratio alone scores 100 for any subset ("EU" vs "EU AI Act fine"), so blend it with
    # the order-insensitive full comparison.
    return (fuzz.token_sort_ratio(na, nb) + fuzz.token_set_ratio(na, nb)) / 2.0


_NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "").replace(".", "") for n in _NUM_RE.findall(text)}


_NEGATIONS = frozenset(
    "not no never none nobody without denied denies deny unconfirmed unverified false "
    "nicht kein keine keinen keiner nie niemals ohne dementiert bestreitet unbestatigt".split()
)


def _negated(text: str) -> bool:
    words = set(normalize_text(text).replace("n t ", " not ").split())
    return bool(words & _NEGATIONS) or "n't" in text.lower()


def _fact_similarity(a: str, b: str) -> float:
    # Facts that state different numbers (years, counts, amounts, CVE ids) are never "the same fact":
    # "Peace Prize 2025" vs "2026" or "120 dead" vs "2,300 dead" must surface as new information.
    if _numbers(a) != _numbers(b):
        return 0.0
    # "has not confirmed" vs "confirmed": opposite claims are the most important update of all.
    if _negated(a) != _negated(b):
        return 0.0
    na, nb = normalize_text(a), normalize_text(b)
    sim = max(fuzz.token_sort_ratio(na, nb), fuzz.ratio(na, nb))
    ta, tb = tokens(a), tokens(b)
    if min(len(ta), len(tb)) >= 3:
        # one fact restating the other with fewer words ("Meta will appeal" ⊂ "Meta will appeal the decision")
        sim = max(sim, fuzz.token_set_ratio(" ".join(ta), " ".join(tb)) - 5)
    return float(sim)


def _vocab_coverage(cand: Candidate, topic_vocab: set[str]) -> float:
    """Share of the candidate title's significant words that already occur in the topic's text."""
    words = {t for t in tokens(cand.title) if len(t) > 2}
    return len(words & topic_vocab) / len(words) if words else 0.0


def fts_query(text_parts: list[str], limit_tokens: int = 16) -> str | None:
    seen: list[str] = []
    for part in text_parts:
        for tok in tokens(part):
            if len(tok) >= 3 and tok not in seen:
                seen.append(tok)
    if not seen:
        return None
    return " OR ".join(f'"{t}"' for t in seen[:limit_tokens])


def match_candidate(conn: sqlite3.Connection, cand: Candidate) -> dict[str, Any]:
    signals: dict[str, _Signals] = {}

    def sig(topic_id: str) -> _Signals:
        return signals.setdefault(topic_id, _Signals())

    cfp = facts_fingerprint(cand.key_facts)
    tfp = title_fingerprint(cand.title)
    hashes = sorted({url_hash(u) for u in cand.source_urls})

    q_marks = ",".join("?" * len(hashes))
    for row in conn.execute(
        f"SELECT DISTINCT s.topic_id FROM story_sources ss JOIN stories s ON s.id = ss.story_id "
        f"WHERE ss.url_hash IN ({q_marks})",
        hashes,
    ):
        sig(row["topic_id"]).url_hit = True
    for row in conn.execute("SELECT DISTINCT topic_id FROM stories WHERE content_fp = ?", (cfp,)):
        sig(row["topic_id"]).same_facts_fp = True
    for row in conn.execute("SELECT DISTINCT topic_id FROM stories WHERE title_fp = ?", (tfp,)):
        sig(row["topic_id"]).same_title_fp = True
    alias = normalize_text(cand.title)
    for row in conn.execute(
        "SELECT id FROM topics WHERE topic_key = ? UNION "
        "SELECT topic_id FROM topic_aliases WHERE alias_norm = ?",
        (cand.topic_key or "", alias),
    ):
        sig(row[0]).key_or_alias = True
    q = fts_query([cand.title, *cand.entities])
    if q:
        for row in conn.execute(
            "SELECT topic_id FROM topic_fts WHERE topic_fts MATCH ? ORDER BY rank LIMIT ?",
            (q, MAX_TOPICS_CONSIDERED),
        ):
            sig(row["topic_id"]).fts = True

    raw = [_score_topic(conn, tid, s, cand) for tid, s in list(signals.items())[: MAX_TOPICS_CONSIDERED * 2]]
    scored = [s for s in raw if s is not None]
    scored.sort(key=lambda s: s.score, reverse=True)

    result: dict[str, Any] = {"candidate_id": cand.candidate_id, "classification": NO_MATCH}
    if not scored:
        result.update(confidence=0.9, reason="no related history")
        return result
    best = scored[0]
    classification, confidence = _classify(best)
    if classification == NO_MATCH:
        result.update(confidence=round(1 - best.score, 2), reason="only weak similarity to history")
        return result
    topic = conn.execute("SELECT * FROM topics WHERE id = ?", (best.topic_id,)).fetchone()
    match: dict[str, Any] = {
        "topic_id": topic["id"],
        "topic_key": topic["topic_key"],
        "title": topic["title"],
        "topic_state": topic["state"],
        "last_posted_at": topic["last_posted"],
    }
    if classification == POSSIBLE_EXISTING_TOPIC:
        match["state_summary"] = topic["state_summary"]
        match["last_material_update"] = topic["last_material_update"]
        match["prior_facts"] = _prior_facts(conn, topic["id"], limit=6)
        match["new_fact_indexes"] = best.new_fact_idx
    result.update(
        classification=classification, confidence=confidence, reason="; ".join(best.reasons), match=match
    )
    alts = [s.topic_id for s in scored[1:3] if s.score >= TOPIC_SCORE_THRESHOLD]
    if alts:
        result["other_related_topic_ids"] = alts
    return result


def _classify(s: TopicScore) -> tuple[str, float]:
    if s.signals.same_facts_fp:
        return EXACT_DUPLICATE, 0.99
    if s.signals.url_hit and s.fact_coverage >= 0.9:
        return EXACT_DUPLICATE, 0.95
    recent = s.age_days <= 30 or s.signals.url_hit
    if (
        recent
        and s.fact_coverage >= 0.8
        and (s.title_sim >= 60 or s.entity_overlap >= 0.4 or s.signals.url_hit)
    ):
        return LIKELY_DUPLICATE, round(min(0.95, 0.5 + s.fact_coverage / 2), 2)
    if s.score >= TOPIC_SCORE_THRESHOLD or s.signals.url_hit or s.signals.key_or_alias:
        return POSSIBLE_EXISTING_TOPIC, round(min(0.9, max(s.score, 0.5)), 2)
    return NO_MATCH, 0.0


def _recent_stories(conn: sqlite3.Connection, topic_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT headline, key_facts, created_at FROM stories WHERE topic_id = ? "
        "ORDER BY created_at DESC LIMIT ?",
        (topic_id, RECENT_STORIES_PER_TOPIC),
    ).fetchall()


def _prior_facts(conn: sqlite3.Connection, topic_id: str, limit: int) -> list[str]:
    out: list[str] = []
    for row in _recent_stories(conn, topic_id):
        for fact in json.loads(row["key_facts"]):
            if fact not in out:
                out.append(fact)
            if len(out) >= limit:
                return out
    return out


def _score_topic(conn: sqlite3.Connection, topic_id: str, s: _Signals, cand: Candidate) -> TopicScore | None:
    topic = conn.execute(
        "SELECT title, entities, last_seen, state_summary FROM topics WHERE id = ?", (topic_id,)
    ).fetchone()
    if topic is None:
        return None
    stories = _recent_stories(conn, topic_id)
    titles = [topic["title"], *(r["headline"] for r in stories)]
    title_sim = max(_title_similarity(cand.title, t) for t in titles)

    topic_entities = {normalize_text(e) for e in json.loads(topic["entities"])}
    cand_entities = {normalize_text(e) for e in cand.entities}
    union = topic_entities | cand_entities
    entity_overlap = len(topic_entities & cand_entities) / len(union) if union else 0.0

    prior = [f for r in stories for f in json.loads(r["key_facts"])]
    new_idx: list[int] = []
    matched = 0
    for i, fact in enumerate(cand.key_facts):
        best = max((_fact_similarity(fact, p) for p in prior), default=0.0)
        if best >= FACT_MATCH_THRESHOLD:
            matched += 1
        else:
            new_idx.append(i)
    coverage = matched / len(cand.key_facts)

    vocab: set[str] = set()
    for text in (*titles, topic["state_summary"], *prior, *json.loads(topic["entities"])):
        vocab.update(tokens(text))
    vocab_cov = _vocab_coverage(cand, vocab)

    score = 0.30 * title_sim / 100 + 0.20 * entity_overlap + 0.25 * coverage + 0.25 * vocab_cov
    reasons = [
        f"title_sim={title_sim:.0f}",
        f"entity_overlap={entity_overlap:.2f}",
        f"fact_coverage={coverage:.2f}",
        f"vocab={vocab_cov:.2f}",
    ]
    if s.url_hit:
        score += 0.25
        reasons.insert(0, "same source URL")
    if s.same_title_fp:
        score += 0.15
        reasons.insert(0, "same title words")
    if s.key_or_alias:
        score += 0.25
        reasons.insert(0, "topic key/alias match")
    if s.same_facts_fp:
        reasons.insert(0, "identical fact set")
    ref = cand.event_time or timeutil.now()
    gap_days = abs((ref - timeutil.parse_iso(topic["last_seen"])).total_seconds()) / 86400
    if gap_days > 30:
        score -= 0.10
        reasons.append(f"{gap_days:.0f}d since topic was last seen")
    return TopicScore(
        topic_id=topic_id,
        score=min(score, 1.0),
        title_sim=title_sim,
        entity_overlap=entity_overlap,
        fact_coverage=coverage,
        new_fact_idx=new_idx,
        signals=s,
        age_days=gap_days,
        reasons=reasons,
    )
