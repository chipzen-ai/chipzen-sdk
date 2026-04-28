# Chipzen SDK

> **Status: alpha.** This repo is open for issues and PRs, but active
> maintainer review begins at external alpha launch (target: TBD).
> Apologies for slow response in the meantime -- we'll auto-acknowledge
> incoming reports and triage on a best-effort basis until then.
> Concrete SLOs land at external alpha.

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

If you want to run 1000 hands of your bot locally vs PluriBot to measure
win-rate, that is **not** what this SDK does. Strategy experimentation
happens against the Chipzen platform itself: build your image, upload
it, and use the platform's challenge / replay surfaces to evaluate it.

The local testing harness shipped with the SDK
(`chipzen-sdk test ... --opponent random/call/tight`) is for catching
**protocol bugs** and **gross strategy regressions**, not for measuring
solver-quality strength. Don't tune your bot to beat `random` -- it
proves nothing.

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
                   possible Chipzen bot (~40 LOC); it's the same image
                   used as the platform's pipeline smoke target.
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

This repo is the **public-facing mirror** of the Chipzen SDK code,
which also lives in the platform monorepo at
[chipzen-ai/Chipzen](https://github.com/chipzen-ai/Chipzen) (private
during closed alpha; public sections will roll out as the platform
opens up).

PRs land here first; maintainers backport accepted changes into the
monorepo. **Do not open SDK PRs against the monorepo** -- they will be
redirected here.
