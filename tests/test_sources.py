"""Source allowlist: built-in list, matching rules, override file, enforcement in match/publish."""

from __future__ import annotations

import dataclasses

import pytest

from conftest import candidate, publish_input, story
from newsrelay import service
from newsrelay.schemas import MatchInput
from newsrelay.sources import load_allowlist, parse

OWNER_LIST = [
    "taz", "Frankfurter Rundschau", "Die Zeit", "Süddeutsche Zeitung", "Der Standard", "Tages-Anzeiger",
    "Der Spiegel", "Berner Zeitung", "Tagesspiegel", "netzpolitik.org", "Correctiv", "Falter", "Profil",
    "Der Bund", "Republik", "Watson", "FragDenStaat", "Volksverpetzer", "Übermedien", "Belltower.News",
    "Dossier", "ZackZack", "Tagesschau", "Deutschlandfunk", "ORF", "ZDF", "ARD", "NDR", "Krautreporter",
    "zufron.com",
]  # fmt: skip


def test_builtin_list_is_exactly_the_configured_outlets():
    allow = load_allowlist(None)
    assert sorted(allow.names()) == sorted(OWNER_LIST)


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://taz.de/!6000000/", True),
        ("https://www.taz.de/artikel", True),
        ("https://news.orf.at/stories/3400000/", True),
        ("https://www.sueddeutsche.de/politik/x", True),
        ("https://sz.de/1.234", True),
        ("https://www.derstandard.at/story/3000000", True),
        ("https://www.zdfheute.de/politik/x", True),
        ("https://www.zufron.com/news/x", True),
        ("https://taz.de.evil.com/x", False),
        ("https://eviltaz.de/x", False),
        ("https://www.bild.de/politik/x", False),
        ("https://example.org/x", False),
        ("https://orf.at.example.org/x", False),
    ],
)
def test_matching_rules(url, ok):
    assert load_allowlist(None).is_allowed(url) is ok


def test_override_file_replaces_builtin(tmp_path):
    f = tmp_path / "sources.toml"
    f.write_text('[[source]]\nname = "Only BBC"\ndomains = ["bbc.co.uk"]\n', encoding="utf-8")
    allow = load_allowlist(f)
    assert allow.names() == ["Only BBC"]
    assert allow.is_allowed("https://www.bbc.co.uk/news/x")
    assert not allow.is_allowed("https://taz.de/x")
    assert load_allowlist(tmp_path / "missing.toml").origin == "built-in sources.toml"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        '[[source]]\nname = "x"\ndomains = []\n',
        '[[source]]\nname = ""\ndomains = ["a.de"]\n',
        '[[source]]\nname = "x"\ndomains = ["https://a.de/path"]\n',
        '[[source]]\nname = "x"\ndomains = ["a.de"]\nurl = "typo"\n',
    ],
)
def test_invalid_files_rejected(bad):
    with pytest.raises(ValueError):
        parse(bad, "test")


def test_match_flags_disallowed_sources(cfg, conn, clock):
    real = dataclasses.replace(cfg, sources_file=None)  # built-in list only
    out = service.match_candidates(
        conn,
        real,
        MatchInput(
            run_key="daily-news/2026-10-03",
            candidates=[
                candidate("ok1"),  # tagesschau.de
                candidate("bad1", source_urls=["https://www.bild.de/x", "https://www.tagesschau.de/y"]),
            ],
        ),
    )["results"]
    by_id = {r["candidate_id"]: r for r in out}
    assert by_id["ok1"]["classification"] == "NO_MATCH"
    assert by_id["bad1"]["classification"] == "SOURCE_NOT_ALLOWED"
    assert "bild.de" in by_id["bad1"]["reason"]


def test_publish_rejects_story_with_disallowed_source(cfg, conn, clock):
    real = dataclasses.replace(cfg, sources_file=None)
    good = story("g1")
    bad = story(
        "b1",
        headline="Story citing a non-listed outlet",
        key_facts=["A fact reported only by an outlet outside the list"],
        sources=[{"url": "https://www.bild.de/x", "name": "Bild"}],
    )
    out = service.publish_digest(conn, real, publish_input("daily-news/2026-10-03", [good, bad]))
    assert [s["candidate_id"] for s in out["stories"]] == ["g1"]
    assert out["rejected"][0]["candidate_id"] == "b1"
    assert "SOURCE_NOT_ALLOWED" in out["rejected"][0]["reason"]
    assert conn.execute("SELECT count(*) FROM outbox o JOIN stories s ON s.id = o.story_id "
                        "WHERE s.candidate_id = 'b1'").fetchone()[0] == 0  # fmt: skip
