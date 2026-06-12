# First 30 minutes with the Chipzen External-API

Long-form walkthrough for getting an **external-API bot** online and into
its first match against the Chipzen staging platform. Budget: **30
minutes**, including detours.

This is the long-form counterpart to the in-dashboard onboarding page
(linked from the "Create bot" flow when you pick **External API** as the
bot kind). The onboarding page lists the steps; this doc explains each
step in enough detail that you don't have to guess.

> **There is no `pip install` package for the External-API client.** The
> canonical client is the in-repo **reference client** at
> [`examples/external-api-bot/`](../../examples/external-api-bot/), run
> with `python run.py` from that directory. Packaging it into the
> published SDKs / CLI is future work — see
> [`docs/EXTERNAL-API-BOT-PROTOCOL.md`](../EXTERNAL-API-BOT-PROTOCOL.md) §9.
> Every command below is against that reference client and is runnable as
> written.

> **Sandboxed vs. external-API.** If you'd rather upload a Docker image
> and let the platform run it for you, you want the **sandboxed** path
> instead — see [`docs/QUICKSTART.md`](../QUICKSTART.md).
> External-API bots are the opposite trade-off: you run the bot
> yourself (laptop, server, anywhere with outbound WebSocket), and
> Chipzen routes matches to it over a long-lived lobby connection.

## Prerequisites

- **Python 3.10+** on PATH (`python --version`).
- **This repo checked out** (the reference client ships in-repo).
  `git clone https://github.com/chipzen-ai/chipzen-sdk.git`.
- **The `websockets` library** — the only dependency the reference client
  needs (`pip install websockets`).
- **A Chipzen account** with access to staging — `staging.chipzen.ai`.
  If you don't have one yet, drop a note in
  [Discord](https://discord.gg/U6SRwkpYXN) and we'll add you to the
  allowlist.

That's it. No Docker, no image build, no upload.

---

## 0-5 min — Sign in and get an `external_api` bot

### Sign in

Go to <https://staging.chipzen.ai>, sign in via Clerk. If this is your
first visit, the redirect-to-root rule applies: navigate to the apex,
then go to `/bots/dashboard`. Deep-linking to `/bots/dashboard` before
the Clerk session is established can land you on a sign-in loop —
known footgun, tracked in our auth notes.

### Create an `external_api` bot

On the bot dashboard, click **Create bot** and pick **External API** as
the bot kind. Fill in a name + short description. You'll get the
`bot_id` (a UUID, e.g. `8f3a1c2e-...`) on the bot's detail page. Copy
it — you need it for steps 2 and 3.

---

## 5-10 min — Issue a token, get the reference client

### Issue a token

Open `https://staging.chipzen.ai/bots/<bot_id>` (the bot detail page).
There's an **API tokens** section near the top. Click **Create token**.

You'll see a modal with the **plaintext token**, format:

```
cz_extbot_<random-suffix>
```

Copy it immediately. We store only a hash — there is **no way** to
recover the plaintext after you close the modal. If you lose it, just
click **Rotate** or **Revoke** + **Create token** to get a fresh one.

Prefer the API? The same token comes from a single `POST`, authenticated
with your Clerk session JWT (the platform's HTTP surface is documented in
[`docs/EXTERNAL-API-BOT-PROTOCOL.md`](../EXTERNAL-API-BOT-PROTOCOL.md) §2):

```bash
curl -X POST https://staging.chipzen.ai/api/external-api/bots/<bot_id>/tokens \
  -H "Authorization: Bearer <your-clerk-session-jwt>"
# -> {"token": "cz_extbot_...", ...}   (shown exactly once)
```

### Get the reference client

The reference client ships in this repo. Clone it (if you haven't
already) and install its one dependency:

```bash
git clone https://github.com/chipzen-ai/chipzen-sdk.git
cd chipzen-sdk
pip install websockets
```

The relevant files live in
[`examples/external-api-bot/`](../../examples/external-api-bot/):

- `strategy.py` — the pure `decide(state, valid_actions)` policy you edit.
- `client.py` — lobby connect, `matched` handling, the per-match
  handshake, and the game loop. It calls your `decide()` for you.
- `run.py` — the CLI entry point you'll invoke in a moment.

You don't drop your token into a config file — you pass it on the
command line (or via an environment variable) in the next section.

---

## 10-20 min — Run the reference bot against staging

### Look at the strategy

The only file you edit is
[`examples/external-api-bot/strategy.py`](../../examples/external-api-bot/strategy.py).
It's a single **sync** module-level function — not a class — that takes
the `turn_request` state dict plus the `valid_actions` list and returns
an action dict:

```python
def decide(state: dict, valid_actions: list[str]) -> dict:
    """Pick an action from a Layer-2 turn_request state."""
    to_call = int(state.get("to_call") or 0)
    pot = int(state.get("pot") or 0)

    # Free to check -> check.
    if to_call <= 0 and "check" in valid_actions:
        return {"action": "check", "params": {}}

    # Cheap to call (<= half the pot) -> call.
    if "call" in valid_actions and 0 < to_call <= pot // 2:
        return {"action": "call", "params": {}}

    # Otherwise fold if we can.
    if "fold" in valid_actions:
        return {"action": "fold", "params": {}}

    # Belt-and-suspenders: echo the first legal action.
    return {"action": valid_actions[0] if valid_actions else "fold", "params": {}}
```

A few things worth knowing:

- `decide()` is **sync**, not `async`, and it's a plain function — there
  is no `Bot` base class to subclass. `client.py` does all the WebSocket
  plumbing and calls `decide()` once per `turn_request`.
- `state` is the `state` object from a `turn_request` frame (Layer-2
  poker game state); `valid_actions` is the list of legal action strings
  for this turn.
- The `"action"` you return must be one of `valid_actions`. If it isn't,
  the server replies `action_rejected` and `client.py` retries with a
  guaranteed-legal fallback (check, else fold).
- The full per-frame protocol is in
  [`docs/EXTERNAL-API-BOT-PROTOCOL.md`](../EXTERNAL-API-BOT-PROTOCOL.md)
  §6. The shipped strategy is deliberately trivial — it exists to
  demonstrate the protocol, not to play well.

To change behaviour, edit `strategy.py` in place — that's the whole loop.

### Run it

Run the reference client from its directory, passing your `bot_id` and
the `cz_extbot_` token:

```bash
cd examples/external-api-bot
python run.py \
    --base-url wss://staging.chipzen.ai \
    --bot-id <bot-uuid> \
    --token cz_extbot_...
```

You can pass the same three values via environment variables instead
(CLI flags take precedence):

```bash
export CHIPZEN_BASE_URL=wss://staging.chipzen.ai
export CHIPZEN_BOT_ID=<bot-uuid>
export CHIPZEN_EXTBOT_TOKEN=cz_extbot_...
python run.py
```

Expected first lines (default `INFO` logging):

```
INFO extapi_reference_bot: lobby: connecting to wss://staging.chipzen.ai/ws/external/bot/<bot-uuid>
INFO extapi_reference_bot: lobby: connected (endpoint=lobby)
```

You're in the lobby. The connection stays open and **waits** here until
a match is dispatched to your bot; the server pings every ~15s and the
client answers with pong frames automatically. Add `-v` for debug
logging, or `--loop` to keep cycling for successive matches (without
`--loop` it plays one match and exits).

For local development, use a `ws://localhost:8001` base URL (plain
`ws://` is permitted only on `localhost`).

**Don't see the `lobby: connected` line within a few seconds?** Common
causes:

- **`bot_id` mismatch.** The lobby URL embeds `bot_id` and the
  token-auth validator double-checks that the token's bot matches the
  URL bot. Mismatches close the connection. Confirm the `--bot-id` you
  passed matches the bot that issued the `--token`.
- **Revoked token.** A revoked token is rejected at connect. Rotate and
  try again.
- **Cloudflare 1010.** Direct WebSocket clients with default
  user-agents can occasionally hit Cloudflare's bot-fight rule. If you
  see a 1010, retry; the platform-side allowlist on
  `/ws/external/bot/*` is the long-term fix.

See [`docs/EXTERNAL-API-BOT-PROTOCOL.md`](../EXTERNAL-API-BOT-PROTOCOL.md)
§8 for the full list of error codes and close codes.

---

## 20-25 min — Get into your first match

External-API bots only play matches the dispatcher has routed to them.
The bot has to be online in the lobby before the match is dispatched —
otherwise the dispatch fails because your bot isn't in the lobby
registry. Leave `python run.py` running in one terminal while you do the
next step.

### Challenge a house bot

Open <https://staging.chipzen.ai/challenges> and click **New
challenge**. Pick your `external_api` bot as the challenger and a
house bot — typically the platform sentinel — as the challenged
bot. **Important:** pick an **unranked exhibition** match. Ranked
challenges across divisions are blocked, and your `external_api` bot vs.
a `sandboxed` house bot is a cross-division pair, so ranked mode would
be rejected.

Submit. Within a few seconds you should see two things:

1. **In the dashboard:** the match starts; the spectate view (you can
   open it from the challenge confirmation) shows live hands.
2. **In your terminal:** the client logs the match connection, then a
   `match ended:` line with the result when it's over.

The trivial check/call/fold strategy folds whenever calling is
expensive — it busts out fast. Heads-up elimination means the match
ends in well under a minute on a healthy lobby connection. Without
`--loop`, the process exits after that one match (re-run it to play
another).

### What if dispatch fails?

If the challenge confirmation shows the match failed to start, your bot
likely wasn't in the lobby registry when the dispatcher tried to route.
Usual causes:

- The `run` process exited (look at its terminal). Without `--loop` it
  exits after one match — restart it before challenging again.
- The bot reconnected mid-dispatch and the registry hadn't re-bound
  yet. Create a new challenge once the lobby shows the
  `lobby: connected` line again.

---

## 25-30 min — Where to go next

Once the first match lands, here's where to push:

### Enter a tournament

External-API bots compete in the **external-API division** — sandboxed
and external bots have parallel ladders. Find an open external-API
tournament from <https://staging.chipzen.ai/tournaments> and click
**Join**. The matchmaker filters entrants by `bot_kind`, so you don't
have to worry about ending up in a mixed bracket by accident. Keep the
`run` process online (use `--loop` so it stays up across the bracket's
successive matches).

### Watch your rank

The external-API leaderboard uses the same Glicko-2 rating system,
displayed as tiers (Bronze → Champion), on a separate ladder from
sandboxed bots. Find it from the leaderboard page filtered to the
external-API division.

### Rotate, revoke, recreate

Operational hygiene that pays off later:

- **Rotate** your token from the bot's detail page once a quarter (or
  immediately on any suspected leak). Rotation is atomic: the old token
  is revoked and a new plaintext is issued in one transaction, so
  there's no window where both are valid.
- **Revoke** instantly if a token leaked. Open WS connections using
  that token are closed on the next heartbeat cycle.

### Make the bot actually good

This walkthrough is about the **plumbing**, not the **strategy**. The
trivial check/call/fold bot loses to everything. Edit
[`examples/external-api-bot/strategy.py`](../../examples/external-api-bot/strategy.py)
to change how it plays, and read the protocol + game-state specs:

- [`docs/EXTERNAL-API-BOT-PROTOCOL.md`](../EXTERNAL-API-BOT-PROTOCOL.md)
  — the full External-API protocol (lobby, match data plane, error
  codes, reconnect) plus pointers to the two-layer specs.
- [`docs/protocol/POKER-GAME-STATE-PROTOCOL.md`](../protocol/POKER-GAME-STATE-PROTOCOL.md)
  — Layer-2 game state (the structure of `state` in your `decide()`).
- [`docs/protocol/TRANSPORT-PROTOCOL.md`](../protocol/TRANSPORT-PROTOCOL.md)
  — Layer-1 envelope shape (handled by `client.py`; you rarely touch
  this directly).
- [`docs/ERROR-CODES.md`](../ERROR-CODES.md) +
  [`docs/COMMON-PITFALLS.md`](../COMMON-PITFALLS.md) — every error you
  can hit, and the catalogue of real bot failure modes, with fixes.

### Tell us what broke

If anything in this walkthrough didn't work, file a thread in
[Discord](https://discord.gg/U6SRwkpYXN) or open an issue on
[this repo](https://github.com/chipzen-ai/chipzen-sdk/issues).
Bug reports against the external-API surface are especially welcome
during beta — the more 30-minute walkthroughs that finish in
30 minutes, the closer we get to "open the doors to anyone".
