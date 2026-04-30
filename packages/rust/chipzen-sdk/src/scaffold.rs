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
    write_file(&project_dir.join("Dockerfile"), DOCKERFILE_PLACEHOLDER)?;

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

const DOCKERFILE_PLACEHOLDER: &str = r#"# Replace this with the IP-protected starter Dockerfile from
# packages/rust/starters/rust/Dockerfile (ships in Phase 3 PR 3 —
# multi-stage cargo build that produces a small, statically-linked
# binary with no readable source for your strategy in the runtime
# image).
#
# For now, a minimal cargo-based development image:

FROM rust:1-slim AS builder
WORKDIR /build
COPY Cargo.toml .
COPY src/ src/
RUN cargo build --release

FROM debian:12-slim
WORKDIR /bot
COPY --from=builder /build/target/release/* /bot/bot
ENTRYPOINT ["/bot/bot"]
"#;

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
