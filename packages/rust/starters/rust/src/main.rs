//! Entry point for the chipzen starter bot binary.
//!
//! The bot's strategy lives in `lib.rs`. This file is the thin
//! command-line shim that wires environment variables and SDK options
//! to [`run_bot`].

use chipzen_bot::{run_bot, RunBotOptions};
use chipzen_starter_bot::MyBot;

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
