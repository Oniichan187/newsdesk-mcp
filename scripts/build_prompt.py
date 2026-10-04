#!/usr/bin/env python3
"""Build your personal Scheduled Task prompt from the template.

Examples:
    python3 scripts/build_prompt.py                                   # default: DACH quality press
    python3 scripts/build_prompt.py --sources examples/sources/international.txt --language English \
        --region "Europe and North America" --time 07:00 --timezone Europe/London
    python3 scripts/build_prompt.py --sources my-sources.txt > my-task-prompt.txt

A sources file has one outlet per line; blank lines and lines starting with '#' are ignored.
Only the Python standard library is used.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "prompts" / "scheduled-task.template.md"
DEFAULT_SOURCES = ROOT / "examples" / "sources" / "dach-quality-press.txt"
DEFAULT_TOPICS = (
    "AI, tech, cybersecurity, privacy, internet policy, law, politics, war/geopolitics, science, health, "
    "economy, energy, climate, disasters, infrastructure, civil liberties, major world events"
)


def read_sources(path: Path) -> list[str]:
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and line not in out:
            out.append(line)
    if not out:
        raise SystemExit(f"no sources found in {path}")
    return out


def build(
    sources: list[str],
    *,
    language: str = "English",
    region: str = "Austria/Europe",
    topics: str = DEFAULT_TOPICS,
    timezone: str = "Europe/Vienna",
) -> str:
    text = TEMPLATE.read_text(encoding="utf-8")
    values = {
        "SOURCES": ", ".join(sources),
        "LANGUAGE": language,
        "REGION": region,
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
    p.add_argument("--sources", type=Path, default=DEFAULT_SOURCES, help="file with one outlet per line")
    p.add_argument("--language", default="English", help="language of the Discord posts")
    p.add_argument("--region", default="Austria/Europe", help="whose perspective decides importance")
    p.add_argument("--topics", default=DEFAULT_TOPICS, help="comma-separated topic scope")
    p.add_argument("--timezone", default="Europe/Vienna", help="IANA timezone used for the daily run key")
    p.add_argument("--time", default="18:00", help="time of day the task runs (shown in the header only)")
    a = p.parse_args(argv)
    body = build(
        read_sources(a.sources), language=a.language, region=a.region, topics=a.topics, timezone=a.timezone
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
