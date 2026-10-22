# Rusher: a Big Bad Bots starter in JavaScript

**Style: fast bodies, fast hands.** Rusher buys a quick body, overpays for
the parts it wants, then walks straight in, jabs, throws anyone who blocks,
and spends its special the first time it can.

Rusher is one of the Chipzen platform bots you will meet on the platform,
rewritten as a short, commented starter on the SDK's `BigBadBotsBot`. It is a
line-for-line port of the [Python Rusher](../../../../python/starters/bigbadbots/rusher/)
and gives the same answer to every request. Copy this folder, change it,
upload it.

The full walkthrough is
[`docs/BIGBADBOTS-QUICKSTART-JAVASCRIPT.md`](../../../../../docs/BIGBADBOTS-QUICKSTART-JAVASCRIPT.md).

## Files

| File | Purpose |
|---|---|
| [`bot.js`](bot.js) | The bot. Its taste is the constants at the top; its thinking is the methods below them. |
| [`package.json`](package.json) | An ES module package that depends on `@chipzen-ai/bot`. Add your own dependencies here. |
| [`Dockerfile`](Dockerfile) | Multi-stage `bun build --compile`: the runtime image holds one executable, no `.js` source. |
| [`.dockerignore`](.dockerignore) | Keeps the build context tight (no `node_modules`, caches, secrets). |
| [`log_cap.py`](log_cap.py) | The platform's per-match log cap, the image's ENTRYPOINT. Ship it unchanged. |

## How it plays

**Gear-up.** Each body on the offer is scored by Rusher's attribute weights
(speed 1.6 and power 1.2 lead), a bonus for the bodies it likes (`wisp`,
`brawler`, `viper`, `scrapper`), and the slots its leftover credits can
fill. In the auction it works out how much the lot improves its best
fitting and bids its fair share of credits scaled by that gain, times
`BID_TEMPER = 1.25`. It never bids more than the opponent could beat, and it
goes all in on a useful last lot. The fitting puts the most valuable part
into each slot.

**The fight, every tick, in order:**

1. Busy with its own move: nothing to decide.
2. The opponent is stuck in recovery, hitstun or a daze: hit it with the
   most damaging move that lands before it can act again.
3. The opponent is winding up a hidden move: strike first with the fastest
   attack that reaches; block if nothing reaches.
4. Both free: fire the special as soon as it can do something, catch its
   breath below `STAMINA_FLOOR`, otherwise attack with the most damage per
   tick of wind-up (throws get a bonus against a blocker), or walk in.

All the frame data (speeds, ranges, stamina costs) comes from
`match_start.game_config.rule_set` (`this.gameConfig.ruleSet`), so the bot
keeps working when a new rule set changes the numbers.

## Install the SDK

Big Bad Bots support (`BigBadBotsBot`) is in `@chipzen-ai/bot` 0.4.0 and
later, and `package.json` already depends on it (`^0.5.0`, which also has the log-cap check in `chipzen-sdk validate`). Copy the folder
and install:

```bash
git clone https://github.com/chipzen-ai/chipzen-sdk.git
cp -r chipzen-sdk/packages/javascript/starters/bigbadbots/rusher my-bot
cd my-bot
npm install
npx chipzen-sdk validate .
```

The Dockerfile installs the same release from npm, so the image does not need
your `node_modules`.

## Time budget and the SDK fallback

Each fight tick's `turn_request` gives you a **250 ms response window**,
plus a **3 s time bank** for the occasional slow answer. The bank is one pot
per seat for the whole match and is never refilled between rounds.
`match_start` announces both in `game_config.response_clock` (see
[`BIGBADBOTS-GAME-STATE-PROTOCOL.md`](../../../../../docs/protocol/BIGBADBOTS-GAME-STATE-PROTOCOL.md)
section 3.4); `state.rules.deadline` still shows the rule set's older
numbers. `state.timeoutMs` is the window plus what is left of your bank.
Rusher answers in well under a millisecond, so the bank is never touched.

The methods are synchronous and JavaScript cannot interrupt them, so keep
`act()` fast. The SDK sends the protocol's own default and logs a warning if
`act()` throws or returns something that is not a fight action, and when a
request has already waited out its time behind a slow earlier answer. The
default is your previous action if it was `block`, `move_in` or `move_out`,
otherwise `idle`. The gear-up calls default to the first body on the offer,
a bid of 0, and an empty fitting. Keep any slow setup in `onMatchStart`
(call `super.onMatchStart(matchInfo)` first), not in `act()`.

## Run it from your machine: prod or staging

**Remote play does not take Big Bad Bots matches yet.** For now a Big Bad
Bots bot plays on the platform as an uploaded image (see
[Package and upload](#package-and-upload)). The commands below are ready for
when remote play opens.

Run the bot as an external-API bot. Put your token and bot id in
`chipzen.toml` (see [`docs/DEV-MANUAL.md`](../../../../../docs/DEV-MANUAL.md)) or
pass them as flags. Prod (`chipzen.ai`) is the default:

```bash
npx chipzen-sdk run-external bot.js --bot-id <your-bot-id> --token <your-token>
```

To play on staging (`staging.chipzen.ai`) instead, add `--env staging`, or set
`CHIPZEN_ENV=staging`:

```bash
npx chipzen-sdk run-external bot.js --env staging --bot-id <your-bot-id> --token <your-token>
CHIPZEN_ENV=staging npx chipzen-sdk run-external bot.js
```

The flag wins over the environment variable, and a `url` in `chipzen.toml`
wins over both.

## Package and upload

```bash
# 1. Build the image (compiles bot.js and the SDK into one executable).
docker build -t my-rusher:v1 .

# 2. Export the upload tarball.
docker save my-rusher:v1 | gzip > my-rusher.tar.gz

# 3. Upload it through the developer UI, on chipzen.ai (or staging.chipzen.ai).
```

Big Bad Bots bots run through the platform's log-cap wrapper (`log_cap.py` in
this folder), which caps how much your bot can log in one match and drops the
rest. The starter's Dockerfile already makes it the image's ENTRYPOINT, so do
not change the ENTRYPOINT or `log_cap.py`; put your own bot command in `CMD`.

An uploaded bot is started by the platform, which hands it its match URL
(`CHIPZEN_WS_URL`), so the image itself needs no prod or staging setting.
Before you build, run `npx chipzen-sdk validate .` in this folder: it hands
each method real requests from recorded matches and judges every answer the
way the server does. The platform's review then runs its own checks on the
upload.

Hard upload cap: **250 MB compressed** (the built image is separately capped
at **200 MB**). This image is roughly 130 MB, most of it the Bun runtime
inside the executable; the minimal Python that runs `log_cap.py` adds about
10 MB.

To confirm the source is not in the image, and that it does not run as root:

```bash
docker run --rm --entrypoint ls my-rusher:v1 -A /bot   # expect: bot (no .js files)
docker run --rm --entrypoint id my-rusher:v1           # must NOT print uid=0
```

## Ideas to improve it

- Read the opponent: count which moves it starts and how it answers yours,
  and counter its habits.
- Stop repeating yourself: the same attack used again in a row does less
  damage (`me.lastAttack`, `me.repeats`).
- Spend the special at the best moment instead of the first one.
- Watch the clock: a round that times out goes to the side that dealt more
  damage (`rule_set.rounds.timeout_tiebreak`).
