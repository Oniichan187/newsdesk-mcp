import json
import threading
from datetime import timedelta

import pytest
from pydantic import ValidationError

from conftest import candidate, publish_input, story
from newsrelay import service, timeutil
from newsrelay.database import SCHEMA_VERSION, connect, current_version, migrate, open_db
from newsrelay.dedup.matcher import EXACT_DUPLICATE, LIKELY_DUPLICATE, NO_MATCH, POSSIBLE_EXISTING_TOPIC
from newsrelay.schemas import BeginRunInput, Candidate, MatchInput, NoopInput, PublishInput, StatusInput

_CFG: dict = {}


@pytest.fixture(autouse=True)
def _bind_cfg(cfg):
    _CFG["cfg"] = cfg


def begin(conn, cfg, key="daily-news/2026-10-03"):
    return service.begin_run(conn, cfg, BeginRunInput(run_key=key))


def match(conn, key, *cands):
    return service.match_candidates(conn, _CFG["cfg"], MatchInput(run_key=key, candidates=list(cands)))[
        "results"
    ]


def outbox_count(conn):
    return conn.execute("SELECT count(*) FROM outbox").fetchone()[0]


def test_migrations_idempotent(cfg, conn):
    assert current_version(conn) == SCHEMA_VERSION
    assert migrate(conn) == SCHEMA_VERSION  # second run is a no-op
    c2 = connect(cfg.db_path)
    assert migrate(c2) == SCHEMA_VERSION
    assert c2.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert c2.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2  # incremental


def test_begin_run_is_read_only_and_idempotent(cfg, conn, clock):
    before = conn.total_changes
    r1 = begin(conn, cfg)
    r2 = begin(conn, cfg)
    assert conn.total_changes == before
    assert r1["status"] == "open" and r1 == r2
    assert r1["research_from"] == timeutil.to_iso(clock.t - timedelta(hours=cfg.initial_lookback_hours))


def test_a_identical_publication_calls_create_one_publication(cfg, conn):  # TEST A
    key = "daily-news/2026-10-03"
    inp = publish_input(key, [story()])
    a = service.publish_digest(conn, cfg, inp)
    b = service.publish_digest(conn, cfg, inp)
    assert a["publication_id"] == b["publication_id"]
    assert b["idempotent_replay"] is True
    assert conn.execute("SELECT count(*) FROM publication_batches").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM stories").fetchone()[0] == 1
    n = outbox_count(conn)
    assert n >= 1
    with pytest.raises(service.RelayError):
        service.publish_digest(conn, cfg, publish_input(key, [story(headline="Different headline here")]))
    assert outbox_count(conn) == n


def test_concurrent_duplicate_publish_requests(cfg, conn):
    key = "daily-news/2026-10-03"
    inp = publish_input(key, [story()])
    results, errors = [], []

    def worker():
        c = connect(cfg.db_path)
        try:
            results.append(service.publish_digest(c, cfg, inp))
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)
        finally:
            c.close()

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len({r["publication_id"] for r in results}) == 1
    assert conn.execute("SELECT count(*) FROM stories").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 1


def test_b_same_url_same_facts_is_exact_duplicate(cfg, conn, clock):  # TEST B
    service.publish_digest(conn, cfg, publish_input("daily-news/2026-10-03", [story()]))
    clock.advance(days=1)
    res = match(conn, "daily-news/2026-10-04", candidate())
    assert res[0]["classification"] == EXACT_DUPLICATE
    # and the publish path re-checks it
    out = service.publish_digest(conn, cfg, publish_input("daily-news/2026-10-04", [story()]))
    assert out["rejected"][0]["reason"] == "EXACT_DUPLICATE"


def test_c_different_url_same_facts_is_likely_duplicate(cfg, conn, clock):  # TEST C
    service.publish_digest(conn, cfg, publish_input("daily-news/2026-10-03", [story()]))
    clock.advance(days=1)
    c = candidate(
        title="Brussels hits Meta with €798m antitrust fine over Marketplace",
        key_facts=[
            "The European Commission fined Meta 798 million euros",
            "The fine concerns tying Facebook Marketplace to the social network",
            "Meta said it will appeal the decision",
        ],
        source_urls=["https://www.spiegel.de/wirtschaft/meta-eu-strafe-a-123.html"],
    )
    res = match(conn, "daily-news/2026-10-04", c)
    assert res[0]["classification"] in (LIKELY_DUPLICATE, EXACT_DUPLICATE)
    assert "prior_facts" not in res[0]["match"]  # duplicates return compact info only


def test_d_material_update_returns_existing_topic(cfg, conn, clock):  # TEST D
    first = service.publish_digest(conn, cfg, publish_input("daily-news/2026-10-03", [story()]))
    assert first["stories"][0]["story_id"]
    topic_id = conn.execute("SELECT id FROM topics").fetchone()[0]
    clock.advance(days=2)
    c = candidate(
        title="Meta appeals EU Marketplace fine at General Court",
        key_facts=[
            "Meta filed an appeal at the EU General Court against the 798 million euro fine",
            "European Commission fined Meta 798 million euros",
            "Court hearing date not yet set",
        ],
        source_urls=["https://www.derstandard.at/story/3000000123/meta-berufung"],
    )
    res = match(conn, "daily-news/2026-10-05", c)[0]
    assert res["classification"] == POSSIBLE_EXISTING_TOPIC
    assert res["match"]["topic_id"] == topic_id
    assert 0 in res["match"]["new_fact_indexes"]
    assert res["match"]["prior_facts"]
    # publish the update -> same topic, new story
    upd = story(
        "u1",
        kind="UPDATE",
        topic_id=topic_id,
        headline="Meta appeals EU Marketplace fine",
        key_facts=list(c.key_facts),
        material_change="Meta formally filed its appeal",
        sources=[{"url": "https://www.derstandard.at/story/3000000123/meta-berufung"}],
    )
    out = service.publish_digest(conn, cfg, publish_input("daily-news/2026-10-05", [upd]))
    assert out["rejected"] == []
    assert conn.execute("SELECT count(*) FROM topics").fetchone()[0] == 1
    assert conn.execute("SELECT story_count FROM topics").fetchone()[0] == 2


def test_same_url_with_changed_facts_is_not_auto_duplicate(cfg, conn, clock):
    service.publish_digest(conn, cfg, publish_input("daily-news/2026-10-03", [story()]))
    clock.advance(days=1)
    c = candidate(
        key_facts=[
            "Meta lost its appeal at the EU General Court",
            "The court upheld the full 798 million euro fine",
            "Meta can still go to the Court of Justice",
        ]
    )
    res = match(conn, "daily-news/2026-10-04", c)[0]
    assert res["classification"] == POSSIBLE_EXISTING_TOPIC


def test_unrelated_candidate_no_match(cfg, conn, clock):
    service.publish_digest(conn, cfg, publish_input("daily-news/2026-10-03", [story()]))
    c = candidate(
        "c9",
        title="Earthquake of magnitude 6.1 strikes central Chile",
        category="disaster",
        entities=["Chile", "SENAPRED"],
        key_facts=["Magnitude 6.1 quake hit central Chile", "No casualties reported so far"],
        source_urls=["https://www.zdf.de/nachrichten/chile-beben-100.html"],
    )
    assert match(conn, "daily-news/2026-10-04", c)[0]["classification"] == NO_MATCH


def test_e_archived_topic_found_without_bulk_history(cfg, conn, clock):  # TEST E
    from newsrelay.maintenance.cleanup import run_cleanup

    service.publish_digest(conn, cfg, publish_input("daily-news/2026-01-01", [story()]))
    # 300 unrelated filler topics
    for i in range(30):
        stories = [
            story(
                f"f{i}_{j}",
                headline=f"Filler event {i}-{j} about town council number {i * 10 + j}",
                key_facts=[f"Town council {i * 10 + j} approved budget item {j}"],
                entities=[f"Town{i * 10 + j}"],
                sources=[{"url": f"https://n{i}.example.org/n/{j}"}],
            )
            for j in range(10)
        ]
        clock.advance(hours=1)
        service.publish_digest(
            conn, cfg, publish_input(f"filler/2026-01-{(i % 28) + 1:02d}T{i % 24:02d}", stories)
        )
    clock.advance(days=400)
    run_cleanup(conn, cfg)
    run_cleanup(conn, cfg)
    assert (
        conn.execute("SELECT state FROM topics WHERE topic_key LIKE 'eu-fines-meta%'").fetchone()[0]
        == "archived"
    )
    res = match(conn, "daily-news/2027-02-08", candidate())[0]
    assert res["classification"] == EXACT_DUPLICATE
    assert res["match"]["topic_state"] == "archived"
    assert len(json.dumps(res)) < 1500  # compact answer, not bulk history


def test_f_noop_advances_checkpoint(cfg, conn, clock):  # TEST F
    key = "daily-news/2026-10-03"
    out = service.complete_noop(
        conn, cfg, NoopInput.model_validate({"run_key": key, "research_through": timeutil.now_iso()})
    )
    assert out["checkpoint"] == timeutil.now_iso()
    again = service.complete_noop(
        conn, cfg, NoopInput.model_validate({"run_key": key, "research_through": timeutil.now_iso()})
    )
    assert again["idempotent_replay"] is True
    clock.advance(days=1)
    nxt = begin(conn, cfg, "daily-news/2026-10-04")
    assert nxt["research_from"] == timeutil.to_iso(clock.t - timedelta(days=1, hours=cfg.overlap_hours))


def test_g_failed_run_does_not_advance_checkpoint(cfg, conn, clock):  # TEST G
    service.complete_noop(
        conn,
        cfg,
        NoopInput.model_validate(
            {"run_key": "daily-news/2026-10-03", "research_through": timeutil.now_iso()}
        ),
    )
    cp = conn.execute("SELECT research_through FROM checkpoint").fetchone()[0]
    clock.advance(days=1)
    begin(conn, cfg, "daily-news/2026-10-04")
    match(conn, "daily-news/2026-10-04", candidate())
    # run fails here: no publish, no noop
    assert conn.execute("SELECT research_through FROM checkpoint").fetchone()[0] == cp
    # invalid publish (future research_through) must not advance either
    with pytest.raises(service.RelayError):
        service.publish_digest(
            conn,
            cfg,
            publish_input(
                "daily-news/2026-10-04", [story()], through=timeutil.to_iso(clock.t + timedelta(hours=3))
            ),
        )
    assert conn.execute("SELECT research_through FROM checkpoint").fetchone()[0] == cp
    assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 1


def test_h_three_day_outage_requests_missing_interval(cfg, conn, clock):  # TEST H
    t0 = clock.t
    service.complete_noop(
        conn,
        cfg,
        NoopInput.model_validate(
            {"run_key": "daily-news/2026-10-03", "research_through": timeutil.now_iso()}
        ),
    )
    clock.advance(days=4)  # runs on 04, 05, 06 never happened
    r = begin(conn, cfg, "daily-news/2026-10-07")
    assert r["research_from"] == timeutil.to_iso(t0 - timedelta(hours=cfg.overlap_hours))
    assert r["research_until"] == timeutil.now_iso()
    assert r["catch_up"] is False


def test_long_gap_is_caught_up_not_capped(cfg, conn, clock):
    t0 = clock.t
    service.complete_noop(
        conn,
        cfg,
        NoopInput.model_validate(
            {"run_key": "daily-news/2026-10-03", "research_through": timeutil.now_iso()}
        ),
    )
    clock.advance(days=60)
    r = begin(conn, cfg, "daily-news/2026-12-02")
    assert r["catch_up"] is True
    assert r["research_from"] == timeutil.to_iso(t0 - timedelta(hours=cfg.overlap_hours))
    assert r["research_until"] == timeutil.to_iso(t0 + timedelta(days=cfg.catchup_window_days))


def test_checkpoint_never_moves_backwards(cfg, conn, clock):
    service.complete_noop(
        conn,
        cfg,
        NoopInput.model_validate({"run_key": "news/2026-10-03T06", "research_through": timeutil.now_iso()}),
    )
    older = timeutil.to_iso(clock.t - timedelta(hours=5))
    service.complete_noop(
        conn, cfg, NoopInput.model_validate({"run_key": "news/2026-10-03T00", "research_through": older})
    )
    assert conn.execute("SELECT research_through FROM checkpoint").fetchone()[0] == timeutil.now_iso()


def test_update_without_topic_rejected(cfg, conn):
    out = service.publish_digest(
        conn, cfg, publish_input("daily-news/2026-10-03", [story(kind="UPDATE", material_change="x changed")])
    )
    assert "existing topic" in out["rejected"][0]["reason"]


def test_publish_status_reports_queued(cfg, conn):
    service.publish_digest(conn, cfg, publish_input("daily-news/2026-10-03", [story()]))
    st = service.publish_status(conn, StatusInput(run_key="daily-news/2026-10-03"))
    assert st["publication"] == "queued"
    assert service.publish_status(conn, StatusInput(run_key="daily-news/2026-10-09"))["publication"] == "none"


def test_match_after_completion_refused(cfg, conn):
    service.complete_noop(
        conn,
        cfg,
        NoopInput.model_validate(
            {"run_key": "daily-news/2026-10-03", "research_through": timeutil.now_iso()}
        ),
    )
    with pytest.raises(service.RelayError):
        match(conn, "daily-news/2026-10-03", candidate())


# ---------------------------------------------------------------- schema validation


@pytest.mark.parametrize(
    "bad",
    [
        {"source_urls": ["http://insecure.example.com/x"]},
        {"source_urls": ["https://user:pw@example.com/x"]},
        {"source_urls": ["javascript:alert(1)"]},
        {"source_urls": ["https://example.com/" + "a" * 700]},
        {"key_facts": []},
        {"key_facts": ["f" * 300]},
        {"key_facts": [f"fact number {i}" for i in range(9)]},
        {"entities": [f"e{i}" for i in range(13)]},
        {"title": "x" * 201},
        {"title": "bad\x00title"},
        {"unknown_field": 1},
        {"candidate_id": "has space"},
        {"category": "sports-gossip"},
        {"event_time": "2026-10-03T10:00:00"},  # naive timestamp
    ],
)
def test_invalid_candidate_rejected(bad):
    data = candidate().model_dump()
    data.update(bad)
    with pytest.raises(ValidationError):
        Candidate.model_validate(data)


def test_oversized_lists_rejected():
    with pytest.raises(ValidationError):
        MatchInput.model_validate(
            {
                "run_key": "daily-news/2026-10-03",
                "candidates": [candidate(f"c{i}").model_dump() for i in range(41)],
            }
        )
    with pytest.raises(ValidationError):
        PublishInput.model_validate(
            {
                "run_key": "daily-news/2026-10-03",
                "research_through": "2026-10-03T00:00:00Z",
                "stories": [story(f"s{i}") for i in range(21)],
            }
        )
    with pytest.raises(ValidationError):
        PublishInput.model_validate(
            {
                "run_key": "daily-news/2026-10-03",
                "research_through": "2026-10-03T00:00:00Z",
                "stories": [story(body="x" * 3501)],
            }
        )


@pytest.mark.parametrize(
    "key", ["../etc/passwd", "daily-news", "Daily/2026-10-03", "a/2026-1-3", "x" * 50 + "/2026-10-03"]
)
def test_bad_run_keys(key):
    with pytest.raises(ValidationError):
        BeginRunInput(run_key=key)


def test_open_db_twice_safe(cfg, conn):
    c = open_db(cfg.db_path)
    assert current_version(c) == SCHEMA_VERSION
