# Chipzen bot quickstart

Get a bot you wrote playing a match on a local Chipzen stack in ~10 minutes.

This page uses the **reference check-fold bot** at
[`examples/reference-bot/`](../examples/reference-bot/) as the worked example —
the smallest possible Chipzen bot. You build its image, tweak one line of
`decide()`, upload it, and play it. Once that loop works, swap in your own
strategy.

Want more detail? Read the full developer manual at
[`DEV-MANUAL.md`](DEV-MANUAL.md), and the protocol specs at
[`protocol/TRANSPORT-PROTOCOL.md`](protocol/TRANSPORT-PROTOCOL.md) and
[`protocol/POKER-GAME-STATE-PROTOCOL.md`](protocol/POKER-GAME-STATE-PROTOCOL.md).

> **Note.** This quickstart assumes you have a local Chipzen platform
> stack running. The platform itself is currently closed alpha — local
> stack instructions in step 7 reference the platform monorepo at
> [chipzen-ai/Chipzen](https://github.com/chipzen-ai/Chipzen). When external
> alpha opens, this quickstart will move to a hosted endpoint and the
> local-stack section will become optional.

## 1. Prerequisites

- **Docker Desktop** running (`docker version` prints Client + Server).
- **Python 3.11+** on PATH.
- **~5 GB free disk** for image layers + base images + a buffer.
- **A Chipzen account** on your local stack. Sign-in is via Clerk; a seeded
  dev user exists in `scripts/seed_demo.py`.

## 2. Get the SDK + reference bot

```bash
git clone https://github.com/chipzen-ai/chipzen-sdk.git
cd chipzen-sdk
ls examples/reference-bot/ starters/python/
```

Expected:

```
examples/reference-bot/: Dockerfile  README.md  bot.py
starters/python/:        Dockerfile  bot.py     requirements.txt
```

`examples/reference-bot/` is our worked example. `starters/python/` is a
scaffolded starter to copy for your own bot; JS and Rust starters live
alongside it under `starters/`.

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

Expected: ~**20 MB**. Upload limit is 50 MB. PluriBot-class images with
numpy/scipy run ~130 MB — trim caches/tests or use `python:3.11-alpine`.

**Common failure:** Windows PowerShell mangles the pipe and produces a
corrupt archive. Use Git Bash or WSL.

## 7. Start the local Chipzen stack

The local stack lives in the platform monorepo at
[chipzen-ai/Chipzen](https://github.com/chipzen-ai/Chipzen). Clone it
separately, then run each terminal from the monorepo root:

```bash
# Terminal 1: DB + Redis
docker compose up -d db redis

# Terminal 2: migrations (one-shot)
PYTHONPATH=src python -m alembic upgrade head

# Terminal 3: Celery worker (REQUIRED -- no eager fallback)
# Windows: --pool=solo. Linux/macOS: drop the flag.
PYTHONPATH=src celery -A chipzen.celery_app worker -l info --pool=solo

# Terminal 4: API
PYTHONPATH=src python -m uvicorn chipzen.app:app --host 127.0.0.1 --port 8001

# Terminal 5: frontend
cd frontend && npm run dev
```

> On Docker Desktop (Windows/macOS), containers reach the API via
> `host.docker.internal` (injected automatically). On native Linux with
> `--network=host` you must bind the API to `0.0.0.0` instead -- see
> "Common first-timer mistakes".

Expected: Celery logs `celery@... ready.`; API logs `Uvicorn running on
http://127.0.0.1:8001`; frontend prints `http://localhost:5173/`.

**Common failure:** API starts but logs `no consumer on bot_builds queue`
-- Celery worker isn't running. Uploads will stick in `pending_review`
forever.

## 8. Upload

1. Open the frontend URL, sign in via Clerk.
2. Go to **My Bots** → click **Upload Bot** (green pill, top-right).
3. Name `my-first-bot`, short description, accept rules, select
   `my-first-bot.tar.gz`.
4. Watch the status badge flip `uploading → pending_review → reviewing →
   approved`. Approval typically lands in **~30 s** for a reference-sized
   bot.

**Common failures:** `File type not accepted` — only `.tar.gz`/`.tgz` are
valid; re-export if you skipped `| gzip`. Stuck on `pending_review` — see
step 7. Flipped to `rejected` — click "Show rejection reason" on the card.

## 9. Play

1. On the bot card, click the small green **Play** button (visible only
   when status is `approved`).
2. You are taken to `/play/table`. Heads-up match begins: your bot posts
   blinds, gets two hole cards, and acts on every turn.
3. Because it calls, it will reach showdown frequently — you'll see its
   two hole cards revealed on each `round_result`.

**Common failures:**

- `Bot container for session X did not attach within 15s` in the API log.
  Cold-start budget is 15 s.
- `WARN safe_default fold` in the API log -- your `decide()` exceeded the
  **5 s** turn budget and the server folded for you.

## 10. Inspect logs

Container stdout/stderr is captured to
`data/bot_logs/<match_id>-<participant_id>.log`.

After the match ends, open the match replay (click the match in your
replay list). Scroll to the **Developer tools** row and click
**Bot logs · seat N** -- opens the log drawer with the tail of that
container's log. The reference bot prints two INFO lines per turn:

```
<ts> reference-bot INFO decide hand=3 phase=flop legal=check,raise
<ts> reference-bot INFO action=check
```

Or tail the file directly: `tail -f data/bot_logs/<match_id>-<pid>.log`.

For a friendlier cross-match view, open
[`/bots/<bot_id>/dashboard`](http://localhost:5173/bots/<bot_id>/dashboard)
-- the per-bot developer dashboard shows aggregate win rate, action
distribution, decision latency (avg / p99 / max), fallback counts, and
the last 20 matches in a paginated table. It parses the same
DECIDE/FALLBACK trace lines from every persisted bot log so you don't
need to eyeball individual files.

## Common first-timer mistakes

Real bugs we hit while building this pipeline.

- **API bind address.** Containers can't reach `127.0.0.1` on native Linux
  with `--network=host` -- bind `0.0.0.0` there. On Docker Desktop the
  executor injects `host.docker.internal`, so `127.0.0.1` is fine.
- **Celery worker is mandatory.** No eager fallback. Uploads stick in
  `pending_review` silently if the worker isn't consuming `bot_builds`.
- **Seccomp quirks.** The executor applies a seccomp profile that
  whitelists a minimal syscall set. A C extension needing an unlisted
  syscall crashes the container silently on startup. Prefer pure-Python
  deps, or file an SDK issue with a minimal repro and we'll audit the
  profile.
- **Cold-start > 15 s trips the attach gate.** Reference bot: ~1 s.
  PluriBot: ~2 s. A bigger model can exceed 15 s. Watch for
  `Bot container for session X did not attach within 15s` in the API
  log.
- **`decide()` timeout is 5 s end-to-end.** Includes Python overhead and
  any network hop. Blow it and the server safe-defaults to fold and logs
  `WARN safe_default` -- your bot looks like it's folding randomly but is
  actually timing out.

## Where to go next

- **Your own bot:** copy `starters/python/` (or `javascript/` /
  `rust/`) and replace `decide()`.
- **Local test without a server:** `chipzen-sdk test my_bot:MyBot
  --opponent random --hands 200` -- see [`../README.md`](../README.md).
- **Protocol:**
  [`protocol/TRANSPORT-PROTOCOL.md`](protocol/TRANSPORT-PROTOCOL.md) (Layer 1)
  +
  [`protocol/POKER-GAME-STATE-PROTOCOL.md`](protocol/POKER-GAME-STATE-PROTOCOL.md)
  (Layer 2).
- **Full manual:** [`DEV-MANUAL.md`](DEV-MANUAL.md).
