# Changelog

All notable changes to the `chipzen-sdk` Rust CLI binary will be
documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`chipzen-sdk run-external`** subcommand — resolves the external-API
  remote-play connection (lobby URL, env, token presence) from
  `chipzen.toml` + flags (`--env` / `--token` / `--bot-id`) and prints what
  it found, without connecting. Because a Rust bot is compiled into its own
  binary (the `chipzen-sdk` tool can't dynamically load and run one the way
  Python's `chipzen run-external my_bot.py` does), this is a config doctor:
  it does the same discovery + env-aware URL resolution `run_external_bot`
  does so you can verify your setup before wiring
  `chipzen_bot::run_external_cli` into your bot binary's `main`. The token is
  never printed. Part of
  [#57](https://github.com/chipzen-ai/chipzen-sdk/issues/57).
- The scaffolded starter (`chipzen-sdk init`) now emits a `run-external`
  mode in `src/main.rs` (flags mirror the Python CLI) and depends on
  `chipzen-bot = "0.3"`, so a new bot can play the external-API remote-play
  path out of the box.

### Documentation

- Updated `chipzen-sdk validate --help` to mention all 4 conformance
  scenarios available via `chipzen_bot::run_conformance_checks` and
  to clarify that the validator is a courtesy linter — the
  authoritative gate is server-side seccomp + cap-drop on the bot
  container. Closes part of
  [#28](https://github.com/chipzen-ai/chipzen-sdk/issues/28).
- Documented the known limitations of `BLOCKED_DEPS` in
  `validate.rs`: the most common Rust process-spawn vector is
  `std::process::Command`, which is built-in and cannot be blocked at
  the Cargo dep level. Build-deps and macro-generated code are also
  outside this list. The runtime sandbox (cap-drop + seccomp) is the
  authoritative gate. Closes part of
  [#28](https://github.com/chipzen-ai/chipzen-sdk/issues/28).

## [0.2.0] — Initial public release

First release of `chipzen-sdk` to crates.io. Companion CLI for the
[`chipzen-bot`](../chipzen-bot/) Rust SDK library.

### CLI surface

Two subcommands. Both have detailed `--help` output via clap.

- **`chipzen-sdk init <name> [--dir <parent>]`** — scaffold a new bot
  project. Emits `Cargo.toml` (depends on `chipzen-bot`, declares
  `[[bin]] name = "bot"` so the IP-protected Dockerfile's
  `cp target/release/bot /build/bot` works regardless of the user's
  package name), `src/main.rs` (a `MyBot` impl + tokio main),
  `Dockerfile` (real IP-protected `cargo build --release` recipe,
  byte-identical to the canonical starter at
  [`packages/rust/starters/rust/`](../starters/rust/)), `.gitignore`,
  `.dockerignore`, `README.md`.
- **`chipzen-sdk validate <path> [--max-size-mb N] [--no-color]`** —
  pre-upload go/no-go. Checks: `size`, `file_structure` (Cargo.toml
  + `src/main.rs`|`lib.rs`), `cargo_metadata` (parses + has
  `[package]` + non-empty `name`), `imports` (FAIL if `chipzen-bot`
  is missing; BLOCKED list: `pnet`, `pcap`, `raw_socket`, `duct`,
  `command-group`; WARN list: `tempfile`, `tempdir`, `memmap2`),
  `bot_impl` (regex), `decide_method` (regex).

### What's intentionally NOT here

The `validate` command does NOT have a `--check-connectivity` flag.
In Rust we can't dynamically import a user's bot the way Python and
JavaScript can — there's no equivalent of `dlopen` for safe Rust
types. The protocol-conformance harness ships as a library function
(`chipzen_bot::run_conformance_checks`) that users call from their
`tests/conformance.rs` and exercise via `cargo test`. The starter at
[`packages/rust/starters/rust/`](../starters/rust/) ships a working
template.

### Packaging

- MSRV: **Rust 1.75** (pinned via workspace `[workspace.package]`).
- Argument parser: **clap 4** with derive.
- TOML parser: **toml 0.8** (used by `validate` to read user
  Cargo.toml manifests).
- Released to crates.io via **Trusted Publishing** (OIDC) — see
  [`packages/rust/RELEASING.md`](../RELEASING.md). No long-lived
  `CARGO_REGISTRY_TOKEN` secret.

### License

Apache-2.0.

[0.2.0]: https://github.com/chipzen-ai/chipzen-sdk/releases/tag/rust-v0.2.0
