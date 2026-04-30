//! `chipzen-sdk` CLI — scaffold and validate Chipzen poker bots.
//!
//! Thin shim over the library at `chipzen_sdk::cli`. Mirrors the
//! Python (`chipzen-sdk`) and JavaScript (`chipzen-sdk`) CLIs so a
//! developer using any of the three SDKs sees the same command
//! surface.

use anyhow::Result;
use clap::Parser;

fn main() -> Result<()> {
    let args = chipzen_sdk::cli::Cli::parse();
    chipzen_sdk::cli::run(args)
}
