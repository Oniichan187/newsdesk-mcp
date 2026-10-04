"""Source allowlist: which outlets may be cited in published stories.

The default list ships with the code (``newsrelay/sources.toml``). An installation can replace it
with ``/etc/newsrelay/sources.toml`` (path configurable) without changing code.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Outlet:
    name: str
    domains: tuple[str, ...]


@dataclass(frozen=True)
class Allowlist:
    outlets: tuple[Outlet, ...]
    origin: str

    def outlet_for(self, url: str) -> Outlet | None:
        """The outlet a URL belongs to: exact domain or a subdomain of it (not lookalikes)."""
        try:
            host = (urlsplit(url).hostname or "").lower().rstrip(".")
        except ValueError:
            return None
        if not host:
            return None
        for outlet in self.outlets:
            for domain in outlet.domains:
                if host == domain or host.endswith("." + domain):
                    return outlet
        return None

    def is_allowed(self, url: str) -> bool:
        return self.outlet_for(url) is not None

    def names(self) -> list[str]:
        return [o.name for o in self.outlets]


def parse(text: str, origin: str) -> Allowlist:
    data = tomllib.loads(text)
    entries = data.get("source")
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{origin}: no [[source]] entries")
    outlets: list[Outlet] = []
    for i, entry in enumerate(entries):
        name = str(entry.get("name", "")).strip()
        domains = tuple(
            str(d).strip().lower().lstrip(".") for d in entry.get("domains", []) if str(d).strip()
        )
        if not name or not domains:
            raise ValueError(f"{origin}: source #{i + 1} needs a name and at least one domain")
        for d in domains:
            if "/" in d or ":" in d or "." not in d or " " in d:
                raise ValueError(f"{origin}: invalid domain {d!r} (use e.g. 'example.org')")
        unknown = set(entry) - {"name", "domains"}
        if unknown:
            raise ValueError(f"{origin}: unknown keys {sorted(unknown)} in source {name!r}")
        outlets.append(Outlet(name, domains))
    return Allowlist(tuple(outlets), origin)


@lru_cache(maxsize=8)
def _load(path_str: str) -> Allowlist:
    path = Path(path_str)
    if path_str and path.is_file():
        return parse(path.read_text(encoding="utf-8"), str(path))
    default = resources.files("newsrelay") / "sources.toml"
    return parse(default.read_text(encoding="utf-8"), "built-in sources.toml")


def load_allowlist(path: Path | None) -> Allowlist:
    """Override file if it exists, otherwise the list shipped with the code."""
    return _load(str(path) if path else "")
