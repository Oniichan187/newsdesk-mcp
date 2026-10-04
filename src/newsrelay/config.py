"""Configuration loading.

Non-secret settings live in /etc/newsrelay/config.toml. Secrets are read from the systemd
credentials directory ($CREDENTIALS_DIRECTORY, populated via LoadCredential=) and are never
part of the Config object's repr.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path("/etc/newsrelay/config.toml")
FALLBACK_SECRET_DIR = Path("/etc/newsrelay/secrets")

WEBHOOK_CREDENTIAL = "discord_webhook"
BOT_TOKEN_CREDENTIAL = "discord_bot_token"  # noqa: S105 (credential file name, not a secret)
OWNER_HASH_CREDENTIAL = "owner_passphrase_hash"


@dataclass(frozen=True)
class Config:
    db_path: Path = Path("/var/lib/newsrelay/newsrelay.db")
    backup_dir: Path = Path("/var/lib/newsrelay/backups")
    runtime_dir: Path = Path("/run/newsrelay")
    listen_host: str = "127.0.0.1"
    listen_port: int = 8787
    # Public HTTPS base URL (Tailscale Funnel hostname). Used as OAuth issuer + MCP resource.
    public_base_url: str = "http://127.0.0.1:8787"
    extra_allowed_hosts: tuple[str, ...] = ()
    # OAuth redirect URIs allowed for dynamically registered clients (prefix match).
    allowed_redirect_prefixes: tuple[str, ...] = (
        "https://chatgpt.com/connector_platform_oauth_redirect",
        "https://chatgpt.com/connector/oauth/",
    )
    access_token_ttl_s: int = 3600
    refresh_token_ttl_days: int = 180
    # Research checkpoint behaviour
    overlap_hours: float = 6.0
    initial_lookback_hours: float = 36.0
    # Long outages are caught up in chronological windows of at most this size; the checkpoint
    # never jumps over time that was not researched.
    catchup_window_days: float = 7.0
    # Discord
    # Bot mode (preferred): token comes from the discord_bot_token credential, channel id is public.
    discord_channel_id: str = ""
    discord_username: str = "News Update"  # webhook mode only
    # Bot shown as online via a Gateway session (newsrelay-presence.service); purely cosmetic.
    discord_presence: bool = True
    discord_presence_status: str = "online"  # online | idle | dnd
    discord_presence_activity_type: str = "watching"  # playing | listening | watching | competing
    discord_presence_text: str = "the news"
    max_message_chars: int = 1850
    suppress_link_embeds: bool = True
    min_send_interval_s: float = 2.5
    worker_poll_s: float = 15.0
    max_5xx_attempts: int = 5
    max_429_attempts: int = 15
    # Retention
    body_retention_days: int = 180
    attempt_retention_days: int = 30
    dormant_after_days: int = 21
    archive_after_days: int = 120
    backup_keep_daily: int = 14
    backup_keep_weekly: int = 8
    backup_keep_monthly: int = 12
    min_free_disk_mb: int = 200
    display_timezone: str = "Europe/Vienna"
    secret_dir_override: Path | None = field(default=None)
    # Optional local replacement for the built-in source allowlist (newsrelay/sources.toml).
    sources_file: Path | None = Path("/etc/newsrelay/sources.toml")

    @property
    def mcp_resource_url(self) -> str:
        return self.public_base_url.rstrip("/") + "/mcp"

    @property
    def public_host(self) -> str:
        from urllib.parse import urlsplit

        return urlsplit(self.public_base_url).netloc


# Keys accepted (and ignored) for compatibility with configs written by older versions.
DEPRECATED_KEYS = {"max_lookback_days", "stale_run_hours", "max_pending_age_hours"}
_PATH_FIELDS = {"db_path", "backup_dir", "runtime_dir", "secret_dir_override", "sources_file"}
_TUPLE_FIELDS = {"extra_allowed_hosts", "allowed_redirect_prefixes"}


def load_config(path: Path | None = None) -> Config:
    path = path or Path(os.environ.get("NEWSRELAY_CONFIG", str(DEFAULT_CONFIG_PATH)))
    data: dict[str, Any] = {}
    if path.exists():
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    for key in DEPRECATED_KEYS & set(data):
        data.pop(key)
    known = Config.__dataclass_fields__
    unknown = set(data) - set(known)
    if unknown:
        raise ValueError(f"unknown config keys in {path}: {sorted(unknown)}")
    kwargs: dict[str, Any] = {}
    for key, value in data.items():
        if key in _PATH_FIELDS:
            value = Path(value)
        elif key in _TUPLE_FIELDS:
            value = tuple(value)
        kwargs[key] = value
    return Config(**kwargs)


def _secret_dirs(cfg: Config) -> list[Path]:
    dirs: list[Path] = []
    if cfg.secret_dir_override:
        dirs.append(cfg.secret_dir_override)
    cred = os.environ.get("CREDENTIALS_DIRECTORY")
    if cred:
        dirs.append(Path(cred))
    dirs.append(FALLBACK_SECRET_DIR)
    return dirs


def read_secret(cfg: Config, name: str) -> str | None:
    """Return a secret's content (stripped) or None. Never logs the value."""
    for d in _secret_dirs(cfg):
        p = d / name
        try:
            value = p.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, PermissionError, NotADirectoryError):
            continue
        if value:
            return value
    return None
