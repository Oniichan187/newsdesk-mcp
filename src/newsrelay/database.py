"""SQLite access: connection setup, transactions, migrations, disk-space guard."""

from __future__ import annotations

import logging
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

from . import timeutil

log = logging.getLogger("newsrelay.db")

SCHEMA_VERSION = 3


class DiskFullError(RuntimeError):
    pass


def connect(path: Path | str, *, readonly: bool = False) -> sqlite3.Connection:
    """Open a connection with safe pragmas. Autocommit mode; use `tx()` for transactions."""
    if readonly:
        conn = sqlite3.connect(
            f"file:{path}?mode=ro", uri=True, isolation_level=None, check_same_thread=False, timeout=10
        )
    else:
        conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA foreign_keys = ON")
    if not readonly:
        if conn.execute("PRAGMA page_count").fetchone()[0] == 0:
            # Fresh file: must precede WAL/table creation; lets maintenance reclaim pages gradually.
            conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        conn.execute("PRAGMA journal_mode = WAL")
        # FULL: fsync WAL on every commit. Write volume is tiny; durability matters more.
        conn.execute("PRAGMA synchronous = FULL")
    return conn


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE transaction: takes the write lock up front, so check-then-insert is atomic."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def check_disk(path: Path, min_free_mb: int) -> None:
    """Fail closed when the filesystem holding the DB is nearly full."""
    target = path if path.exists() else path.parent
    free_mb = shutil.disk_usage(target).free // (1024 * 1024)
    if free_mb < min_free_mb:
        raise DiskFullError(f"only {free_mb} MiB free (< {min_free_mb}); refusing writes")


def _migration_scripts() -> list[tuple[int, str]]:
    pkg = resources.files("newsrelay") / "migrations"
    out: list[tuple[int, str]] = []
    for entry in pkg.iterdir():
        name = entry.name
        if name.endswith(".sql") and name[:4].isdigit():
            out.append((int(name[:4]), entry.read_text(encoding="utf-8")))
    return sorted(out)


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if not row:
        return 0
    v = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    return int(v or 0)


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations atomically. Returns resulting schema version."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    have = current_version(conn)
    for version, sql in _migration_scripts():
        if version <= have:
            continue
        log.info("applying migration", extra={"fields": {"version": version}})
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _split_sql(sql):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, timeutil.now_iso()),
            )
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    final = current_version(conn)
    if final > SCHEMA_VERSION:
        raise RuntimeError(f"database schema {final} is newer than this code ({SCHEMA_VERSION})")
    return final


def _split_sql(script: str) -> list[str]:
    statements: list[str] = []
    buf: list[str] = []
    for line in script.splitlines():
        stripped = line.strip()
        if stripped.startswith("--") or not stripped:
            continue
        buf.append(line)
        candidate = "\n".join(buf)
        if stripped.endswith(";") and sqlite3.complete_statement(candidate):
            statements.append(candidate)
            buf = []
    if "".join(buf).strip():
        statements.append("\n".join(buf))
    return statements


def open_db(path: Path | str) -> sqlite3.Connection:
    conn = connect(path)
    migrate(conn)
    return conn
