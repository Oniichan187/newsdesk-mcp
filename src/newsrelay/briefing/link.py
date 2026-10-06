"""Public address of the RSVP reader, as linked under the briefing PDF."""

from __future__ import annotations

import json
import urllib.request

from ..config import Config


def reader_link(cfg: Config, timeout: float = 2.0) -> str:
    """`reader_url` if set, else the current Cloudflare quick-tunnel address, else "" (no link)."""
    if cfg.reader_url:
        return cfg.reader_url
    host_port = cfg.reader_tunnel_metrics.strip()
    if not host_port or not host_port.startswith(("127.0.0.1:", "localhost:", "[::1]:")):
        return ""  # loopback only: the relay must never fetch arbitrary addresses
    try:
        with urllib.request.urlopen(f"http://{host_port}/quicktunnel", timeout=timeout) as resp:
            host = str(json.load(resp).get("hostname") or "")
    except (OSError, ValueError):
        return ""
    if not host or "/" in host or not host.endswith(".trycloudflare.com"):
        return ""
    return f"https://{host}"
