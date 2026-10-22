# Zoner: a Big Bad Bots starter

**Style: long arms, long kicks.** Zoner buys reach, then stands at the very
edge of its own high-kick range, where it can hit you and your jab cannot hit
it. When you wind up, it steps back out of range; when you whiff, it
punishes. It never brawls and it never throws.

Zoner is one of the Chipzen house bots you will meet on the platform,
rewritten as a short, commented starter on the SDK's `BigBadBotsBot`. Copy
this folder, change it, upload it.

## Files

| File | Purpose |
|---|---|
| [`bot.py`](bot.py) | The bot. Its taste is the constants at the top; its thinking is the methods below them. |
| [`requirements.txt`](requirements.txt) | The SDK pin (`chipzen-bot~=0.5`). Add your own dependencies here. |
| [`Dockerfile`](Dockerfile) | Multi-stage Cython build: the runtime image contains no `.py` source for the bot. |
| [`.dockerignore`](.dockerignore) | Keeps the build context tight (no caches, virtualenvs, secrets). |
| [`log_cap.py`](log_cap.py) | The platform's per-match log cap, the image's ENTRYPOINT. Ship it unchanged. |

## How it plays

**Gear-up.** Each body on the offer is scored by Zoner's attribute weights
(reach 1.8 leads everything), a bonus for the bodies it likes (`mantis`,
`kite`, `viper`), and the slots its leftover credits can fill. In the
auction it works out how much the lot improves its best fitting and bids its
fair share of credits scaled by that gain, times `BID_TEMPER = 0.75`. It
never bids more than the opponent could beat, and it goes all in on a useful
last lot. The fitting puts the most valuable part into each slot.

**The fight, every tick, in order:**

1. Busy with its own move: nothing to decide.
2. The opponent is stuck in recovery, hitstun or a daze (a whiff): hit it
   with the most damaging move that lands before it can act again, or walk
   in if a kick can still land in time.
3. The opponent is winding up a hidden move that could reach: step back out
   of range, or block with a wall behind it.
4. Both free: hold `PREFERRED_RANGE`, 85% to 100% of its own high-kick
   reach. Too close, back off (or fight out of a corner); too far, walk in;
   in range, kick if it has the stamina.

All the frame data (speeds, ranges, stamina costs) comes from
`match_start.game_config.rule_set`, so the bot keeps working when a new rule
set changes the numbers.

## Time budget and the SDK fallback

Each fight tick's `turn_request` gives you a **250 ms response window**,
plus a **3 s time bank** for the occasional slow answer. The bank is one pot
per seat for the whole match and is never refilled between rounds.
`match_start` announces both in `game_config.response_clock` (see
[`BIGBADBOTS-GAME-STATE-PROTOCOL.md`](../../../../../docs/protocol/BIGBADBOTS-GAME-STATE-PROTOCOL.md)
section 3.4); `state.rules.deadline` still shows the rule set's older
numbers. `state.timeout_ms` is the window plus what is left of your bank.
Zoner answers in well under a millisecond, so the bank is never touched.

You never send a late answer by accident: the SDK waits for `act()` until
just short of `timeout_ms`, then sends the protocol's own default and logs a
warning. The default is your previous action if it was `block`, `move_in` or
`move_out`, otherwise `idle`. The same default goes out if `act()` raises or
returns something that is not a fight action. The gear-up calls default to
the first body on the offer, a bid of 0, and an empty fitting. Keep any slow
setup in `on_match_start`, not in `act()`.

## Run it from your machine: prod or staging

**Remote play does not take Big Bad Bots matches yet.** For now a Big Bad
Bots bot plays on the platform as an uploaded image (see
[Package and upload](#package-and-upload)). The commands below are ready for
when remote play opens.

Install the SDK (Big Bad Bots support is in `chipzen-bot` 0.5.0 and later):

```bash
pip install "chipzen-bot~=0.5"
```

Then run the bot as an external-API bot. Put your token and bot id in
`chipzen.toml` (see [`docs/DEV-MANUAL.md`](../../../../../docs/DEV-MANUAL.md)) or
pass them as flags. Prod (`chipzen.ai`) is the default:

```bash
chipzen run-external bot.py --bot-id <your-bot-id> --token <your-token>
```

To play on staging (`staging.chipzen.ai`) instead, add `--env staging`, or set
`CHIPZEN_ENV=staging`:

```bash
chipzen run-external bot.py --env staging --bot-id <your-bot-id> --token <your-token>
CHIPZEN_ENV=staging chipzen run-external bot.py
```

The flag wins over the environment variable, and a `url` in `chipzen.toml`
wins over both.

## Package and upload

```bash
# 1. Build the image (compiles bot.py; the image holds only the .so).
docker build -t my-zoner:v1 .

# 2. Export the upload tarball.
docker save my-zoner:v1 | gzip > my-zoner.tar.gz

# 3. Upload it through the developer UI, on chipzen.ai (or staging.chipzen.ai).
```

Big Bad Bots bots run through the platform's log-cap wrapper (`log_cap.py` in
this folder), which caps how much your bot can log in one match and drops the
rest. The starter's Dockerfile already makes it the image's ENTRYPOINT, so do
not change the ENTRYPOINT or `log_cap.py`; put your own bot command in `CMD`.

An uploaded bot is started by the platform, which hands it its match URL
(`CHIPZEN_WS_URL`), so the image itself needs no prod or staging setting.
Before you build, run `chipzen validate .` in this folder: it hands each
method real requests from recorded matches and judges every answer the way
the server does. The `imports: ... os` warning is expected (`main()` reads
`CHIPZEN_WS_URL`). The platform's review then runs its own checks on the
upload. After that, challenge the house bots and read the replays (see
[`BIGBADBOTS-QUICKSTART.md`](../../../../../docs/BIGBADBOTS-QUICKSTART.md)
section 9).

Hard upload cap: **250 MB compressed** (the built image is separately capped
at **200 MB**). This image exports at roughly 45 MB compressed.

To confirm the source is not in the image:

```bash
docker run --rm --entrypoint sh my-zoner:v1 -c "ls /bot/"
# Expect bot.cpython-312-<arch>-linux-gnu.so and requirements.txt, no bot.py.
```

## Ideas to improve it

- Bait: step into the opponent's range for a tick and step out again to draw
  a whiff, then punish it.
- Read the opponent: a bot that always walks in can be met with a kick timed
  to land as it arrives.
- Use the special: Zoner only fires a striking special as a punish.
- Watch the clock: a round that times out goes to the side that dealt more
  damage (`rule_set.rounds.timeout_tiebreak`), so a lead is worth protecting.
