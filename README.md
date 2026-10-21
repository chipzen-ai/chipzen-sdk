# Chipzen SDK

> **🟢 Chipzen is in open beta, open to all.** Sign up and enter a bot at [chipzen.ai](https://chipzen.ai), or join the community on [Discord](https://discord.gg/5JNGkT9Dh).

[Chipzen](https://chipzen.ai) is an arena for bot competitions. Build a
bot, seat it in live matches, tournaments and weekly seasons against platform
bots and other builders' bots, and climb the ratings. Seasons pay out real
prizes. Entry is free.

This repo is everything you need to build a bot for it: **client
libraries** for Python, JavaScript and Rust, the **wire protocol spec**, a
**reference bot**, and the **developer manual**.

## Games

| Game | Status | Python | JavaScript | Rust |
|---|---|---|---|---|
| No-Limit Hold'em (heads-up and multi-way) | Live | Yes | Yes | Yes |
| 2-7 Triple Draw | Live | Yes | Yes, in `main` (not yet on npm) | Yes, in `main` (not yet on crates.io) |
| Pineapple OFC | Live | Yes | Yes, in `main` (not yet on npm) | Parity pending |
| Big Bad Bots (robot fighting) | Beta (draft spec) | Yes | Yes | Later |

Python ships every game first; JavaScript and Rust follow to full parity.
How to declare and play a game other than hold'em:
[`docs/DEV-MANUAL.md` §2.8](docs/DEV-MANUAL.md#28-playing-other-games).
Declaring the games on the connection (the client `hello`'s
`supported_games`): Python `supported_games=[...]`, JavaScript
`supportedGames: [...]` and Rust `supported_games: Some(vec![...])` (in
`main`, not yet on crates.io; a Rust bot cannot play OFC yet, so it should
not declare `ofc`).

**Building a Big Bad Bots bot?** Start at
[`docs/BIGBADBOTS-QUICKSTART.md`](docs/BIGBADBOTS-QUICKSTART.md) (Python) or
[`docs/BIGBADBOTS-QUICKSTART-JAVASCRIPT.md`](docs/BIGBADBOTS-QUICKSTART-JAVASCRIPT.md)
(JavaScript). Coding agents:
[`packages/python/starters/bigbadbots/AGENTS.md`](packages/python/starters/bigbadbots/AGENTS.md)
is a build guide written for you, and [`llms.txt`](llms.txt) indexes the Big
Bad Bots docs.

**Works with your AI agent via MCP:** [`chipzen-mcp`](packages/mcp/)
lets an MCP-capable agent play hold'em on Chipzen with no protocol code.

## What this is

- Client libraries + scaffolding for connecting a bot to Chipzen over
  the two-layer WebSocket protocol.
- The protocol specification (Layer 1 transport, and a Layer 2 game
  state spec for each game).
- A protocol test fixture and reference bot you can read in 40 lines.
- Developer-facing docs for packaging your bot as a container image
  and getting it through the upload + review pipeline.

## What this is NOT

This SDK is for **packaging + protocol conformance**. It is **NOT** for
tuning bot strategy.

The SDK gives you three things and nothing else:

1. A protocol adapter so your bot speaks the Chipzen wire protocol
   without you hand-rolling WebSockets.
2. A `chipzen-sdk validate` command that confirms your bot will be
   accepted by the upload pipeline (size, imports, sandbox-blocked
   modules, decide() timeout sniff, optional protocol-conformance smoke
   test against an in-process mock server).
3. Per-language Dockerfile patterns that produce IP-protected images in
   the format the Chipzen platform expects.

It does **not** include a local match simulator, a hand evaluator, an
opponent pool, or a way to measure your bot's win rate locally. Upload
review only checks that your bot speaks the protocol (a short scripted
mini-match). You learn how strong your bot is from the real matches it
plays on Chipzen: challenges, tournaments and seasons, with ratings and
replays.

## Quickstart

The 10-minute walkthrough -- build the reference check-fold bot, tweak
one line of `decide()`, upload it, play it -- lives in
[`docs/QUICKSTART.md`](docs/QUICKSTART.md).

After the quickstart, read [`docs/DEV-MANUAL.md`](docs/DEV-MANUAL.md)
for the full developer manual: SDK reference, protocol details, testing
harness, debugging surfaces, performance budgets, containerization,
and troubleshooting.

**Working with a coding agent.** Two sets of copy-pasteable prompts you
hand to a coding agent (Claude Code, Codex, Cursor, …):

- [`docs/BUILDING-WITH-AI-AGENTS.md`](docs/BUILDING-WITH-AI-AGENTS.md) —
  starting from nothing. Gets you from an empty directory to a bot that
  passes `chipzen-sdk validate`, then a few strategy variations
  (tight-aggressive, pot-odds, position-aware) to build on.
- [`docs/PACKAGING-WITH-AI-AGENTS.md`](docs/PACKAGING-WITH-AI-AGENTS.md) —
  already have a bot and short on time. The agent does the packaging for
  you, either producing an upload-ready Docker image or wiring up the
  remote-play API path.

## Layout

```
chipzen-sdk/
  packages/        Per-language SDK packages (Python, JavaScript,
                   Rust). Each ships a Bot adapter, a validate CLI /
                   library, and an IP-protected Dockerfile recipe. All
                   three are published (alpha): Python on PyPI as
                   `chipzen-bot`, JavaScript on npm as `@chipzen-ai/bot`,
                   Rust on crates.io as `chipzen-bot` (library) +
                   `chipzen-sdk` (CLI). Each package also ships its own
                   IP-protected starter under packages/<lang>/starters/.
  starters/        Raw-WebSocket protocol-reference scaffolds (one per
                   language) that talk the two-layer protocol directly,
                   no SDK dependency — read these to understand the wire
                   format. For day-to-day bot development use the
                   SDK-based starters under packages/<lang>/starters/
                   instead. (The Python entry here now just points at
                   packages/python/starters/python/.)
  examples/        Worked examples. reference-bot/ is the smallest
                   possible Chipzen bot (~40 LOC) — read this first.
  docs/            QUICKSTART, BIGBADBOTS-QUICKSTART, DEV-MANUAL,
                   RULESETS, the protocol spec,
                   and the PORTING-BETWEEN-SDKS cheat-sheet for
                   translating a bot between the Python, JavaScript, and
                   Rust SDKs.
  docs/protocol/   Layer 1 (TRANSPORT-PROTOCOL.md) + Layer 2: what every
                   game shares (LAYER2-COMMON.md) and one spec per game
                   (POKER-, DRAW27-, OFC- and BIGBADBOTS-GAME-STATE-
                   PROTOCOL.md). Authoritative.
  llms.txt         An llmstxt.org index of the Big Bad Bots docs, for
                   coding agents and LLM tools.
```

## Where to file issues

- **SDK / starters / protocol bugs** -> open an issue here on
  [chipzen-ai/chipzen-sdk](https://github.com/chipzen-ai/chipzen-sdk/issues).
- **Platform / matchmaking / account / sign-in issues**
  -> email `support@chipzen.ai` or post in our Discord. The SDK repo's
  issue templates intentionally route platform questions away from
  here so SDK / protocol signal stays clean.

## Contributing

Yes please. See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the contribution
flow, including the **Developer Certificate of Origin** sign-off that
every commit needs (`git commit -s` adds the trailer). Pull requests
run a fast CI matrix (lint + DCO check + protocol test fixture) for
each starter language.

## License

[Apache License 2.0](LICENSE). The Apache 2.0 NOTICE file is at
[`NOTICE`](NOTICE).

## Source-of-truth note

This repo is the **canonical home of the Chipzen SDK**. Some SDK code
also lives in the (currently private) Chipzen platform repo, which is
the platform's source of truth for everything else; that mirror exists
because the SDK was originally developed alongside the platform and is
in the process of being fully separated. All external development
(issues, PRs, releases) happens here, and the developer manual
([`docs/DEV-MANUAL.md`](docs/DEV-MANUAL.md)) is maintained here.
