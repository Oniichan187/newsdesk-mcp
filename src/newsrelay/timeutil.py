"""UTC time helpers. All canonical timestamps are ISO-8601 UTC strings ending in 'Z'."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

_now_override: Callable[[], datetime] | None = None


def set_clock(fn: Callable[[], datetime] | None) -> None:
    """Test hook: override the clock."""
    global _now_override
    _now_override = fn


def now() -> datetime:
    if _now_override is not None:
        return _now_override()
    return datetime.now(UTC)


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("naive datetime not allowed as canonical time")
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    return to_iso(now())


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"timestamp without timezone: {value!r}")
    return dt.astimezone(UTC)


def iso_plus(seconds: float) -> str:
    return to_iso(now() + timedelta(seconds=seconds))


def display(value: str | None, tz: str = "Europe/Vienna") -> str:
    if not value:
        return "-"
    return parse_iso(value).astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M %Z")
