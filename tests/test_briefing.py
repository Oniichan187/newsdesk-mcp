"""Briefing PDF (Bionic Reading, queued before the stories, uploaded as a file) and the RSVP reader."""

from __future__ import annotations

import dataclasses
import json
import re
from datetime import date

import httpx2 as httpx
import pytest

from conftest import as_story, publish_input, story
from fakes import LiveServer, free_port
from newsrelay import service
from newsrelay.briefing.pdf import render_pdf
from newsrelay.briefing.store import day_pdfs, day_stories
from newsrelay.briefing.text import (
    bionic_markdown,
    bionic_split,
    clean,
    parse_body,
    reading_words,
    story_text,
)
from newsrelay.publishing.discord import BotTransport, WebhookTransport
from newsrelay.reader.app import create_reader_app

TOKEN = "MTAwMDAwMDAwMDAwMDAwMDAwMA.GaBcDe.abcdefghijklmnopqrstuvwxyz0123456789ABCD"
HOOK = "https://discord.com/api/webhooks/123456789012345678/" + "T0ken" * 12


def _rich(cid: str = "c1", **kw):
    data = story(
        cid,
        body=(
            "**What happened:** The European Commission fined Meta 798 million euros on Monday for tying "
            "Facebook Marketplace to its social network. It is the first fine of this kind against Meta.\n\n"
            "**Key facts:**\n- Fine of 798 million euros\n- Meta says it will appeal\n\n"
            "**Confirmed / unclear:** The decision is confirmed; the appeal outcome is open."
        ),
        outlook=[{"event": "Meta appeals", "likelihood": "very_likely", "basis": "Meta announced it"}],
    )
    data.update(kw)
    return data


# --------------------------------------------------------------------------- text


@pytest.mark.parametrize(("word", "k"), [("a", 1), ("the", 1), ("fine", 2), ("Saudi", 2), ("Commission", 4)])
def test_bionic_split(word, k):
    assert bionic_split(word) == k


def test_bionic_markdown_bolds_word_starts_and_neutralises_markers():
    assert bionic_markdown("Meta fined") == "**Me**ta **fi**ned"
    assert bionic_markdown("3.8% in 2026") == "3.8% **i**n 2026"  # numbers stay plain
    out = bionic_markdown("a -- b __c__ **d**")
    assert "--" not in out and "__" not in out and out.count("**") % 2 == 0


def test_parse_body_and_clean():
    secs = parse_body(_rich()["body"])
    assert [s.label for s in secs] == ["What happened", "Key facts", "Confirmed / unclear"]
    assert secs[1].bullets == ["Fine of 798 million euros", "Meta says it will appeal"]
    assert clean("🌍 **Region:** [ORF](<https://orf.at/x>) ✅") == "Region: ORF"


def test_reading_words_follow_the_post_order():
    st = story_text(as_story(_rich()), "5 Oct 2026")
    labels = [label for label, _ in reading_words(st, "Austria")]
    order = [
        "Headline",
        "What happened",
        "Key facts",
        "Confirmed / unclear",
        "Importance",
        "Impact Austria",
        "Impact global",
        "Outlook",
    ]
    seen = [x for i, x in enumerate(labels) if i == 0 or labels[i - 1] != x]
    assert seen == order


# --------------------------------------------------------------------------- pdf


def test_pdf_renders_without_emoji_and_with_contents():
    sts = [story_text(as_story(_rich(f"c{i}", importance=i + 3)), "5 Oct 2026") for i in range(3)]
    data = render_pdf(date(2026, 10, 6), sts, "Austria")
    assert data.startswith(b"%PDF-")
    assert len(re.findall(rb"/Type /Page\b", data)) >= 4  # cover + one page per story
    assert b"/Link" in data  # contents and sources are clickable


def test_publish_queues_pdf_first_and_keeps_full_story(cfg, conn, clock):
    cfg = dataclasses.replace(cfg, briefing_pdf=True, reader_url="https://reader.example:8443")
    service.publish_digest(
        conn,
        cfg,
        publish_input(
            "daily-news/2026-10-03",
            [
                _rich("a", importance=4),
                _rich(
                    "b",
                    importance=9,
                    headline="Second distinct story headline here",
                    key_facts=["Another unrelated fact"],
                ),
            ],
        ),
    )
    rows = conn.execute("SELECT story_id, payload FROM outbox ORDER BY id").fetchall()
    first = json.loads(rows[0]["payload"])
    assert rows[0]["story_id"] is None and "file" in first
    assert first["file"]["name"].endswith(".pdf") and "2 stories" in first["content"]
    assert "https://reader.example:8443" in first["content"]
    assert all(r["story_id"] for r in rows[1:])
    pdfs = day_pdfs(cfg, date(2026, 10, 3))
    assert len(pdfs) == 1 and pdfs[0].read_bytes().startswith(b"%PDF-")
    stored = day_stories(conn, cfg, date(2026, 10, 3))
    assert [s.importance for s in stored] == [9, 4]
    assert stored[0].impact_global and stored[1].outlook


def test_pdf_failure_never_blocks_the_stories(cfg, conn, clock, monkeypatch):
    import newsrelay.briefing.pdf as pdf_mod

    def boom(*a, **k):
        raise RuntimeError("font missing")

    monkeypatch.setattr(pdf_mod, "render_pdf", boom)
    cfg = dataclasses.replace(cfg, briefing_pdf=True)
    service.publish_digest(conn, cfg, publish_input("daily-news/2026-10-03", [_rich()]))
    payloads = [json.loads(r[0]) for r in conn.execute("SELECT payload FROM outbox ORDER BY id")]
    assert payloads and all("file" not in p for p in payloads)


# --------------------------------------------------------------------------- upload


class _Capture:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={"id": "1234567890123456789"})


def test_bot_uploads_the_pdf_as_attachment(tmp_path):
    f = tmp_path / "briefing-2026-10-06-daily-news.pdf"
    f.write_bytes(b"%PDF-1.7 test")
    cap = _Capture()
    t = BotTransport(TOKEN, "100000000000000001", client=httpx.Client(transport=httpx.MockTransport(cap)))
    payload = {"content": "**Daily Briefing**", "file": {"path": str(f), "name": f.name}}
    assert t.send(payload, key="k1").http_status == 200
    req = cap.requests[0]
    assert req.headers["content-type"].startswith("multipart/form-data")
    body = req.read()
    assert b'name="payload_json"' in body and b'"attachments"' in body and b'"nonce"' in body
    assert f.name.encode() in body and b"%PDF-1.7 test" in body


def test_webhook_upload_and_missing_file_falls_back_to_text(tmp_path):
    cap = _Capture()
    t = WebhookTransport(HOOK, client=httpx.Client(transport=httpx.MockTransport(cap)))
    t.send({"content": "x", "file": {"path": str(tmp_path / "gone.pdf"), "name": "gone.pdf"}})
    assert cap.requests[0].headers["content-type"] == "application/json"
    assert b'"file"' not in cap.requests[0].read()


# --------------------------------------------------------------------------- reader


def test_stories_from_before_schema_3_are_still_readable(cfg, conn, clock):
    service.publish_digest(conn, cfg, publish_input("daily-news/2026-10-03", [_rich()]))
    conn.execute("UPDATE stories SET story_json = NULL")  # as published by 1.7.0 and older
    from newsrelay.briefing.store import days

    assert days(conn, cfg) == [(date(2026, 10, 3), 1)]
    (st,) = day_stories(conn, cfg, date(2026, 10, 3))
    assert st.headline.startswith("EU fines Meta") and st.importance == 0
    assert st.sections[0].label == "What happened" and st.sources
    labels = {label for label, _ in reading_words(st, "Austria")}
    assert "Importance" not in labels and "Impact Austria" not in labels


class _Tunnel:
    """Stands in for cloudflared's metrics server."""

    def __init__(self, body: dict) -> None:
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        outer = self
        self.paths: list[str] = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.paths.append(self.path)
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.addr = f"127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


def test_reader_link_prefers_config_then_quick_tunnel(cfg):
    from newsrelay.briefing.link import reader_link

    t = _Tunnel({"hostname": "calm-river-1234.trycloudflare.com"})
    try:
        assert (
            reader_link(dataclasses.replace(cfg, reader_url="https://news.example")) == "https://news.example"
        )
        assert reader_link(dataclasses.replace(cfg, reader_tunnel_metrics=t.addr)) == (
            "https://calm-river-1234.trycloudflare.com"
        )
        assert t.paths == ["/quicktunnel"]
    finally:
        t.server.shutdown()
    bad = _Tunnel({"hostname": "evil.example.com"})
    try:
        assert reader_link(dataclasses.replace(cfg, reader_tunnel_metrics=bad.addr)) == ""
    finally:
        bad.server.shutdown()
    assert (
        reader_link(dataclasses.replace(cfg, reader_tunnel_metrics="10.0.0.1:20241")) == ""
    )  # loopback only
    assert (
        reader_link(dataclasses.replace(cfg, reader_tunnel_metrics=f"127.0.0.1:{free_port()}")) == ""
    )  # down


def test_reader_pages(cfg, conn, clock):
    cfg = dataclasses.replace(cfg, briefing_pdf=True)
    service.publish_digest(conn, cfg, publish_input("daily-news/2026-10-03", [_rich()]))
    name = day_pdfs(cfg, date(2026, 10, 3))[0].name
    srv = LiveServer(create_reader_app(cfg), free_port())
    try:
        with httpx.Client(base_url=srv.base) as c:
            index = c.get("/")
            assert index.status_code == 200 and "/day/2026-10-03" in index.text
            for level in ("<summary>2026", "<summary>October", "<summary>Week 40", "Saturday, 3 October"):
                assert level in index.text  # year > month > week > day
            assert "default-src 'none'" in index.headers["content-security-policy"]
            page = c.get("/day/2026-10-03")
            assert page.status_code == 200
            assert 'min="100" max="2000"' in page.text
            assert 'id="ctxl"' in page.text and 'id="ctxr"' in page.text  # context beside the word
            assert "EU fines Meta" in page.text and "Impact Austria" in page.text
            assert f"/day/2026-10-03/pdf/{name}" in page.text
            pdf = c.get(f"/day/2026-10-03/pdf/{name}")
            assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF-")
            assert c.get("/day/2026-10-03/pdf/..%2F..%2Fnewsrelay.db").status_code == 404
            assert c.get("/day/not-a-date").status_code == 404
            assert (
                "No stories" in c.get("/day/2020-01-01").text or c.get("/day/2020-01-01").status_code == 200
            )
    finally:
        srv.stop()
