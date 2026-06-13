//! Entry point for the chipzen starter bot binary.
//!
//! The bot's strategy lives in `lib.rs`. This file is the thin
//! command-line shim that wires environment variables and SDK options
//! to the SDK's run loops.
//!
//! Two ways to play:
//!   * Default (containerized / direct match URL): the platform's executor
//!     runs this binary in a container and injects CHIPZEN_WS_URL +
//!     CHIPZEN_TOKEN. Set them yourself (or pass the URL as the first
//!     argument) to test locally.
//!   * `bot run-external [--env staging] [--max-matches 1]`: external-API
//!     remote-play — run on YOUR machine with a `cz_extbot_` token and let
//!     the platform match + dispatch you. Reads token/bot_id/url from a
//!     `chipzen.toml` (or pass --token / --bot-id).

use chipzen_bot::{run_bot, run_external_cli, EnvName, RunBotOptions, RunExternalArgs};
use chipzen_starter_bot::MyBot;

#[tokio::main]
async fn main() -> Result<(), chipzen_bot::Error> {
    let args: Vec<String> = std::env::args().skip(1).collect();

    if args.first().map(String::as_str) == Some("run-external") {
        return run_external_mode(&args[1..]).await.map(|_| ());
    }

    let url = args
        .first()
        .cloned()
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

    run_bot(&url, MyBot, options).await.map(|_| ())
}

/// Parse `run-external` flags and play. A fresh `MyBot` per match.
async fn run_external_mode(flags: &[String]) -> Result<(), chipzen_bot::Error> {
    let mut args = RunExternalArgs::new();
    let mut i = 0;
    while i < flags.len() {
        match flags[i].as_str() {
            "--env" => {
                i += 1;
                args.env = flags.get(i).and_then(|e| EnvName::parse(e));
            }
            "--token" => {
                i += 1;
                args.token = flags.get(i).cloned();
            }
            "--bot-id" => {
                i += 1;
                args.bot_id = flags.get(i).cloned();
            }
            "--max-matches" => {
                i += 1;
                args.max_matches = flags.get(i).and_then(|v| v.parse().ok());
            }
            "--no-safe-mode" => args.safe_mode = false,
            other => eprintln!("warning: ignoring unknown flag {other:?}"),
        }
        i += 1;
    }
    let results = run_external_cli(|| MyBot, args).await?;
    eprintln!("played {} match(es)", results.len());
    Ok(())
}
