from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from fakes import LiveServer, free_port
from newsrelay import timeutil
from newsrelay.api.app import create_app
from newsrelay.api.auth import hash_passphrase
from newsrelay.config import OWNER_HASH_CREDENTIAL, WEBHOOK_CREDENTIAL, Config
from newsrelay.database import open_db
from newsrelay.schemas import Candidate, PublishInput, Story


class Clock:
    def __init__(self, start: datetime) -> None:
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw: float) -> None:
        self.t += timedelta(**kw)


@pytest.fixture
def clock() -> Iterator[Clock]:
    c = Clock(datetime(2026, 10, 3, 6, 0, tzinfo=UTC))
    timeutil.set_clock(c)
    yield c
    timeutil.set_clock(None)


# Built-in allowlist plus the neutral domains used by tests.
TEST_EXTRA_SOURCES = """
[[source]]
name = "Test outlets"
domains = ["example.org", "example.com", "heise.de", "cisa.gov", "127.0.0.1"]
"""


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    from importlib import resources

    (tmp_path / "secrets").mkdir()
    builtin = (resources.files("newsrelay") / "sources.toml").read_text(encoding="utf-8")
    (tmp_path / "sources.toml").write_text(builtin + TEST_EXTRA_SOURCES, encoding="utf-8")
    return Config(
        db_path=tmp_path / "newsrelay.db",
        backup_dir=tmp_path / "backups",
        runtime_dir=tmp_path / "run",
        public_base_url="http://127.0.0.1:8787",
        secret_dir_override=tmp_path / "secrets",
        min_send_interval_s=0.0,
        min_free_disk_mb=1,
        allowed_redirect_prefixes=("https://chatgpt.com/connector_platform_oauth_redirect",),
        sources_file=tmp_path / "sources.toml",
    )


@pytest.fixture
def conn(cfg: Config, clock: Clock) -> Iterator[sqlite3.Connection]:
    cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
    c = open_db(cfg.db_path)
    yield c
    c.close()


def candidate(cid: str = "c1", **kw: Any) -> Candidate:
    data: dict[str, Any] = {
        "candidate_id": cid,
        "title": "EU fines Meta 800 million euros over Marketplace antitrust breach",
        "category": "law-regulation",
        "entities": ["European Commission", "Meta", "Facebook Marketplace"],
        "key_facts": [
            "European Commission fined Meta 798 million euros",
            "Fine concerns tying Facebook Marketplace to the social network",
            "Meta says it will appeal the decision",
        ],
        "source_urls": ["https://www.tagesschau.de/wirtschaft/meta-strafe-eu-100.html?utm_source=rss"],
    }
    data.update(kw)
    return Candidate.model_validate(data)


def story(cid: str = "c1", **kw: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "candidate_id": cid,
        "kind": "NEW",
        "category": "law-regulation",
        "headline": "EU fines Meta €798m over Marketplace",
        "body": "The European Commission fined Meta €798 million for tying Facebook Marketplace to its "
        "social network.\n\nWhy it matters: first major EU antitrust fine against Meta.\n\n"
        "Meta says it will appeal.",
        "key_facts": [
            "European Commission fined Meta 798 million euros",
            "Fine concerns tying Facebook Marketplace to the social network",
            "Meta says it will appeal the decision",
        ],
        "entities": ["European Commission", "Meta", "Facebook Marketplace"],
        "impact": "Marketplace may be unbundled from Facebook for EU users.",
        "impact_region": "EU",
        "impact_global": "Sets a precedent for EU antitrust cases against other large platforms.",
        "importance": 5,
        "importance_reason": "First large EU antitrust fine against Meta; few direct effects.",
        "topic_state_summary": "EU fined Meta €798m for Marketplace tying; Meta to appeal.",
        "confidence": "confirmed",
        "sources": [
            {"url": "https://www.tagesschau.de/wirtschaft/meta-strafe-eu-100.html", "name": "Tagesschau"}
        ],
    }
    data.update(kw)
    return data


def publish_input(run_key: str, stories: list[dict[str, Any]], through: str | None = None) -> PublishInput:
    return PublishInput.model_validate(
        {"run_key": run_key, "research_through": through or timeutil.now_iso(), "stories": stories}
    )


def as_story(d: dict[str, Any]) -> Story:
    return Story.model_validate(d)


PASS = "correct-horse-battery-staple-42"
HOOK_SECRET = "https://discord.com/api/webhooks/123456789012345678/" + "S3cr3tT0ken" * 6


@pytest.fixture
def live(cfg, tmp_path):
    timeutil.set_clock(None)
    port = free_port()
    cfg = dataclasses.replace(cfg, listen_port=port, public_base_url=f"http://127.0.0.1:{port}")
    cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
    (cfg.secret_dir_override / OWNER_HASH_CREDENTIAL).write_text(hash_passphrase(PASS))
    (cfg.secret_dir_override / WEBHOOK_CREDENTIAL).write_text(HOOK_SECRET)
    conn = open_db(cfg.db_path)
    app = create_app(cfg, conn)
    srv = LiveServer(app, port)
    (cfg.runtime_dir / "worker.heartbeat").touch()
    yield cfg, srv, conn
    srv.stop()
    conn.close()
