import sqlite3

from conftest import publish_input, story
from newsrelay import service
from newsrelay.logutil import redact
from newsrelay.maintenance import backup, cleanup, integrity
from newsrelay.publishing import outbox
from newsrelay.publishing.discord import Outcome, SendResult


def test_q_backup_opens_and_passes_integrity(cfg, conn, clock):  # TEST Q
    service.publish_digest(conn, cfg, publish_input("daily-news/2026-10-03", [story()]))
    res = backup.run_backup(conn, cfg)
    assert res["ok"]
    b = sqlite3.connect(res["file"])
    assert b.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert b.execute("SELECT count(*) FROM stories").fetchone()[0] == 1
    b.close()
    rt = backup.restore_test(cfg)
    assert rt["ok"] and rt["counts"]["stories"] == 1
    # weekly/monthly seeded once
    assert len(list((cfg.backup_dir / "weekly").glob("*.db"))) == 1
    assert len(list((cfg.backup_dir / "monthly").glob("*.db"))) == 1


def test_backup_rotation(cfg, conn, clock):
    for _ in range(cfg.backup_keep_daily + 5):
        clock.advance(days=1)
        backup.run_backup(conn, cfg)
    assert len(list((cfg.backup_dir / "daily").glob("*.db"))) == cfg.backup_keep_daily
    assert len(list((cfg.backup_dir / "weekly").glob("*.db"))) <= cfg.backup_keep_weekly


def test_restore_test_never_touches_production(cfg, conn, clock):
    service.publish_digest(conn, cfg, publish_input("daily-news/2026-10-03", [story()]))
    backup.run_backup(conn, cfg)
    before = cfg.db_path.stat().st_mtime_ns
    backup.restore_test(cfg)
    assert cfg.db_path.stat().st_mtime_ns == before


def test_retention_prunes_bulky_keeps_identity(cfg, conn, clock):
    service.publish_digest(conn, cfg, publish_input("daily-news/2026-10-03", [story()]))
    item = outbox.claim(conn, conn.execute("SELECT id FROM outbox").fetchone()[0])
    outbox.finish(conn, item, SendResult(Outcome.DELIVERED, 200, message_id="1"), cfg)
    clock.advance(days=cfg.body_retention_days + 1)
    res = cleanup.run_cleanup(conn, cfg)
    assert res["bodies_pruned"] == 1 and res["payloads_pruned"] == 1 and res["attempts_deleted"] == 1
    row = conn.execute("SELECT body, key_facts, content_fp, title_fp FROM stories").fetchone()
    assert row["body"] is None and row["key_facts"] and row["content_fp"]
    assert conn.execute("SELECT count(*) FROM story_sources").fetchone()[0] == 1
    assert conn.execute("SELECT idempotency_key FROM outbox").fetchone()[0]
    assert integrity.check(conn, full=True) == []


def test_topic_aging(cfg, conn, clock):
    service.publish_digest(conn, cfg, publish_input("daily-news/2026-10-03", [story()]))
    clock.advance(days=cfg.dormant_after_days + 1)
    cleanup.run_cleanup(conn, cfg)
    assert conn.execute("SELECT state FROM topics").fetchone()[0] == "dormant"
    clock.advance(days=cfg.archive_after_days)
    cleanup.run_cleanup(conn, cfg)
    assert conn.execute("SELECT state FROM topics").fetchone()[0] == "archived"


def test_disk_full_fails_closed(cfg, conn):
    import dataclasses

    import pytest

    from newsrelay.database import DiskFullError

    tight = dataclasses.replace(cfg, min_free_disk_mb=10**9)
    with pytest.raises(DiskFullError):
        service.publish_digest(conn, tight, publish_input("daily-news/2026-10-03", [story()]))
    assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


def test_redaction():
    hook = "https://discord.com/api/webhooks/123456789012345678/abcDEF_123-xyz" + "q" * 50
    s = redact(f"error posting to {hook}?wait=true Authorization: Bearer abc.def.ghi token=zzz")
    assert "abcDEF" not in s and "abc.def.ghi" not in s and "zzz" not in s
    assert "[REDACTED_WEBHOOK]" in s
