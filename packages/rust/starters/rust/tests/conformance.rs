//! Protocol-conformance test for the starter bot.
//!
//! Drives [`MyBot`] through the SDK's canned full-match exchange
//! (handshake + 1 hand + match_end) against an in-process mock
//! WebSocket. Verifies the bot completes the script without error
//! and emits a valid `turn_action`.
//!
//! Run with `cargo test`.
//!
//! Expand this with your own scenarios as your strategy grows.

use chipzen_bot::{run_conformance_checks, ConformanceSeverity, RunConformanceOptions};
use chipzen_starter_bot::MyBot;

#[tokio::test]
async fn starter_bot_completes_canned_full_match() {
    let results = run_conformance_checks(MyBot, RunConformanceOptions::default()).await;
    assert_eq!(results.len(), 1);
    let check = &results[0];
    assert_eq!(
        check.severity,
        ConformanceSeverity::Pass,
        "conformance check {:?} failed: {}",
        check.name,
        check.message,
    );
}
