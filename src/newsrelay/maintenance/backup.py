"""SQLite-safe online backups (sqlite3 backup API) with daily/weekly/monthly rotation."""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from .. import timeutil
from ..config import Config
from ..database import connect, migrate
from ..logutil import log
from . import integrity

logger = logging.getLogger("newsrelay.backup")

PREFIX = "newsrelay-"


def _snapshot(src: sqlite3.Connection, dest: Path) -> None:
    tmp = dest.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    out = sqlite3.connect(str(tmp))
    try:
        src.backup(out, pages=256)
        out.execute("PRAGMA journal_mode = DELETE")
    finally:
        out.close()
    fd = os.open(tmp, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, dest)
    os.chmod(dest, 0o600)


def _rotate(directory: Path, keep: int) -> list[str]:
    files = sorted(directory.glob(f"{PREFIX}*.db"))
    removed = []
    for f in files[: max(0, len(files) - keep)]:
        f.unlink()
        removed.append(f.name)
    return removed


def run_backup(conn: sqlite3.Connection, cfg: Config) -> dict[str, Any]:
    now = timeutil.now()
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    daily = cfg.backup_dir / "daily"
    weekly = cfg.backup_dir / "weekly"
    monthly = cfg.backup_dir / "monthly"
    for d in (daily, weekly, monthly):
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
    dest = daily / f"{PREFIX}{stamp}.db"
    _snapshot(conn, dest)
    problems = integrity.check_file(dest)
    if problems:
        bad = dest.with_suffix(".db.corrupt")
        os.replace(dest, bad)
        log(logger, logging.CRITICAL, "backup failed integrity check", problems=problems)
        return {"ok": False, "problems": problems}
    iso_year, iso_week, _ = now.isocalendar()
    week_tag = f"{iso_year}W{iso_week:02d}"
    month_tag = now.strftime("%Y%m")
    if not any(week_tag in f.name for f in weekly.glob(f"{PREFIX}*.db")):
        shutil.copy2(dest, weekly / f"{PREFIX}{week_tag}-{stamp}.db")
    if not any(f"{PREFIX}{month_tag}" in f.name for f in monthly.glob(f"{PREFIX}*.db")):
        shutil.copy2(dest, monthly / f"{PREFIX}{month_tag}-{stamp}.db")
    removed = (
        _rotate(daily, cfg.backup_keep_daily)
        + _rotate(weekly, cfg.backup_keep_weekly)
        + _rotate(monthly, cfg.backup_keep_monthly)
    )
    res = {"ok": True, "file": str(dest), "bytes": dest.stat().st_size, "rotated_out": len(removed)}
    log(logger, logging.INFO, "backup done", **res)
    return res


def latest_backup(cfg: Config) -> Path | None:
    files = sorted((cfg.backup_dir / "daily").glob(f"{PREFIX}*.db"))
    return files[-1] if files else None


def restore_test(cfg: Config, backup: Path | None = None) -> dict[str, Any]:
    """Restore the newest backup into a throw-away copy and validate it. Never touches production."""
    backup = backup or latest_backup(cfg)
    if backup is None:
        return {"ok": False, "error": "no backup found"}
    with tempfile.TemporaryDirectory(prefix="newsrelay-restore-") as td:
        copy = Path(td) / "restored.db"
        shutil.copy2(backup, copy)
        conn = connect(copy)
        try:
            version = migrate(conn)
            problems = integrity.check(conn, full=True)
            counts = {
                t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                for t in ("topics", "stories", "story_sources", "runs", "outbox")
            }
            fts_ok = conn.execute("SELECT count(*) FROM topic_fts").fetchone()[0] >= 0
        finally:
            conn.close()
    res = {
        "ok": not problems and fts_ok,
        "backup": backup.name,
        "schema_version": version,
        "problems": problems,
        "counts": counts,
    }
    log(logger, logging.INFO if res["ok"] else logging.CRITICAL, "restore test", **res)
    return res
