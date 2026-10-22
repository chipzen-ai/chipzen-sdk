# Big Bad Bots quickstart

Get from nothing to an uploaded Big Bad Bots bot that fights the house bots,
in about 20 minutes.

This page uses Python. Writing your bot in JavaScript? Follow
[`BIGBADBOTS-QUICKSTART-JAVASCRIPT.md`](BIGBADBOTS-QUICKSTART-JAVASCRIPT.md)
instead.

**Big Bad Bots** (game type `bigbadbots`) is Chipzen's robot-fighting game, in
beta. A match is one bot against one bot, in two stages:

1. **Gear-up.** Each bot buys a robot body from a private offer, bids in a
   sealed-bid auction for parts, and fits the parts it won into its body's
   slots.
2. **Fight.** The two robots fight a best-of-three on a one-dimensional
   arena. Time moves in **ticks**, ten a second, and on every tick both bots
   choose an action at the same moment.

This page uses one of the three **starters** in
[`packages/python/starters/bigbadbots/`](../packages/python/starters/bigbadbots/)
as the worked example. You copy it, make one change, check it with
`chipzen validate`, build and upload the image, then challenge the house bots
and watch the replays.

**There is no local simulator.** The game engine runs on the platform only,
so you iterate on the platform: check a change with `chipzen validate`,
upload it, challenge the house bots, read the replays, change something,
upload again. Section 9 is that loop.

**Prod or staging.** Everything below uses `chipzen.ai`. To use the staging
site instead, use `staging.chipzen.ai` wherever `chipzen.ai` appears, and
`--env staging` (or `CHIPZEN_ENV=staging`) on any `chipzen` command that
talks to the platform.

More detail:

- [`DEV-MANUAL.md` §10](DEV-MANUAL.md#10-big-bad-bots): the full Big Bad Bots
  chapter (the per-tick budget, masking, the auction, reading the rule set).
- [`COMMON-PITFALLS.md`](COMMON-PITFALLS.md#big-bad-bots-pitfalls): the
  mistakes that cost fights, and how to spot them.
- [`protocol/BIGBADBOTS-GAME-STATE-PROTOCOL.md`](protocol/BIGBADBOTS-GAME-STATE-PROTOCOL.md):
  the wire spec. Authoritative if any guide disagrees with it.
- [`RULESETS.md`](RULESETS.md): rule sets, the versioned numbers a match is
  played under.

**Prefer to let a coding agent build it?** Point Claude Code, Codex, Cursor
or Copilot at
[`packages/python/starters/bigbadbots/AGENTS.md`](../packages/python/starters/bigbadbots/AGENTS.md),
a build guide written for coding agents. [`llms.txt`](../llms.txt) at the repo
root indexes the Big Bad Bots docs for tools that read that format.

## 1. Prerequisites

- **Docker** installed and running (`docker version` prints Client and Server).
- **Python 3.10+** and **Git** on PATH.
- A **Chipzen account** on `chipzen.ai`.
- On Windows, use Git Bash or WSL for the commands below. PowerShell corrupts
  the `docker save | gzip` pipe in step 7.

## 2. Install the SDK

Big Bad Bots support is in the Python SDK (`chipzen-bot`) from 0.5.0:

```bash
pip install "chipzen-bot~=0.5"
chipzen --help    # sanity check: lists init, validate, run-external
```

The CLI is installed under two names, `chipzen` and `chipzen-sdk`. They are
the same program.

## 3. Copy a starter

```bash
git clone https://github.com/chipzen-ai/chipzen-sdk.git
cp -r chipzen-sdk/packages/python/starters/bigbadbots/rusher my-bot
cd my-bot
ls -A    # .dockerignore  Dockerfile  README.md  bot.py  log_cap.py  requirements.txt
```

Pick whichever style you want to build on. They are three of the house bots
you will fight on the platform, rewritten as short, commented starters:

| Starter | Style | Good first read for |
|---|---|---|
| [`rusher/`](../packages/python/starters/bigbadbots/rusher/) | Fast bodies, fast hands: walks in, jabs, throws blockers, spends its special early. | Aggressive pressure, reading the opponent's state every tick. |
| [`zoner/`](../packages/python/starters/bigbadbots/zoner/) | Long reach: holds the tip of its kick range, steps out of wind-ups, punishes whiffs. | Spacing, `distance`, `strike_range` and the walls. |
| [`turtle/`](../packages/python/starters/bigbadbots/turtle/) | Armour and patience: blocks, waits for a mistake, punishes it hard. | Blocking every tick, punishing recovery, the round clock. |

Each folder is self-contained: the bot, `requirements.txt`, an IP-protected
`Dockerfile` (it compiles `bot.py` to a Cython `.so`, so the image holds no
readable source), `log_cap.py` (see section 7), `.dockerignore` and a README. This page uses Rusher.

## 4. Read the bot

A Big Bad Bots bot subclasses `chipzen.BigBadBotsBot` and answers four calls,
one method each:

```python
from chipzen import Action, BigBadBotsBot
from chipzen.bigbadbots import AuctionState, BodyShopState, FightState, FittingState

class Rusher(BigBadBotsBot):
    def choose_body(self, state: BodyShopState) -> Action: ...  # once: buy a body
    def bid(self, state: AuctionState) -> Action: ...           # once per auction lot
    def fit(self, state: FittingState) -> Action: ...           # once: parts into slots
    def act(self, state: FightState) -> Action: ...             # every fight tick
```

Only `act()` is required. The other three have defaults (the first body on
the offer, a bid of 0, each slot filled with the first part that fits) that
are legal but weak. Every state carries `state.rules`, the rule set the match
is played under: read every number from there, never hard-code one.

The SDK handles the connection, the handshake, echoing `request_id`, and the
timing. If `act()` runs late, raises, or returns something that is not a
fight action, the SDK sends the protocol's own default answer and logs a
warning.

Open `bot.py`. The constants at the top are Rusher's taste; the methods below
them are how it thinks. The fight logic in `act()` runs in four steps: skip
while busy, punish an opponent stuck in recovery, strike first against a
wind-up, otherwise attack or walk in.

## 5. Make one change

Make Rusher bid less hard in the auction, so it keeps more credits for later
lots. Edit `bot.py`:

```diff
-BID_TEMPER = 1.25
+BID_TEMPER = 1.0
```

Not necessarily better. The point is to see a change go all the way to a
fight on the platform.

## 6. Check it with `chipzen validate`

From inside `my-bot/`:

```bash
chipzen validate .
```

Expected (counts can differ between SDK versions):

```
Chipzen Bot Validation
==================================================
  [PASS] size: Size OK (0.0MB uncompressed / 250MB limit)
  [PASS] file_structure: Entry point found: bot.py
  [PASS] syntax: Valid Python syntax
  [WARN] imports: Potentially restricted imports: os. Some functionality may be blocked in the sandbox.
  [PASS] bot_class: Found bot class: Rusher
  [PASS] act_method: Rusher.act() implemented
  [PASS] requirements: All requirements are in the allowed package list
  [PASS] smoke_test: Rusher instantiated
  [PASS] bigbadbots_choose_body: choose_body() answered all 10 requests legally
  [PASS] bigbadbots_bid: bid() answered all 63 requests legally
  [PASS] bigbadbots_fit: fit() answered all 10 requests legally
  [PASS] bigbadbots_act: act() answered all 64 requests legally
  [PASS] bigbadbots_state_mutation: no handler changed its state
  [PASS] bigbadbots_timing: every call answered in time (slowest 0.1ms)

All checks passed (1 warning)
```

`validate` sees `BigBadBotsBot` and runs the Big Bad Bots checks: it hands
each of your four methods real `turn_request`s from recorded matches and
judges every answer the way the server does. A **malformed** answer (the
server would reject it) fails; a **corrected** one (well-shaped but not
legal, such as a bid above your credits) warns. It also catches a method
that changes the state it is handed, raises `SystemExit`, crashes on every
tick, or is slow for the per-tick deadline.

The `os` warning is expected: the starter's `main()` reads `CHIPZEN_WS_URL`
from the environment. Exit code 0 means go.

A clean pass means the platform will not reject your bot on protocol
grounds. It says nothing about how well it fights; only real matches tell
you that.

## 7. Build and export

```bash
docker build -t my-bot:v1 .
docker save my-bot:v1 | gzip > my-bot.tar.gz
ls -lh my-bot.tar.gz    # roughly 45 MB
```

Big Bad Bots bots run through the platform's log-cap wrapper (`log_cap.py` in
your folder), which caps how much your bot can log in one match and drops the
rest. The starter's Dockerfile already makes it the image's ENTRYPOINT, so do
not change the ENTRYPOINT or `log_cap.py`; put your own bot command in `CMD`.

The upload cap is **250 MB compressed** (the built image is separately capped
at **200 MB**). The image must run as a non-root user; the starter's
Dockerfile already sets one. To check:

```bash
docker run --rm --entrypoint id my-bot:v1    # must NOT print uid=0
```

## 8. Upload

On `chipzen.ai`, upload `my-bot.tar.gz` through the developer UI and choose
**Big Bad Bots** as the bot's game. A bot is only seated at tables for the
games it declares, and a `BigBadBotsBot` declares `bigbadbots` on the
connection for you.

The bot moves through `pending_review`, then `reviewing`, then `active`.
Activation is automatic when review passes. Review checks that the bot starts
and speaks the protocol; it does not judge strength.

An uploaded bot is started by the platform, which hands it its match URL
(`CHIPZEN_WS_URL`), so the image needs no prod or staging setting. The same
tarball uploads to either site.

## 9. Challenge the house bots, then iterate

Once the bot is `active`, challenge a house bot with **Big Bad Bots** as the
game. A challenge against a house bot starts straight away (nobody has to
accept it). Games against house bots, or between two of your own bots, are
practice and are not rated; a game against another builder's bot is rated, and
in the beta a bot's rating is simply the share of its rated games it wins.

The house bots include the originals of the three starters (rusher, zoner and
turtle) and several more. A good first baseline: your edited Rusher against
the house rusher. If the only difference is your change, the replays show
what it did.

Then loop:

1. **Watch the replay.** Every match records both bots' answers, tick by
   tick. Look for where you lost the round: a block that dropped, a throw you
   ate while blocking, a wind-up you walked into, credits left unspent.
2. **Read your log.** Your bot's stdout goes to its per-match log, as for any
   uploaded bot ([`DEV-MANUAL.md` §5](DEV-MANUAL.md#5-debugging-a-live-match)).
   Log a line per round or per match, not per tick (ten a second adds up).
3. **Change one thing**, then `chipzen validate .`, rebuild with a new tag
   (`my-bot:v2`), and upload the new version.
4. **Play several matches per version.** Body offers, auction lots and start
   positions are drawn from each match's seed, so one match is one sample.

## Five things to know before you write your own fight logic

Each has a section in the dev manual chapter.

1. **The per-tick budget is 100 ms**, plus a **3 s time bank** per round
   (both provisional; read them from `state.rules.deadline`). A slow `act()`
   spends the bank; an empty bank means defaults. See
   [§10.3](DEV-MANUAL.md#103-the-per-tick-budget).
2. **Send `block` again on every tick you want to block.** There is no
   "hold" action: each tick is a new question, and a tick you skip or miss
   plays the server's default. See
   [§10.4](DEV-MANUAL.md#104-one-answer-per-tick-send-block-again-every-tick).
3. **The opponent's move is hidden while it winds up.** You see `"attack"`
   in `phase: "startup"`, not which attack. See
   [§10.5](DEV-MANUAL.md#105-the-masked-opponent).
4. **The auction is sealed-bid, and the winner pays its own bid.** One
   budget pays for the body and every part, and credits left at the end are
   lost. See [§10.6](DEV-MANUAL.md#106-gear-up-and-auction-strategy-basics).
5. **Every number lives in the rule set.** `state.rules` has the id, typed
   `gearup`, `arena`, `rounds`, `forfeit` and `deadline` sections, and the
   full table (`state.rules["moves"]`, `["bodies"]`, `["parts"]`, ...). See
   [§10.7](DEV-MANUAL.md#107-reading-the-rule-set-staterules).
