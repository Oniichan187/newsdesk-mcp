# Verification record

Date: 2026-10-04 (UTC evening of 2026-10-03). Release: **newsrelay 1.1.0**, schema 1.

## Platform (target, measured)

| Item | Value |
|---|---|
| Hardware | Raspberry Pi 4 Model B Rev 1.2, 3.7 GiB RAM, 59 GB SD card (ext4) |
| OS | Debian GNU/Linux 13 (trixie), kernel 6.18.50+rpt-rpi-v8, aarch64 |
| Python / SQLite | 3.13.5 / 3.46.1 (FTS5 available) |
| systemd / Tailscale | 257 / 1.102.4 |
| Runtime deps | mcp 2.3.0, mcp-types 2.3.0, uvicorn 0.54.0, starlette 1.7.0, pydantic 2.13.5, httpx2 2.13.1, httpcore2 2.13.1, rapidfuzz 3.14.6 (+ transitive, see requirements.lock) |

## Static checks and tests (clean `git archive` checkout, fresh venv from the locks, on the Pi)

| Check | Result |
|---|---|
| `pytest` | 161 passed, 0 failed |
| `ruff check`, `ruff format --check` | clean |
| `mypy --strict` (src) | clean |
| `python -W error -m compileall` | clean |
| `sh -n` on all scripts | clean |

Coverage highlights: URL/Unicode normalization (+ property tests), dedup incl. adversarial cases
(shared entities, recurring yearly events, negation, changed numbers, vulnerability → exploitation,
allegation → confirmation, syndicated/tracking URLs, archived topics), idempotent/concurrent publish,
checkpoint windows for 3/20/90-day outages, outbox (429, 5xx, unsent, write error, read timeout,
crash mid-dispatch, revoked + restored webhook, nothing dropped by age, retention never prunes
unresolved items), worker READY ordering / lock and DB start failures / frozen loop stops watchdog /
missing and stale heartbeat, migrations (failing migration leaves DB untouched, old code refuses newer
schema, upgrade-check on copy), HTTP (oversized Content-Length and chunked → 413, malformed JSON,
unknown tool arguments, expired/foreign-audience tokens), OAuth (DCR redirect rules, PKCE, single-use
codes, refresh rotation, revocation, consent lockout, `iss`), mention suppression, secret redaction,
no outbound fetch of caller URLs (TCP canary).

## Dependency lock

- `requirements.lock` regenerated with pip-tools 7.6.1 from `requirements.in`, PyPI only, 546
  SHA-256 hashes (all distributions).
- Clean install on the Pi (aarch64): `pip install --require-hashes --only-binary=:all:` OK, `pip check` OK.
- x86_64 Linux: `pip download --platform manylinux… --require-hashes` resolved and verified all 29
  packages. (Windows is not a target: `mcp` pulls `pywin32` there.)

## Production checks on the Pi

| Check | Result |
|---|---|
| Upgrade 1.0.0 → 1.1.0 via installer | rehearsal on copy OK, snapshot, migration, healthy |
| Failing migration (scenario A) | aborted before any change; release, schema, services unchanged |
| Schema change + broken API (scenario B) | rolled back code **and** DB snapshot (schema 2 → 1), previous release healthy |
| Installer re-run | config, secrets, passphrase, DB, checkpoint preserved |
| SIGSTOP api | "Watchdog timeout (limit 2min)" → killed → restarted |
| SIGSTOP worker | "Watchdog timeout (limit 3min)" → killed → restarted; READY logged after recovery |
| Second worker instance | refused (exit 1, lock) |
| Worker with unopenable DB | exit 1 before READY |
| Missing heartbeat | healthcheck: 3 strikes → worker restarted |
| Start-limit-hit (found during testing) | fixed: installer/healthcheck run `reset-failed` before restarts |
| `systemd-analyze security` | api 1.1, worker 1.3, maintenance 1.0 (OK) |
| Maintenance unit + restore test | success; latest backup opens, integrity ok |
| Public endpoint (via Funnel IP) | `/healthz` `/readyz` `/livez` → `{"ok":true}`; `/mcp` unauth → 401; 1 MB chunked → 413; AS metadata with `iss` support |
| Production OAuth + MCP probe | DCR → consent → PKCE token → initialize → tools/list (6) → read-only tools OK → revocation OK |
| Secret scan | owner passphrase not in journal/files |
| Resources (idle) | api ≈ 76 MiB RSS, worker ≈ 27 MiB RSS |
| Reboot (after final deploy) | back in ~70 s: NTP synced, tailscaled/api/worker/timers active, 0 restarts, credentials loaded, Funnel config persisted, DB quick_check ok, status ok; public Funnel ingress fully reachable ~3 min after boot (Tailscale-side warm-up; one of three ingress IPs answered immediately) |

## Not verified (requires the owner)

- **Discord**: no webhook configured yet → real test message + delete not yet performed.
- **ChatGPT**: interactive connection, interactive write call, and an unattended Scheduled Task run
  have not been performed (requires the owner's ChatGPT UI; browser automation deliberately not used).
