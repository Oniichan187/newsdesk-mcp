"""Retention: prune bulky/diagnostic data, keep compact identity data forever, age topics."""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any

from .. import timeutil
from ..config import Config
from ..database import tx
from ..logutil import log

logger = logging.getLogger("newsrelay.maintenance")


def run_cleanup(conn: sqlite3.Connection, cfg: Config) -> dict[str, Any]:
    body_cut = timeutil.iso_plus(-cfg.body_retention_days * 86400)
    attempt_cut = timeutil.iso_plus(-cfg.attempt_retention_days * 86400)
    dormant_cut = timeutil.iso_plus(-cfg.dormant_after_days * 86400)
    archive_cut = timeutil.iso_plus(-cfg.archive_after_days * 86400)
    res: dict[str, Any] = {}
    with tx(conn):
        # Bulky text: rendered bodies and Discord payloads. Fingerprints/facts/titles/URLs stay.
        # Only content that is fully resolved (delivered or deliberately cancelled) may lose its
        # body/payload. Pending, failed and uncertain items keep everything needed to resend.
        res["bodies_pruned"] = conn.execute(
            "UPDATE stories SET body = NULL WHERE body IS NOT NULL AND created_at < ? "
            "AND NOT EXISTS (SELECT 1 FROM outbox o WHERE o.story_id = stories.id "
            "AND o.state NOT IN ('delivered','cancelled'))",
            (body_cut,),
        ).rowcount
        res["payloads_pruned"] = conn.execute(
            "UPDATE outbox SET payload = NULL WHERE payload IS NOT NULL AND created_at < ? "
            "AND state IN ('delivered','cancelled')",
            (body_cut,),
        ).rowcount
        res["attempts_deleted"] = conn.execute(
            "DELETE FROM delivery_attempts WHERE started_at < ? AND outbox_id IN "
            "(SELECT id FROM outbox WHERE state IN ('delivered','cancelled'))",
            (attempt_cut,),
        ).rowcount
        res["batch_meta_deleted"] = conn.execute(
            "DELETE FROM meta WHERE key LIKE 'batch_result:%' AND key IN (SELECT 'batch_result:' || b.id "
            "FROM publication_batches b WHERE b.created_at < ? AND NOT EXISTS (SELECT 1 FROM outbox o "
            "WHERE o.batch_id = b.id AND o.state NOT IN ('delivered','cancelled')))",
            (body_cut,),
        ).rowcount
        now = time.time()
        conn.execute("DELETE FROM oauth_pending WHERE expires_at < ?", (now,))
        conn.execute("DELETE FROM oauth_codes WHERE expires_at < ?", (now,))
        conn.execute("DELETE FROM oauth_tokens WHERE expires_at < ?", (int(now) - 86400,))
        conn.execute("DELETE FROM auth_failures WHERE ts < ?", (now - 86400,))
        # Topic aging: still searchable locally (FTS + fingerprints); just not "active".
        res["topics_dormant"] = conn.execute(
            "UPDATE topics SET state = 'dormant' WHERE state = 'active' AND last_seen < ?", (dormant_cut,)
        ).rowcount
        res["topics_archived"] = conn.execute(
            "UPDATE topics SET state = 'archived' WHERE state = 'dormant' AND last_seen < ?", (archive_cut,)
        ).rowcount
    # Reclaim a bounded number of free pages; avoids full VACUUM write amplification on SD cards.
    free = conn.execute("PRAGMA freelist_count").fetchone()[0]
    if free > 256:
        conn.execute("PRAGMA incremental_vacuum(512)").fetchall()
    conn.execute("PRAGMA optimize")
    conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchall()
    res["freelist_pages_before"] = free
    log(logger, logging.INFO, "cleanup done", **res)
    return res
