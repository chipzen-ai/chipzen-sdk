//! The `Bot` trait — the user-facing extension point.
//!
//! Implement [`Bot::decide`] to return one [`crate::Action`] per
//! `turn_request`. Override the lifecycle hooks if you want to react
//! to match/round events (default impls do nothing).

use crate::models::{Action, GameState};
use serde_json::Value;

/// User-implementable poker bot.
///
/// `Bot` is `Send + 'static` so [`crate::run_bot`] can move the
/// instance into the async session loop. It is intentionally **not**
/// `Sync` — only the session loop calls into it, and forcing `Sync`
/// would push every implementation into wrapping its strategy state
/// in a `Mutex` for no benefit.
///
/// All hook methods take `&mut self` so a bot can keep mutable state
/// (opponent model, hand counter, etc.) without interior mutability.
///
/// The `*_msg` arguments are passed as raw `serde_json::Value` for
/// forward-compat — the protocol may add fields the SDK doesn't yet
/// know about, and consumers can read them without waiting for an SDK
/// update.
pub trait Bot: Send + 'static {
    /// Required: pick the next action. Must be in
    /// `state.valid_actions`; raises must satisfy
    /// `state.min_raise <= amount <= state.max_raise`. The session
    /// loop substitutes a safe fallback if your decide panics or
    /// returns an invalid action — but lean on the validator
    /// (`chipzen-sdk validate`) to catch those during development.
    fn decide(&mut self, state: &GameState) -> Action;

    /// Called when the server's `match_start` arrives. Use for
    /// per-match setup (read game_config, prep opponent model).
    fn on_match_start(&mut self, _msg: &Value) {}

    /// Called when each new hand starts.
    fn on_round_start(&mut self, _msg: &Value) {}

    /// Called when the betting phase changes (preflop → flop, etc.).
    fn on_phase_change(&mut self, _msg: &Value) {}

    /// Called after every turn the server resolves — yours or any
    /// opponent's. Useful for opponent modeling.
    fn on_turn_result(&mut self, _msg: &Value) {}

    /// Called when a hand ends with the result envelope.
    fn on_round_result(&mut self, _msg: &Value) {}

    /// Called when the match ends. Last chance to flush state if you
    /// were persisting anything to disk.
    fn on_match_end(&mut self, _results: &Value) {}
}
