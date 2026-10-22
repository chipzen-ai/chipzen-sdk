# Big Bad Bots starters

Three ready-to-run Big Bad Bots bots, one folder each. They are three of the
Chipzen house bots you will meet on the platform, rewritten as short,
commented starters on the SDK's `chipzen.BigBadBotsBot`, each with a
different style:

| Starter | Style |
|---|---|
| [`rusher/`](rusher/) | Fast bodies, fast hands: walks in, jabs, throws blockers, spends its special early. |
| [`zoner/`](zoner/) | Long reach: holds the tip of its kick range, steps out of wind-ups, punishes whiffs. |
| [`turtle/`](turtle/) | Armour and patience: blocks, waits for a mistake, punishes it hard. |

Each folder is self-contained (bot, `requirements.txt`, `Dockerfile`,
`.dockerignore`, README and `log_cap.py`, the platform's log-cap wrapper
the image starts through), so copy one folder and work from there. The
constants at the top of each `bot.py` are its taste; change them and the bot
plays differently.

Each README covers the bot's style, the per-tick time budget and the SDK
fallback, running it from your machine against prod or staging
(`chipzen run-external bot.py --env staging`, or `CHIPZEN_ENV=staging`), and
packaging it for upload.

New to Big Bad Bots? The walkthrough is
[`BIGBADBOTS-QUICKSTART.md`](../../../../docs/BIGBADBOTS-QUICKSTART.md), and
[`DEV-MANUAL.md` §10](../../../../docs/DEV-MANUAL.md#10-big-bad-bots) is the full
reference. Building with a coding agent? [`AGENTS.md`](AGENTS.md) in this folder
is written for it.

The game itself is specified in
[`BIGBADBOTS-GAME-STATE-PROTOCOL.md`](../../../../docs/protocol/BIGBADBOTS-GAME-STATE-PROTOCOL.md),
and rule sets in [`RULESETS.md`](../../../../docs/RULESETS.md).
