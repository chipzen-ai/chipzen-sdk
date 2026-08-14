# Building your first bot with an AI coding tool

Copy-paste prompts that produce a working Chipzen bot from nothing.

If you already have a poker bot and want it packaged for upload or wired to the
remote-play API, you want the other document:
[`PACKAGING-WITH-AI-AGENTS.md`](PACKAGING-WITH-AI-AGENTS.md). This one is for
starting from an empty directory.

Every prompt here follows the same shape: point the tool at files that exist in
this repo, tell it what to build, and end on a check the tool has to run rather
than a claim it can make. The check is the part that matters. A coding tool will
confidently produce a `decide()` that returns an action the server never offered,
and you will only find out when your bot folds every hand.

## Contents

- [Before you paste anything](#before-you-paste-anything)
- [Prompt 1: a first bot that plays](#prompt-1-a-first-bot-that-plays)
- [Strategy variations](#strategy-variations)
  - [A: tight-aggressive heads-up](#a-tight-aggressive-heads-up)
  - [B: pot-odds caller](#b-pot-odds-caller)
  - [C: position-aware opener](#c-position-aware-opener)
- [Notes per tool](#notes-per-tool)
- [Checking what came back](#checking-what-came-back)
- [Ways these prompts go wrong](#ways-these-prompts-go-wrong)

## Before you paste anything

**1. Clone the repo so the tool can read it.**

```bash
git clone https://github.com/chipzen-ai/chipzen-sdk.git
cd chipzen-sdk
```

The prompts reference repo paths directly. A tool that cannot open those paths
will fall back on whatever it remembers about poker APIs in general, which is
how you get a bot calling methods that do not exist.

**2. Install the SDK and the CLI.**

```bash
pip install chipzen-bot     # Python 3.10+
chipzen-sdk --help
```

`chipzen-sdk validate` is the go/no-go gate at the end of every prompt. It runs
the same checks the upload pipeline runs, so a green validate means the pipeline
will accept your bot. It says nothing about whether the bot plays well.

**3. Know which files are worth putting in context.**

Adding the whole repo wastes context and buries the parts that matter. These are
the files the prompts actually depend on:

| File | Why the tool needs it |
|---|---|
| [`docs/QUICKSTART.md`](QUICKSTART.md) | The build, validate, export loop, and the common first-time mistakes |
| [`packages/python/starters/python/bot.py`](../packages/python/starters/python/bot.py) | The scaffold to copy. Ships a `table_position()` helper and a `main()` the Dockerfile expects |
| [`examples/reference-bot/bot.py`](../examples/reference-bot/bot.py) | A worked bot with real branching: preflop buckets, made-hand classes, opponent-aggression tracking |
| [`docs/DEV-MANUAL.md`](DEV-MANUAL.md) sections 2, 4, 6 | SDK surface, the local test harness, the decision-timeout budget |
| [`docs/protocol/POKER-GAME-STATE-PROTOCOL.md`](protocol/POKER-GAME-STATE-PROTOCOL.md) | Layer 2. What is in a `GameState` and what the fields mean |
| [`docs/protocol/TRANSPORT-PROTOCOL.md`](protocol/TRANSPORT-PROTOCOL.md) | Layer 1. Only needed if the tool starts hand-rolling WebSocket code, which it should not |
| [`docs/COMMON-PITFALLS.md`](COMMON-PITFALLS.md) | The mistakes that pass validate and lose matches |

For JavaScript or Rust, swap the starter path for
[`packages/javascript/starters/javascript/`](../packages/javascript/starters/javascript/)
or [`packages/rust/starters/rust/`](../packages/rust/starters/rust/), and see
[`docs/PORTING-BETWEEN-SDKS.md`](PORTING-BETWEEN-SDKS.md) for how the same bot
translates across the three.

## Prompt 1: a first bot that plays

Start here. This produces a bot that is not good but is correct: it never returns
an illegal action, it stays in hands long enough to see flops, and it passes
validate. Get this green before asking for strategy.

````text
Build me a working Chipzen poker bot from the SDK starter.

READ THESE FIRST, and follow them over anything you remember about poker APIs.
If a doc and your memory disagree, the doc wins. Do not invent methods, fields
or CLI flags; if something you expect is missing, tell me instead of guessing.
- docs/QUICKSTART.md
- packages/python/starters/python/bot.py     (the scaffold you are copying)
- examples/reference-bot/bot.py              (a worked bot with real branching)
- docs/DEV-MANUAL.md sections 2 and 4        (SDK surface, local test harness)
- docs/protocol/POKER-GAME-STATE-PROTOCOL.md (what is in a GameState)

STEPS
1. Copy packages/python/starters/python/ to a new directory OUTSIDE this repo,
   at <PATH FOR MY BOT>. Keep the Dockerfile, .dockerignore and requirements.txt
   exactly as they are. Keep bot.py's main() and the `from bot import main`
   entry point the Dockerfile calls. You are replacing decide(), nothing else.
2. Write decide(self, state: GameState) -> Action with this behaviour:
   - Check when checking is free.
   - Call when the amount to call is at most 10% of my stack.
   - Otherwise fold.
   This is deliberately simple. I want a correct bot before a clever one.
3. Obey these rules, which are the ones that actually break bots:
   - The action you return MUST be in state.valid_actions. Read that list every
     time; never assume "fold" or "check" is available.
   - Action.raise_to(n) takes a TOTAL bet size, not an increment, and n must be
     within [state.min_raise, state.max_raise]. Both are 0 when raising is not
     legal, so guard on that before computing a raise.
   - decide() must be fast and synchronous. The end-to-end budget is 10000 ms
     for human-vs-bot and only 2000 ms for ranked bot-vs-bot and tournaments.
   - Do not load anything heavy at module import. Cold start budget is ~15 s.
     If you need a table or a model, build it in on_match_start.
4. Write a local test that drives decide() through the situations it branches
   on, constructing GameState directly. Cover at minimum: a free check, a cheap
   call, an expensive fold, and a turn where "check" is absent from
   valid_actions. Follow the harness described in DEV-MANUAL.md section 4.
5. Run the gate, in this order, and fix whatever it reports until both are green:
       chipzen-sdk validate <PATH FOR MY BOT>
       chipzen-sdk validate <PATH FOR MY BOT> --check-connectivity
   Do not tell me it is done while either is red. Show me the actual output.

DELIVERABLES
- The bot directory, with decide() replaced and everything else intact.
- The test file, and the output of running it.
- The output of both validate runs.
- A short note on which state fields you used and which you ignored.
````

The `--check-connectivity` run is the one worth waiting for. The static checks
only read your file; this one drives your bot through a canned protocol exchange
against an in-process mock WebSocket (handshake, a full hand, multi-turn
`request_id` echo, `match_end`). It catches a `decide()` that works on the states
you imagined and crashes on the ones the protocol actually sends.

## Strategy variations

Run these **after** Prompt 1 is green. Each one replaces `decide()` in a bot that
already works, so if validate goes red you know the strategy change caused it.

They are deliberately narrow. Each expresses one idea clearly enough that you can
read the result and see the idea in it, which is the point: these are examples of
how to say what you want, not strategies worth deploying.

### A: tight-aggressive heads-up

````text
Replace decide() in my working Chipzen bot with a tight-aggressive heads-up
strategy. Keep everything else, including main() and the entry point.

Read examples/reference-bot/bot.py first. Its _preflop_bucket() and
_made_hand_class() helpers are the shape I want; write your own in that style
rather than importing them, since that file is an example and not a library.

BEHAVIOUR
- Preflop: play few hands, and raise rather than call when you play one. Bucket
  hole cards into premium / strong / medium / weak from state.hole_cards. Open
  premium and strong hands with a raise to roughly 3x the minimum raise. Fold
  medium and weak hands to any meaningful bet.
- Postflop: with two pair or better, bet about two thirds of state.pot. With one
  pair, check, and call only bets up to a third of the pot. With nothing, check
  or fold. No bluffs.
- Never bet into aggression you have not accounted for. Track opponent raises
  within the current hand using the on_turn_result hook, reset per hand in
  on_round_start, and check the count before betting for value.

CONSTRAINTS
- Every returned action must be in state.valid_actions.
- Every raise amount must be clamped into [state.min_raise, state.max_raise],
  and you must handle both being 0, which means raising is illegal here.
- Heads-up means len(state.opponent_stacks) == 1. Do not hardcode that; read the
  table size as len(state.opponent_stacks) + 1 so the bot survives a 6-max seat.

Extend my existing tests to cover: a premium open, a fold to a big preflop
raise, a value bet with two pair, and a fold to postflop pressure with one pair.
Then run chipzen-sdk validate --check-connectivity and show me the output.
````

### B: pot-odds caller

````text
Replace decide() in my working Chipzen bot with a pot-odds strategy. Keep
main() and the entry point.

BEHAVIOUR
- Compute pot odds as state.to_call / (state.pot + state.to_call). Guard against
  a zero denominator.
- Call when the price is good, fold when it is not. Use a threshold I can change
  in one place, as a named constant at the top of the file, not a literal buried
  in a branch.
- Check when checking is free. This bot does not raise at all: it is a baseline
  for whether pot odds alone beat the passive house bots, so leave the raising
  branch out entirely rather than adding a token one.
- Do not use randomness. I want this bot's decisions reproducible so I can
  compare threshold values against each other.

packages/python/src/chipzen/examples/tight_aggressive.py has a pot-odds branch
you can read for the arithmetic, but it mixes in randomness and preflop logic
that I do not want here.

Write tests that pin the threshold behaviour: a call just inside the threshold,
a fold just outside it, and a free check. Then run
chipzen-sdk validate --check-connectivity and show me the output.
````

### C: position-aware opener

````text
Replace decide() in my working Chipzen bot with a position-aware opening
strategy. Keep main() and the entry point.

The starter you copied already ships a table_position() helper in bot.py that
derives position from state.your_seat, state.dealer_seat and the table size.
Read it and use it. Do not write your own.

BEHAVIOUR
- Preflop, open wider in late position than early position. Concretely: raise a
  broad range on the button or cutoff, a narrow one in early position, and
  defend the big blind against small raises.
- Postflop, continuation-bet when you were the preflop aggressor and the board
  is unthreatening. Check otherwise. You will need to remember whether you
  raised preflop, so track it per hand and reset it in on_round_start.
- Derive the table size as len(state.opponent_stacks) + 1 every time. The bot
  must behave sensibly heads-up, where table_position() returns "button_sb" or
  "big_blind", and at a 6-max table without changes.

CONSTRAINTS
- Every returned action must be in state.valid_actions.
- Clamp raises into [state.min_raise, state.max_raise] and handle both being 0.

Write tests that construct GameStates with your_seat and dealer_seat set so each
position branch is exercised, at both 2 and 6 players. Then run
chipzen-sdk validate --check-connectivity and show me the output.
````

## Notes per tool

The prompts work as written in all of these. What changes is how you get the
repo in front of the tool.

**Claude Code, Codex, Aider, and other terminal agents.** Run them from inside
the cloned repo, or from a directory that can see both the clone and where you
want the bot. They can read the referenced paths, run `chipzen-sdk validate`
themselves, and act on the output, which is the whole point of the last step.
This is the setup the prompts are written for.

**Cursor, Windsurf, and other editor agents.** Open the clone as the workspace.
Reference the files explicitly with the editor's file-mention syntax rather than
relying on automatic retrieval, which tends to pull fragments of the protocol
docs and miss the starter. Run the validate step in the integrated terminal.

**GitHub Copilot chat.** Attach the specific files rather than the workspace.
Copilot's default context is narrow, so a prompt that names seven files will
often get answered from one of them. Consider running Prompt 1 and each variation
as separate conversations with only the files that step needs.

**Plain ChatGPT or Claude in a browser.** No filesystem access, so paste the
contents of `packages/python/starters/python/bot.py` and
`examples/reference-bot/bot.py` into the conversation, and drop step 1 from the
prompt since it cannot copy directories. You will run validate yourself and paste
the output back. This works, but the loop is manual and slow, and the tool cannot
verify its own claims. Prefer one of the above if you can.

## Checking what came back

Read the diff before you run anything. Four things are worth checking by eye,
because all four produce a bot that validates and then plays badly:

1. **Does every return path check `state.valid_actions`?** A bare
   `return Action.fold()` at the bottom of `decide()` is the common bug. Folding
   is not always legal.
2. **Are raise amounts totals?** `Action.raise_to(state.pot // 2)` is wrong if
   the tool meant "bet half the pot" and the pot already contains your own bet.
   Check the arithmetic against `min_raise` and `max_raise`.
3. **Did it invent an SDK surface?** `state.opponent_stack` (singular),
   `state.position`, and `Action.bet()` do not exist. The real field list is in
   the `GameState` docstring in
   [`packages/python/src/chipzen/models.py`](../packages/python/src/chipzen/models.py).
4. **Is anything heavy at module scope?** An import-time table build or model
   load can blow the ~15 s cold-start budget, and the failure looks like the
   platform rejecting your container rather than anything about your code.

Then run the gate yourself rather than trusting the transcript:

```bash
chipzen-sdk validate ./my-bot/
chipzen-sdk validate ./my-bot/ --check-connectivity
```

Both must exit 0. After that, follow
[`QUICKSTART.md` section 5](QUICKSTART.md#5-build) onward to build the image,
export the tarball, and upload.

## Ways these prompts go wrong

**The tool writes its own WebSocket client.** It has seen a lot of poker bots
that hand-roll transports. The SDK already handles the connection, the two-layer
handshake, `request_id` echoing, ping/pong, `action_rejected` retries and
reconnect. If you see `websockets` or `asyncio` plumbing appearing in `bot.py`
beyond what the starter shipped, stop and point the tool back at the starter.

**The tool rewrites `main()` or the Dockerfile.** The Python starter's Dockerfile
compiles `bot.py` to a Cython `.so` and its `ENTRYPOINT` calls
`from bot import main; main()`. Renaming `main`, or moving the bot class into
another module, breaks the image in a way that only shows up at container start.
Every prompt above says to leave these alone; check that it did.

**The tool claims validate passed without running it.** Ask for the literal
output. `chipzen-sdk validate` prints a per-check result list, so a transcript
without one is a transcript without a run.

**The bot is legal but folds constantly.** That is not a prompt failure, it is
what a cautious strategy looks like. Compare against the baselines in
[`packages/python/src/chipzen/examples/`](../packages/python/src/chipzen/examples/):
`call_bot.py` never folds, `random_bot.py` picks uniformly from the legal
actions, and `tight_aggressive.py` sits in between. If your bot loses to
`call_bot`, the strategy is the problem, not the plumbing.

**You expected the SDK to tell you if the bot is any good.** It will not.
`chipzen-sdk validate` proves your bot conforms to the protocol and will be
accepted at upload. There is no local match simulator, hand evaluator or win-rate
measurement in this SDK by design. Strength testing happens on the platform after
upload.

## Where to file issues

- **A prompt that produced something `chipzen-sdk validate` rejected, or that
  referenced a file or method that does not exist**: open an issue on
  [chipzen-ai/chipzen-sdk](https://github.com/chipzen-ai/chipzen-sdk/issues).
  Include the tool you used and what it produced.
- **Platform, account, matchmaking or billing questions**: `support@chipzen.ai`
  or Discord, not this repo.
