import json

import pytest

from conftest import as_story, story
from newsrelay.publishing.formatter import build_payloads, render_text, sanitize_mentions
from newsrelay.publishing.splitter import split_message, ulen

LIMIT = 1850


def test_short_text_single_message():
    assert split_message("hello world", LIMIT) == ["hello world"]


def test_m_long_story_split_on_paragraphs():  # TEST M
    paras = [f"Paragraph {i}. " + ("Lorem ipsum dolor sit amet. " * 18) for i in range(6)]
    text = "\n\n".join(paras)
    assert ulen(text) > 2500
    chunks = split_message(text, LIMIT)
    assert len(chunks) >= 2
    assert all(ulen(c) <= LIMIT for c in chunks)  # TEST N
    # paragraphs are kept whole
    for p in paras:
        assert any(p.strip() in c for c in chunks)


def test_sentence_split_when_paragraph_too_long():
    text = " ".join(f"Sentence number {i} has some words in it." for i in range(120))
    chunks = split_message(text, 500)
    assert all(ulen(c) <= 500 for c in chunks)
    assert all(c.endswith(".") for c in chunks)


def test_urls_and_markdown_links_never_cut():
    url = "https://example.com/" + "a" * 150
    link = f"[Some source name here]({url})"
    text = " ".join([f"word{i}" for i in range(100)] + [link] + [f"x{i}" for i in range(100)] + [url] * 3)
    chunks = split_message(text, 200)
    assert all(ulen(c) <= 200 for c in chunks)
    joined = "\n".join(chunks)
    assert joined.count(url) == 4
    assert link in joined


def test_unicode_emoji_and_combining_not_broken():
    family = "👨‍👩‍👧‍👦"
    text = (family * 300) + ("é" * 300)
    chunks = split_message(text, 100)
    assert all(ulen(c) <= 100 for c in chunks)
    assert "".join(chunks) == text
    for c in chunks:
        assert not c.startswith("‍") and not c.startswith("́")
        c.encode("utf-8")  # no lone surrogates


def test_sanitize_mentions():
    s = sanitize_mentions("hi @everyone and @here <@123456789012345678> <@&42> <#99>")
    assert "@everyone" not in s and "@here" not in s
    assert "<@1" not in s and "<@&" not in s


def test_o_payload_never_pings():  # TEST O
    d = story(body="Breaking: @everyone must read this. Also @here and <@&1234567890>. " * 3)
    payloads = build_payloads(
        as_story(d), username="News Update", limit=LIMIT, suppress_embeds=True, display_tz="Europe/Vienna"
    )
    for p in payloads:
        assert p["allowed_mentions"] == {"parse": []}
        assert "@everyone" not in p["content"]
        assert p["username"] == "News Update"


def test_n_long_story_payloads_within_limit_and_numbered():  # TEST M + N
    body = "\n\n".join(("Detail sentence about the event. " * 15) for _ in range(8))
    d = story(body=body[:3500])
    payloads = build_payloads(
        as_story(d), username="News Update", limit=LIMIT, suppress_embeds=True, display_tz="Europe/Vienna"
    )
    assert len(payloads) >= 2
    assert all(ulen(p["content"]) <= LIMIT for p in payloads)
    assert all("continued" not in p["content"] and not p["content"].startswith("-#") for p in payloads)
    assert len(json.dumps(payloads[0])) < 8000


def test_render_update_prefix_and_sources():
    d = story(
        kind="UPDATE",
        material_change="Meta filed appeal",
        confidence="unverified_claim",
        sources=[{"url": "https://www.derstandard.at/story/1"}, {"url": "https://orf.at/x", "name": "ORF"}],
    )
    text = render_text(as_story(d), "Europe/Vienna")
    assert text.startswith("## 🔄 Update: ")
    assert "unverified claim" in text
    assert "[derstandard.at](<https://www.derstandard.at/story/1>)" in text
    assert "[ORF](<https://orf.at/x>)" in text


@pytest.mark.parametrize("limit", [10, 49])
def test_limit_too_small(limit):
    with pytest.raises(ValueError):
        split_message("x", limit)


def test_impact_and_outlook_rendered_before_sources():
    d = story(
        impact="EU users may see Marketplace split from Facebook.",
        impact_region="Global",
        outlook=[
            {
                "event": "Court overturns the fine",
                "likelihood": "unlikely",
                "probability_percent": 25,
                "basis": "antitrust lawyers quoted by Tagesschau",
            },
            {"event": "Meta appeals", "likelihood": "very_likely", "basis": "Meta announced it"},
        ],
    )
    text = render_text(as_story(d), "Europe/Vienna")
    assert "🌍 **Region:** Global" in text
    assert "⭐ **Importance:** 5/10 — First large EU antitrust fine against Meta; few direct effects." in text
    assert "🇦🇹 **Impact Austria:** EU users may see Marketplace split from Facebook." in text
    assert "🌍 **Impact global:** Sets a precedent" in text
    order = ["Region:", "**Confirmed", "Importance:", "Impact Austria:", "Impact global:", "🔮", "Sources:"]
    assert [text.index(m) for m in order if m in text] == sorted(text.index(m) for m in order if m in text)
    assert "- Court overturns the fine → **unlikely, ~25 %** (antitrust lawyers quoted by Tagesschau)" in text
    assert "- Meta appeals → **very likely** (Meta announced it)" in text
    assert text.index("Impact global:") < text.index("🔮") < text.index("📰 **Sources:**")


def test_no_outlook_section_without_outlook():
    text = render_text(as_story(story()), "Europe/Vienna")
    assert "📍 **Region:** EU" in text and "Impact Austria:" in text and "🔮" not in text


def test_impact_label_follows_reader_country():
    text = render_text(as_story(story()), "Europe/Berlin", "Germany")
    assert "🇩🇪 **Impact Germany:**" in text
    assert "📍 **Impact Chile:**" in render_text(as_story(story()), "America/Santiago", "Chile")


@pytest.mark.parametrize("value", [0, 11])
def test_importance_scale_is_1_to_10(value):
    with pytest.raises(ValueError):
        as_story(story(importance=value))


@pytest.mark.parametrize(
    "field", ["impact", "impact_global", "impact_region", "importance", "importance_reason"]
)
def test_impact_and_region_are_required(field):
    d = story()
    del d[field]
    with pytest.raises(ValueError):
        as_story(d)


@pytest.mark.parametrize(("level", "pct"), [("very_unlikely", 90), ("likely", 20), ("uncertain", 95)])
def test_outlook_percent_must_fit_level(level, pct):
    d = story(
        outlook=[
            {
                "event": "Something happens",
                "likelihood": level,
                "probability_percent": pct,
                "basis": "some estimate",
            }
        ]
    )
    with pytest.raises(ValueError):
        as_story(d)


def test_several_sources_all_rendered():
    d = story(
        sources=[
            {"url": "https://www.tagesschau.de/a/meta-100.html", "name": "Tagesschau"},
            {"url": "https://www.derstandard.at/story/1", "name": "Der Standard"},
            {"url": "https://orf.at/stories/3400000/", "name": "ORF"},
        ]
    )
    text = render_text(as_story(d), "Europe/Vienna")
    assert "[Tagesschau](" in text and "[Der Standard](" in text and "[ORF](" in text


@pytest.mark.parametrize(
    "sources",
    [
        [{"url": "https://orf.at/"}],
        [{"url": "https://www.derstandard.at"}],
        [{"url": "https://orf.at/stories/1/"}, {"url": "https://orf.at/stories/1/"}],
    ],
)
def test_homepage_and_duplicate_sources_rejected(sources):
    with pytest.raises(ValueError):
        as_story(story(sources=sources))
