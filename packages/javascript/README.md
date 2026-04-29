# JavaScript adapter — placeholder

The JavaScript adapter for the Chipzen SDK lands here in **Phase 2** of
the public-alpha rollout.

When it ships you'll find:

- `src/` — the `Bot` base class, `GameState` / `Action` types, the
  WebSocket client, `validate` CLI, scaffolding.
- `tests/` — unit + integration + protocol-conformance tests
  (vitest).
- `package.json` — publishes as **`@chipzen-ai/bot`** on npm.
- Per-package `README.md` / `CHANGELOG.md`.
- `starters/javascript/` — thin SDK-based starter plus an
  IP-protected Dockerfile recipe (bytecode/SEA multi-stage).

Until then, the raw-WebSocket starter at
[`/starters/javascript/`](../../starters/javascript/) demonstrates the
underlying protocol if you'd like to start exploring.
