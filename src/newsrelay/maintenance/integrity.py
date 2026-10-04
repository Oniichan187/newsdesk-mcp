"""Database integrity checks."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from ..database import SCHEMA_VERSION, connect, current_version
from ..logutil import log

logger = logging.getLogger("newsrelay.integrity")


def check(conn: sqlite3.Connection, *, full: bool) -> list[str]:
    """Return [] when healthy, else a list of problems."""
    pragma = "integrity_check" if full else "quick_check"
    rows = [r[0] for r in conn.execute(f"PRAGMA {pragma}").fetchall()]
    problems = [] if rows == ["ok"] else rows[:20]
    fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    if fk:
        problems.append(f"{len(fk)} foreign key violations")
    v = current_version(conn)
    if v != SCHEMA_VERSION:
        problems.append(f"schema version {v} != expected {SCHEMA_VERSION}")
    return problems


def check_file(path: Path) -> list[str]:
    conn = connect(path, readonly=True)
    try:
        return check(conn, full=True)
    finally:
        conn.close()


def run(conn: sqlite3.Connection, *, full: bool) -> list[str]:
    problems = check(conn, full=full)
    if problems:
        log(logger, logging.CRITICAL, "database integrity problem", problems=problems)
    else:
        log(logger, logging.INFO, "integrity ok", mode="full" if full else "quick")
    return problems
