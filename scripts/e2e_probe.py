"""Production end-to-end probe: DCR -> authorize -> consent -> token (PKCE) -> MCP read-only tools.

Run as root (reads the owner passphrase file; never prints it):
    sudo /opt/newsrelay/current/venv/bin/python scripts/e2e_probe.py https://<host>
Only read-only tools are called, so the research checkpoint and Discord are untouched.
The probe revokes the tokens it obtained at the end.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx2 as httpx

REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect"


def main(base: str) -> int:
    passphrase = Path("/etc/newsrelay/secrets/owner_passphrase.txt").read_text().strip()
    resource = base.rstrip("/") + "/mcp"
    c = httpx.Client(base_url=base, follow_redirects=False, timeout=30)
    reg = c.post(
        "/register",
        json={
            "redirect_uris": [REDIRECT],
            "client_name": "newsrelay e2e probe",
            "token_endpoint_auth_method": "client_secret_post",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
    )
    reg.raise_for_status()
    client_id = reg.json()["client_id"]
    client_secret = reg.json()["client_secret"]
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    r = c.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "p",
            "scope": "newsrelay offline_access",
            "resource": resource,
        },
    )
    assert r.status_code == 302, r.text
    req_id = parse_qs(urlsplit(r.headers["location"]).query)["req"][0]
    r = c.post("/oauth/consent", data={"req": req_id, "passphrase": passphrase})
    assert r.status_code == 302, r.status_code
    code = parse_qs(urlsplit(r.headers["location"]).query)["code"][0]
    tok = c.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "client_id": client_id,
            "client_secret": client_secret,
            "code_verifier": verifier,
            "resource": resource,
        },
    )
    tok.raise_for_status()
    access = tok.json()["access_token"]
    h = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access}",
    }
    init = c.post(
        "/mcp",
        headers=h,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "probe", "version": "1"},
            },
        },
    )
    init.raise_for_status()
    h["MCP-Protocol-Version"] = init.json()["result"]["protocolVersion"]
    tools = c.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()
    names = sorted(t["name"] for t in tools["result"]["tools"])
    out = {"oauth": "ok", "protocol": h["MCP-Protocol-Version"], "tools": names}
    for i, (name, args) in enumerate(
        [
            ("newsrelay_health", {}),
            ("newsrelay_begin_run", {"run_key": "e2e-probe/2000-01-01"}),
            (
                "newsrelay_match_candidates",
                {
                    "run_key": "e2e-probe/2000-01-01",
                    "candidates": [
                        {
                            "candidate_id": "p1",
                            "title": "Probe candidate",
                            "category": "other",
                            "key_facts": ["This is a connectivity probe"],
                            "source_urls": ["https://example.org/probe"],
                        }
                    ],
                },
            ),
        ],
        start=3,
    ):
        res = c.post(
            "/mcp",
            headers=h,
            json={
                "jsonrpc": "2.0",
                "id": i,
                "method": "tools/call",
                "params": {"name": name, "arguments": args},
            },
        ).json()["result"]
        out[name] = (
            "error"
            if res.get("isError")
            else json.loads(res["content"][0]["text"]).get(
                "status", json.loads(res["content"][0]["text"]).get("ok", "ok")
            )
        )
    rv = c.post("/revoke", data={"token": access, "client_id": client_id, "client_secret": client_secret})
    out["revoke_http"] = f"{rv.status_code} {rv.text[:120]}"
    after = c.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 9, "method": "tools/list"})
    out["revoked_token_rejected"] = after.status_code == 401
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
