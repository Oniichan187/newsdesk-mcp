"""The prompt generator and the committed default prompt stay consistent."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("build_prompt", ROOT / "scripts" / "build_prompt.py")
assert spec and spec.loader
bp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bp)


def test_default_prompt_contains_every_source_and_no_placeholders():
    sources = bp.read_sources(bp.DEFAULT_SOURCES)
    assert len(sources) == 30 and "zufron.com" in sources
    text = bp.build(sources)
    assert "{{" not in text
    for s in sources:
        assert s in text


def test_committed_prompt_doc_matches_generator():
    doc = (ROOT / "docs" / "SCHEDULED_TASK_PROMPT.md").read_text(encoding="utf-8")
    generated = bp.build(bp.read_sources(bp.DEFAULT_SOURCES)).strip()
    assert generated in doc


def test_all_example_source_files_build(tmp_path):
    for f in (ROOT / "examples" / "sources").glob("*.txt"):
        assert bp.build(bp.read_sources(f), language="German", region="the world")


def test_custom_options(tmp_path):
    f = tmp_path / "s.txt"
    f.write_text("# comment\nOutlet A\n\nOutlet B\nOutlet A\n", encoding="utf-8")
    assert bp.read_sources(f) == ["Outlet A", "Outlet B"]
    text = bp.build(["Outlet A"], language="German", region="Switzerland", timezone="Europe/Zurich")
    assert "German Discord markdown" in text and "Switzerland" in text and "Europe/Zurich" in text
