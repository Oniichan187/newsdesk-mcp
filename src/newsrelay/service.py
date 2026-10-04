"""Core news-relay operations. Transport-agnostic: MCP tools and the CLI both call these."""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import sqlite3
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import __version__, timeutil
from .config import Config
from .database import SCHEMA_VERSION, check_disk, current_version, tx
from .dedup.fingerprint import facts_fingerprint, title_fingerprint, url_hash
from .dedup.matcher import match_candidate
from .dedup.normalize import canonical_url, normalize_text, slugify, url_domain
from .logutil import log
from .publishing import outbox
from .publishing.formatter import build_payloads
from .schemas import BeginRunInput, MatchInput, NoopInput, PublishInput, StatusInput, Story

logger = logging.getLogger("newsrelay.service")

CLOCK_SKEW_TOLERANCE = timedelta(minutes=10)


class RelayError(ValueError):
    """User-facing error; message is returned to the caller verbatim (never contains secrets)."""


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def run_id_for(run_key: str) -> str:
    """Deterministic run id: begin_run needs no write, retries map to the same run."""
    return "run_" + hashlib.sha256(run_key.encode()).hexdigest()[:16]


def _find_run(conn: sqlite3.Connection, run_key: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute("SELECT * FROM runs WHERE run_key = ?", (run_key,)).fetchone()
    return row


def _checkpoint(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT research_through FROM checkpoint WHERE id = 1").fetchone()
    return row[0] if row else None


def _advance_checkpoint(conn: sqlite3.Connection, run_id: str, through: str) -> str:
    cur = _checkpoint(conn)
    if cur is None or through > cur:
        conn.execute(
            "INSERT INTO checkpoint(id, research_through, run_id, updated_at) VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET research_through = excluded.research_through, "
            "run_id = excluded.run_id, updated_at = excluded.updated_at",
            (through, run_id, timeutil.now_iso()),
        )
        return through
    return cur


def _research_window(conn: sqlite3.Connection, cfg: Config) -> dict[str, Any]:
    """Research window derived from the checkpoint (stable until a run completes).

    from  = checkpoint - overlap   (first run: now - initial_lookback)
    until = min(now, checkpoint + catchup_window)

    After a long outage the window is a bounded chronological chunk; completing it advances the
    checkpoint only to `until`, so the next run continues where this one stopped. Time is never
    skipped (no silent coverage gaps), and token use per run stays bounded.
    """
    now = timeutil.now()
    cp = _checkpoint(conn)
    window = timedelta(days=cfg.catchup_window_days)
    if cp:
        base = timeutil.parse_iso(cp)
        research_from = base - timedelta(hours=cfg.overlap_hours)
        note = None
    else:
        base = now - timedelta(hours=cfg.initial_lookback_hours)
        research_from = base
        note = "first run: no checkpoint yet"
    until = min(now, base + window)
    out: dict[str, Any] = {
        "research_from": timeutil.to_iso(research_from),
        "research_until": timeutil.to_iso(until),
        "catch_up": until < now - timedelta(minutes=1),
    }
    if out["catch_up"]:
        out["behind_days"] = round((now - until).total_seconds() / 86400, 1)
        note = "catching up after an outage: research only this window, prioritise major events"
    if note:
        out["note"] = note
    return out


def _effective_through(through_dt: datetime, window: dict[str, Any]) -> str:
    """Clamp the reported research end to the window, so the checkpoint never passes unresearched time."""
    now = timeutil.now()
    if through_dt > now + CLOCK_SKEW_TOLERANCE:
        raise RelayError("research_through is in the future (check clocks); use the current UTC time")
    through = min(timeutil.to_iso(min(through_dt, now)), str(window["research_until"]))
    if through < window["research_from"]:
        raise RelayError(f"research_through must not be before research_from ({window['research_from']})")
    return through


def _create_run(
    conn: sqlite3.Connection, window: dict[str, Any], run_key: str, status: str, through: str
) -> str:
    """Record a completed run (inside a transaction). UNIQUE(run_key) backs idempotency."""
    run_id = run_id_for(run_key)
    t = timeutil.now_iso()
    conn.execute(
        "INSERT INTO runs(id, run_key, research_from, research_through, started_at, completed_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (run_id, run_key, window["research_from"], through, t, t, status),
    )
    return run_id


def _delivery_signal(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """Only returned when delivery is degraded, to keep normal responses tiny."""
    b = outbox.backlog(conn)
    stale = b["oldest_pending"] and b["oldest_pending"] < timeutil.iso_plus(-3600)
    if b["failed"] or b["uncertain"] or stale:
        return {"degraded": True, "pending": b["pending"], "failed": b["failed"], "uncertain": b["uncertain"]}
    return None


def _server_time() -> dict[str, str]:
    n = timeutil.now()
    return {
        "utc": timeutil.to_iso(n),
        "vienna": n.astimezone(ZoneInfo("Europe/Vienna")).strftime("%Y-%m-%d %H:%M %Z"),
    }


# --------------------------------------------------------------------------- begin_run (read-only)


def begin_run(conn: sqlite3.Connection, cfg: Config, inp: BeginRunInput) -> dict[str, Any]:
    existing = _find_run(conn, inp.run_key)
    out: dict[str, Any] = {
        "run_key": inp.run_key,
        "status": existing["status"] if existing else "open",
        "server_time_utc": timeutil.now_iso(),
    }
    if existing:
        out["note"] = f"run already finished ({existing['status']}); nothing more to do for this run_key"
    else:
        out.update(_research_window(conn, cfg))
    delivery = _delivery_signal(conn)
    if delivery:
        out["delivery"] = delivery
    log(logger, logging.INFO, "begin_run", run_key=inp.run_key, status=out["status"])
    return out


# --------------------------------------------------------------------------- match (read-only)


def match_candidates(conn: sqlite3.Connection, inp: MatchInput) -> dict[str, Any]:
    run = _find_run(conn, inp.run_key)
    if run is not None:
        raise RelayError(f"run {inp.run_key} is already {run['status']}")
    ids = [c.candidate_id for c in inp.candidates]
    if len(set(ids)) != len(ids):
        raise RelayError("candidate_id values must be unique within a request")
    results = [match_candidate(conn, c) for c in inp.candidates]
    summary: dict[str, int] = {}
    for r in results:
        summary[r["classification"]] = summary.get(r["classification"], 0) + 1
    log(logger, logging.INFO, "match_candidates", run_key=inp.run_key, **summary)
    return {"run_key": inp.run_key, "results": results}


# --------------------------------------------------------------------------- publish


def _request_hash(inp: PublishInput) -> str:
    return hashlib.sha256(inp.model_dump_json().encode()).hexdigest()


def _unique_topic_key(conn: sqlite3.Connection, base: str) -> str:
    key = base[:72]
    n = 2
    while conn.execute("SELECT 1 FROM topics WHERE topic_key = ?", (key,)).fetchone():
        key = f"{base[:72]}-{n}"
        n += 1
    return key


def _reindex_topic(conn: sqlite3.Connection, topic_id: str) -> None:
    topic = conn.execute(
        "SELECT title, entities, state_summary FROM topics WHERE id = ?", (topic_id,)
    ).fetchone()
    aliases = [
        r[0] for r in conn.execute("SELECT alias_norm FROM topic_aliases WHERE topic_id = ?", (topic_id,))
    ]
    facts: list[str] = []
    for r in conn.execute(
        "SELECT key_facts FROM stories WHERE topic_id = ? ORDER BY created_at DESC LIMIT 5", (topic_id,)
    ):
        facts.extend(json.loads(r[0]))
    body = "\n".join(
        [topic["title"], *aliases, *json.loads(topic["entities"]), topic["state_summary"], *facts]
    )
    conn.execute("DELETE FROM topic_fts WHERE topic_id = ?", (topic_id,))
    conn.execute("INSERT INTO topic_fts(topic_id, body) VALUES (?, ?)", (topic_id, body))


def _resolve_topic(conn: sqlite3.Connection, story: Story) -> sqlite3.Row | None:
    row: sqlite3.Row | None = None
    if story.topic_id:
        row = conn.execute("SELECT * FROM topics WHERE id = ?", (story.topic_id,)).fetchone()
    elif story.topic_key:
        row = conn.execute("SELECT * FROM topics WHERE topic_key = ?", (story.topic_key,)).fetchone()
    return row


def _store_story(
    conn: sqlite3.Connection, cfg: Config, run: sqlite3.Row, batch_id: str, story: Story, seq_start: int
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, int]:
    """Returns (accepted_info, rejected_info, next_seq)."""
    now_s = timeutil.now_iso()
    cfp = facts_fingerprint(story.key_facts)
    dup = conn.execute(
        "SELECT s.id, s.topic_id FROM stories s WHERE s.content_fp = ? LIMIT 1", (cfp,)
    ).fetchone()
    if dup:
        return (
            None,
            {"candidate_id": story.candidate_id, "reason": "EXACT_DUPLICATE", "topic_id": dup["topic_id"]},
            seq_start,
        )

    topic = _resolve_topic(conn, story)
    if story.kind in ("UPDATE", "CORRECTION"):
        if topic is None:
            return (
                None,
                {
                    "candidate_id": story.candidate_id,
                    "reason": "UPDATE/CORRECTION needs an existing topic_id from match results",
                },
                seq_start,
            )
        if not story.material_change:
            return (
                None,
                {"candidate_id": story.candidate_id, "reason": "UPDATE/CORRECTION requires material_change"},
                seq_start,
            )
    entities = list(dict.fromkeys(e.strip() for e in story.entities))
    if topic is None:
        topic_id = _new_id("top")
        key = _unique_topic_key(conn, story.topic_key or slugify(story.headline))
        conn.execute(
            "INSERT INTO topics(id, topic_key, title, norm_title, state_summary, category, entities, first_seen, "
            "last_seen, last_material_update, state, importance, story_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, 0)",
            (
                topic_id,
                key,
                story.headline,
                normalize_text(story.headline),
                story.topic_state_summary,
                story.category,
                json.dumps(entities[:30], ensure_ascii=False),
                now_s,
                now_s,
                now_s,
                story.importance,
            ),
        )
    else:
        topic_id = topic["id"]
        merged = list(dict.fromkeys([*json.loads(topic["entities"]), *entities]))[:30]
        conn.execute(
            "UPDATE topics SET state_summary = ?, entities = ?, last_seen = ?, last_material_update = ?, "
            "state = 'active', importance = MAX(importance, ?) WHERE id = ?",
            (
                story.topic_state_summary,
                json.dumps(merged, ensure_ascii=False),
                now_s,
                now_s,
                story.importance,
                topic_id,
            ),
        )
    conn.execute("UPDATE topics SET story_count = story_count + 1 WHERE id = ?", (topic_id,))
    conn.execute(
        "INSERT OR IGNORE INTO topic_aliases(topic_id, alias_norm) VALUES (?, ?)",
        (topic_id, normalize_text(story.headline)),
    )

    story_id = _new_id("sto")
    conn.execute(
        "INSERT INTO stories(id, topic_id, run_id, candidate_id, kind, headline, norm_title, key_facts, "
        "material_change, confidence, category, event_time, content_fp, title_fp, created_at, body) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            story_id,
            topic_id,
            run["id"],
            story.candidate_id,
            story.kind,
            story.headline,
            normalize_text(story.headline),
            json.dumps(list(story.key_facts), ensure_ascii=False),
            story.material_change,
            story.confidence,
            story.category,
            timeutil.to_iso(story.event_time) if story.event_time else None,
            cfp,
            title_fingerprint(story.headline),
            now_s,
            story.body,
        ),
    )
    for src in story.sources:
        conn.execute(
            "INSERT OR IGNORE INTO story_sources(story_id, url_hash, canonical_url, domain, source_name) "
            "VALUES (?, ?, ?, ?, ?)",
            (story_id, url_hash(src.url), canonical_url(src.url), url_domain(src.url), src.name),
        )
    _reindex_topic(conn, topic_id)

    payloads = build_payloads(
        story,
        username=cfg.discord_username,
        limit=cfg.max_message_chars,
        suppress_embeds=cfg.suppress_link_embeds,
        display_tz=cfg.display_timezone,
    )
    seq = seq_start
    for i, payload in enumerate(payloads):
        key = hashlib.sha256(f"{run['run_key']}|{story.candidate_id}|{i}".encode()).hexdigest()
        conn.execute(
            "INSERT INTO outbox(batch_id, story_id, seq, idempotency_key, payload, state, created_at, "
            "updated_at, next_attempt_at) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
            (batch_id, story_id, seq, key, json.dumps(payload, ensure_ascii=False), now_s, now_s, now_s),
        )
        seq += 1
    return (
        {
            "candidate_id": story.candidate_id,
            "story_id": story_id,
            "topic_id": topic_id,
            "messages": len(payloads),
        },
        None,
        seq,
    )


def publish_digest(conn: sqlite3.Connection, cfg: Config, inp: PublishInput) -> dict[str, Any]:
    check_disk(cfg.db_path, cfg.min_free_disk_mb)
    ids = [s.candidate_id for s in inp.stories]
    if len(set(ids)) != len(ids):
        raise RelayError("candidate_id values must be unique within a publish request")
    req_hash = _request_hash(inp)
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    with tx(conn):
        run = _find_run(conn, inp.run_key)
        if run is not None:
            batch = conn.execute(
                "SELECT request_hash FROM publication_batches WHERE run_id = ?", (run["id"],)
            ).fetchone()
            if batch is None:
                raise RelayError(f"run is already {run['status']}; nothing was changed")
            if batch["request_hash"] != req_hash:
                raise RelayError(
                    "this run was already published with different content; nothing was "
                    "changed. Each run publishes once; new stories belong to the next run."
                )
            replay = True
            cp = _checkpoint(conn)
        else:
            window = _research_window(conn, cfg)
            through = _effective_through(inp.research_through, window)
            run_id = _create_run(conn, window, inp.run_key, "published", through)
            run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            batch_id = _new_id("pub")
            conn.execute(
                "INSERT INTO publication_batches(id, run_id, idempotency_key, request_hash, created_at, "
                "story_count) VALUES (?, ?, ?, ?, ?, 0)",
                (batch_id, run_id, f"pub:{inp.run_key}", req_hash, timeutil.now_iso()),
            )
            seq = 0
            for story in inp.stories:
                acc, rej, seq = _store_story(conn, cfg, run, batch_id, story, seq)
                if acc:
                    accepted.append(acc)
                if rej:
                    rejected.append(rej)
            conn.execute(
                "UPDATE publication_batches SET story_count = ? WHERE id = ?", (len(accepted), batch_id)
            )
            conn.execute(
                "UPDATE runs SET accepted_count = ?, candidate_count = ? WHERE id = ?",
                (len(accepted), len(inp.stories), run_id),
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                (f"batch_result:{batch_id}", json.dumps({"rejected": rejected})),
            )
            cp = _advance_checkpoint(conn, run_id, through)
            replay = False
    status = publish_status(conn, StatusInput(run_key=inp.run_key))
    status["idempotent_replay"] = replay
    status["checkpoint"] = cp
    hint = _next_hint(conn, cfg)
    if hint:
        status["next"] = hint
    if not replay:
        log(
            logger,
            logging.INFO,
            "publish_digest",
            run_key=inp.run_key,
            publication_id=status["publication_id"],
            accepted=len(accepted),
            rejected=len(rejected),
        )
        for r in rejected:
            log(logger, logging.INFO, "story rejected at publish", run_key=inp.run_key, **r)
    return status


# --------------------------------------------------------------------------- noop


def complete_noop(conn: sqlite3.Connection, cfg: Config, inp: NoopInput) -> dict[str, Any]:
    with tx(conn):
        run = _find_run(conn, inp.run_key)
        if run is not None:
            if run["status"] == "completed_noop":
                return {
                    "run_key": inp.run_key,
                    "status": run["status"],
                    "checkpoint": _checkpoint(conn),
                    "idempotent_replay": True,
                }
            raise RelayError(f"run is already {run['status']}")
        window = _research_window(conn, cfg)
        through = _effective_through(inp.research_through, window)
        run_id = _create_run(conn, window, inp.run_key, "completed_noop", through)
        cp = _advance_checkpoint(conn, run_id, through)
    log(logger, logging.INFO, "complete_noop", run_key=inp.run_key, checkpoint=cp)
    out = {"run_key": inp.run_key, "status": "completed_noop", "checkpoint": cp, "idempotent_replay": False}
    hint = _next_hint(conn, cfg)
    if hint:
        out["next"] = hint
    return out


# --------------------------------------------------------------------------- status / health


def publish_status(conn: sqlite3.Connection, inp: StatusInput) -> dict[str, Any]:
    run = _find_run(conn, inp.run_key)
    if run is None:
        return {"run_key": inp.run_key, "run_status": "open", "publication": "none"}
    batch = conn.execute("SELECT * FROM publication_batches WHERE run_id = ?", (run["id"],)).fetchone()
    out: dict[str, Any] = {
        "run_key": inp.run_key,
        "run_status": run["status"],
        "research_through": run["research_through"],
    }
    if batch is None:
        out["publication"] = "none"
        return out
    stories = conn.execute(
        "SELECT candidate_id, id, publication_state, posted_at FROM stories WHERE run_id = ? ORDER BY created_at",
        (run["id"],),
    ).fetchall()
    counts = {
        r["state"]: r["n"]
        for r in conn.execute(
            "SELECT state, count(*) n FROM outbox WHERE batch_id = ? GROUP BY state", (batch["id"],)
        )
    }
    meta = conn.execute("SELECT value FROM meta WHERE key = ?", (f"batch_result:{batch['id']}",)).fetchone()
    if counts.get("uncertain"):
        overall = "uncertain"
    elif counts.get("failed") or counts.get("cancelled"):
        overall = "partially_failed" if counts.get("delivered") else "failed"
    elif counts.get("pending") or counts.get("dispatching"):
        overall = "queued"
    elif counts:
        overall = "delivered"
    else:
        overall = "accepted_nothing_to_send"
    out.update(
        {
            "publication_id": batch["id"],
            "publication": overall,
            "messages": counts,
            "stories": [
                {
                    "candidate_id": s["candidate_id"],
                    "story_id": s["id"],
                    "state": s["publication_state"],
                    "posted_at": s["posted_at"],
                }
                for s in stories
            ],
            "rejected": json.loads(meta[0])["rejected"] if meta else [],
        }
    )
    return out


def _meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def health(conn: sqlite3.Connection, cfg: Config) -> dict[str, Any]:
    from .publishing.worker import heartbeat_age

    hb_age = heartbeat_age(cfg.runtime_dir)
    schema = current_version(conn)
    # Missing heartbeat (worker never ran) or a stale one both count as unhealthy.
    worker_ok = hb_age is not None and hb_age < max(120.0, cfg.worker_poll_s * 6)
    return {
        "ok": schema == SCHEMA_VERSION and worker_ok,
        "version": __version__,
        "schema_version": schema,
        "worker_alive": worker_ok,
        "worker_heartbeat_age_s": None if hb_age is None else round(hb_age, 1),
        "discord_mode": _meta(conn, "discord_mode") or "unknown",
        "discord_configured": _meta(conn, "webhook_configured") == "yes",
        "discord_status": _meta(conn, "webhook_status") or "unknown",
        "last_checkpoint": _checkpoint(conn),
        "delivery": outbox.backlog(conn),
        "server_time": _server_time(),
    }


def _next_hint(conn: sqlite3.Connection, cfg: Config) -> dict[str, Any] | None:
    """After completion: tell the caller when unresearched time remains (outage catch-up)."""
    cp = _checkpoint(conn)
    if cp is None:
        return None
    behind = (timeutil.now() - timeutil.parse_iso(cp)).total_seconds() / 3600
    if behind <= 1.0:
        return None
    w = _research_window(conn, cfg)
    return {
        "behind_hours": round(behind, 1),
        "next_research_from": w["research_from"],
        "next_research_until": w["research_until"],
    }
