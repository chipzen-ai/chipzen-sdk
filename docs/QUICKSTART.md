# Chipzen bot quickstart

Get from zero to a Chipzen-compatible bot image, locally validated and
ready to upload, in ~10 minutes.

This page uses the **reference check-fold bot** at
[`examples/reference-bot/`](../examples/reference-bot/) as the worked
example — the smallest possible Chipzen bot. You build its image, tweak
one line of `decide()`, validate it locally, and produce the upload
tarball. Once that loop works, swap in your own strategy.

Want more detail? Read the full developer manual at
[`DEV-MANUAL.md`](DEV-MANUAL.md), and the protocol specs at
[`protocol/TRANSPORT-PROTOCOL.md`](protocol/TRANSPORT-PROTOCOL.md) and
[`protocol/POKER-GAME-STATE-PROTOCOL.md`](protocol/POKER-GAME-STATE-PROTOCOL.md).

## 1. Prerequisites

- **Docker** installed and running (`docker version` prints Client + Server).
- **Python 3.11+** on PATH.
- **~5 GB free disk** for image layers + base images + a buffer.

## 2. Get the SDK + reference bot

```bash
git clone https://github.com/chipzen-ai/chipzen-sdk.git
cd chipzen-sdk
ls examples/reference-bot/ packages/python/starters/python/
```

Expected:

```
examples/reference-bot/:           Dockerfile  README.md  bot.py
packages/python/starters/python/:  Dockerfile  README.md  bot.py  requirements.txt  .dockerignore
```

`examples/reference-bot/` is our worked example.
`packages/python/starters/python/` is the IP-protected starter you copy
for your own bot — its Dockerfile compiles `bot.py` to a Cython `.so`
so the runtime image contains no readable `.py` source for your
strategy. See [`../packages/python/IP-PROTECTION.md`](../packages/python/IP-PROTECTION.md)
for what that protects.

JS and Rust starters live under `starters/javascript/` and
`starters/rust/` for now and will move to their own packages once the
JS and Rust adapters ship (Phase 2 / Phase 3).

## 3. Inspect the reference bot

Open `examples/reference-bot/bot.py`. The entire strategy is three lines:

```python
class ReferenceBot(Bot):
    def decide(self, state: GameState) -> Action:
        if "check" in (state.valid_actions or ()):
            return Action.check()
        return Action.fold()
```

Subclass `chipzen.Bot`, override `decide()`, return an `Action`
(`Action.check()` / `.fold()` / `.call()` / `.raise_to(amount)`). The SDK
handles WebSocket, handshake, `request_id` echoing, ping/pong, and retries —
see [`../README.md`](../README.md) for the three-layer model.

## 4. Customize — call instead of fold

Make the bot a calling station. Edit `examples/reference-bot/bot.py`:

```diff
         if "check" in valid:
             return Action.check()
+        if "call" in valid:
+            return Action.call()
         return Action.fold()
```

Not a *good* bot, but one that stays in hands long enough to see flops.

## 5. Build

From the `examples/reference-bot/` directory:

```bash
cd examples/reference-bot
docker build -t my-first-bot:v1 .
```

Expected tail: `naming to docker.io/library/my-first-bot:v1`. The
Dockerfile installs `chipzen-bot` from PyPI, so no extra build context
is required beyond this directory.

## 6. Export the tarball

```bash
docker save my-first-bot:v1 | gzip > my-first-bot.tar.gz
ls -lh my-first-bot.tar.gz
```

Expected: ~**20 MB**. Recommended max: **300 MB compressed**; hard
upload cap is **500 MB**. Non-trivial bots with numpy/scipy + a model
checkpoint typically run 100–250 MB — trim caches/tests, drop unused
deps, or use `python:3.11-alpine` if you're brushing the recommendation.

**Common failure:** Windows PowerShell mangles the pipe and produces a
corrupt archive. Use Git Bash or WSL.

## 7. Validate locally

Run the same checks the platform's upload pipeline runs, before you
upload anything:

```bash
chipzen-sdk validate ./examples/reference-bot/
```

Expected: all checks PASS, exit 0. The validator covers size budget,
imports, sandbox-blocked modules, and a `decide()` timeout sniff. See
[`DEV-MANUAL.md` §4](DEV-MANUAL.md#4-testing-your-bot) for the full
list of checks.

For a stricter check that also exercises the wire protocol against an
in-process mock server:

```bash
chipzen-sdk validate ./examples/reference-bot/ --check-connectivity
```

A clean pass here means "your bot will not be rejected on protocol
grounds at upload time". It is **not** a strategy-strength check — the
platform runs comprehensive bot-vs-bot evaluation after upload.

## 8. Upload, play, debug

The remaining steps happen on the Chipzen platform: upload the tarball
through the developer UI, watch the bot move through
`pending_review → reviewing → approved → active`, then play it in
human-vs-bot or bot-vs-bot matches and inspect per-match logs.

Those steps live on the platform itself rather than in the SDK — the
upload UI walks you through them, and the match-side observability
surfaces (per-match log drawer, decisions tab, per-bot dashboard) are
documented in
[`DEV-MANUAL.md` §5](DEV-MANUAL.md#5-debugging-a-live-match).

## Common first-time mistakes

- **Seccomp quirks.** The platform applies a seccomp profile that
  whitelists a minimal syscall set. A C extension needing an unlisted
  syscall can crash the container silently on startup. Prefer
  pure-Python deps; if you need a native extension, file an SDK issue
  with a minimal repro and the syscall name so the maintainers can
  review the profile.
- **Cold-start > 15 s trips the attach gate.** Reference bot: ~1 s. A
  non-trivial bot with a small model: ~2 s. A bigger model can exceed
  15 s. If your image is slow to start, lazy-load expensive deps inside
  `on_match_start` rather than at module-import time.
- **`decide()` timeout is 5 s end-to-end.** Includes Python overhead and
  any network hop. Blow it and the server safe-defaults to fold — your
  bot will look like it's folding randomly but is actually timing out.
  Always check `decide()` traces or the per-match log drawer when a
  bot's decisions look unexpectedly passive.

## Where to go next

- **Your own bot:** copy `packages/python/starters/python/` (Python,
  IP-protected via Cython) or `starters/javascript/` / `starters/rust/`
  (raw-WebSocket pending their adapters), and replace `decide()`.
- **Pre-upload check:** `chipzen-sdk validate ./my_bot/
  --check-connectivity` — see
  [`DEV-MANUAL.md` §4](DEV-MANUAL.md#4-testing-your-bot).
- **Protocol:**
  [`protocol/TRANSPORT-PROTOCOL.md`](protocol/TRANSPORT-PROTOCOL.md) (Layer 1)
  +
  [`protocol/POKER-GAME-STATE-PROTOCOL.md`](protocol/POKER-GAME-STATE-PROTOCOL.md)
  (Layer 2).
- **Full manual:** [`DEV-MANUAL.md`](DEV-MANUAL.md).
