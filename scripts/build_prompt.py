#!/usr/bin/env python3
"""Build the Scheduled Task prompt from the template and the source allowlist.

The list of outlets comes from the same file the relay enforces: src/newsrelay/sources.toml
(or your own copy, e.g. /etc/newsrelay/sources.toml), so the prompt and the server always agree.

Examples:
    python3 scripts/build_prompt.py                                   # built-in list, English, 18:00
    python3 scripts/build_prompt.py --language German --time 07:00
    python3 scripts/build_prompt.py --country Germany --timezone Europe/Berlin
    python3 scripts/build_prompt.py --sources /etc/newsrelay/sources.toml > my-task-prompt.md

Only the Python standard library is used.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "prompts" / "scheduled-task.template.md"
DEFAULT_SOURCES = ROOT / "src" / "newsrelay" / "sources.toml"
DEFAULT_TOPICS = (
    "AI, tech, cybersecurity, privacy, internet policy, law, politics, war/geopolitics, science, health, "
    "economy, energy, climate, disasters, infrastructure, civil liberties, major world events"
)


def read_sources(path: Path) -> list[str]:
    """Outlet names (with their domains) from a sources.toml allowlist."""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    out: list[str] = []
    for entry in data.get("source", []):
        name = str(entry.get("name", "")).strip()
        domains = [str(d) for d in entry.get("domains", [])]
        if name and domains:
            label = name if name.lower() == domains[0].lower() else f"{name} ({domains[0]})"
            if label not in out:
                out.append(label)
    if not out:
        raise SystemExit(f"no sources found in {path}")
    return out


def build(
    sources: list[str],
    *,
    language: str = "English",
    country: str = "Austria",
    topics: str = DEFAULT_TOPICS,
    timezone: str = "Europe/Vienna",
) -> str:
    text = TEMPLATE.read_text(encoding="utf-8")
    values = {
        "SOURCES": ", ".join(sources),
        "LANGUAGE": language,
        "COUNTRY": country,
        "TOPICS": topics,
        "TIMEZONE": timezone,
    }
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    left = re.findall(r"\{\{[A-Z_]+\}\}", text)
    if left:
        raise SystemExit(f"unfilled placeholders: {left}")
    return text


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sources", type=Path, default=DEFAULT_SOURCES, help="sources.toml allowlist")
    p.add_argument("--language", default="English", help="language of the Discord posts")
    p.add_argument(
        "--country",
        "--region",
        dest="country",
        default="Austria",
        help="country the readers live in; only news that affects it is posted",
    )
    p.add_argument("--topics", default=DEFAULT_TOPICS, help="comma-separated topic scope")
    p.add_argument("--timezone", default="Europe/Vienna", help="IANA timezone used for the daily run key")
    p.add_argument("--time", default="18:00", help="time of day the task runs (shown in the header only)")
    a = p.parse_args(argv)
    body = build(
        read_sources(a.sources), language=a.language, country=a.country, topics=a.topics, timezone=a.timezone
    )
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to a legacy codepage
    sys.stdout.write(
        f"<!-- Schedule this task daily at {a.time} ({a.timezone}). Enable the News Relay app for it. -->\n\n"
        + body
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
