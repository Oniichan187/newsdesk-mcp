"""End-to-end: OAuth -> MCP tools -> SQLite -> outbox -> worker -> fake Discord (real HTTP)."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2 as httpx

from conftest import story
from fakes import CountingListener, FakeDiscord, free_port
from newsrelay import timeutil
from newsrelay.database import open_db
from newsrelay.publishing import outbox
from newsrelay.publishing.discord import WebhookTransport
from newsrelay.publishing.worker import Worker

PASS = "correct-horse-battery-staple-42"
REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect"
HOOK = "https://discord.com/api/webhooks/123456789012345678/" + "S3cr3tT0ken" * 6


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def oauth_login(base: str, resource: str) -> dict[str, Any]:
    with httpx.Client(base_url=base, follow_redirects=False) as c:
        reg = c.post(
            "/register",
            json={
                "redirect_uris": [REDIRECT],
                "client_name": "ChatGPT <script>x</script>",
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
            },
        )
        assert reg.status_code == 201, reg.text
        client_id = reg.json()["client_id"]
        verifier, challenge = _pkce()
        r = c.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": REDIRECT,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "st4te",
                "scope": "newsrelay offline_access",
                "resource": resource,
            },
        )
        assert r.status_code == 302, r.text
        consent_url = r.headers["location"]
        req_id = parse_qs(urlsplit(consent_url).query)["req"][0]
        page = c.get(f"/oauth/consent?req={req_id}")
        assert page.status_code == 200
        assert "<script>x</script>" not in page.text and "&lt;script&gt;" in page.text
        assert page.headers["x-frame-options"] == "DENY"
        bad = c.post("/oauth/consent", data={"req": req_id, "passphrase": "wrong"})
        assert bad.status_code == 401
        good = c.post("/oauth/consent", data={"req": req_id, "passphrase": PASS})
        assert good.status_code == 302
        loc = good.headers["location"]
        assert loc.startswith(REDIRECT)
        q = parse_qs(urlsplit(loc).query)
        assert q["state"] == ["st4te"]
        tok = c.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": q["code"][0],
                "redirect_uri": REDIRECT,
                "client_id": client_id,
                "code_verifier": verifier,
                "resource": resource,
            },
        )
        assert tok.status_code == 200, tok.text
        # code is single-use
        again = c.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": q["code"][0],
                "redirect_uri": REDIRECT,
                "client_id": client_id,
                "code_verifier": verifier,
                "resource": resource,
            },
        )
        assert again.status_code == 400
        out = tok.json()
        out["client_id"] = client_id
        return out


class Mcp:
    def __init__(self, base: str, token: str | None) -> None:
        self.c = httpx.Client(base_url=base, timeout=30)
        self.headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"
        self.n = 0

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> httpx.Response:
        self.n += 1
        return self.c.post(
            "/mcp",
            headers=self.headers,
            json={"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}},
        )

    def init(self) -> None:
        r = self.rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        )
        assert r.status_code == 200, r.text
        self.headers["MCP-Protocol-Version"] = r.json()["result"]["protocolVersion"]

    def call(self, name: str, args: dict[str, Any]) -> tuple[bool, Any]:
        r = self.rpc("tools/call", {"name": name, "arguments": args})
        assert r.status_code == 200, r.text
        res = r.json()["result"]
        text = res["content"][0]["text"]
        if res.get("isError"):
            return False, text
        return True, json.loads(text)


def test_s_unauthenticated_caller_cannot_publish(live):  # TEST S
    cfg, srv, conn = live
    for token in (None, "not-a-real-token"):
        m = Mcp(srv.base, token)
        r = m.rpc("tools/call", {"name": "newsrelay_publish_digest", "arguments": {}})
        assert r.status_code == 401
        assert "resource_metadata" in r.headers.get("www-authenticate", "")
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


def test_metadata_endpoints(live):
    cfg, srv, _ = live
    with httpx.Client(base_url=srv.base) as c:
        prm = c.get("/.well-known/oauth-protected-resource/mcp")
        assert prm.status_code == 200
        assert prm.json()["resource"].rstrip("/") == cfg.mcp_resource_url
        asm = c.get("/.well-known/oauth-authorization-server").json()
        assert "S256" in asm["code_challenge_methods_supported"]
        assert asm["registration_endpoint"].endswith("/register")
        assert c.get("/healthz").json() == {"ok": True}


def test_registration_rejects_foreign_redirect(live):
    _, srv, _ = live
    with httpx.Client(base_url=srv.base) as c:
        r = c.post(
            "/register",
            json={"redirect_uris": ["https://evil.example.com/cb"], "token_endpoint_auth_method": "none"},
        )
        assert r.status_code == 400


def test_full_flow_oauth_mcp_outbox_fake_discord(live, caplog):
    cfg, srv, conn = live
    caplog.set_level(logging.INFO)
    tokens = oauth_login(srv.base, cfg.mcp_resource_url)
    m = Mcp(srv.base, tokens["access_token"])
    m.init()

    tools = {t["name"]: t for t in m.rpc("tools/list").json()["result"]["tools"]}
    assert set(tools) == {
        "newsrelay_begin_run",
        "newsrelay_match_candidates",
        "newsrelay_publish_digest",
        "newsrelay_complete_noop",
        "newsrelay_publish_status",
        "newsrelay_health",
    }
    ro = {n for n, t in tools.items() if t.get("annotations", {}).get("readOnlyHint")}
    assert ro == {
        "newsrelay_begin_run",
        "newsrelay_match_candidates",
        "newsrelay_publish_status",
        "newsrelay_health",
    }

    run_key = "daily-news/2026-10-03"
    ok, begin = m.call("newsrelay_begin_run", {"run_key": run_key})
    assert ok and begin["status"] == "open" and begin["research_from"]

    canary = CountingListener()
    cand = {
        "candidate_id": "c1",
        "title": "Test event for integration",
        "category": "computing",
        "entities": ["Integration"],
        "key_facts": ["Integration tests exercise the whole pipeline"],
        "source_urls": [f"https://127.0.0.1:{canary.port}/article"],
    }
    ok, matched = m.call("newsrelay_match_candidates", {"run_key": run_key, "candidates": [cand]})
    assert ok and matched["results"][0]["classification"] == "NO_MATCH"

    ok, err = m.call(
        "newsrelay_match_candidates",
        {"run_key": run_key, "candidates": [{**cand, "source_urls": ["http://insecure.example.com"]}]},
    )
    assert not ok and "https" in err

    body = ("Paragraph about the event @everyone. " * 25 + "\n\n") * 3
    st = story(
        "c1",
        headline="Integration event happened",
        body=body[:3400],
        key_facts=["Integration tests exercise the whole pipeline"],
        sources=[{"url": f"https://127.0.0.1:{canary.port}/article", "name": "Canary"}],
    )
    args = {"run_key": run_key, "research_through": timeutil.now_iso(), "stories": [st]}
    ok, pub = m.call("newsrelay_publish_digest", args)
    assert ok, pub
    assert pub["publication"] == "queued"
    ok, pub2 = m.call("newsrelay_publish_digest", args)
    assert ok and pub2["idempotent_replay"] and pub2["publication_id"] == pub["publication_id"]

    # Worker delivers to the fake Discord over real HTTP
    fake = FakeDiscord()
    transport = WebhookTransport(HOOK, client=fake.client())
    w = Worker(cfg, open_db(cfg.db_path), transport)
    while outbox.next_due(w.conn)[0] is not None:
        w.run_once()
    assert len(fake.received) >= 2  # long story split
    assert all(p["allowed_mentions"] == {"parse": []} for p in fake.received)
    assert all(len(p["content"]) <= cfg.max_message_chars for p in fake.received)
    assert all("wait=true" in p for p in fake.paths)

    ok, status = m.call("newsrelay_publish_status", {"run_key": run_key})
    assert ok and status["publication"] == "delivered"
    ok, health = m.call("newsrelay_health", {})
    assert ok

    # TEST T: nothing ever connected to the caller-supplied URL
    assert canary.accepted == 0
    canary.close()

    # TEST R: secret never appears in responses or logs
    token_part = HOOK.rsplit("/", 1)[1]
    for blob in (
        json.dumps(begin),
        json.dumps(matched),
        json.dumps(pub),
        json.dumps(status),
        json.dumps(health),
        caplog.text,
    ):
        assert token_part not in blob
        assert HOOK not in blob

    # refresh token rotation
    with httpx.Client(base_url=srv.base) as c:
        r = c.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": tokens["client_id"],
            },
        )
        assert r.status_code == 200, r.text
        new = r.json()
        r2 = c.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": tokens["client_id"],
            },
        )
        assert r2.status_code == 400  # old refresh token revoked
    old = Mcp(srv.base, tokens["access_token"])
    assert old.rpc("tools/list").status_code == 401
    fresh = Mcp(srv.base, new["access_token"])
    fresh.init()
    assert fresh.rpc("tools/list").status_code == 200
    fake.close()


def test_noop_via_mcp_and_oversized_request(live):
    cfg, srv, conn = live
    tokens = oauth_login(srv.base, cfg.mcp_resource_url)
    m = Mcp(srv.base, tokens["access_token"])
    m.init()
    ok, res = m.call(
        "newsrelay_complete_noop",
        {"run_key": "daily-news/2026-10-02", "research_through": timeutil.now_iso()},
    )
    assert ok and res["status"] == "completed_noop"
    huge = {
        "jsonrpc": "2.0",
        "id": 9,
        "method": "tools/call",
        "params": {"name": "newsrelay_health", "arguments": {}, "pad": "x" * 400_000},
    }
    r = m.c.post("/mcp", headers=m.headers, json=huge)
    assert r.status_code == 413


def test_unknown_tool_arguments_rejected(live):
    cfg, srv, _ = live
    tokens = oauth_login(srv.base, cfg.mcp_resource_url)
    m = Mcp(srv.base, tokens["access_token"])
    m.init()
    ok, err = m.call(
        "newsrelay_match_candidates",
        {
            "run_key": "daily-news/2026-10-03",
            "candidates": [
                {
                    "candidate_id": "c1",
                    "title": "t",
                    "category": "ai",
                    "key_facts": ["fact one"],
                    "source_urls": ["https://example.com/a"],
                    "webhook_url": "https://evil.example.com",
                }
            ],
        },
    )
    assert not ok


def test_consent_lockout(live):
    cfg, srv, _ = live
    with httpx.Client(base_url=srv.base, follow_redirects=False) as c:
        reg = c.post(
            "/register", json={"redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none"}
        ).json()
        _, challenge = _pkce()
        loc = c.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": reg["client_id"],
                "redirect_uri": REDIRECT,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "s",
            },
        ).headers["location"]
        req_id = parse_qs(urlsplit(loc).query)["req"][0]
        for _ in range(5):
            assert c.post("/oauth/consent", data={"req": req_id, "passphrase": "nope"}).status_code == 401
        r = c.post("/oauth/consent", data={"req": req_id, "passphrase": PASS})
        assert r.status_code == 429  # even the right passphrase is refused while locked


def test_ambiguous_timeout_against_real_server(cfg, conn, clock):
    from conftest import publish_input
    from newsrelay import service

    service.publish_digest(conn, cfg, publish_input("daily-news/2026-10-03", [story()]))
    fake = FakeDiscord()
    fake.mode = "slow"
    w = Worker(cfg, conn, WebhookTransport(HOOK, client=fake.client(read_timeout=0.5)))
    assert w.run_once() == "sent:uncertain"
    assert len(fake.received) == 1
    fake.mode = "ok"
    clock.advance(days=1)
    assert w.run_once() == "idle"
    assert len(fake.received) == 1
    fake.close()


def test_connection_refused_is_retryable(cfg, conn, clock):
    from conftest import publish_input
    from newsrelay import service

    service.publish_digest(conn, cfg, publish_input("daily-news/2026-10-03", [story()]))
    port = free_port()  # nothing listening

    class Refused(httpx.HTTPTransport):
        def handle_request(self, request):
            request.url = request.url.copy_with(scheme="http", host="127.0.0.1", port=port)
            return super().handle_request(request)

    w = Worker(cfg, conn, WebhookTransport(HOOK, client=httpx.Client(transport=Refused(), timeout=2)))
    assert w.run_once() == "sent:pending"
    assert conn.execute("SELECT state FROM outbox").fetchone()[0] == "pending"
