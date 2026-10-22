"""Spoken briefings: sentence plan over the reader's words, rendering with a stand-in voice, serving."""

from __future__ import annotations

import dataclasses
import json
from datetime import date
from itertools import pairwise

import httpx2 as httpx
import pytest

from conftest import as_story, publish_input, story
from fakes import LiveServer, free_port
from newsrelay import service
from newsrelay.audio.build import audio_dir, content_hash, plan
from newsrelay.briefing.store import day_stories
from newsrelay.briefing.text import reading_words, story_text
from newsrelay.reader.app import create_reader_app

BODY = (
    "**What happened:** The European Commission fined Meta 798 million euros on Monday. It is the first "
    "fine of this kind against Meta.\n\n**Key facts:**\n- Fine of 798 million euros\n- Meta will appeal\n\n"
    "**Confirmed / unclear:** The decision is confirmed."
)


def _st():
    return story_text(as_story(story(body=BODY)), "5 Oct 2026")


def test_plan_covers_every_word_once_in_order():
    st = _st()
    p = plan(st, "Austria")
    spans = [(s.w0, s.w1) for s in p.segments if s.w0 is not None]
    assert spans[0][0] == 0 and spans[-1][1] == p.words == len(reading_words(st, "Austria"))
    assert all(a[1] == b[0] for a, b in pairwise(spans))  # contiguous, no gaps
    cues = [s.text for s in p.segments if s.w0 is None]
    assert "Key facts." in cues and "Impact Austria." in cues and "Headline." not in cues
    words = [w for _, w in reading_words(st, "Austria")]
    assert all(len(words[a:b]) <= 38 for a, b in spans)
    # sentences end at sentence punctuation
    first_body = next(s for s in p.segments if s.w0 is not None and words[s.w0] == "The")
    assert first_body.text.endswith("Monday.")


def test_content_hash_changes_with_text_and_voice(cfg):
    a = content_hash([_st()], cfg)
    assert a == content_hash([_st()], cfg)
    assert a != content_hash([_st()], dataclasses.replace(cfg, tts_voice="bf_emma"))
    other = story_text(as_story(story(body=BODY.replace("798", "800"))), "5 Oct 2026")
    assert a != content_hash([other], cfg)


def test_build_day_with_stand_in_voice(cfg, conn, clock):
    np = pytest.importorskip("numpy")
    pytest.importorskip("lameenc")
    from newsrelay.audio.build import SAMPLE_RATE, build_day, run

    service.publish_digest(conn, cfg, publish_input("daily-news/2026-10-03", [story(body=BODY)]))
    calls: list[str] = []

    def synth(text: str):
        calls.append(text)
        return np.zeros(int(SAMPLE_RATE * 0.05 * len(text.split())), dtype=np.float32)

    d = date(2026, 10, 3)
    assert build_day(cfg, conn, d, synth) is True
    out = audio_dir(cfg, d)
    t = json.loads((out / "timing.json").read_text())
    assert (out / "day.mp3").stat().st_size > 0 and (out / "story-01.mp3").is_file()
    seg = t["stories"][0]["segments"]
    assert seg[0][0] == 0.0 and all(s[1] > s[0] for s in seg) and seg[-1][3] == t["stories"][0]["words"]
    assert calls[0].startswith("Daily briefing for Saturday, 3 October 2026")
    assert build_day(cfg, conn, d, synth) is False  # unchanged content -> no re-render
    assert run(cfg, conn, only=d) == 0


def test_word_starts_from_phoneme_timings():
    from newsrelay.audio.build import word_starts

    # "about 1,600 — a record." -> phoneme words: about | one thousand six-hundred | — | a | record.
    ph = [
        ("ɐ", 0.0),
        ("b", 0.1),
        (" ", 0.2),
        ("w", 0.3),
        (" ", 0.5),
        ("θ", 0.6),
        (" ", 0.9),
        ("s", 1.0),
        (" ", 1.4),
        ("—", 1.5),
        (" ", 1.6),
        ("ɐ", 1.7),
        (" ", 1.8),
        ("ɹ", 1.9),
        (".", 2.3),
    ]
    assert word_starts(ph, [1, 3, 0, 1, 1]) == [0.0, 0.3, 1.7, 1.7, 1.9]
    assert word_starts(ph, [1, 2, 0, 1, 1]) is None  # does not add up -> no guessing


def test_build_day_keeps_per_word_starts_and_upgrades_old_timing(cfg, conn, clock):
    np = pytest.importorskip("numpy")
    pytest.importorskip("lameenc")
    from newsrelay.audio.build import SAMPLE_RATE, Spoken, build_day

    service.publish_digest(conn, cfg, publish_input("daily-news/2026-10-03", [story(body=BODY)]))

    def synth(text: str):
        n = len(text.split(" "))
        return Spoken(np.zeros(int(SAMPLE_RATE * 0.1 * n), dtype=np.float32), [0.1 * k for k in range(n)])

    d = date(2026, 10, 3)
    assert build_day(cfg, conn, d, synth) is True
    path = audio_dir(cfg, d) / "timing.json"
    t = json.loads(path.read_text())
    assert t["version"] == 2
    for t0, t1, w0, w1, at in t["stories"][0]["segments"]:
        assert len(at) == w1 - w0 and at[0] == t0 and all(t0 <= x <= t1 for x in at)
    t["version"] = 1  # audio from before per-word timings is rendered again
    path.write_text(json.dumps(t))
    assert build_day(cfg, conn, d, synth) is True


def _fake_audio(cfg, d: date, words: int) -> None:
    out = audio_dir(cfg, d)
    out.mkdir(parents=True)
    (out / "day.mp3").write_bytes(b"ID3" + bytes(5000))
    (out / "story-01.mp3").write_bytes(b"ID3" + bytes(3000))
    timing = {
        "version": 1,
        "content": "x",
        "voice": "af_heart",
        "day": {"file": "day.mp3", "duration": 12.0},
        "stories": [
            {"file": "story-01.mp3", "duration": 10.0, "words": words, "segments": [[0.0, 9.5, 0, words]]}
        ],
    }
    (out / "timing.json").write_text(json.dumps(timing))


def test_reader_serves_audio_with_ranges_and_three_buttons(cfg, conn, clock):
    cfg = dataclasses.replace(cfg, briefing_pdf=True)
    service.publish_digest(conn, cfg, publish_input("daily-news/2026-10-03", [story(body=BODY)]))
    d = date(2026, 10, 3)
    words = len(reading_words(day_stories(conn, cfg, d)[0], cfg.reader_country))
    srv = LiveServer(create_reader_app(cfg), free_port())
    try:
        with httpx.Client(base_url=srv.base) as c:
            before = c.get("/").text
            assert 'title="The English spoken version is being prepared">MP3 EN' in before
            assert '"audio": null' in c.get("/day/2026-10-03").text
            _fake_audio(cfg, d, words)
            index = c.get("/").text
            assert ">Read</a>" in index and "/pdf/briefing-2026-10-03" in index
            assert 'href="/day/2026-10-03/audio/day.mp3" download="briefing-2026-10-03.mp3"' in index
            assert ">MP3 EN<" in index
            page = c.get("/day/2026-10-03").text
            assert '"/day/2026-10-03/audio/story-01.mp3"' in page and 'id="voice"' in page
            assert 'id="pages"' in page and "integrity" in page  # PDF below, pdf.js pinned by SRI
            assert (
                "translate_a/element.js" not in page
                and 'data-i18n="wpm"' in page
                and "Wörter pro Minute" in page
            )
            r = c.get("/day/2026-10-03/audio/day.mp3", headers={"Range": "bytes=0-99"})
            assert (
                r.status_code == 206 and len(r.content) == 100 and r.headers["content-type"] == "audio/mpeg"
            )
            assert c.get("/day/2026-10-03/audio/timing.json").status_code == 404
            assert c.get("/day/2026-10-03/audio/..%2F..%2Fnewsrelay.db").status_code == 404
            csp = c.get("/").headers["content-security-policy"]
            assert "frame-ancestors 'none'" in csp and "google" not in csp and "cdnjs.cloudflare.com" in csp
    finally:
        srv.stop()


def test_audio_hidden_when_stories_changed_since_render(cfg, conn, clock):
    service.publish_digest(conn, cfg, publish_input("daily-news/2026-10-03", [story(body=BODY)]))
    d = date(2026, 10, 3)
    _fake_audio(cfg, d, 5)
    t = json.loads((audio_dir(cfg, d) / "timing.json").read_text())
    t["stories"].append(t["stories"][0])  # two audio stories, one story in the database
    (audio_dir(cfg, d) / "timing.json").write_text(json.dumps(t))
    srv = LiveServer(create_reader_app(cfg), free_port())
    try:
        with httpx.Client(base_url=srv.base) as c:
            assert '"audio": null' in c.get("/day/2026-10-03").text
    finally:
        srv.stop()
