"""Durable outbox state machine.

    pending --claim--> dispatching --(2xx)----------------> delivered
                                    --(unsent/429/5xx/401/403/404)--> pending (backoff, FIFO head waits)
                                    --(429/5xx retry budget used up)--> failed
                                    --(other 4xx)----------> failed
                                    --(ambiguous)----------> uncertain   (never auto-resent)
    dispatching found at startup -> uncertain (crash/power loss mid-send)
    failed | uncertain --operator--> pending (requeue/resend) | delivered | cancelled

Nothing is ever dropped by age: 'failed' and 'uncertain' keep their payload until an operator
resolves them, and retention only prunes payloads of delivered/cancelled items.

No DB transaction is held while network I/O happens: claim() commits, the caller sends, finish()
commits the result in a second transaction.
"""

from __future__ import annotations

import logging
import random
import sqlite3
from dataclasses import dataclass
from typing import Any

from .. import timeutil
from ..config import Config
from ..database import tx
from ..logutil import log
from .discord import Outcome, SendResult

logger = logging.getLogger("newsrelay.outbox")


@dataclass(frozen=True)
class Claimed:
    id: int
    attempt_id: int
    attempt_no: int
    payload: str | None
    story_id: str | None
    idempotency_key: str = ""


def backoff_seconds(attempt: int, base: float = 30.0, cap: float = 3600.0) -> float:
    delay = min(cap, base * (2 ** max(0, attempt - 1)))
    return float(delay * random.uniform(0.8, 1.2))


MAX_NONCE_RETRIES = 4
NONCE_RETRY_DELAY_S = 15.0


def _ambiguous_streak(conn: sqlite3.Connection, outbox_id: int, current_attempt: int) -> int:
    """Number of most recent *earlier* attempts of this item that ended ambiguously."""
    n = 0
    for (status,) in conn.execute(
        "SELECT status FROM delivery_attempts WHERE outbox_id = ? AND id != ? ORDER BY id DESC LIMIT 10",
        (outbox_id, current_attempt),
    ):
        if status not in ("uncertain", "interrupted"):
            break
        n += 1
    return n


def recover_after_crash(conn: sqlite3.Connection, dedup_window_s: float = 0.0) -> int:
    """Items left in 'dispatching' may or may not have reached Discord.

    Without server-side de-duplication they become 'uncertain' (never auto-resent). With the bot
    transport (nonce + enforce_nonce) an item whose interrupted attempt is still inside the
    de-duplication window is safely re-queued instead.
    """
    t = timeutil.now_iso()
    safe_after = timeutil.iso_plus(-(dedup_window_s - 60.0)) if dedup_window_s > 60 else None
    with tx(conn):
        conn.execute(
            "UPDATE delivery_attempts SET finished_at = ?, status = 'interrupted' WHERE finished_at IS NULL",
            (t,),
        )
        rows = conn.execute(
            "SELECT id, story_id, updated_at FROM outbox WHERE state = 'dispatching'"
        ).fetchall()
        for row in rows:
            if safe_after and row["updated_at"] >= safe_after:
                conn.execute(
                    "UPDATE outbox SET state = 'pending', updated_at = ?, next_attempt_at = ?, "
                    "last_error = 'interrupted; retrying with same nonce' WHERE id = ?",
                    (t, t, row["id"]),
                )
            else:
                conn.execute(
                    "UPDATE outbox SET state = 'uncertain', updated_at = ?, "
                    "last_error = 'worker stopped while request was in flight' WHERE id = ?",
                    (t, row["id"]),
                )
            _refresh_story(conn, row["story_id"])
    for row in rows:
        log(logger, logging.WARNING, "outbox item marked uncertain after restart", outbox_id=row["id"])
    return len(rows)


def next_due(conn: sqlite3.Connection) -> tuple[sqlite3.Row | None, str | None]:
    """Return (due item, None) or (None, next wake time). Strict FIFO preserves message order."""
    head = conn.execute(
        "SELECT id, next_attempt_at FROM outbox WHERE state = 'pending' ORDER BY id LIMIT 1"
    ).fetchone()
    if head is None:
        return None, None
    if head["next_attempt_at"] > timeutil.now_iso():
        return None, head["next_attempt_at"]
    return head, None


def claim(conn: sqlite3.Connection, outbox_id: int) -> Claimed | None:
    t = timeutil.now_iso()
    with tx(conn):
        cur = conn.execute(
            "UPDATE outbox SET state = 'dispatching', attempts = attempts + 1, updated_at = ? "
            "WHERE id = ? AND state = 'pending'",
            (t, outbox_id),
        )
        if cur.rowcount != 1:
            return None  # someone else claimed it, or state changed
        row = conn.execute(
            "SELECT attempts, payload, story_id, idempotency_key FROM outbox WHERE id = ?", (outbox_id,)
        ).fetchone()
        aid = conn.execute(
            "INSERT INTO delivery_attempts(outbox_id, attempt_no, started_at, status) "
            "VALUES (?, ?, ?, 'started')",
            (outbox_id, row["attempts"], t),
        ).lastrowid
    assert aid is not None
    return Claimed(outbox_id, aid, row["attempts"], row["payload"], row["story_id"], row["idempotency_key"])


def finish(
    conn: sqlite3.Connection, item: Claimed, result: SendResult, cfg: Config, dedup_window_s: float = 0.0
) -> str:
    """Persist the send result. Returns the new outbox state."""
    t = timeutil.now_iso()
    retry_at: str | None = None
    o = result.outcome
    if o is Outcome.DELIVERED:
        state = "delivered"
    elif o is Outcome.UNCERTAIN:
        # Re-sending the same nonce well inside Discord's de-duplication window cannot double-post.
        if (
            dedup_window_s > 4 * NONCE_RETRY_DELAY_S
            and _ambiguous_streak(conn, item.id, item.attempt_id) < MAX_NONCE_RETRIES
        ):
            state = "pending"
            retry_at = timeutil.iso_plus(NONCE_RETRY_DELAY_S)
        else:
            state = "uncertain"
    elif o is Outcome.PERMANENT:
        state = "failed"
    elif o is Outcome.RATE_LIMITED:
        if item.attempt_no >= cfg.max_429_attempts:
            state = "failed"
        else:
            state = "pending"
            retry_at = timeutil.iso_plus((result.retry_after or 5.0) + 0.5)
    elif o is Outcome.SERVER_ERROR:
        if item.attempt_no >= cfg.max_5xx_attempts:
            state = "failed"
        else:
            state = "pending"
            retry_at = timeutil.iso_plus(backoff_seconds(item.attempt_no))
    elif o is Outcome.AUTH_FAILED:
        # Webhook missing/revoked: keep the item (no data loss) and back off up to 6h until the
        # webhook is replaced or the item expires.
        state = "pending"
        retry_at = timeutil.iso_plus(backoff_seconds(item.attempt_no, base=900, cap=6 * 3600))
    else:  # UNSENT: known not transmitted
        state = "pending"
        retry_at = timeutil.iso_plus(backoff_seconds(item.attempt_no, base=20, cap=1800))

    with tx(conn):
        conn.execute(
            "UPDATE delivery_attempts SET finished_at = ?, status = ?, http_status = ?, error = ? WHERE id = ?",
            (t, o.value, result.http_status, result.error, item.attempt_id),
        )
        conn.execute(
            "UPDATE outbox SET state = ?, updated_at = ?, next_attempt_at = COALESCE(?, next_attempt_at), "
            "last_error = ?, discord_message_id = COALESCE(?, discord_message_id), "
            "delivered_at = CASE WHEN ? = 'delivered' THEN ? ELSE delivered_at END "
            "WHERE id = ? AND state = 'dispatching'",
            (state, t, retry_at, result.error, result.message_id, state, t, item.id),
        )
        if o is Outcome.AUTH_FAILED:
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('webhook_status', ?)",
                (f"invalid since {t} (HTTP {result.http_status})",),
            )
        elif o is Outcome.DELIVERED:
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('webhook_status', 'ok')")
        _refresh_story(conn, item.story_id)
    log(
        logger,
        logging.INFO if state in ("delivered", "pending") else logging.WARNING,
        "outbox transition",
        outbox_id=item.id,
        attempt=item.attempt_no,
        outcome=o.value,
        state=state,
        http_status=result.http_status,
    )
    return state


def _refresh_story(conn: sqlite3.Connection, story_id: str | None) -> None:
    """Recompute a story's publication_state from its outbox items (call inside a transaction)."""
    if not story_id:
        return
    states = [r[0] for r in conn.execute("SELECT state FROM outbox WHERE story_id = ?", (story_id,))]
    if not states:
        return
    if all(s == "delivered" for s in states):
        new = "delivered"
    elif "uncertain" in states:
        new = "uncertain"
    elif "failed" in states or "cancelled" in states:
        new = "partial" if "delivered" in states else "failed"
    else:
        new = "queued"
    posted = None
    if new == "delivered":
        posted = conn.execute(
            "SELECT MIN(delivered_at) FROM outbox WHERE story_id = ?", (story_id,)
        ).fetchone()[0]
    conn.execute(
        "UPDATE stories SET publication_state = ?, posted_at = COALESCE(?, posted_at) WHERE id = ?",
        (new, posted, story_id),
    )
    if posted:
        conn.execute(
            "UPDATE topics SET last_posted = MAX(COALESCE(last_posted, ''), ?) "
            "WHERE id = (SELECT topic_id FROM stories WHERE id = ?)",
            (posted, story_id),
        )


def resolve(conn: sqlite3.Connection, outbox_id: int, action: str) -> str:
    """Operator decision for an uncertain/failed item.

    delivered: it did appear in Discord · resend/requeue: send again · cancel: give up (kept for audit).
    """
    if action not in ("delivered", "resend", "requeue", "cancel"):
        raise ValueError("action must be delivered|resend|requeue|cancel")
    new = {"delivered": "delivered", "resend": "pending", "requeue": "pending", "cancel": "cancelled"}[action]
    t = timeutil.now_iso()
    with tx(conn):
        row = conn.execute(
            "SELECT story_id, payload FROM outbox WHERE id = ? AND state IN ('uncertain','failed')",
            (outbox_id,),
        ).fetchone()
        if row is None:
            raise ValueError("item not found or not in uncertain/failed state")
        if new == "pending" and row["payload"] is None:
            raise ValueError("payload no longer available; cannot resend")
        conn.execute(
            "UPDATE outbox SET state = ?, updated_at = ?, next_attempt_at = ?, attempts = "
            "CASE WHEN ? = 'pending' THEN 0 ELSE attempts END, "
            "delivered_at = CASE WHEN ? = 'delivered' THEN ? ELSE delivered_at END WHERE id = ?",
            (new, t, t, new, new, t, outbox_id),
        )
        _refresh_story(conn, row["story_id"])
    log(logger, logging.INFO, "operator resolved outbox item", outbox_id=outbox_id, action=action)
    return new


# Backwards-compatible name used by older call sites/tests.
resolve_uncertain = resolve


def requeue_failed(conn: sqlite3.Connection) -> int:
    """Put every 'failed' item back into the queue (e.g. after replacing a broken webhook)."""
    ids = [r[0] for r in conn.execute("SELECT id FROM outbox WHERE state = 'failed' ORDER BY id")]
    for oid in ids:
        resolve(conn, oid, "requeue")
    return len(ids)


def backlog(conn: sqlite3.Connection) -> dict[str, Any]:
    """Compact delivery-health view (used by status, health and begin_run)."""
    counts = summarize(conn)
    oldest = conn.execute("SELECT MIN(created_at) FROM outbox WHERE state = 'pending'").fetchone()[0]
    last = conn.execute("SELECT MAX(delivered_at) FROM outbox WHERE state = 'delivered'").fetchone()[0]
    return {
        "pending": counts.get("pending", 0) + counts.get("dispatching", 0),
        "failed": counts.get("failed", 0),
        "uncertain": counts.get("uncertain", 0),
        "oldest_pending": oldest,
        "last_delivery": last,
    }


def summarize(conn: sqlite3.Connection) -> dict[str, Any]:
    return {r["state"]: r["n"] for r in conn.execute("SELECT state, count(*) n FROM outbox GROUP BY state")}
