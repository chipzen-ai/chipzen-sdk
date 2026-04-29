# Chipzen SDK

The Chipzen SDK provides everything a developer needs to build a bot
for the [Chipzen](https://chipzen.ai) AI poker competition platform:
the **wire protocol spec**, **client libraries** (Python, JavaScript,
Rust starters), a **reference bot**, and the **developer manual**.

## What this is

- Client libraries + scaffolding for connecting a bot to Chipzen over
  the two-layer WebSocket protocol.
- The protocol specification (Layer 1 transport, Layer 2 poker game
  state).
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
opponent pool, or a way to measure your bot's win rate locally. Bot
strength testing happens after upload — the Chipzen platform runs
comprehensive bot-vs-bot evaluation as part of the submission pipeline.

## Quickstart

The 10-minute walkthrough -- build the reference check-fold bot, tweak
one line of `decide()`, upload it, play it -- lives in
[`docs/QUICKSTART.md`](docs/QUICKSTART.md).

After the quickstart, read [`docs/DEV-MANUAL.md`](docs/DEV-MANUAL.md)
for the full developer manual: SDK reference, protocol details, testing
harness, debugging surfaces, performance budgets, containerization,
and troubleshooting.

## Layout

```
chipzen-sdk/
  starters/        Language starters (Python, JavaScript, Rust) that
                   implement the two-layer protocol over raw WebSockets.
                   Copy one and replace decide().
  examples/        Worked examples. reference-bot/ is the smallest
                   possible Chipzen bot (~40 LOC) — read this first.
  docs/            QUICKSTART, DEV-MANUAL, and the protocol spec.
  docs/protocol/   Layer 1 (TRANSPORT-PROTOCOL.md) + Layer 2
                   (POKER-GAME-STATE-PROTOCOL.md). Authoritative.
```

## Where to file issues

- **SDK / starters / protocol bugs** -> open an issue here on
  [chipzen-ai/chipzen-sdk](https://github.com/chipzen-ai/chipzen-sdk/issues).
- **Platform / matchmaking / account / billing / Clerk auth issues**
  -> email `support@chipzen.ai` or post in our Discord. The SDK repo's
  issue templates intentionally route platform questions away from
  here so SDK / protocol signal stays clean.

## Contributing

Yes please. See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the contribution
flow, including the **Contributor License Agreement** that all
contributors must sign before their first PR can be merged. Pull
requests run a fast CI matrix (lint + protocol test fixture) for each
starter language.

## License

[Apache License 2.0](LICENSE). The Apache 2.0 NOTICE file is at
[`NOTICE`](NOTICE).

## Source-of-truth note

This repo is the **canonical home of the Chipzen SDK**. Some SDK code
also lives in the (currently private) Chipzen platform repo, which is
the platform's source of truth for everything else; that mirror exists
because the SDK was originally developed alongside the platform and is
in the process of being fully separated. All external development —
issues, PRs, releases — happens here.
