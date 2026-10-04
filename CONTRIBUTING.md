# Contributing

Thanks for helping! Issues and pull requests are welcome — especially translations of the post
layout, dedup test cases, and fixes to outlet domains in `src/newsrelay/sources.toml`.

## Ground rules

- Keep it boring and small: Python standard library + the pinned dependencies, SQLite, systemd.
- Every behaviour change needs a test; bug fixes start with a failing test.
- Never commit secrets, databases, tokens, webhook URLs or personal hostnames.

## Checks (same as CI)

```sh
.venv/bin/pytest -q
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy
```

Changing dependencies: edit `requirements.in` / `requirements-dev.in`, run `sh scripts/lock.sh`,
commit both `.lock` files.

## Security issues

Please report vulnerabilities privately via GitHub's *Report a vulnerability* (Security tab), not in
public issues.
