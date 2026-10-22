# Big Bad Bots quickstart: JavaScript

Get from nothing to an uploaded Big Bad Bots bot, written in JavaScript, that
fights the platform bots.

This is the JavaScript version of the
[Big Bad Bots quickstart](BIGBADBOTS-QUICKSTART.md), which uses Python. The
game, the platform and the loop are the same; only the SDK
(`@chipzen-ai/bot`), the starter and the commands differ. The Big Bad Bots
beta supports Python and JavaScript bots.

**Big Bad Bots** (game type `bigbadbots`) is Chipzen's robot-fighting game, in
beta. A match is one bot against one bot, in two stages:

1. **Gear-up.** Each bot buys a robot body from a private offer, bids in a
   sealed-bid auction for parts, and fits the parts it won into its body's
   slots.
2. **Fight.** The two robots fight a best-of-three on a one-dimensional
   arena. Time moves in **ticks**, ten a second, and on every tick both bots
   choose an action at the same moment.

This page uses the JavaScript **Rusher** starter in
[`packages/javascript/starters/bigbadbots/rusher/`](../packages/javascript/starters/bigbadbots/rusher/)
as the worked example. You copy it, make one change, check it with
`chipzen-sdk validate`, build and upload the image, then challenge the platform
bots and watch the replays.

**There is no local simulator.** The game engine runs on the platform only,
so you iterate on the platform: check a change with `chipzen-sdk validate`,
upload it, challenge the platform bots, read the replays, change something,
upload again.

**Prod or staging.** Everything below uses `chipzen.ai`. To use the staging
site instead, use `staging.chipzen.ai` wherever `chipzen.ai` appears, and
`--env staging` (or `CHIPZEN_ENV=staging`) on any `chipzen-sdk` command that
talks to the platform.

More detail:

- [`DEV-MANUAL.md` §10](DEV-MANUAL.md#10-big-bad-bots): the full Big Bad Bots
  chapter (the per-tick budget, masking, the auction, reading the rule set).
  Its examples are Python; the JavaScript names are the same in camelCase
  (`choose_body()` is `chooseBody()`, `state.me.strike_range` is
  `state.me.strikeRange`).
- [`COMMON-PITFALLS.md`](COMMON-PITFALLS.md#big-bad-bots-pitfalls): the
  mistakes that cost fights, and how to spot them.
- [`protocol/BIGBADBOTS-GAME-STATE-PROTOCOL.md`](protocol/BIGBADBOTS-GAME-STATE-PROTOCOL.md):
  the wire spec. Authoritative if any guide disagrees with it.
- [`RULESETS.md`](RULESETS.md): rule sets, the versioned numbers a match is
  played under.

## 1. Prerequisites

- **Docker** installed and running (`docker version` prints Client and Server).
- **Node.js 20+** (with npm) and **Git** on PATH.
- A **Chipzen account** on `chipzen.ai`.
- On Windows, use Git Bash or WSL for the commands below. PowerShell corrupts
  the `docker save | gzip` pipe in step 7.

## 2. Install the SDK

Big Bad Bots support is in the JavaScript SDK (`@chipzen-ai/bot`) from 0.4.0.
The starter's `package.json` already depends on it (`"@chipzen-ai/bot": "^0.5.0"`),
so `npm install` in step 3 installs it. To add it to a bot folder of your own
instead:

```bash
npm install @chipzen-ai/bot@^0.5.0
```

## 3. Copy the starter

```bash
git clone https://github.com/chipzen-ai/chipzen-sdk.git
cp -r chipzen-sdk/packages/javascript/starters/bigbadbots/rusher my-bot
cd my-bot
npm install
npx chipzen-sdk --help    # sanity check: lists init, validate, run-external
ls -A    # .dockerignore  Dockerfile  README.md  bot.js  log_cap.py  node_modules  package-lock.json  package.json
```

Rusher fights with fast bodies and fast hands: it walks in, jabs, throws
blockers and spends its special early. It is one of the platform bots you will
fight on the platform, rewritten as a short, commented starter, and it plays
exactly like the [Python Rusher](../packages/python/starters/bigbadbots/rusher/).
The Python starters also include Zoner (spacing) and Turtle (blocking); their
`bot.py` files read the same way and port to JavaScript the same way.

The folder is self-contained: the bot, `package.json`, an IP-protected
`Dockerfile` (it compiles `bot.js` and the SDK into one executable with
`bun build --compile`, so the image holds no readable source), `log_cap.py`
(see section 7), `.dockerignore` and a README.

## 4. Read the bot

A Big Bad Bots bot extends `BigBadBotsBot` and answers four calls, one method
each:

```js
import { Action, BigBadBotsBot } from "@chipzen-ai/bot";

export class Rusher extends BigBadBotsBot {
  chooseBody(state) { /* once: buy a body */ }
  bid(state) { /* once per auction lot */ }
  fit(state) { /* once: parts into slots */ }
  act(state) { /* every fight tick */ }
}
```

Only `act()` is required. The other three have defaults (the first body on
the offer, a bid of 0, each slot filled with the first part that fits) that
are legal but weak. Every state carries `state.rules`, the rule set the match
is played under: read every number from there, never hard-code one. The full
parameter table is `this.gameConfig.ruleSet` (`moves`, `bodies`, `parts`,
...), with the wire's snake_case keys.

Export the bot class: `chipzen-sdk validate` and `run-external` import it
from `bot.js`.

The SDK handles the connection, the handshake, echoing `request_id`, and the
timing. If `act()` throws or returns something that is not a fight action,
the SDK sends the protocol's own default answer and logs a warning. The
methods are synchronous, and JavaScript cannot interrupt them, so keep
`act()` fast: a request that has already waited out its time behind a slow
answer gets the default without calling your bot.

Open `bot.js`. The constants at the top are Rusher's taste; the methods below
them are how it thinks. The fight logic in `act()` runs in four steps: skip
while busy, punish an opponent stuck in recovery, strike first against a
wind-up, otherwise attack or walk in.

## 5. Make one change

Make Rusher bid less hard in the auction, so it keeps more credits for later
lots. Edit `bot.js`:

```diff
-export const BID_TEMPER = 1.25;
+export const BID_TEMPER = 1.0;
```

Not necessarily better. The point is to see a change go all the way to a
fight on the platform.

## 6. Check it with `chipzen-sdk validate`

From inside `my-bot/`:

```bash
npx chipzen-sdk validate .
```

Expected (counts can differ between SDK versions):

```
Chipzen Bot Validation
==================================================
  [PASS] size: Size OK (0.0 MB uncompressed / 250 MB limit)
  [PASS] file_structure: Entry point found: bot.js
  [PASS] syntax: Valid JavaScript syntax
  [PASS] imports: No blocked imports detected
  [PASS] bot_class: Found bot class: Rusher
  [PASS] act_method: Rusher.act() implemented
  [PASS] bigbadbots_dockerfile: Dockerfile starts the bot through the log-cap wrapper
  [PASS] smoke_test: Rusher instantiated
  [PASS] bigbadbots_choose_body: choose_body() answered all 10 requests legally
  [PASS] bigbadbots_bid: bid() answered all 63 requests legally
  [PASS] bigbadbots_fit: fit() answered all 10 requests legally
  [PASS] bigbadbots_act: act() answered all 64 requests legally
  [PASS] bigbadbots_state_mutation: no handler changed its state
  [PASS] bigbadbots_timing: every call answered in time (slowest 0.2ms)

All checks passed! Your bot is ready to upload.
```

`validate` sees `BigBadBotsBot` and runs the Big Bad Bots checks: it hands
each of your four methods real `turn_request`s from recorded matches (the
same ones the Python SDK uses) and judges every answer the way the server
does. A **malformed** answer (the server would reject it) fails; a
**corrected** one (well-shaped but not legal, such as a bid above your
credits) warns. It also catches a method that calls `process.exit()`, throws
on every tick, or is slow for the per-tick deadline. For a bot that extends
plain `Bot` and answers through `decide()`, pass `--game bigbadbots`.

A clean pass means the platform will not reject your bot on protocol
grounds. It says nothing about how well it fights; only real matches tell
you that.

## 7. Build and export

```bash
docker build -t my-bot:v1 .
docker save my-bot:v1 | gzip > my-bot.tar.gz
ls -lh my-bot.tar.gz
```

The Dockerfile installs `@chipzen-ai/bot` from npm at the version
`package.json` pins, so the image does not depend on your `node_modules`.

Big Bad Bots bots run through the platform's log-cap wrapper (`log_cap.py` in
your folder), which caps how much your bot can log in one match and drops the
rest. The starter's Dockerfile already makes it the image's ENTRYPOINT, so do
not change the ENTRYPOINT or `log_cap.py`; put your own bot command in `CMD`.

The upload cap is **250 MB compressed** (the built image is separately capped
at **200 MB**); this image is roughly 130 MB. The image must run as a
non-root user; the starter's Dockerfile already sets one. To check:

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

## 9. Challenge the platform bots, then iterate

Once the bot is `active`, challenge a platform bot with **Big Bad Bots** as the
game. A challenge against a platform bot starts straight away. Games against platform
bots, or between two of your own bots, are practice and are not rated; a game
against another builder's bot is rated, and in the beta a bot's rating is simply
the share of its rated games it wins. A good first baseline: your edited
Rusher against the platform rusher.

Then loop: watch the replay, read your bot's per-match log (its stdout and
`console` output; log a line per round, not per tick), change one thing, run
`npx chipzen-sdk validate .`, rebuild with a new tag (`my-bot:v2`) and upload
the new version. Play several matches per version: body offers, auction lots
and start positions are drawn from each match's seed, so one match is one
sample. The [Python quickstart's section 9](BIGBADBOTS-QUICKSTART.md#9-challenge-the-platform-bots-then-iterate)
has the details.

## Running from your own machine (remote play)

**Remote play does not take Big Bad Bots matches yet**; a Big Bad Bots bot
plays as an uploaded image. When remote play opens, the same `bot.js` runs as
an external-API bot. Prod (`chipzen.ai`) is the default:

```bash
npx chipzen-sdk run-external bot.js --bot-id <your-bot-id> --token <your-token>
```

For staging (`staging.chipzen.ai`), add `--env staging` or set
`CHIPZEN_ENV=staging`. The flag wins over the environment variable, and a
`url` in `chipzen.toml` wins over both.

## Five things to know before you write your own fight logic

1. **The per-tick budget is 250 ms**, plus a **3 s time bank for the whole
   match**, never refilled between rounds. The platform announces both in
   `match_start` as `game_config.response_clock` (`deadline_ms`,
   `time_bank_ms`, `bank_scope`), not in `state.rules.deadline`, which still
   shows the rule set's older numbers. The SDK does not parse it yet; read it
   from the raw message in `onMatchStart()` (call `super.onMatchStart()`
   first):

   ```js
   onMatchStart(matchInfo) {
     super.onMatchStart(matchInfo);
     const clock = matchInfo.game_config?.response_clock ?? {};
     this.windowMs = clock.deadline_ms ?? 250;
   }
   ```

   `state.timeoutMs` is the window plus what is left of your bank. A slow
   `act()` spends the bank for the rest of the match; an empty bank means
   defaults.
2. **Send `block` again on every tick you want to block.** There is no
   "hold" action: each tick is a new question, and a tick you skip or miss
   plays the server's default.
3. **The opponent's move is hidden while it winds up.** You see `"attack"`
   in `phase: "startup"`, not which attack.
4. **The auction is sealed-bid, and the winner pays its own bid.** One
   budget pays for the body and every part, and credits left at the end are
   lost.
5. **Every number lives in the rule set.** `state.rules` has the id and typed
   `gearup`, `arena`, `rounds`, `forfeit` and `deadline` sections;
   `state.rules.params` (and `this.gameConfig.ruleSet`) is the full table.
   A `BigBadBotsBot` warns at `match_start` when the rule set is one this SDK
   release does not know, and plays on the numbers the server sent.
