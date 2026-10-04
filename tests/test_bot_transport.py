"""Bot transport: endpoint, auth, nonce-based safe retries, crash recovery, secret hygiene."""

from __future__ import annotations

import dataclasses
import json

import httpx2 as httpx
import pytest

from newsrelay import timeutil
from newsrelay.publishing import outbox, worker
from newsrelay.publishing.discord import BotTransport, WebhookTransport, validate_bot_token
from test_outbox_worker import HOOK, Recorder, drain, ok, queue_one, states

TOKEN = "MTAwMDAwMDAwMDAwMDAwMDAwMA.GaBcDe.abcdefghijklmnopqrstuvwxyz0123456789ABCD"
CHANNEL = "123456789012345678"


def bot(handler) -> BotTransport:
    return BotTransport(TOKEN, CHANNEL, client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_token_and_channel_validation():
    assert validate_bot_token(TOKEN)
    assert not validate_bot_token("short.a.b")
    assert not validate_bot_token("no-dots-" + "x" * 60)
    with pytest.raises(ValueError):
        BotTransport("bad", CHANNEL)
    with pytest.raises(ValueError):
        BotTransport(TOKEN, "../../users/@me")
    assert TOKEN not in repr(bot(ok))


def test_request_shape(cfg, conn, clock):
    rec = Recorder([ok])
    queue_one(conn, cfg)
    drain(worker.Worker(cfg, conn, bot(rec)), clock)
    req = rec.requests[0]
    assert req.method == "POST"
    assert str(req.url) == f"https://discord.com/api/v10/channels/{CHANNEL}/messages"
    assert req.headers["Authorization"] == f"Bot {TOKEN}"
    body = json.loads(req.content)
    assert "username" not in body  # bots cannot override their name
    assert body["allowed_mentions"] == {"parse": []}
    assert body["flags"] == 4
    assert body["enforce_nonce"] is True
    key = conn.execute("SELECT idempotency_key FROM outbox").fetchone()[0]
    assert body["nonce"] == key[:25]
    assert states(conn) == ["delivered"]


def test_ambiguous_send_is_retried_with_same_nonce(cfg, conn, clock):
    def timeout(req):
        raise httpx.ReadTimeout("lost", request=req)

    rec = Recorder([timeout, timeout, ok])
    queue_one(conn, cfg)
    drain(worker.Worker(cfg, conn, bot(rec)), clock)
    assert states(conn) == ["delivered"]
    nonces = {json.loads(r.content)["nonce"] for r in rec.requests}
    assert len(rec.requests) == 3 and len(nonces) == 1  # Discord de-duplicates by this nonce


def test_ambiguous_retries_are_bounded(cfg, conn, clock):
    def timeout(req):
        raise httpx.ReadTimeout("lost", request=req)

    rec = Recorder([timeout])
    queue_one(conn, cfg)
    drain(worker.Worker(cfg, conn, bot(rec)), clock)
    assert states(conn) == ["uncertain"]
    assert len(rec.requests) == outbox.MAX_NONCE_RETRIES + 1


def test_webhook_still_never_retries_ambiguous(cfg, conn, clock):
    def timeout(req):
        raise httpx.ReadTimeout("lost", request=req)

    rec = Recorder([timeout, ok])
    queue_one(conn, cfg)
    t = WebhookTransport(HOOK, client=httpx.Client(transport=httpx.MockTransport(rec)))
    drain(worker.Worker(cfg, conn, t), clock)
    assert states(conn) == ["uncertain"] and len(rec.requests) == 1


def test_crash_recovery_uses_nonce_window(cfg, conn, clock):
    queue_one(conn, cfg)
    oid = conn.execute("SELECT id FROM outbox").fetchone()[0]
    outbox.claim(conn, oid)
    assert outbox.recover_after_crash(conn, BotTransport.dedup_window_s) == 1
    assert states(conn) == ["pending"]  # restarted quickly: safe to resend with the same nonce
    outbox.claim(conn, oid)
    clock.advance(minutes=10)
    outbox.recover_after_crash(conn, BotTransport.dedup_window_s)
    assert states(conn) == ["uncertain"]  # outside the window: operator decides


def test_missing_permission_keeps_item_queued(cfg, conn, clock):
    rec = Recorder([lambda r: httpx.Response(403, json={"code": 50013, "message": "Missing Permissions"})])
    queue_one(conn, cfg)
    worker.Worker(cfg, conn, bot(rec)).run_once()
    assert states(conn) == ["pending"]
    status = conn.execute("SELECT value FROM meta WHERE key='webhook_status'").fetchone()[0]
    assert "invalid" in status


def test_make_transport_prefers_bot(cfg, clock):
    (cfg.secret_dir_override / "discord_webhook").write_text(HOOK)
    assert isinstance(worker.make_transport(cfg), WebhookTransport)
    (cfg.secret_dir_override / "discord_bot_token").write_text(TOKEN)
    assert isinstance(worker.make_transport(cfg), WebhookTransport)  # channel id missing -> fallback
    with_channel = dataclasses.replace(cfg, discord_channel_id=CHANNEL)
    t = worker.make_transport(with_channel)
    assert isinstance(t, BotTransport)
    assert worker.transport_mode(t) == "bot"


def test_bot_token_never_logged(cfg, conn, clock, caplog):
    def boom(req):
        raise httpx.ConnectError(f"cannot reach {req.url} with {req.headers['Authorization']}", request=req)

    queue_one(conn, cfg)
    worker.Worker(cfg, conn, bot(Recorder([boom]))).run_once()
    err = conn.execute("SELECT last_error FROM outbox").fetchone()[0]
    assert TOKEN not in err and TOKEN not in caplog.text
    assert timeutil.now_iso()
