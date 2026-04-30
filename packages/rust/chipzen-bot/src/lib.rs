//! # chipzen-bot
//!
//! Build, test, and deploy poker bots for the [Chipzen](https://chipzen.ai)
//! competition platform.
//!
//! Implement the [`Bot`] trait, return [`Action`]s from [`Bot::decide`],
//! call [`run_bot`] — the SDK handles the WebSocket connection,
//! the two-layer protocol handshake, ping/pong, `request_id` echoing,
//! `action_rejected` retries, and reconnect.
//!
//! ```no_run
//! use chipzen_bot::{Bot, Action, GameState, run_bot, RunBotOptions};
//!
//! struct MyBot;
//!
//! #[async_trait::async_trait]
//! impl Bot for MyBot {
//!     fn decide(&mut self, state: &GameState) -> Action {
//!         if state.valid_actions.iter().any(|a| a == "check") {
//!             Action::Check
//!         } else {
//!             Action::Fold
//!         }
//!     }
//! }
//!
//! # async fn _doctest() -> Result<(), chipzen_bot::Error> {
//! let url = std::env::var("CHIPZEN_WS_URL").unwrap();
//! run_bot(&url, MyBot, RunBotOptions::default()).await?;
//! # Ok(()) }
//! ```
//!
//! See the [`docs/protocol/`] docs in the chipzen-sdk repo for the
//! full two-layer protocol specification, and the
//! [IP-PROTECTION.md](https://github.com/chipzen-ai/chipzen-sdk/blob/main/packages/rust/IP-PROTECTION.md)
//! for what the starter Dockerfile does to your release binary.
//!
//! [`docs/protocol/`]: https://github.com/chipzen-ai/chipzen-sdk/tree/main/docs/protocol

mod bot;
mod client;
mod error;
mod models;

pub use bot::Bot;
pub use client::{
    run_bot, MessageReader, MessageWriter, RunBotOptions, SessionContext,
    SUPPORTED_PROTOCOL_VERSIONS,
};
pub use error::Error;
pub use models::{
    parse_card, parse_game_state, Action, ActionHistoryEntry, ActionKind, Card, GameState,
};

// Internals used by the conformance harness in a future PR. Not part of
// the supported public API; the underscore prefix is a convention copied
// from the Python and JavaScript SDKs.
#[doc(hidden)]
pub use client::{_extract_match_id, _run_session, _safe_fallback_action};
