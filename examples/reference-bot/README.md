# Reference bot (`reference-bot`)

A non-trivial **demonstration** Chipzen bot built on top of the
public `chipzen-bot` SDK. ~250 lines of plain, readable Python that
shows off everything the protocol exposes:

- Per-match state via `on_match_start` (seat assignment).
- Per-hand state via `on_round_start` (resets per-hand trackers).
- Live observation via `on_turn_result` (counts opponent aggression
  in the current hand).
- Branching on `state.phase` for preflop vs. postflop.
- Heuristic preflop bucketing using `state.hole_cards` (`premium` /
  `strong` / `medium` / `weak`).
- Made-hand class detection from `state.hole_cards` + `state.board`
  (no pair / pair / two pair / trips+).
- Action-history awareness — the per-hand counter is reconciled
  against `state.action_history` for cross-validation.
- Strict `state.valid_actions` and `min_raise`/`max_raise` checking
  — the bot will never return an action the server hasn't offered.

**It's not a strong bot.** It folds too much, doesn't bluff, ignores
pot odds, and has no draw recognition. The point of this file is to
show that the SDK can carry real strategy state cleanly, not to win
matches. If you're starting your own bot, copy
[`packages/python/starters/python/`](../../packages/python/starters/python/)
— it's a thin scaffold with the IP-protected Cython Dockerfile.

## What's in the image

- `python:3.11-alpine` base.
- The `chipzen-bot` SDK installed from PyPI (pulls in `websockets`).
- `bot.py` — a ~250-line `ChipzenBot` subclass with lifecycle hooks,
  preflop bucketing, postflop hand-class branching, and opponent
  aggression tracking.
- No checkpoints, no data files, no native deps.

Target compressed size: **well under 50 MB** via
`docker save reference-bot:test | gzip` — this image stays small
because the bot is pure Python with no model weights or numerical
deps.

## Build

From this directory:

```bash
docker build -t reference-bot:test .
```

Export for upload:

```bash
docker save reference-bot:test | gzip > reference-bot.tar.gz
ls -lh reference-bot.tar.gz   # expect < 50 MB
```

## Upload via the frontend

1. Sign in to the Chipzen platform as a developer.
2. Developer page -> **Upload Bot**.
3. Name: `reference-bot`. Description: "always check/fold -- pipeline
   smoke target". Accept the rules and select `reference-bot.tar.gz`.
4. Wait for the review to move through
   `uploading -> pending_review -> reviewing -> approved`. Because this
   bot has no interesting strategy, the review should complete in
   under 30 seconds.
5. Click **Play** on the approved bot to start a free human-vs-bot
   match. The bot should produce a first log line within ~2 seconds
   and act on every turn without triggering the server's
   safe-default fallback.

## Run manually (optional sanity check)

The upload pipeline already does this for you, but if you want to smoke
the container outside the pipeline:

```bash
docker run --rm \
    -e CHIPZEN_WS_URL="ws://host.docker.internal:8001/ws/match/<match_id>/bot" \
    -e CHIPZEN_TOKEN="" \
    --name reference-bot-sanity \
    reference-bot:test
```

On Linux without Docker Desktop, use `--network=host` and
`ws://127.0.0.1:8001/...`.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `CHIPZEN_WS_URL` | *(required)* | WebSocket URL; injected by the platform at runtime. |
| `CHIPZEN_TOKEN` | *(empty)* | Bot API token. Empty is fine for local dev. |
| `CHIPZEN_TICKET` | *(unset)* | Single-use ticket alternative. |
| `REFERENCE_BOT_LOG_LEVEL` | `INFO` | Python logging level. |

## Logging

Every `decide()` call emits two INFO lines:

```
<ts> reference-bot INFO decide hand=<n> phase=<street> legal=<csv>
<ts> reference-bot INFO action=<check|fold>
```

No hole cards, no board, no stakes, no tokens, no user identifiers.
Human-vs-bot matches on Chipzen are always free; the bot never
inspects stake information and has no reason to.
