"""Audit tests: no silent coverage gaps, durable outbox, retention safety, watchdog semantics."""

from __future__ import annotations

import os
import threading
import time
from datetime import timedelta

import httpx2 as httpx
import pytest

from conftest import publish_input, story
from newsrelay import service, timeutil
from newsrelay.database import connect, current_version, migrate
from newsrelay.maintenance.cleanup import run_cleanup
from newsrelay.publishing import outbox, worker
from newsrelay.publishing.discord import Outcome, SendResult, WebhookTransport
from newsrelay.schemas import BeginRunInput, NoopInput
from test_outbox_worker import HOOK, Recorder, drain, mock_transport, ok, queue_one, states

# ---------------------------------------------------------------- checkpoint: no silent gaps


def _noop(conn, cfg, key: str, through: str | None = None) -> dict:
    return service.complete_noop(
        conn,
        cfg,
        NoopInput.model_validate({"run_key": key, "research_through": through or timeutil.now_iso()}),
    )


@pytest.mark.parametrize("outage_days", [3, 20, 90])
def test_outage_is_covered_contiguously_without_gaps(cfg, conn, clock, outage_days):
    _noop(conn, cfg, "daily-news/2026-10-03")
    covered_until = timeutil.parse_iso(conn.execute("SELECT research_through FROM checkpoint").fetchone()[0])
    clock.advance(days=outage_days)
    runs = 0
    while True:
        runs += 1
        assert runs < 40
        prefix = "daily-news" if runs == 1 else f"catchup{runs}"
        key = f"{prefix}/{timeutil.now_iso()[:10]}"
        w = service.begin_run(conn, cfg, BeginRunInput(run_key=key))
        start = timeutil.parse_iso(w["research_from"])
        until = timeutil.parse_iso(w["research_until"])
        # every window starts at (or overlaps) the previous coverage end -> no gap
        assert start <= covered_until
        assert until - covered_until <= timedelta(days=cfg.catchup_window_days)
        # ChatGPT reports "researched until now"; the relay must clamp to the window
        out = _noop(conn, cfg, key)
        assert timeutil.parse_iso(out["checkpoint"]) == until
        covered_until = until
        if covered_until >= clock.t.replace(microsecond=0):
            assert "next" not in out
            break
        assert out["next"]["behind_hours"] > 1  # caller is told time is still unresearched
    expected_runs = (
        1 if outage_days <= cfg.catchup_window_days else -(-outage_days // int(cfg.catchup_window_days))
    )
    assert runs == expected_runs
    assert covered_until == clock.t.replace(microsecond=0)


def test_checkpoint_never_passes_window_even_if_caller_claims_more(cfg, conn, clock):
    _noop(conn, cfg, "daily-news/2026-10-03")
    t0 = clock.t
    clock.advance(days=30)
    out = _noop(conn, cfg, "daily-news/2026-11-02", through=timeutil.now_iso())
    assert out["checkpoint"] == timeutil.to_iso(t0 + timedelta(days=cfg.catchup_window_days))
    assert out["next"]["behind_hours"] > 24


def test_publish_also_clamped_to_window(cfg, conn, clock):
    _noop(conn, cfg, "daily-news/2026-10-03")
    t0 = clock.t
    clock.advance(days=20)
    out = service.publish_digest(conn, cfg, publish_input("daily-news/2026-10-23", [story()]))
    assert out["checkpoint"] == timeutil.to_iso(t0 + timedelta(days=cfg.catchup_window_days))
    assert out["next"]["behind_hours"] > 24


def test_begin_run_is_compact(cfg, conn, clock):
    service.publish_digest(conn, cfg, publish_input("daily-news/2026-10-03", [story()]))
    clock.advance(days=1)
    r = service.begin_run(conn, cfg, BeginRunInput(run_key="daily-news/2026-10-04"))
    assert "recent_headlines" not in r
    assert set(r) <= {
        "run_key",
        "status",
        "server_time_utc",
        "research_from",
        "research_until",
        "catch_up",
        "behind_days",
        "note",
        "delivery",
    }
    assert "delivery" in r  # the queued, undelivered story is >1h old -> degraded signal


# ---------------------------------------------------------------- outbox durability


def test_nothing_is_ever_dropped_by_age(cfg, conn, clock):
    queue_one(conn, cfg)
    clock.advance(days=365)
    worker.Worker(cfg, conn, None).run_once()
    assert states(conn) == ["pending"]
    assert conn.execute("SELECT payload FROM outbox").fetchone()[0]


def test_failed_items_are_recoverable_after_webhook_fix(cfg, conn, clock):
    rec = Recorder([lambda r: httpx.Response(503)])
    queue_one(conn, cfg)
    drain(worker.Worker(cfg, conn, mock_transport(rec)), clock)
    assert states(conn) == ["failed"]
    assert outbox.requeue_failed(conn) == 1
    drain(worker.Worker(cfg, conn, mock_transport(Recorder([ok]))), clock)
    assert states(conn) == ["delivered"]


def test_retention_never_prunes_unresolved_items(cfg, conn, clock):
    for i, key in enumerate(["daily-news/2026-10-01", "daily-news/2026-10-02", "daily-news/2026-10-03"]):
        queue_one(
            conn,
            cfg,
            key=key,
            candidate_id=f"c{i}",
            headline=f"Distinct headline number {i}",
            key_facts=[f"distinct fact {i} about a separate event"],
            sources=[{"url": f"https://example.org/{i}"}],
        )
    ids = [r[0] for r in conn.execute("SELECT id FROM outbox ORDER BY id")]
    # item 0 failed, item 1 uncertain, item 2 pending
    c0 = outbox.claim(conn, ids[0])
    outbox.finish(conn, c0, SendResult(Outcome.PERMANENT, 400, error="bad"), cfg)
    c1 = outbox.claim(conn, ids[1])
    outbox.finish(conn, c1, SendResult(Outcome.UNCERTAIN, error="timeout"), cfg)
    clock.advance(days=cfg.body_retention_days * 5)
    run_cleanup(conn, cfg)
    rows = conn.execute("SELECT state, payload FROM outbox ORDER BY id").fetchall()
    assert [r["state"] for r in rows] == ["failed", "uncertain", "pending"]
    assert all(r["payload"] for r in rows)
    assert conn.execute("SELECT count(*) FROM stories WHERE body IS NULL").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM delivery_attempts").fetchone()[0] == 2


def test_resolve_rejects_resend_without_payload(cfg, conn):
    queue_one(conn, cfg)
    oid = conn.execute("SELECT id FROM outbox").fetchone()[0]
    c = outbox.claim(conn, oid)
    outbox.finish(conn, c, SendResult(Outcome.UNCERTAIN, error="t"), cfg)
    conn.execute("UPDATE outbox SET payload = NULL")
    with pytest.raises(ValueError):
        outbox.resolve(conn, oid, "resend")
    assert outbox.resolve(conn, oid, "delivered") == "delivered"


def test_write_error_after_partial_send_is_uncertain(cfg, conn, clock):
    def write_err(req):
        raise httpx.WriteError("broken pipe", request=req)

    queue_one(conn, cfg)
    drain(worker.Worker(cfg, conn, mock_transport(Recorder([write_err]))), clock)
    assert states(conn) == ["uncertain"]


def test_transport_errors_never_leak_webhook(cfg, conn, clock, caplog):
    def boom(req):
        raise httpx.ConnectError(f"failed to reach {req.url}", request=req)

    queue_one(conn, cfg)
    t = mock_transport(Recorder([boom]))
    worker.Worker(cfg, conn, t).run_once()
    err = conn.execute("SELECT last_error FROM outbox").fetchone()[0]
    token = HOOK.rsplit("/", 1)[1]
    assert token not in err
    assert token not in caplog.text
    assert token not in repr(t)


# ---------------------------------------------------------------- worker readiness / watchdog


class Notes:
    def __init__(self) -> None:
        self.msgs: list[tuple[float, str]] = []

    def __call__(self, m: str) -> bool:
        self.msgs.append((time.monotonic(), m))
        return True

    def names(self) -> list[str]:
        return [m for _, m in self.msgs]


def test_worker_ready_only_after_recovery(cfg, conn, clock):
    queue_one(conn, cfg)
    oid = conn.execute("SELECT id FROM outbox").fetchone()[0]
    outbox.claim(conn, oid)  # simulate crash mid-dispatch
    notes = Notes()
    stop = threading.Event()
    w = worker.Worker(cfg, conn, None, stop, notes)
    order = []
    orig = w.prepare

    def tracked_prepare() -> int:
        order.append(("prepare", list(notes.names())))
        return orig()

    w.prepare = tracked_prepare  # type: ignore[method-assign]
    t = threading.Thread(target=w.run_forever)
    t.start()
    time.sleep(0.3)
    stop.set()
    t.join(5)
    assert order[0][1] == []  # nothing (no READY) was sent before recovery ran
    assert notes.names()[0] == "READY=1"
    assert states(conn) == ["uncertain"]


def test_worker_main_lock_failure_never_ready(cfg, clock):
    notes = Notes()
    lock = worker.acquire_lock(cfg.runtime_dir)
    try:
        assert worker.main(cfg, notify=notes) == 1
    finally:
        lock.close()
    assert "READY=1" not in notes.names()


def test_worker_main_db_failure_never_ready(cfg, tmp_path, clock):
    import dataclasses

    bad = dataclasses.replace(cfg, db_path=tmp_path / "missing-dir" / "x.db")
    notes = Notes()
    with pytest.raises(Exception):
        worker.main(bad, notify=notes)
    assert "READY=1" not in notes.names()
    # the lock was released on the failure path
    worker.acquire_lock(cfg.runtime_dir).close()


def test_frozen_loop_stops_watchdog(cfg, conn, clock):
    gate = threading.Event()

    class Hanging:
        dedup_window_s = 0.0

        def close(self):
            pass

        def send(self, payload, key=None):
            gate.wait(5)  # loop is "frozen" inside a call
            return SendResult(Outcome.DELIVERED, 200, message_id="1")

    queue_one(conn, cfg)
    notes = Notes()
    stop = threading.Event()
    w = worker.Worker(cfg, conn, Hanging(), stop, notes)
    t = threading.Thread(target=w.run_forever)
    t.start()
    time.sleep(0.5)
    n_before = notes.names().count("WATCHDOG=1")
    time.sleep(1.0)
    assert notes.names().count("WATCHDOG=1") == n_before  # no pings while hung
    gate.set()
    time.sleep(0.5)
    stop.set()
    t.join(5)
    assert notes.names().count("WATCHDOG=1") > n_before  # pings resume once the loop runs again


def test_missing_and_stale_heartbeat_are_unhealthy(cfg, conn):
    hb = cfg.runtime_dir / "worker.heartbeat"
    hb.unlink(missing_ok=True)
    assert service.health(conn, cfg)["worker_alive"] is False
    hb.touch()
    assert service.health(conn, cfg)["worker_alive"] is True
    old = time.time() - 3600
    os.utime(hb, (old, old))
    assert service.health(conn, cfg)["worker_alive"] is False


def test_worker_closes_transport_on_shutdown(cfg, clock, tmp_path):
    (cfg.secret_dir_override / "discord_webhook").write_text(HOOK)
    closed = []
    orig = WebhookTransport.close

    def spy(self):
        closed.append(True)
        orig(self)

    WebhookTransport.close = spy  # type: ignore[method-assign]
    try:
        notes = Notes()

        def stop_soon(m: str) -> bool:
            notes(m)
            if m == "READY=1":
                os.kill(os.getpid(), __import__("signal").SIGINT)
            return True

        assert worker.main(cfg, notify=stop_soon) == 0
    finally:
        WebhookTransport.close = orig  # type: ignore[method-assign]
    assert closed == [True]
    assert "STOPPING=1" in notes.names()


# ---------------------------------------------------------------- migrations


def test_failing_migration_leaves_db_untouched(cfg, conn, monkeypatch):
    from newsrelay import database

    real = database._migration_scripts
    nxt = database.SCHEMA_VERSION + 1
    monkeypatch.setattr(database, "SCHEMA_VERSION", nxt)
    monkeypatch.setattr(
        database,
        "_migration_scripts",
        lambda: [*real(), (nxt, "CREATE TABLE new_thing (x INTEGER);\nTHIS IS NOT SQL;\n")],
    )
    c = connect(cfg.db_path)
    with pytest.raises(Exception):
        migrate(c)
    assert current_version(c) == nxt - 1
    assert c.execute("SELECT count(*) FROM sqlite_master WHERE name='new_thing'").fetchone()[0] == 0


def test_old_code_refuses_newer_schema(cfg, conn):
    conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (99, 'x')")
    with pytest.raises(RuntimeError):
        migrate(connect(cfg.db_path))


def test_upgrade_check_on_copy(cfg, conn, clock, tmp_path, capsys):
    from newsrelay.cli import cmd_upgrade_check

    service.publish_digest(conn, cfg, publish_input("daily-news/2026-10-03", [story()]))
    copy = tmp_path / "copy.db"
    conn.execute(f"VACUUM INTO '{copy}'")
    before = cfg.db_path.stat().st_mtime_ns

    class A:
        db_copy = str(copy)

    assert cmd_upgrade_check(cfg, A()) == 0  # type: ignore[arg-type]
    assert cfg.db_path.stat().st_mtime_ns == before
    assert '"ok": true' in capsys.readouterr().out


def test_first_run_is_capped_and_says_so(cfg, conn, clock):
    w = service.begin_run(conn, cfg, BeginRunInput(run_key="daily-news/2026-10-03"))
    assert "first run" in w["note"] and str(cfg.first_run_max_stories) in w["note"]
    assert w["research_from"] == timeutil.to_iso(clock.t - timedelta(hours=cfg.initial_lookback_hours))
    many = [
        story(f"s{i}", headline=f"Distinct first-run story number {i}", importance=1 + (i % 3),
              key_facts=[f"unique fact {i} for the first run"], sources=[{"url": f"https://example.org/{i}"}])
        for i in range(12)
    ]  # fmt: skip
    out = service.publish_digest(conn, cfg, publish_input("daily-news/2026-10-03", many))
    assert len(out["stories"]) == cfg.first_run_max_stories
    limited = [r for r in out["rejected"] if r["reason"].startswith("FIRST_RUN_LIMIT")]
    assert len(limited) == 12 - cfg.first_run_max_stories
    kept = {s["candidate_id"] for s in out["stories"]}
    assert {f"s{i}" for i in range(12) if 1 + (i % 3) == 3} <= kept  # most important first
    # second run: no cap any more
    clock.advance(days=1)
    more = [
        story(f"t{i}", headline=f"Second-day distinct story {i}", key_facts=[f"second day fact {i}"],
              sources=[{"url": f"https://example.org/t{i}"}])
        for i in range(10)
    ]  # fmt: skip
    out2 = service.publish_digest(conn, cfg, publish_input("daily-news/2026-10-04", more))
    assert len(out2["stories"]) == 10
