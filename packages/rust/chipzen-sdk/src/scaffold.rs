//! `chipzen-sdk init <name>` — scaffold a new Chipzen bot project.
//!
//! Mirrors the Python and JavaScript scaffold shape. Emits a Cargo
//! project that depends on `chipzen-bot`, with a starter `MyBot` impl
//! and a Dockerfile placeholder (replaced with the real IP-protected
//! recipe in Phase 3 PR 3).

use anyhow::{anyhow, bail, Context, Result};
use std::fs;
use std::path::{Path, PathBuf};

#[derive(Debug, Default, Clone)]
pub struct ScaffoldOptions {
    /// Where to create the project. `None` means current directory.
    pub parent_dir: Option<PathBuf>,
}

pub fn scaffold_bot(name: &str, opts: &ScaffoldOptions) -> Result<PathBuf> {
    if !is_valid_project_name(name) {
        bail!(
            "Invalid project name {name:?}. Use ASCII letters, digits, underscores, and dashes only."
        );
    }
    let parent = match &opts.parent_dir {
        Some(p) => p.clone(),
        None => std::env::current_dir().context("could not resolve current directory")?,
    };
    let project_dir = parent.join(name);

    if project_dir.exists() {
        return Err(anyhow!(
            "Directory already exists: {}",
            project_dir.display()
        ));
    }

    fs::create_dir_all(&project_dir)
        .with_context(|| format!("creating {}", project_dir.display()))?;
    fs::create_dir_all(project_dir.join("src"))
        .with_context(|| format!("creating {}/src", project_dir.display()))?;

    write_file(&project_dir.join("Cargo.toml"), &cargo_toml(name))?;
    write_file(&project_dir.join("src").join("main.rs"), MAIN_RS_TEMPLATE)?;
    write_file(&project_dir.join(".gitignore"), GITIGNORE_TEMPLATE)?;
    write_file(&project_dir.join(".dockerignore"), DOCKERIGNORE_TEMPLATE)?;
    write_file(&project_dir.join("README.md"), &readme_template(name))?;
    write_file(&project_dir.join("Dockerfile"), DOCKERFILE_TEMPLATE)?;

    Ok(project_dir)
}

fn write_file(path: &Path, contents: &str) -> Result<()> {
    fs::write(path, contents).with_context(|| format!("writing {}", path.display()))
}

fn is_valid_project_name(name: &str) -> bool {
    !name.is_empty()
        && name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
}

fn cargo_toml(name: &str) -> String {
    format!(
        r#"[package]
name = "{name}"
version = "0.1.0"
edition = "2021"

# The release binary is named `bot` so the IP-protected Dockerfile can
# `cp target/release/bot /build/bot` without knowing the package name.
[[bin]]
name = "bot"
path = "src/main.rs"

[dependencies]
chipzen-bot = "0.2"
tokio = {{ version = "1", features = ["macros", "rt-multi-thread"] }}

[profile.release]
opt-level = 3
lto = "thin"
strip = "symbols"
codegen-units = 1
"#
    )
}

const MAIN_RS_TEMPLATE: &str = r#"//! Chipzen starter bot — replace `decide` with your strategy.
//! The SDK handles WebSocket, handshake, ping/pong, retries, and reconnect.

use chipzen_bot::{run_bot, Action, Bot, GameState, RunBotOptions};

struct MyBot;

impl Bot for MyBot {
    fn decide(&mut self, state: &GameState) -> Action {
        // Return one of: Action::Fold, Action::Check, Action::Call,
        // Action::Raise(amount), Action::AllIn. The chosen action's
        // wire-form must be in state.valid_actions; raises must satisfy
        // state.min_raise <= amount <= state.max_raise.
        if state.valid_actions.iter().any(|a| a == "check") {
            Action::Check
        } else {
            Action::Fold
        }
    }
}

#[tokio::main]
async fn main() -> Result<(), chipzen_bot::Error> {
    // The Chipzen platform injects CHIPZEN_WS_URL and CHIPZEN_TOKEN
    // (or CHIPZEN_TICKET) at container launch time. For local testing
    // against your own stack, set them yourself or pass the URL as the
    // first positional argument.
    let url = std::env::args()
        .nth(1)
        .or_else(|| std::env::var("CHIPZEN_WS_URL").ok())
        .unwrap_or_else(|| {
            eprintln!("error: CHIPZEN_WS_URL not set and no URL passed on the command line");
            std::process::exit(1);
        });

    let options = RunBotOptions {
        token: std::env::var("CHIPZEN_TOKEN").ok(),
        ticket: std::env::var("CHIPZEN_TICKET").ok(),
        ..Default::default()
    };

    run_bot(&url, MyBot, options).await
}
"#;

const GITIGNORE_TEMPLATE: &str = "target/\nCargo.lock\n.env\n.env.*\n.DS_Store\n";

const DOCKERIGNORE_TEMPLATE: &str =
    "target/\n.git/\n.gitignore\n.env\n.env.*\n*.md\nREADME*\nLICENSE*\n.DS_Store\n";

// Kept identical to packages/rust/starters/rust/Dockerfile so a
// scaffolded project and the canonical starter directory ship the same
// recipe. A test enforces this byte-identity invariant.
const DOCKERFILE_TEMPLATE: &str = r#"# syntax=docker/dockerfile:1.7
#
# IP-protected Chipzen Rust bot image.
#
# Multi-stage build that compiles the bot to a single, statically-
# linked release binary in the builder stage, then ships only that
# binary in the runtime stage. The runtime image contains no readable
# Rust source for your strategy code — only the stripped binary.
#
# See ../../IP-PROTECTION.md for what this protects (and what it doesn't).
#
# Build:   docker build -t my-bot:test .
# Export:  docker save my-bot:test | gzip > my-bot.tar.gz
#
# Build context for this directory should be small (Cargo.toml +
# src/ + this file). The .dockerignore alongside this file keeps the
# target/ build cache and editor metadata out.

# -----------------------------------------------------------------------------
# Stage 1: cargo build --release. The .rs source lives only in this stage and
# is discarded before the runtime stage starts.
# -----------------------------------------------------------------------------
# Base pinned by tag — Dependabot can rotate to digest pinning later. Tag:
# rust:1-slim (debian-bookworm-based; the runtime stage below also uses
# debian-bookworm so glibc + libssl line up for the compiled binary).
FROM rust:1-slim AS builder

WORKDIR /build

# Build tools needed by tokio-tungstenite's `native-tls` feature
# (links against system openssl). pkg-config tells the openssl-sys
# build script where libssl + libcrypto live.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        pkg-config \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Bring in the bot source + manifest. Only these are copied — keep
# the build context narrow so the .dockerignore is the only allowlist.
COPY Cargo.toml ./
COPY src/ ./src/

# Build the release binary. The starter's [profile.release] is already
# tuned for a small, symbol-stripped binary (lto=thin, opt-level=3,
# codegen-units=1).
RUN cargo build --release --bin bot \
    && cp target/release/bot /build/bot \
    && rm -rf src/ target/ Cargo.toml

# -----------------------------------------------------------------------------
# Stage 2: Runtime. Only the compiled binary + ENTRYPOINT.
# No .rs source for the bot's strategy is present.
# -----------------------------------------------------------------------------
# Base pinned by tag — Dependabot can rotate to digest pinning later. Tag:
# debian:12-slim. Matches the glibc the builder stage links against and
# carries libssl3 + ca-certs for outbound TLS to the platform's wss://.
FROM debian:12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        libssl3 \
        dumb-init \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /bot

# Copy ONLY the compiled binary from the builder stage.
COPY --from=builder /build/bot /bot/bot
RUN chmod +x /bot/bot

# Run as non-root (defense in depth — the platform also applies seccomp
# and cap-drop on top of this).
RUN groupadd --system --gid 10001 bot \
    && useradd --system --uid 10001 --gid bot --home-dir /bot --shell /usr/sbin/nologin bot \
    && chown -R bot:bot /bot
USER 10001

ENTRYPOINT ["dumb-init", "/bot/bot"]
"#;
pub const _DOCKERFILE_TEMPLATE_FOR_TEST: &str = DOCKERFILE_TEMPLATE;

fn readme_template(name: &str) -> String {
    format!(
        r#"# {name}

A poker bot for the [Chipzen](https://chipzen.ai) platform.

## Quick start

```bash
cargo build
```

Edit `src/main.rs` to implement your strategy in the `decide` method.

Validate before uploading:

```bash
chipzen-sdk validate .
```

Build and export the upload tarball:

```bash
docker build -t {name}:v1 .
docker save {name}:v1 | gzip > {name}.tar.gz
```

Then upload via the Chipzen platform UI.
"#
    )
}
