"""Small-batch staging: stories handed over in pieces, published by a small final call or by the worker."""

from __future__ import annotations

import json

import pytest

from conftest import story
from newsrelay import service, timeutil
from newsrelay.publishing.worker import Worker
from newsrelay.schemas import PublishInput, StageInput


def _story(n: int, **kw):
    return story(
        f"c{n}",
        headline=f"Distinct staged story number {n} about a separate topic",
        key_facts=[f"Unique staged fact {n}", f"Second staged fact {n}"],
        entities=[f"StageEntity{n}"],
        importance=n,
        **kw,
    )


def _stage(conn, cfg, run_key, *ns, **kw):
    return service.stage_stories(
        conn,
        cfg,
        StageInput.model_validate(
            {
                "run_key": run_key,
                "research_through": timeutil.now_iso(),
                "stories": [_story(n, **kw) for n in ns],
            }
        ),
    )


def _publish(conn, cfg, run_key, stories=()):
    return service.publish_digest(
        conn,
        cfg,
        PublishInput.model_validate(
            {"run_key": run_key, "research_through": timeutil.now_iso(), "stories": list(stories)}
        ),
    )


def test_staged_batches_are_published_by_a_call_without_stories(cfg, conn, clock):
    assert _stage(conn, cfg, "daily-news/2026-10-03", 1, 2)["staged_total"] == 2
    assert _stage(conn, cfg, "daily-news/2026-10-03", 3)["staged_total"] == 3
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0  # nothing posted yet
    res = _publish(conn, cfg, "daily-news/2026-10-03")
    assert res["idempotent_replay"] is False
    headlines = [json.loads(r[0])["content"] for r in conn.execute("SELECT payload FROM outbox ORDER BY id")]
    assert len(headlines) == 3 and "number 3" in headlines[0]  # most important first
    assert conn.execute("SELECT count(*) FROM staged_stories").fetchone()[0] == 0
    again = _publish(conn, cfg, "daily-news/2026-10-03")  # ChatGPT retries the final call
    assert again["idempotent_replay"] is True
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 3


def test_staging_rejects_foreign_sources_and_runs_already_done(cfg, conn, clock):
    res = _stage(
        conn, cfg, "daily-news/2026-10-03", 1, sources=[{"url": "https://evil.example.net/a/b", "name": "X"}]
    )
    assert res["staged"] == [] and res["rejected"][0]["reason"].startswith("SOURCE_NOT_ALLOWED")
    _stage(conn, cfg, "daily-news/2026-10-03", 2)
    _publish(conn, cfg, "daily-news/2026-10-03")
    with pytest.raises(service.RelayError):
        _stage(conn, cfg, "daily-news/2026-10-03", 3)


def test_publish_without_any_stories_is_an_error(cfg, conn, clock):
    with pytest.raises(service.RelayError, match="nothing to publish"):
        _publish(conn, cfg, "daily-news/2026-10-03")


def test_stories_sent_again_with_the_final_call_win(cfg, conn, clock):
    _stage(conn, cfg, "daily-news/2026-10-03", 1)
    _publish(
        conn,
        cfg,
        "daily-news/2026-10-03",
        [_story(1, body="**What happened:** Corrected text for story one here.")],
    )
    (payload,) = [json.loads(r[0])["content"] for r in conn.execute("SELECT payload FROM outbox")]
    assert "Corrected text" in payload


def test_worker_publishes_a_run_whose_final_call_never_arrived(cfg, conn, clock):
    _stage(conn, cfg, "daily-news/2026-10-03", 1, 2)
    w = Worker(cfg, conn, None)
    assert w.autopublish() == 0  # too early
    clock.advance(minutes=cfg.stage_autopublish_minutes + 1)
    w._autopublish_checked = None
    assert w.autopublish() == 1
    assert (
        conn.execute("SELECT status FROM runs WHERE run_key = 'daily-news/2026-10-03'").fetchone()[0]
        == "published"
    )
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 2
    # ChatGPT's late final call is then a harmless replay
    assert _publish(conn, cfg, "daily-news/2026-10-03")["idempotent_replay"] is True
