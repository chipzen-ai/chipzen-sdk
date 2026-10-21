# AGENTS.md: building a Big Bad Bots bot

Instructions for a coding agent (Claude Code, Codex, Cursor, Copilot and
similar) building a bot for **Big Bad Bots** (game type `bigbadbots`),
Chipzen's robot-fighting game, in Python with the Chipzen SDK.

**Where this file lives and why.** It sits next to the three Big Bad Bots
starters (`rusher/`, `zoner/`, `turtle/`), so an agent working anywhere under
`packages/python/starters/bigbadbots/` in the SDK repo picks it up, and an
agent working elsewhere in the SDK does not. If you build in your own folder,
copy this file next to your `bot.py` as `AGENTS.md`. For Claude Code, also add
a `CLAUDE.md` containing the line `@AGENTS.md`, since Claude Code reads
`CLAUDE.md`.

Links below are absolute so they still work after you copy the file.

## The task in one paragraph

Copy one starter folder, change its strategy, make `chipzen validate .` pass,
build the Docker image, and export it as `<name>.tar.gz`. The human uploads
the tarball on chipzen.ai, then challenges the house bots and reads the
replays. **There is no local simulator and no way to get one: the game engine
is private and runs on the platform only.** Do not try to write a simulator,
fetch an engine, or measure win rate locally. Your local gate is
`chipzen validate`; strength is measured on the platform.

## Setup

```bash
# The SDK. Big Bad Bots support is in main, not yet in a PyPI release.
pip install "chipzen-bot @ https://github.com/chipzen-ai/chipzen-sdk/archive/refs/heads/main.tar.gz#subdirectory=packages/python"

# A starter to build on (pick rusher, zoner or turtle).
git clone https://github.com/chipzen-ai/chipzen-sdk.git
cp -r chipzen-sdk/packages/python/starters/bigbadbots/rusher my-bot
cd my-bot
```

Each starter folder holds `bot.py`, `requirements.txt`, `Dockerfile`,
`.dockerignore` and `README.md`. Keep that layout.

## Commands

| Step | Command | Pass condition |
|---|---|---|
| Check | `chipzen validate .` | Exit code 0. Every `bigbadbots_*` line PASS. The `imports: ... os` warning is expected. |
| Build | `docker build -t my-bot:v1 .` | Image builds. |
| Non-root | `docker run --rm --entrypoint id my-bot:v1` | Does not print `uid=0`. |
| Export | `docker save my-bot:v1 \| gzip > my-bot.tar.gz` | Under 250 MB (the image under 200 MB). On Windows run this in Git Bash or WSL, not PowerShell. |

Run `chipzen validate .` after every change to `bot.py`. Treat any FAIL as a
bug to fix, and any WARN on a `bigbadbots_*` check as a bug too (it means the
server would correct one of your answers).

## The game, as much as you need

A match is one bot against one bot. Every step is simultaneous: both bots are
asked at the same moment and neither sees the other's answer first.

| Phase | Method | Called | Answer |
|---|---|---|---|
| `body_shop` | `choose_body(self, state: BodyShopState)` | once | `Action.choose_body(body_id)`, an id from `state.offer` |
| `auction` | `bid(self, state: AuctionState)` | once per lot | `Action.bid(amount)`, an int in `0..state.me.credits` |
| `fitting` | `fit(self, state: FittingState)` | once | `Action.fit({slot_index: part_id})`, slot indexes into `state.slots` |
| `fight` | `act(self, state: FightState)` | every tick, 10 a second | `Action.act(move)`, one of the ten fight actions |

- **Gear-up.** One budget (`state.rules.gearup.budget`) pays for the body and
  every part; credits left after the auction are lost. The auction is
  sealed-bid, one lot at a time; the highest bid wins and pays its own bid,
  ties are a coin flip, both bidding 0 leaves the lot unsold. Never bid more
  than `state.opp.credits + 1`.
- **Fight actions:** `idle`, `move_in`, `move_out`, `block`, `punch_high`,
  `uppercut`, `kick_high`, `kick_low`, `throw`, `special` (once per round).
  A strike beats a throw, a throw beats a block, a block beats a strike.
- **Hidden wind-up.** While the opponent winds up, `state.opp.action` is
  `"attack"`, `state.opp.phase` is `"startup"` and `state.opp.frames_left` is
  `None`. Its `stamina`, `special_used`, `last_attack`, `repeats` and `buffs`
  are frozen at their pre-move values. Under the launch rule set the move is
  revealed only after it lands, so decide on `phase == "startup"`, not on
  the move name.
- **Rounds.** Best of three. A round ends on a KO, at
  `state.rules.rounds.round_ticks`, or by forfeit; at the tick limit
  `state.rules.rounds.timeout_tiebreak` decides it.

## Hard rules for the code

1. **Subclass `chipzen.BigBadBotsBot` and implement `act()`.** Do not use the
   poker `Bot.decide()` pattern, poker actions (`fold`, `call`, `raise_to`),
   or raw WebSocket code. The SDK handles the connection, `request_id` and
   timing.
2. **Return an `Action` built with `Action.choose_body`, `Action.bid`,
   `Action.fit` or `Action.act` from every method, on every call.** Never
   return `None`.
3. **Answer every tick.** `block` protects only on the tick it is sent; to
   keep blocking, return `block` again on the next tick. Keep any multi-tick
   plan on `self` and re-check it in `act()`.
4. **Stay under the per-tick deadline**, `state.rules.deadline.deadline_ms`
   (100 ms today, provisional). The 3 s per-round time bank is for
   emergencies; a slow `act()` drains it and then every slow tick plays the
   default. In `act()`: no file or network I/O, no imports, no model loading,
   no logging on every tick. Precompute in `on_match_start()`.
5. **Read every number from `state.rules`; hard-code none.** Typed sections:
   `state.rules.gearup`, `.arena`, `.rounds`, `.forfeit`, `.deadline`. The
   full table: `state.rules["moves"]`, `["specials"]`, `["bodies"]`,
   `["parts"]`, `["combat"]` and the rest (read-only mappings). Use `.get()`
   with a default for optional keys such as
   `state.rules["combat"].get("intent_reveal", "on_active")`. Branch on those
   behaviour keys, not on `state.rules.id`.
6. **If you override `on_match_start()` or `on_reconnected()`, call `super()`
   first.** Without it `state.rules` is `None`.
7. **Do not mutate the state objects you are handed.** `chipzen validate`
   fails a bot that does.
8. **Track your own fighter from the next state, not from what you sent.** A
   busy fighter (`state.me.phase is not None`) ignores new actions, and an
   action it cannot afford plays as `idle`. Neither is reported as an error.
9. **Fittings:** one part per slot, and the part's `slot` must equal
   `state.slots[index]`. Wrong entries are dropped.
10. **Keep the Dockerfile's packaging intact:** the Cython compile step
    (`cythonize -i bot.py && rm bot.py`), `USER 10001`, and the
    `ENTRYPOINT`. Add dependencies to `requirements.txt` only if needed;
    `chipzen validate` checks them against the platform's allow-list.
11. **Keep `main()`** in `bot.py` as the starter has it. The platform runs
    the image and passes the match URL in `CHIPZEN_WS_URL`.

## Where things are in the starters

Use the starter you copied as the pattern library. In `rusher/bot.py`:

- `move_data`, `reach_of`, `startup_of`, `can_afford`: reading frame data from
  the rule set (a move has a fixed `range`, or a `reach` multiple of
  `fighter.strike_range`).
- `part_value`, `best_fitting`: valuing parts and building a fitting.
- `bid`: the auction pattern (gain to the best fitting, fair share of
  credits, cap at `opp.credits + 1`, all in on a useful last lot).
- `act`: the per-tick decision order (busy, punish, answer a wind-up,
  neutral).

`zoner/bot.py` adds spacing and walls; `turtle/bot.py` adds blocking every
tick and playing the round clock.

## Done checklist

- [ ] `chipzen validate .` exits 0, every `bigbadbots_*` check PASS.
- [ ] `bigbadbots_timing` shows the slowest call well under the per-tick
      deadline.
- [ ] No number from the rule set is hard-coded in `bot.py`.
- [ ] `docker build` succeeds; the image runs as non-root; the tarball is
      under 250 MB.
- [ ] You told the human: the tarball path, what you changed and why, and
      what to look for in the replays.

The human then uploads the tarball on chipzen.ai (or staging.chipzen.ai),
chooses Big Bad Bots as the bot's game, waits for review to make it
`active`, and challenges the house bots.

## Reference

- Walkthrough: https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/BIGBADBOTS-QUICKSTART.md
- Full reference (budget, masking, auction, rule set):
  https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/DEV-MANUAL.md#10-big-bad-bots
- Pitfalls: https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/COMMON-PITFALLS.md#big-bad-bots-pitfalls
- Wire spec (authoritative):
  https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/protocol/BIGBADBOTS-GAME-STATE-PROTOCOL.md
- Rule sets: https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/RULESETS.md
- SDK source: `chipzen.bigbadbots_bot` (the base class),
  `chipzen.bigbadbots` (the state models), `chipzen.bigbadbots_rules`
  (`state.rules`), under
  https://github.com/chipzen-ai/chipzen-sdk/tree/main/packages/python/src/chipzen
