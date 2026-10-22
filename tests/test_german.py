"""German edition: sentence-wise translation keeps labels and timings; reader offers EN/DE."""

from __future__ import annotations

import json
from datetime import date
from itertools import pairwise

import httpx2 as httpx
import pytest

from conftest import as_story, publish_input, story
from fakes import LiveServer, free_port
from newsrelay import service
from newsrelay.audio.build import audio_dir
from newsrelay.audio.german import german_story, label_de, long_date_de
from newsrelay.briefing.text import story_text
from newsrelay.reader.app import create_reader_app

BODY = (
    "**What happened:** The European Commission fined Meta 798 million euros on Monday. It is the first "
    "fine of this kind against Meta.\n\n**Key facts:**\n- Fine of 798 million euros\n- Meta will appeal"
)


def fake_translate(texts: list[str]) -> list[str]:
    return ["DE " + t for t in texts]


def test_german_story_translates_sentence_by_sentence_with_german_labels():
    st = story_text(as_story(story(body=BODY, headline="EU fines Meta over Marketplace")), "5 Oct 2026")
    headline, words, sa = german_story(st, "Austria", fake_translate)
    assert headline == "DE EU fines Meta over Marketplace"
    labels = [lab for lab, _ in words]
    assert labels[0] == "Überschrift" and "Wichtige Fakten" in labels and "Auswirkung Österreich" in labels
    assert "Impact Austria" not in labels and "Key facts" not in labels
    spans = [(s.w0, s.w1) for s in sa.segments if s.w0 is not None]
    assert spans[0][0] == 0 and spans[-1][1] == sa.words == len(words)
    assert all(a[1] == b[0] for a, b in pairwise(spans))
    cues = [s.text for s in sa.segments if s.w0 is None]
    assert "Wichtige Fakten." in cues and "Auswirkung Österreich." in cues


def test_labels_and_dates():
    assert label_de("Impact Germany", "Germany", fake_translate) == "Auswirkung Deutschland"
    assert label_de("Impact Chile", "Chile", fake_translate) == "Auswirkung DE Chile"
    assert label_de("Something new", "Austria", fake_translate) == "DE Something new"
    assert long_date_de(date(2026, 10, 3)) == ("Samstag", "3. Oktober 2026")


def _render(cfg, conn):
    np = pytest.importorskip("numpy")
    pytest.importorskip("lameenc")
    from newsrelay.audio.build import SAMPLE_RATE, Spoken, build_day, build_day_de

    def synth(text: str):
        n = len(text.split(" "))
        return Spoken(np.zeros(int(SAMPLE_RATE * 0.1 * n), dtype=np.float32), [0.1 * k for k in range(n)])

    d = date(2026, 10, 3)
    assert build_day(cfg, conn, d, synth) is True
    assert build_day_de(cfg, conn, d, fake_translate, synth) is True
    assert build_day_de(cfg, conn, d, fake_translate, synth) is False  # unchanged -> no re-render
    return d


def test_build_day_de_writes_text_and_audio(cfg, conn, clock):
    service.publish_digest(conn, cfg, publish_input("daily-news/2026-10-03", [story(body=BODY)]))
    d = _render(cfg, conn)
    out = audio_dir(cfg, d, "de")
    assert out.name == "audio-de" and (out / "day.mp3").is_file() and (out / "story-01.mp3").is_file()
    t = json.loads((out / "timing.json").read_text(encoding="utf-8"))
    (text,) = t["text"]
    assert text["headline"].startswith("DE ") and t["stories"][0]["words"] == len(text["words"])
    assert t["voice"] == cfg.tts_german_voice


def test_reader_offers_the_german_edition(cfg, conn, clock):
    service.publish_digest(conn, cfg, publish_input("daily-news/2026-10-03", [story(body=BODY)]))
    srv = LiveServer(create_reader_app(cfg), free_port())
    try:
        with httpx.Client(base_url=srv.base) as c:
            page = c.get("/day/2026-10-03").text
            assert '"de": null' in page and 'id="lang-de"' in page  # not rendered yet
            _render(cfg, conn)
            page = c.get("/day/2026-10-03").text
            assert '"/day/2026-10-03/audio-de/story-01.mp3"' in page and "Überschrift" in page
            r = c.get("/day/2026-10-03/audio-de/day.mp3")
            assert r.status_code == 200 and "briefing-2026-10-03-de.mp3" in r.headers["content-disposition"]
            assert c.get("/day/2026-10-03/audio-xx/day.mp3").status_code == 404
            index = c.get("/").text
            assert "MP3 DE" in index and "MP3 EN" in index
    finally:
        srv.stop()


def test_older_days_without_german_are_backfilled(cfg, conn, clock):
    from newsrelay.audio.build import _german_backlog

    service.publish_digest(conn, cfg, publish_input("daily-news/2026-10-03", [story(body=BODY)]))
    today = date(2026, 10, 10)
    assert _german_backlog(cfg, conn, today, 2) == [date(2026, 10, 3)]
    assert _german_backlog(cfg, conn, date(2026, 10, 4), 2) == []  # still inside the normal window
    _render(cfg, conn)
    assert _german_backlog(cfg, conn, today, 2) == []
