import json
import threading
from collections.abc import Callable

import httpx2 as httpx
import pytest

from conftest import publish_input, story
from newsrelay import service, timeutil
from newsrelay.publishing import outbox
from newsrelay.publishing.discord import Outcome, WebhookTransport, validate_webhook_url
from newsrelay.publishing.worker import SingleInstanceError, Worker, acquire_lock
from newsrelay.schemas import StatusInput

HOOK = "https://discord.com/api/webhooks/123456789012345678/" + "T" * 68


def mock_transport(handler: Callable[[httpx.Request], httpx.Response]) -> WebhookTransport:
    return WebhookTransport(HOOK, client=httpx.Client(transport=httpx.MockTransport(handler)))


class Recorder:
    def __init__(self, responses: list[Callable[[httpx.Request], httpx.Response]]):
        self.responses = responses
        self.requests: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        fn = self.responses[min(len(self.requests) - 1, len(self.responses) - 1)]
        return fn(req)


def ok(req: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"id": str(900000000000000000 + len(req.content))})


def queue_one(conn, cfg, key="daily-news/2026-10-03", **kw):
    service.publish_digest(conn, cfg, publish_input(key, [story(**kw)]))


def drain(w: Worker, clock, max_steps=50) -> list[str]:
    out = []
    for _ in range(max_steps):
        r = w.run_once()
        out.append(r)
        if r == "idle":
            due, wake = outbox.next_due(w.conn)
            if wake is None:
                break
            clock.t = timeutil.parse_iso(wake)
    return out


def states(conn):
    return [r[0] for r in conn.execute("SELECT state FROM outbox ORDER BY id")]


def test_webhook_url_validation():
    assert validate_webhook_url(HOOK)
    assert not validate_webhook_url("https://evil.example.com/api/webhooks/1/abc")
    assert not validate_webhook_url("http://discord.com/api/webhooks/123456789012345678/" + "T" * 68)
    with pytest.raises(ValueError):
        WebhookTransport("https://example.com/x")
    assert "T" * 20 not in repr(WebhookTransport(HOOK))


def test_delivery_success_uses_wait_true_and_stores_message_id(cfg, conn, clock):
    rec = Recorder([ok])
    queue_one(conn, cfg)
    drain(Worker(cfg, conn, mock_transport(rec)), clock)
    assert states(conn) == ["delivered"]
    req = rec.requests[0]
    assert req.url.params["wait"] == "true"
    body = json.loads(req.content)
    assert body["allowed_mentions"] == {"parse": []}
    assert conn.execute("SELECT discord_message_id FROM outbox").fetchone()[0]
    st = service.publish_status(conn, StatusInput(run_key="daily-news/2026-10-03"))
    assert st["publication"] == "delivered"
    assert conn.execute("SELECT last_posted FROM topics").fetchone()[0]


def test_i_429_respects_retry_after(cfg, conn, clock):  # TEST I
    rec = Recorder([lambda r: httpx.Response(429, json={"retry_after": 7.5, "global": False}), ok])
    queue_one(conn, cfg)
    w = Worker(cfg, conn, mock_transport(rec))
    assert w.run_once() == "sent:pending"
    nxt = conn.execute("SELECT next_attempt_at FROM outbox").fetchone()[0]
    delta = (timeutil.parse_iso(nxt) - clock.t).total_seconds()
    assert 7.5 <= delta <= 9
    assert w.run_once() == "idle"  # not due yet: no hammering
    assert len(rec.requests) == 1
    clock.advance(seconds=9)
    assert w.run_once() == "sent:delivered"


def test_http_500_bounded_retry(cfg, conn, clock):
    rec = Recorder([lambda r: httpx.Response(500)])
    queue_one(conn, cfg)
    drain(Worker(cfg, conn, mock_transport(rec)), clock)
    assert states(conn) == ["failed"]
    assert len(rec.requests) == cfg.max_5xx_attempts


def test_permanent_4xx_not_retried(cfg, conn, clock):
    rec = Recorder([lambda r: httpx.Response(400, json={"code": 50035})])
    queue_one(conn, cfg)
    drain(Worker(cfg, conn, mock_transport(rec)), clock)
    assert states(conn) == ["failed"]
    assert len(rec.requests) == 1


def test_j_temporary_outage_does_not_lose_data(cfg, conn, clock):  # TEST J
    def down(req):
        raise httpx.ConnectError("Temporary failure in name resolution", request=req)

    rec = Recorder([down, down, down, lambda r: httpx.Response(503), ok])
    queue_one(conn, cfg)
    drain(Worker(cfg, conn, mock_transport(rec)), clock)
    assert states(conn) == ["delivered"]
    assert len(rec.requests) == 5
    assert conn.execute("SELECT count(*) FROM delivery_attempts").fetchone()[0] == 5


def test_revoked_webhook_keeps_items(cfg, conn, clock):
    rec = Recorder([lambda r: httpx.Response(404, json={"code": 10015})])
    queue_one(conn, cfg)
    w = Worker(cfg, conn, mock_transport(rec))
    w.run_once()
    assert states(conn) == ["pending"]
    assert "invalid" in conn.execute("SELECT value FROM meta WHERE key='webhook_status'").fetchone()[0]
    # Weeks later the item is still queued (never dropped by age) and nothing hammered Discord.
    clock.advance(days=30)
    drain(w, clock, max_steps=5)
    assert states(conn) == ["pending"]
    assert len(rec.requests) <= 6
    # Webhook replaced -> the very same item is delivered.
    w.transport = mock_transport(Recorder([ok]))
    clock.advance(hours=7)
    drain(w, clock)
    assert states(conn) == ["delivered"]


@pytest.mark.parametrize("exc", [httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.ReadError])
def test_l_ambiguous_delivery_not_blindly_retried(cfg, conn, clock, exc):  # TEST L
    def hang(req):
        raise exc("response lost", request=req)

    rec = Recorder([hang, ok])
    queue_one(conn, cfg)
    w = Worker(cfg, conn, mock_transport(rec))
    drain(w, clock)
    assert states(conn) == ["uncertain"]
    assert len(rec.requests) == 1
    clock.advance(days=3)
    drain(w, clock)
    assert len(rec.requests) == 1  # never auto-resent
    st = service.publish_status(conn, StatusInput(run_key="daily-news/2026-10-03"))
    assert st["publication"] == "uncertain"
    # explicit operator decision
    oid = conn.execute("SELECT id FROM outbox").fetchone()[0]
    outbox.resolve_uncertain(conn, oid, "resend")
    drain(w, clock)
    assert states(conn) == ["delivered"]


def test_k_restart_does_not_double_send(cfg, conn, clock):  # TEST K
    rec = Recorder([ok])
    queue_one(conn, cfg)
    queue_one(
        conn,
        cfg,
        key="daily-news/2026-10-04",
        candidate_id="c2",
        headline="Second distinct story here",
        key_facts=["A completely different fact about something else"],
        sources=[{"url": "https://orf.at/stories/1"}],
    )
    w1 = Worker(cfg, conn, mock_transport(rec))
    w1.run_once()
    # "restart": fresh worker objects, recovery pass, many times
    for _ in range(5):
        outbox.recover_after_crash(conn)
        w = Worker(cfg, conn, mock_transport(rec))
        drain(w, clock)
    assert states(conn) == ["delivered", "delivered"]
    assert len(rec.requests) == 2


def test_crash_mid_dispatch_becomes_uncertain(cfg, conn, clock):
    queue_one(conn, cfg)
    oid = conn.execute("SELECT id FROM outbox").fetchone()[0]
    assert outbox.claim(conn, oid) is not None  # process "dies" after claim, before result
    assert outbox.recover_after_crash(conn) == 1
    assert states(conn) == ["uncertain"]
    rec = Recorder([ok])
    drain(Worker(cfg, conn, mock_transport(rec)), clock)
    assert rec.requests == []


def test_claim_is_exclusive(cfg, conn):
    queue_one(conn, cfg)
    oid = conn.execute("SELECT id FROM outbox").fetchone()[0]
    assert outbox.claim(conn, oid) is not None
    assert outbox.claim(conn, oid) is None


def test_single_instance_lock(cfg):
    lock = acquire_lock(cfg.runtime_dir)
    with pytest.raises(SingleInstanceError):
        acquire_lock(cfg.runtime_dir)
    lock.close()
    acquire_lock(cfg.runtime_dir).close()


def test_ordering_preserved_across_retries(cfg, conn, clock):
    sent = []

    def handler(req):
        sent.append(json.loads(req.content)["content"][:40])
        if len(sent) == 1:
            return httpx.Response(429, json={"retry_after": 2})
        return ok(req)

    long_body = "\n\n".join(("Detail sentence about the event. " * 15) for _ in range(8))[:3500]
    queue_one(conn, cfg, body=long_body)
    queue_one(
        conn,
        cfg,
        key="daily-news/2026-10-04",
        candidate_id="c2",
        headline="Second distinct story here",
        key_facts=["A completely different fact about something else"],
        sources=[{"url": "https://orf.at/stories/1"}],
    )
    drain(Worker(cfg, conn, mock_transport(handler)), clock)
    contents = [json.loads(r[0])["content"] for r in conn.execute("SELECT payload FROM outbox ORDER BY id")]
    assert [c[:40] for c in contents] == sent[1:]
    assert all(s == "delivered" for s in states(conn))


def test_no_webhook_holds_queue(cfg, conn, clock):
    queue_one(conn, cfg)
    assert Worker(cfg, conn, None).run_once() == "blocked:no-webhook"
    assert states(conn) == ["pending"]


def test_worker_stops_on_event(cfg, conn):
    stop = threading.Event()
    w = Worker(cfg, conn, mock_transport(Recorder([ok])), stop)
    t = threading.Thread(target=w.run_forever)
    t.start()
    stop.set()
    t.join(timeout=10)
    assert not t.is_alive()


def test_classify_outcomes():
    from newsrelay.publishing.discord import classify_response

    assert classify_response(httpx.Response(204)).outcome is Outcome.DELIVERED
    assert classify_response(httpx.Response(401)).outcome is Outcome.AUTH_FAILED
    r = classify_response(httpx.Response(429, headers={"Retry-After": "3"}))
    assert r.outcome is Outcome.RATE_LIMITED and r.retry_after == 3.0
    r = classify_response(
        httpx.Response(
            200, json={"id": "1"}, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "1.5"}
        )
    )
    assert r.bucket_wait == 1.5
