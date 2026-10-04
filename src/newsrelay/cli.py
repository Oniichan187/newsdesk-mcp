"""Command-line entry point: services, maintenance and operator tools."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import __version__, timeutil
from .config import (
    BOT_TOKEN_CREDENTIAL,
    FALLBACK_SECRET_DIR,
    OWNER_HASH_CREDENTIAL,
    WEBHOOK_CREDENTIAL,
    Config,
    load_config,
    read_secret,
)
from .database import connect, current_version, migrate, open_db
from .logutil import log, setup_logging

logger = logging.getLogger("newsrelay.cli")

ROOT_ONLY = {"set-webhook", "set-bot-token", "set-passphrase", "test-webhook", "test-discord"}


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def _write_secret(name: str, value: str) -> Path:
    FALLBACK_SECRET_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(FALLBACK_SECRET_DIR, 0o700)
    target = FALLBACK_SECRET_DIR / name
    tmp = target.with_suffix(".new")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(value + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, target)
    return target


# --------------------------------------------------------------------------- services


def _local_get(cfg: Config, path: str, timeout: float = 10.0) -> int | None:
    """HTTP status of a local endpoint, or None if unreachable."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{cfg.listen_port}{path}", timeout=timeout) as r:
            return int(r.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except OSError:
        return None


def cmd_api(cfg: Config, _a: argparse.Namespace) -> int:
    """API service. READY=1 only once /readyz answers 200 through the real listener; WATCHDOG=1 only
    while /livez answers (a hung event loop cannot answer, so systemd restarts the service)."""
    import uvicorn

    from .api.app import create_app
    from .sdnotify import notify

    app = create_app(cfg)  # opens DB, validates/migrates schema, initialises OAuth
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=cfg.listen_host,
            port=cfg.listen_port,
            log_config=None,
            access_log=False,
            proxy_headers=True,
            forwarded_allow_ips="127.0.0.1",
            server_header=False,
            timeout_keep_alive=30,
            limit_concurrency=32,
        )
    )

    def supervisor() -> None:
        while not server.started:
            if server.should_exit:
                return
            time.sleep(0.2)
        deadline = time.monotonic() + 60
        while _local_get(cfg, "/readyz") != 200:
            if server.should_exit:
                return
            if time.monotonic() > deadline:
                log(logger, logging.ERROR, "api never became ready; exiting")
                server.should_exit = True
                return
            time.sleep(1)
        notify("READY=1")
        while not server.should_exit:
            if _local_get(cfg, "/livez") == 200:
                notify("WATCHDOG=1")
            time.sleep(20)

    threading.Thread(target=supervisor, daemon=True, name="supervisor").start()
    log(logger, logging.INFO, "api starting", version=__version__, port=cfg.listen_port)
    server.run()
    notify("STOPPING=1")
    return 0 if server.started else 1


def cmd_worker(cfg: Config, _a: argparse.Namespace) -> int:
    from .publishing import worker

    return worker.main(cfg)


def cmd_migrate(cfg: Config, a: argparse.Namespace) -> int:
    path = Path(a.db) if a.db else cfg.db_path
    conn = connect(path)
    try:
        v = migrate(conn)
    finally:
        conn.close()
    log(logger, logging.INFO, "migrations applied", schema_version=v, db=str(path))
    print(f"schema_version={v}")
    return 0


def cmd_upgrade_check(cfg: Config, a: argparse.Namespace) -> int:
    """Migrate a COPY of the production DB with this release and smoke-test it. Never touches prod."""
    import dataclasses

    from . import service
    from .maintenance import integrity
    from .schemas import BeginRunInput, MatchInput

    path = Path(a.db_copy)
    conn = connect(path)
    try:
        before = current_version(conn)
        after = migrate(conn)
        problems = integrity.check(conn, full=True)
        test_cfg = dataclasses.replace(cfg, db_path=path)
        service.begin_run(conn, test_cfg, BeginRunInput(run_key="upgrade-check/2000-01-01"))
        service.match_candidates(
            conn,
            MatchInput.model_validate(
                {
                    "run_key": "upgrade-check/2000-01-01",
                    "candidates": [
                        {
                            "candidate_id": "u1",
                            "title": "Upgrade smoke test",
                            "category": "other",
                            "key_facts": ["upgrade smoke test fact"],
                            "source_urls": ["https://example.org/upgrade-check"],
                        }
                    ],
                }
            ),
        )
        service.health(conn, test_cfg)
    finally:
        conn.close()
    res = {"ok": not problems, "schema_before": before, "schema_after": after, "problems": problems}
    _print(res)
    return 0 if res["ok"] else 2


def cmd_maintenance(cfg: Config, a: argparse.Namespace) -> int:
    from .maintenance import backup, cleanup, integrity

    conn = open_db(cfg.db_path)
    rc = 0
    # Backup first, so pruning never runs before a verified snapshot exists.
    b = backup.run_backup(conn, cfg)
    if not b["ok"]:
        rc = 2
    full = a.full or timeutil.now().weekday() == 6
    problems = integrity.run(conn, full=full)
    if problems:
        print("INTEGRITY PROBLEMS:", problems, file=sys.stderr)
        return 3  # never prune a possibly corrupt database
    c = cleanup.run_cleanup(conn, cfg)
    if timeutil.now().day == 1 or a.full:
        r = backup.restore_test(cfg)
        if not r["ok"]:
            rc = 4
    _print({"backup": b, "integrity": "ok", "cleanup": c})
    return rc


def cmd_backup(cfg: Config, _a: argparse.Namespace) -> int:
    from .maintenance import backup

    res = backup.run_backup(open_db(cfg.db_path), cfg)
    _print(res)
    return 0 if res["ok"] else 2


def cmd_restore_test(cfg: Config, a: argparse.Namespace) -> int:
    from .maintenance import backup

    res = backup.restore_test(cfg, Path(a.file) if a.file else None)
    _print(res)
    return 0 if res["ok"] else 2


def cmd_status(cfg: Config, _a: argparse.Namespace) -> int:
    """One-screen operator view (no secrets)."""
    from . import service
    from .maintenance import backup

    conn = connect(cfg.db_path, readonly=True)
    h = service.health(conn, cfg)
    api_ready = _local_get(cfg, "/readyz") == 200
    latest = backup.latest_backup(cfg)
    db_bytes = sum(f.stat().st_size for f in cfg.db_path.parent.glob(cfg.db_path.name + "*") if f.is_file())
    out = {
        "ok": bool(h["ok"]) and api_ready,
        "version": h["version"],
        "schema_version": h["schema_version"],
        "api_ready": api_ready,
        "worker_alive": h["worker_alive"],
        "worker_heartbeat_age_s": h["worker_heartbeat_age_s"],
        "discord_mode": h["discord_mode"],
        "discord_configured": h["discord_configured"],
        "discord_status": h["discord_status"],
        "research_checkpoint": h["last_checkpoint"],
        "delivery": h["delivery"],
        "db_bytes": db_bytes,
        "latest_backup": latest.name if latest else None,
        "public_endpoint": cfg.mcp_resource_url,
        "counts": {
            t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("topics", "stories", "runs", "outbox")
        },
        "last_runs": [
            dict(r)
            for r in conn.execute(
                "SELECT run_key, status, research_through, accepted_count FROM runs "
                "ORDER BY completed_at DESC LIMIT 5"
            )
        ],
        "server_time": h["server_time"],
    }
    conn.close()
    _print(out)
    return 0 if out["ok"] else 1


def cmd_healthcheck(cfg: Config, a: argparse.Namespace) -> int:
    """Local probe used by newsrelay-healthcheck.timer; restarts the failing unit after 3 strikes."""
    import subprocess

    from .publishing.worker import heartbeat_age

    state = cfg.runtime_dir / "healthcheck.failures"
    problems: dict[str, str] = {}
    ready = _local_get(cfg, "/readyz", timeout=15)
    if ready is None:
        problems["newsrelay-api.service"] = "api unreachable"
    elif ready != 200:
        problems["newsrelay-api.service"] = f"api not ready (HTTP {ready})"
    age = heartbeat_age(cfg.runtime_dir)
    if age is None or age > max(120.0, cfg.worker_poll_s * 6):
        problems["newsrelay-worker.service"] = (
            "worker heartbeat missing" if age is None else "worker heartbeat stale"
        )
    if not problems:
        state.write_text("0")
        return 0
    try:
        failures = int(state.read_text()) + 1
    except (FileNotFoundError, ValueError):
        failures = 1
    state.write_text(str(failures))
    log(logger, logging.WARNING, "healthcheck failed", problems=problems, consecutive=failures)
    if a.restart and failures >= 3:
        for unit in problems:
            log(logger, logging.ERROR, "restarting unit after repeated health failures", unit=unit)
            # reset-failed: a unit stuck in "start-limit-hit" would otherwise stay down for good.
            subprocess.run(["/usr/bin/systemctl", "reset-failed", unit], check=False)  # noqa: S603
            subprocess.run(["/usr/bin/systemctl", "restart", unit], check=False)  # noqa: S603
        state.write_text("0")
    return 1


# --------------------------------------------------------------------------- operator tools (root)


def cmd_set_webhook(cfg: Config, _a: argparse.Namespace) -> int:
    from .publishing.discord import validate_webhook_url

    if sys.stdin.isatty():
        url = getpass.getpass("Discord webhook URL (input hidden): ").strip()
    else:
        url = sys.stdin.readline().strip()
    if not validate_webhook_url(url):
        print("Rejected: not a valid https://discord.com/api/webhooks/<id>/<token> URL.", file=sys.stderr)
        return 2
    _write_secret(WEBHOOK_CREDENTIAL, url)
    print(
        "Webhook stored (root-only file, passed to the worker as a systemd credential).\n"
        "Apply: sudo systemctl restart newsrelay-worker\n"
        "Items that failed while the old webhook was broken: sudo newsrelay outbox requeue-failed --yes"
    )
    return 0


def cmd_set_passphrase(cfg: Config, a: argparse.Namespace) -> int:
    from .api.auth import hash_passphrase

    if read_secret(cfg, OWNER_HASH_CREDENTIAL) and not a.force:
        print("A passphrase is already set; use --force to replace it.", file=sys.stderr)
        return 2
    if a.generate:
        words = secrets.token_urlsafe(18)
        passphrase = "-".join(words[i : i + 6] for i in range(0, 24, 6))
    else:
        passphrase = getpass.getpass("New owner passphrase (min 16 chars): ")
        if len(passphrase) < 16:
            print("Too short.", file=sys.stderr)
            return 2
    _write_secret(OWNER_HASH_CREDENTIAL, hash_passphrase(passphrase))
    if a.generate:
        print(f"OWNER PASSPHRASE (shown once, store it in your password manager): {passphrase}")
    print("Hash stored. Restart newsrelay-api to apply.")
    return 0


def cmd_set_bot_token(cfg: Config, _a: argparse.Namespace) -> int:
    from .publishing.discord import validate_bot_token

    if sys.stdin.isatty():
        token = getpass.getpass("Discord bot token (input hidden): ").strip()
    else:
        token = sys.stdin.readline().strip()
    if not validate_bot_token(token):
        print("Rejected: does not look like a Discord bot token.", file=sys.stderr)
        return 2
    _write_secret(BOT_TOKEN_CREDENTIAL, token)
    print(
        "Bot token stored (root-only, passed to the worker as a systemd credential).\n"
        "Set discord_channel_id in /etc/newsrelay/config.toml, then: sudo systemctl restart newsrelay-worker"
    )
    return 0


def cmd_test_discord(cfg: Config, a: argparse.Namespace) -> int:
    """Post one clearly marked test message through the configured transport and delete it again."""
    from .publishing.discord import Outcome
    from .publishing.worker import make_transport, transport_mode

    t = make_transport(cfg)
    if t is None:
        print("No Discord bot (token + discord_channel_id) or webhook configured.", file=sys.stderr)
        return 2
    stamp = timeutil.now_iso()
    res = t.send(
        {
            "content": f"newsrelay {transport_mode(t)} test {stamp} – this message deletes itself @everyone",
            "username": cfg.discord_username,
            "allowed_mentions": {"parse": []},
            "flags": 4,
        },
        key=f"test{int(time.time())}",
    )
    out: dict[str, Any] = {
        "mode": transport_mode(t),
        "send": res.outcome.value,
        "http_status": res.http_status,
        "message_id_returned": bool(res.message_id),
    }
    if res.error:
        out["error"] = res.error
    if res.outcome is Outcome.DELIVERED and res.message_id and not a.keep:
        time.sleep(3)
        out["delete_http_status"] = t.delete_message(res.message_id)  # type: ignore[attr-defined]
    t.close()
    _print(out)
    return 0 if res.outcome is Outcome.DELIVERED else 1


def cmd_outbox(cfg: Config, a: argparse.Namespace) -> int:
    from .publishing import outbox

    conn = open_db(cfg.db_path)
    if a.action == "list":
        rows = conn.execute(
            "SELECT id, batch_id, seq, state, attempts, next_attempt_at, last_error, discord_message_id "
            "FROM outbox WHERE state != 'delivered' OR ? ORDER BY id DESC LIMIT 50",
            (a.all,),
        ).fetchall()
        _print([dict(r) for r in rows])
        return 0
    if a.action == "requeue-failed":
        if not a.yes:
            print(
                "This re-sends every item in state 'failed'. Re-run with --yes to confirm.", file=sys.stderr
            )
            return 2
        print(f"requeued {outbox.requeue_failed(conn)} failed item(s)")
        return 0
    new = outbox.resolve(conn, a.id, a.action)
    print(f"outbox item {a.id} -> {new}")
    return 0


def cmd_revoke_tokens(cfg: Config, _a: argparse.Namespace) -> int:
    from .api.auth import SqliteOAuthProvider

    n = SqliteOAuthProvider(open_db(cfg.db_path), cfg, None).revoke_all()
    print(f"revoked {n} tokens; ChatGPT must re-authorize")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="newsrelay", description=f"newsrelay {__version__}")
    p.add_argument("--config", help="config file (default /etc/newsrelay/config.toml)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("api")
    sub.add_parser("worker")
    mg = sub.add_parser("migrate")
    mg.add_argument("--db", help="migrate this database file instead of the configured one")
    uc = sub.add_parser("upgrade-check")
    uc.add_argument("db_copy", help="path to a COPY of the production database")
    m = sub.add_parser("maintenance")
    m.add_argument("--full", action="store_true")
    sub.add_parser("backup")
    rt = sub.add_parser("restore-test")
    rt.add_argument("file", nargs="?")
    sub.add_parser("status")
    hc = sub.add_parser("healthcheck")
    hc.add_argument("--restart", action="store_true")
    sub.add_parser("set-webhook")
    sp = sub.add_parser("set-passphrase")
    sp.add_argument("--generate", action="store_true")
    sp.add_argument("--force", action="store_true")
    sub.add_parser("set-bot-token")
    for name in ("test-discord", "test-webhook"):
        tw = sub.add_parser(name)
        tw.add_argument("--keep", action="store_true")
    ob = sub.add_parser("outbox")
    ob.add_argument("action", choices=["list", "delivered", "resend", "requeue", "cancel", "requeue-failed"])
    ob.add_argument("id", nargs="?", type=int)
    ob.add_argument("--all", action="store_true")
    ob.add_argument("--yes", action="store_true")
    sub.add_parser("revoke-tokens")
    sub.add_parser("version")
    a = p.parse_args(argv)
    setup_logging(os.environ.get("NEWSRELAY_LOG_LEVEL", "INFO"))
    if a.cmd == "version":
        print(__version__)
        return 0
    cfg = load_config(Path(a.config) if a.config else None)
    if a.cmd in ROOT_ONLY and os.geteuid() != 0:
        print(f"'{a.cmd}' must run as root (sudo).", file=sys.stderr)
        return 2
    if a.cmd not in ROOT_ONLY and os.geteuid() == 0 and a.cmd != "healthcheck":
        print(
            f"'{a.cmd}' must not run as root (it would create root-owned DB files). "
            f"Use: sudo -u newsrelay newsrelay {a.cmd}",
            file=sys.stderr,
        )
        return 2
    if a.cmd == "outbox" and a.action not in ("list", "requeue-failed") and a.id is None:
        p.error("outbox delivered|resend|cancel needs an id")
    handlers = {
        "api": cmd_api,
        "worker": cmd_worker,
        "migrate": cmd_migrate,
        "maintenance": cmd_maintenance,
        "backup": cmd_backup,
        "upgrade-check": cmd_upgrade_check,
        "restore-test": cmd_restore_test,
        "status": cmd_status,
        "healthcheck": cmd_healthcheck,
        "set-webhook": cmd_set_webhook,
        "set-passphrase": cmd_set_passphrase,
        "test-webhook": cmd_test_discord,
        "test-discord": cmd_test_discord,
        "set-bot-token": cmd_set_bot_token,
        "outbox": cmd_outbox,
        "revoke-tokens": cmd_revoke_tokens,
    }
    return handlers[a.cmd](cfg, a)


if __name__ == "__main__":
    sys.exit(main())
