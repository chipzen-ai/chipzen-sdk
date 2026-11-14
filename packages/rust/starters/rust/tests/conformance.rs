//! Protocol-conformance test for the starter bot.
//!
//! Drives [`MyBot`] through every scenario of the SDK's conformance
//! harness (a full match, multi-turn `request_id` echo, an
//! `action_rejected` retry, a retry storm) against an in-process mock
//! WebSocket, and requires each to pass.
//!
//! Run with `cargo test`.
//!
//! Expand this with your own scenarios as your strategy grows.

use chipzen_bot::{run_conformance_checks, ConformanceSeverity, RunConformanceOptions};
use chipzen_starter_bot::MyBot;

#[tokio::test]
async fn starter_bot_passes_every_conformance_scenario() {
    let results = run_conformance_checks(MyBot, RunConformanceOptions::default()).await;
    // The SDK adds scenarios over time; require every one to pass, not a count.
    assert!(!results.is_empty(), "the SDK ran no conformance scenarios");
    for check in &results {
        assert_eq!(
            check.severity,
            ConformanceSeverity::Pass,
            "conformance check {:?} failed: {}",
            check.name,
            check.message,
        );
    }
}
