# AGENTS.md: building a Big Bad Bots bot in JavaScript

Instructions for a coding agent (Claude Code, Codex, Cursor, Copilot and
similar) building a bot for **Big Bad Bots** (game type `bigbadbots`),
Chipzen's robot-fighting game, in JavaScript with `@chipzen-ai/bot`.

This is the JavaScript version of the
[Python AGENTS.md](https://github.com/chipzen-ai/chipzen-sdk/blob/main/packages/python/starters/bigbadbots/AGENTS.md).
The game and the rules are the same; the SDK, the starter and the commands
differ.

**Where this file lives and why.** It sits next to the JavaScript starter
(`rusher/`), so an agent working under
`packages/javascript/starters/bigbadbots/` in the SDK repo picks it up. If
you build in your own folder, copy this file next to your `bot.js` as
`AGENTS.md`. For Claude Code, also add a `CLAUDE.md` containing the line
`@AGENTS.md`, since Claude Code reads `CLAUDE.md`.

Links below are absolute so they still work after you copy the file.

## The task in one paragraph

Copy the Rusher starter, change its strategy, make
`npx chipzen-sdk validate .` pass, build the Docker image, and export it as
`<name>.tar.gz`. The human uploads the tarball on chipzen.ai, then
challenges the platform bots and reads the replays. **There is no local
simulator and no way to get one: the game engine is private and runs on the
platform only.** Do not try to write a simulator, fetch an engine, or
measure win rate locally. Your local gate is `chipzen-sdk validate`;
strength is measured on the platform.

## Setup

Needs Node.js 20+ and npm, Git and Docker.

```bash
# A starter to build on. Its package.json depends on @chipzen-ai/bot ^0.5.0 || ^0.6.0
# (Big Bad Bots support is in 0.4.0 and later; the Dockerfile check in
# `chipzen-sdk validate` needs 0.5.0).
git clone https://github.com/chipzen-ai/chipzen-sdk.git
cp -r chipzen-sdk/packages/javascript/starters/bigbadbots/rusher my-bot
cd my-bot
npm install
```

The starter folder holds `bot.js`, `package.json`, `Dockerfile`,
`.dockerignore`, `log_cap.py` and `README.md`. Keep that layout. There is
only one JavaScript starter. The Python starters also include Zoner
(spacing) and Turtle (blocking); their `bot.py` files port to JavaScript the
same way.

## Commands

| Step | Command | Pass condition |
|---|---|---|
| Check | `npx chipzen-sdk validate .` | Exit code 0. Every `bigbadbots_*` line PASS. |
| Build | `docker build -t my-bot:v1 .` | Image builds. |
| Non-root | `docker run --rm --entrypoint id my-bot:v1` | Does not print `uid=0`. |
| Export | `docker save my-bot:v1 \| gzip > my-bot.tar.gz` | Under 250 MB (the image under 200 MB). On Windows run this in Git Bash or WSL, not PowerShell. |

Run `npx chipzen-sdk validate .` after every change to `bot.js`. Treat any
FAIL as a bug to fix, and any WARN on a `bigbadbots_*` check as a bug too (it
means the server would correct one of your answers, or your code changes
the state it is handed).

## The game, as much as you need

A match is one bot against one bot. Every step is simultaneous: both bots are
asked at the same moment and neither sees the other's answer first.

| Phase | Method | Called | Answer |
|---|---|---|---|
| `body_shop` | `chooseBody(state)` | once | `Action.chooseBody(bodyId)`, an id from `state.offer` |
| `auction` | `bid(state)` | once per lot | `Action.bid(amount)`, an integer in `0..state.me.credits` |
| `fitting` | `fit(state)` | once | `Action.fit({ [slotIndex]: partId })`, slot indexes into `state.slots` |
| `fight` | `act(state)` | every tick, 10 a second | `Action.act(move)`, one of the ten fight actions |

- **Gear-up.** One budget (`state.rules.gearup.budget`) pays for the body and
  every part; credits left after the auction are lost. The auction is
  sealed-bid, one lot at a time; the highest bid wins and pays its own bid,
  ties are a coin flip, both bidding 0 leaves the lot unsold. Never bid more
  than `state.opp.credits + 1`.
- **Fight actions:** `idle`, `move_in`, `move_out`, `block`, `punch_high`,
  `uppercut`, `kick_high`, `kick_low`, `throw`, `special` (once per round).
  A strike beats a throw, a throw beats a block, a block beats a strike.
- **Hidden wind-up.** While the opponent winds up, `state.opp.action` is
  `"attack"`, `state.opp.phase` is `"startup"` and `state.opp.framesLeft` is
  `null`. Its `stamina`, `specialUsed`, `lastAttack`, `repeats` and `buffs`
  are frozen at their pre-move values. Under the launch rule set the move is
  revealed only after it lands, so decide on `phase === "startup"`, not on
  the move name.
- **Rounds.** Best of three. A round ends on a KO, at
  `state.rules.rounds.roundTicks`, or by forfeit; at the tick limit
  `state.rules.rounds.timeoutTiebreak` decides it.

## Hard rules for the code

1. **Extend `BigBadBotsBot` from `@chipzen-ai/bot`, implement `act()`, and
   export the class.** `chipzen-sdk validate` imports it from `bot.js`. Do
   not use the poker `decide()` pattern, poker actions, or raw WebSocket
   code. The SDK handles the connection, `request_id` and timing.
2. **Return an `Action` built with `Action.chooseBody`, `Action.bid`,
   `Action.fit` or `Action.act` from every method, on every call.** Never
   return `null` or `undefined`. `Action.bid` throws on a non-integer, so
   round your bid (`Math.trunc`). `Action.act` throws on anything that is
   not a fight action.
3. **Answer every tick.** `block` protects only on the tick it is sent; to
   keep blocking, return `block` again on the next tick. Keep any multi-tick
   plan on `this` and re-check it in `act()`.
4. **Stay under the per-tick response window**, 250 ms, announced in
   `match_start` as `game_config.response_clock.deadline_ms` (not
   `state.rules.deadline.deadlineMs`, which still shows the rule set's older
   100 ms). The SDK does not parse `response_clock` yet; read it from the raw
   message in `onMatchStart(matchInfo)`. The 3 s time bank
   (`response_clock.time_bank_ms`) is one pot for the whole match, never
   refilled between rounds, and is for emergencies. The methods are
   synchronous and JavaScript cannot interrupt them, so a slow `act()` holds
   up every later tick. In `act()`: no file or network I/O, no `await`, no
   module loading, no logging on every tick. Precompute in `onMatchStart()`.
5. **Read every number from the rule set; hard-code none.** Typed sections,
   camelCase: `state.rules.gearup`, `.arena`, `.rounds`, `.forfeit`,
   `.deadline`. The full table (`moves`, `specials`, `bodies`, `parts`,
   `combat` and the rest) is `state.rules.params` and
   `this.gameConfig.ruleSet`, with the wire's snake_case keys. Use `??` with
   a default for optional keys, for example
   `state.rules.params.combat?.intent_reveal ?? "on_active"`. Branch on those
   behaviour keys, not on `state.rules.id`.
6. **If you override `onMatchStart()` or `onReconnected()`, call `super`
   first** (`super.onMatchStart(matchInfo)`, `super.onReconnected(message)`).
   Without it `state.rules` and `this.gameConfig` are `null`.
7. **Do not mutate the state objects you are handed.** `chipzen-sdk validate`
   warns on a bot that does. Copy what you need to change.
8. **Never call `process.exit()` from a bot method.** `chipzen-sdk validate`
   fails a bot that does: on the platform it ends the bot, and a seat that
   stops answering forfeits.
9. **Track your own fighter from the next state, not from what you sent.** A
   busy fighter (`state.me.phase !== null`) ignores new actions, and an
   action it cannot afford plays as `idle`. Neither is reported as an error.
10. **Fittings:** one part per slot, and the part's `slot` must equal
    `state.slots[index]`. Wrong entries are dropped.
11. **Keep the Dockerfile's packaging intact:** the `bun build --compile`
    step, `USER 10001`, the `COPY log_cap.py /opt/chipzen/log_cap.py` line
    and the `ENTRYPOINT`
    (`["python3", "-I", "-S", "-u", "/opt/chipzen/log_cap.py", "--"]`). The
    ENTRYPOINT is the platform's log-cap wrapper, which caps how much the bot
    can log in one match; the platform's review rejects an image that does
    not start through it. Never edit `log_cap.py` or the ENTRYPOINT; the bot
    command is `CMD`. `chipzen-sdk validate` checks the ENTRYPOINT.
12. **Avoid blocked Node modules.** `chipzen-sdk validate` fails on
    `child_process`, `cluster`, `dgram`, `dns`, `net`, `tls`, `repl`, `vm`
    and `worker_threads`, and warns on `fs`. Add npm dependencies to
    `package.json` only if needed: the Dockerfile installs from it and
    bundles everything into one executable.
13. **Keep `main()`** in `bot.js` as the starter has it, including the check
    that runs it only as the entry point. The platform runs the image and
    passes the match URL in `CHIPZEN_WS_URL`.

## Where things are in the starter

In `rusher/bot.js`:

- `ruleSection`, `moveData`, `reachOf`, `startupOf`, `canAfford`: reading
  frame data from the rule set (a move has a fixed `range`, or a `reach`
  multiple of the fighter's `strikeRange`).
- `partValue`, `bestFitting`: valuing parts and building a fitting.
- `bid`: the auction pattern (gain to the best fitting, fair share of
  credits, cap at `opp.credits + 1`, all in on a useful last lot).
- `act`: the per-tick decision order (busy, punish, answer a wind-up,
  neutral).

## Done checklist

- [ ] `npx chipzen-sdk validate .` exits 0, every `bigbadbots_*` check PASS.
- [ ] `bigbadbots_timing` shows the slowest call well under the per-tick
      deadline.
- [ ] No number from the rule set is hard-coded in `bot.js`.
- [ ] `docker build` succeeds; the image runs as non-root; the tarball is
      under 250 MB.
- [ ] You told the human: the tarball path, what you changed and why, and
      what to look for in the replays.

The human then uploads the tarball on chipzen.ai, chooses Big Bad Bots as
the bot's game, waits for review to make it `active`, and challenges the
platform bots.

## Reference

- Walkthrough: https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/BIGBADBOTS-QUICKSTART-JAVASCRIPT.md
- Full reference (budget, masking, auction, rule set; Python examples, same
  names in camelCase):
  https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/DEV-MANUAL.md#10-big-bad-bots
- Pitfalls: https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/COMMON-PITFALLS.md#big-bad-bots-pitfalls
- Wire spec (authoritative):
  https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/protocol/BIGBADBOTS-GAME-STATE-PROTOCOL.md
- Rule sets: https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/RULESETS.md
- SDK source: `bigbadbots_bot.ts` (the base class), `bigbadbots.ts` (the
  state models and `RuleSet`), under
  https://github.com/chipzen-ai/chipzen-sdk/tree/main/packages/javascript/src
