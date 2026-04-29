# Python adapter — placeholder

The Python adapter for the Chipzen SDK lands here in **Phase 1** of
the public-alpha rollout.

When it ships you'll find:

- `src/chipzen/` — the `Bot` base class, `GameState` / `Action` /
  `Card` models, the WebSocket client, the `validate` and `init`
  CLI commands, and bot examples.
- `tests/` — unit + integration + protocol-conformance tests.
- `pyproject.toml` — publishes as **`chipzen-bot`** on PyPI.
- `Makefile` and per-package `README.md` / `QUICKSTART.md` /
  `CHANGELOG.md` / `IP-PROTECTION.md`.
- `starters/python/` — the thin SDK-based starter (replaces the
  raw-WebSocket starter currently at the repo root under
  `starters/python/`) plus an IP-protected multi-stage Dockerfile
  recipe (Cython compile to `.so`).

Until then, the raw-WebSocket starter at
[`/starters/python/`](../../starters/python/) demonstrates the
underlying protocol if you'd like to start exploring.
