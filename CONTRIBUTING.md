# Contributing to the Chipzen SDK

Thanks for your interest in contributing. This document covers the
practical parts of submitting a change.

## Quick summary

1. Sign off every commit with the Developer Certificate of Origin --
   `git commit -s` adds the `Signed-off-by:` trailer (see "DCO sign-off"
   below).
2. Open issues / PRs that fit this repo's scope (see "Scope" below).
   Platform issues go to `support@chipzen.ai` or Discord, not here.
3. Run the local checks before pushing: `ruff check`, `ruff format
   --check`, `pytest`, `npm test`, `cargo test` -- whichever apply to
   the directories you touched. CI runs them all.
4. Use a clear commit message. Conventional Commits (`feat:`, `fix:`,
   `docs:`, `chore:`) are encouraged but not required.

## DCO sign-off

We use the [Developer Certificate of Origin](https://developercertificate.org/)
instead of a Contributor License Agreement. The DCO is a lightweight
statement that you wrote the contribution (or have the right to submit
it) and that you're submitting it under the project's license
(Apache-2.0). Read the full text at the link above -- it's eight lines.

You sign by adding a `Signed-off-by:` trailer to every commit. Git
adds it automatically when you commit with `-s`:

```bash
git commit -s -m "fix: …"
```

The trailer must match your commit author identity:

```
Signed-off-by: Jane Doe <jane@example.com>
```

CI's DCO workflow (`.github/workflows/dco.yml`) verifies every
non-merge commit in your PR has a matching trailer. Mismatched or
missing trailers fail the check.

If you forgot to sign earlier commits, fix them with:

```bash
git rebase --signoff main
git push --force-with-lease
```

There's no separate per-account registration -- every PR is checked
on its own commits.

If your contribution is on behalf of an employer with IP rights, make
sure you have their permission before opening the PR. The DCO covers
the assertion that you have the right to submit; the legal exposure
sits with you and your employer, not with the project.

## Scope

This repository contains:

- **Client libraries** for connecting bots to Chipzen
  (`starters/python|javascript|rust/`).
- **Examples** -- worked reference bots (`examples/reference-bot/`).
- **Protocol specifications** (`docs/protocol/`).
- **Developer-facing documentation** (`docs/`).

This repository does **not** contain:

- The Chipzen platform backend, frontend, matchmaker, infra, or
  database schemas. Those live in the (currently private) platform
  monorepo. Changes to platform code don't go here.
- Strategy tuning helpers, trainers, or solvers. The SDK is about
  packaging + protocol conformance, not about making your bot stronger.

If you're not sure whether an idea fits, open an issue first and ask.

## Where issues go

| Type of issue | Where to file |
|---|---|
| SDK bug (Python/JS/Rust starter, runner, validate CLI) | This repo |
| Protocol spec bug or proposal | This repo |
| Reference / example bot bug | This repo |
| Platform bug (matchmaking, leaderboards, Clerk auth, billing, account) | `support@chipzen.ai` or Discord |
| Security issue (any) | `security@chipzen.ai` -- see [SECURITY.md](SECURITY.md) |

The SDK repo's issue templates route platform questions to email /
Discord on purpose. Don't try to bypass that by filing as a "bug".

## PR checklist

Before opening a PR:

- [ ] Branch from `main`.
- [ ] Every commit signed off (`git commit -s`) -- DCO check enforces this.
- [ ] One logical change per PR. Refactors and feature work in
      separate PRs.
- [ ] Tests added or updated where it makes sense. The protocol test
      fixture in `examples/reference-bot/` is the canonical "does this
      still speak v2" smoke target.
- [ ] Docs updated if you changed public API or wire format.
- [ ] CI green (lint + tests for each language touched).
- [ ] Commit message describes the *why*, not just the *what*.

The PR template (`.github/PULL_REQUEST_TEMPLATE.md`) prompts for these.

## Code style

- **Python:** `ruff check` + `ruff format` (config in `pyproject.toml`).
  Type hints on public APIs. Docstrings on public classes / functions.
  Python 3.10+.
- **JavaScript:** ESLint (config in starter directory). Node 20+.
- **Rust:** `cargo fmt` + `cargo clippy -- -D warnings`. Stable
  toolchain.

## Maintainership and review SLA

This repo is currently maintained by the Chipzen founder. During
**internal alpha** (where we are now), expect:

- Auto-acknowledgement on issues and PRs within 24 hours.
- Substantive review on a best-effort basis -- we are honest that
  this can be slow until external alpha launches.
- No formal review SLA yet.

At **external alpha launch**, we'll publish a real SLO and add a
maintainer rotation. For now: thanks for your patience, and please
don't take silence as rejection -- we *will* get to your PR.

## Code of conduct

This project follows the
[Contributor Covenant 2.1](CODE_OF_CONDUCT.md). Please read it before
participating.
