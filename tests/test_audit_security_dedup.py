"""Audit tests: HTTP edge behaviour, OAuth hardening, webhook validation, adversarial dedup, properties."""

from __future__ import annotations

import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx2 as httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from conftest import candidate, publish_input, story
from newsrelay import service
from newsrelay.api.auth import redirect_allowed
from newsrelay.dedup.matcher import EXACT_DUPLICATE, LIKELY_DUPLICATE, NO_MATCH, POSSIBLE_EXISTING_TOPIC
from newsrelay.dedup.normalize import canonical_url, normalize_text
from newsrelay.publishing.discord import validate_webhook_url
from newsrelay.publishing.splitter import split_message, ulen
from newsrelay.schemas import MatchInput
from test_api_integration import PASS, REDIRECT, Mcp, _pkce, oauth_login

ALLOWED = ("https://chatgpt.com/connector_platform_oauth_redirect", "https://chatgpt.com/connector/oauth/")

# ---------------------------------------------------------------- redirect URI validation


@pytest.mark.parametrize(
    ("uri", "ok"),
    [
        ("https://chatgpt.com/connector_platform_oauth_redirect", True),
        ("https://chatgpt.com/connector/oauth/abc_DEF-123", True),
        ("https://chatgpt.com/connector_platform_oauth_redirectX", False),
        ("https://chatgpt.com/connector_platform_oauth_redirect/../evil", False),
        ("https://chatgpt.com/connector/oauth/abc/def", False),
        ("https://chatgpt.com/connector/oauth/", False),
        ("https://chatgpt.com/connector/oauth/%2e%2e", False),
        ("https://chatgpt.com.evil.com/connector_platform_oauth_redirect", False),
        ("https://evil.com@chatgpt.com/connector_platform_oauth_redirect", False),
        ("https://chatgpt.com:8443/connector_platform_oauth_redirect", False),
        ("http://chatgpt.com/connector_platform_oauth_redirect", False),
        ("https://chatgpt.com/connector_platform_oauth_redirect?x=https://evil", False),
        ("https://chatgpt.com/connector_platform_oauth_redirect#frag", False),
        ("javascript:alert(1)", False),
        ("https://[::1/", False),
    ],
)
def test_redirect_allowed(uri, ok):
    assert redirect_allowed(uri, ALLOWED) is ok


# ---------------------------------------------------------------- webhook URL validation


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://discord.com/api/webhooks/123456789012345678/" + "a" * 68, True),
        ("https://discord.com/api/v10/webhooks/123456789012345678/" + "A_b-9" * 30, True),
        ("https://ptb.discord.com/api/webhooks/123456789012345678/" + "x" * 100, True),
        ("https://discordapp.com/api/webhooks/123456789012345678/" + "x" * 68, True),
        ("https://discord.com/api/webhooks/123456789012345678/" + "x" * 68 + "?wait=true", False),
        ("https://discord.com.evil.io/api/webhooks/123456789012345678/" + "x" * 68, False),
        ("https://discord.com:444/api/webhooks/123456789012345678/" + "x" * 68, False),
        ("https://u:p@discord.com/api/webhooks/123456789012345678/" + "x" * 68, False),
        ("http://discord.com/api/webhooks/123456789012345678/" + "x" * 68, False),
        ("https://discord.com/api/webhooks/abc/" + "x" * 68, False),
        ("https://discord.com/api/webhooks/123456789012345678/short", False),
        ("https://discord.com/api/webhooks/123456789012345678/" + "x" * 68 + "/github", False),
    ],
)
def test_webhook_url_structure(url, ok):
    assert validate_webhook_url(url) is ok


# ---------------------------------------------------------------- HTTP edge behaviour (live server)


def test_chunked_oversized_body_is_413_and_server_survives(live):
    cfg, srv, _ = live
    tokens = oauth_login(srv.base, cfg.mcp_resource_url)

    def gen():
        for _ in range(40):
            yield b"x" * 10_000

    with httpx.Client(base_url=srv.base, timeout=30) as c:
        for path, headers in (
            ("/register", {"Content-Type": "application/json"}),
            ("/token", {"Content-Type": "application/x-www-form-urlencoded"}),
            ("/oauth/consent", {"Content-Type": "application/x-www-form-urlencoded"}),
            (
                "/mcp",
                {
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                    "Authorization": f"Bearer {tokens['access_token']}",
                },
            ),
        ):
            r = c.post(path, content=gen(), headers=headers)
            assert r.status_code == 413, (path, r.status_code)
        assert c.get("/livez").status_code == 200
        assert c.get("/healthz").json() == {"ok": True}


def test_oversized_content_length_rejected_raw(live):
    import socket

    _, srv, _ = live
    port = int(srv.base.rsplit(":", 1)[1])
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        crlf = b"\r\n"
        req = crlf.join(
            [
                b"POST /token HTTP/1.1",
                b"Host: 127.0.0.1",
                b"Content-Length: 999999999",
                b"Content-Type: application/x-www-form-urlencoded",
                b"",
                b"abc",
            ]
        )
        sock.sendall(req)
        head = sock.recv(200)
    assert b" 413 " in head


def test_malformed_json_and_unknown_fields(live):
    cfg, srv, _ = live
    tokens = oauth_login(srv.base, cfg.mcp_resource_url)
    m = Mcp(srv.base, tokens["access_token"])
    m.init()
    r = m.c.post("/mcp", headers=m.headers, content=b"{not json")
    assert 400 <= r.status_code < 500 or "error" in r.json()
    r = m.rpc(
        "tools/call",
        {
            "name": "newsrelay_begin_run",
            "arguments": {"run_key": "daily-news/2026-10-03", "webhook_url": "https://evil"},
        },
    )
    body = r.json()
    assert "error" in body and "unknown argument" in body["error"]["message"]
    ok, _ = m.call("newsrelay_begin_run", {"run_key": "daily-news/2026-10-03"})
    assert ok
    # the strict-argument table matches the advertised tool schemas
    from newsrelay.api.app import TOOL_ARGS

    tools = m.rpc("tools/list").json()["result"]["tools"]
    assert {t["name"]: frozenset(t["inputSchema"].get("properties", {})) for t in tools} == TOOL_ARGS
    assert m.c.get("/livez").status_code == 200


def test_expired_token_rejected(live):
    cfg, srv, conn = live
    tokens = oauth_login(srv.base, cfg.mcp_resource_url)
    conn.execute("UPDATE oauth_tokens SET expires_at = ? WHERE kind = 'access'", (int(time.time()) - 5,))
    r = Mcp(srv.base, tokens["access_token"]).rpc("tools/list")
    assert r.status_code == 401


def test_token_for_other_resource_rejected(live):
    cfg, srv, conn = live
    tokens = oauth_login(srv.base, cfg.mcp_resource_url)
    conn.execute("UPDATE oauth_tokens SET resource = 'https://other.example/mcp'")
    assert Mcp(srv.base, tokens["access_token"]).rpc("tools/list").status_code == 401


def test_authorize_returns_iss_and_metadata_advertises_it(live):
    cfg, srv, _ = live
    with httpx.Client(base_url=srv.base, follow_redirects=False) as c:
        md = c.get("/.well-known/oauth-authorization-server").json()
        assert md["issuer"] == cfg.public_base_url
        assert md["authorization_response_iss_parameter_supported"] is True
        assert "none" in md["token_endpoint_auth_methods_supported"]
        assert "offline_access" in md["scopes_supported"]
        reg = c.post(
            "/register", json={"redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none"}
        ).json()
        _, ch = _pkce()
        loc = c.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": reg["client_id"],
                "redirect_uri": REDIRECT,
                "code_challenge": ch,
                "code_challenge_method": "S256",
                "state": "s",
            },
        ).headers["location"]
        req = parse_qs(urlsplit(loc).query)["req"][0]
        r = c.post("/oauth/consent", data={"req": req, "passphrase": PASS})
        q = parse_qs(urlsplit(r.headers["location"]).query)
        assert q["iss"] == [cfg.public_base_url]


def test_authorize_without_pkce_or_bad_redirect_refused(live):
    _, srv, _ = live
    with httpx.Client(base_url=srv.base, follow_redirects=False) as c:
        reg = c.post(
            "/register", json={"redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none"}
        ).json()
        r = c.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": reg["client_id"],
                "redirect_uri": REDIRECT,
                "state": "s",
            },
        )
        assert r.status_code in (302, 400)
        if r.status_code == 302:
            assert "error=" in r.headers["location"]
        r = c.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": reg["client_id"],
                "redirect_uri": "https://evil.example/cb",
                "code_challenge": "x" * 43,
                "code_challenge_method": "S256",
            },
        )
        assert r.status_code == 400
        assert "evil.example" not in r.headers.get("location", "")


def test_public_endpoints_expose_no_internals(live):
    _, srv, _ = live
    with httpx.Client(base_url=srv.base) as c:
        for path in ("/healthz", "/readyz", "/livez"):
            assert set(c.get(path).json()) == {"ok"}
        assert c.get("/").status_code == 404
        assert c.get("/etc/passwd").status_code == 404


# ---------------------------------------------------------------- adversarial dedup


_CFG: dict = {}


@pytest.fixture(autouse=True)
def _bind_cfg(cfg):
    _CFG["cfg"] = cfg


def _match(conn, cand):
    return service.match_candidates(
        conn, _CFG["cfg"], MatchInput(run_key="daily-news/2026-12-01", candidates=[cand])
    )["results"][0]


@pytest.fixture
def seeded(cfg, conn, clock):
    service.publish_digest(conn, cfg, publish_input("daily-news/2026-10-03", [story()]))
    return conn


def test_shared_entity_unrelated_event_not_duplicate(seeded):
    c = candidate(
        "x1",
        title="EU Commission proposes new rules for drinking water quality",
        category="law-regulation",
        entities=["European Commission"],
        key_facts=[
            "Commission proposed stricter limits for PFAS in drinking water",
            "Member states would have until 2030 to comply",
        ],
        source_urls=["https://www.derstandard.at/story/9/wasser"],
    )
    r = _match(seeded, c)
    assert r["classification"] not in (EXACT_DUPLICATE, LIKELY_DUPLICATE)


def test_same_company_unrelated_announcement_not_duplicate(seeded):
    c = candidate(
        "x2",
        title="Meta releases new open-weight language model",
        category="ai",
        entities=["Meta"],
        key_facts=[
            "Meta released a new open-weight language model",
            "The model is available under a community licence",
        ],
        source_urls=["https://www.heise.de/news/meta-model.html"],
    )
    assert _match(seeded, c)["classification"] in (NO_MATCH, POSSIBLE_EXISTING_TOPIC)
    assert _match(seeded, c)["classification"] != LIKELY_DUPLICATE


def test_syndicated_copy_with_tracking_params_is_duplicate(seeded):
    c = candidate(
        source_urls=["https://tagesschau.de/wirtschaft/meta-strafe-eu-100.html?utm_medium=x&fbclid=1#top"]
    )
    assert _match(seeded, c)["classification"] == EXACT_DUPLICATE


def test_rewritten_headline_same_facts_is_duplicate(seeded):
    c = candidate(
        title="Meta must pay nearly 800 million euros, Brussels decides",
        key_facts=[
            "European Commission fined Meta 798 million euros",
            "The fine concerns tying Facebook Marketplace to the social network",
            "Meta says it will appeal",
        ],
        source_urls=["https://www.zeit.de/digital/meta-strafe"],
    )
    assert _match(seeded, c)["classification"] in (LIKELY_DUPLICATE, EXACT_DUPLICATE)


def test_allegation_to_confirmation_is_update_candidate(cfg, conn, clock):
    leak = story(
        "l1",
        category="cybersecurity",
        headline="Hacker group claims breach of Austrian health insurer",
        key_facts=[
            "Hacker group claims to have stolen insurer data",
            "The insurer has not confirmed the breach",
        ],
        entities=["ÖGK", "Hacker group"],
        confidence="unverified_claim",
        sources=[{"url": "https://orf.at/stories/leak1"}],
    )
    service.publish_digest(conn, cfg, publish_input("daily-news/2026-10-03", [leak]))
    clock.advance(days=2)
    c = candidate(
        "l2",
        title="Austrian health insurer confirms data breach",
        category="cybersecurity",
        entities=["ÖGK"],
        key_facts=[
            "The insurer officially confirmed the data breach",
            "Data of 1.2 million insured persons is affected",
        ],
        source_urls=["https://www.derstandard.at/story/leak2"],
    )
    r = _match(conn, c)
    assert r["classification"] == POSSIBLE_EXISTING_TOPIC
    assert r["match"]["new_fact_indexes"] == [0, 1]


def test_recurring_annual_event_not_duplicate(cfg, conn, clock):
    y1 = story(
        "n1",
        category="world",
        headline="Nobel Peace Prize 2025 awarded",
        key_facts=["The Nobel Peace Prize 2025 was awarded to Organisation A"],
        entities=["Nobel Committee"],
        sources=[{"url": "https://www.tagesschau.de/nobel-2025"}],
    )
    service.publish_digest(conn, cfg, publish_input("daily-news/2026-10-03", [y1]))
    clock.advance(days=365)
    c = candidate(
        "n2",
        title="Nobel Peace Prize 2026 awarded",
        category="world",
        entities=["Nobel Committee"],
        key_facts=["The Nobel Peace Prize 2026 was awarded to Person B"],
        source_urls=["https://www.tagesschau.de/nobel-2026"],
    )
    assert _match(conn, c)["classification"] not in (EXACT_DUPLICATE, LIKELY_DUPLICATE)


def test_vulnerability_exploited_then_patched_are_updates(cfg, conn, clock):
    v = story(
        "v1",
        category="cybersecurity",
        headline="Critical flaw CVE-2026-1234 found in ExampleVPN",
        key_facts=["CVE-2026-1234 allows remote code execution in ExampleVPN", "No patch is available yet"],
        entities=["ExampleVPN", "CVE-2026-1234"],
        sources=[{"url": "https://www.heise.de/cve-1"}],
    )
    service.publish_digest(conn, cfg, publish_input("daily-news/2026-10-03", [v]))
    clock.advance(days=3)
    c = candidate(
        "v2",
        title="CVE-2026-1234 in ExampleVPN now actively exploited",
        category="cybersecurity",
        entities=["ExampleVPN", "CVE-2026-1234"],
        key_facts=["CISA reports active exploitation of CVE-2026-1234"],
        source_urls=["https://www.cisa.gov/kev/cve-2026-1234"],
    )
    assert _match(conn, c)["classification"] == POSSIBLE_EXISTING_TOPIC


def test_casualty_number_revision_surfaces_new_fact(cfg, conn, clock):
    q = story(
        "q1",
        category="disaster",
        headline="Earthquake in Region X kills at least 120",
        key_facts=["At least 120 people were killed in the Region X earthquake"],
        entities=["Region X"],
        sources=[{"url": "https://www.zdf.de/beben-1"}],
    )
    service.publish_digest(conn, cfg, publish_input("daily-news/2026-10-03", [q]))
    clock.advance(days=1)
    c = candidate(
        "q2",
        title="Region X earthquake death toll rises to 2,300",
        category="disaster",
        entities=["Region X"],
        key_facts=["At least 2300 people were killed in the Region X earthquake"],
        source_urls=["https://www.zdf.de/beben-2"],
    )
    r = _match(conn, c)
    assert r["classification"] in (POSSIBLE_EXISTING_TOPIC, LIKELY_DUPLICATE)
    # the decision is left to ChatGPT, which sees prior vs new numbers when it is a topic match
    if r["classification"] == POSSIBLE_EXISTING_TOPIC:
        assert r["match"]["prior_facts"]


def test_match_response_is_compact(seeded):
    r = _match(
        seeded,
        candidate(
            "z1",
            source_urls=["https://example.org/other"],
            key_facts=["Meta lost its appeal against the fine"],
        ),
    )
    assert len(json.dumps(r)) < 1500


# ---------------------------------------------------------------- properties


url_part = st.text(alphabet=st.characters(codec="ascii", categories=("L", "N")), min_size=1, max_size=12)


@settings(max_examples=150, deadline=None)
@given(host=url_part, path=st.lists(url_part, max_size=4), q=st.dictionaries(url_part, url_part, max_size=3))
def test_canonical_url_idempotent(host, path, q):
    from urllib.parse import urlencode

    url = f"https://www.{host}.org/{'/'.join(path)}?{urlencode(q)}&utm_source=x#frag"
    once = canonical_url(url)
    assert canonical_url(once) == once
    assert "utm_source" not in once and "#" not in once


@settings(max_examples=150, deadline=None)
@given(st.text(min_size=0, max_size=300))
def test_normalize_idempotent(text):
    n = normalize_text(text)
    assert normalize_text(n) == n


@settings(max_examples=120, deadline=None)
@given(
    st.text(alphabet=st.characters(blacklist_categories=("Cs",)), min_size=1, max_size=3000),
    st.integers(min_value=60, max_value=1900),
)
def test_split_invariants(text, limit):
    chunks = split_message(text, limit)
    assert all(ulen(c) <= limit for c in chunks)
    assert "".join("".join(c.split()) for c in chunks) == "".join(text.split())
