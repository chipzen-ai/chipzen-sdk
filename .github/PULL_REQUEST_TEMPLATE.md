<!--
Thanks for opening a PR! A few quick checks before reviewers can act.
-->

## Summary

<!-- 1-3 bullets on what changed and why. -->

## Linked issues

<!-- "Closes #N" or "Refs #N". -->

## CLA

- [ ] I have signed the Contributor License Agreement on a previous PR
      to this repo, or I will sign it via the CLA Assistant comment
      that appears on this PR.

## Scope check

This repo is for the SDK, protocol spec, starters, sample bots, and
developer-facing docs. Platform / matchmaking / account / billing /
auth issues belong on `support@chipzen.ai` or Discord, not here.

- [ ] This change fits the SDK / protocol / docs scope above.

## Test plan

- [ ] Local lints pass for the affected directory (Python `ruff
      check` + `ruff format --check`, JS `eslint` + `tsc`, Rust
      `cargo check` + `cargo test`).
- [ ] Relevant unit tests pass.
- [ ] Documentation updated if behavior changes.

## Compatibility

- [ ] No breaking change to the wire-protocol payload shape, OR I
      have added a version-bump entry in `docs/protocol/` describing
      the migration path.
