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
    /// returns an invalid action (`check`, else `fold`; a stand pat on a
    /// 2-7 Triple Draw draw turn) — but lean on the validator
    /// (`chipzen-sdk validate`) to catch those during development.
    ///
    /// `decide` is the only method the session loop calls for a turn, at
    /// every table. A bot that plays 2-7 Triple Draw branches here:
    ///
    /// ```ignore
    /// fn decide(&mut self, state: &GameState) -> Action {
    ///     if state.is_draw_phase {
    ///         return self.decide_draw(state); // 2-7 Triple Draw
    ///     }
    ///     // ... betting decision ...
    /// }
    /// ```
    fn decide(&mut self, state: &GameState) -> Action;

    /// 2-7 Triple Draw: return your `draw` action for a draw turn. Called
    /// only if your [`Bot::decide`] calls it; the SDK never does on its own.
    ///
    /// The default is a **stand pat** ([`Action::stand_pat`]), which is also
    /// what the server substitutes for a seat that times out in a draw
    /// phase. It is legal in every draw phase, so the default is never
    /// rejected; it is just rarely the best draw.
    ///
    /// Discard at most `state.max_discard` cards with [`Action::discard`].
    /// The bound lives in the state, not in `state.valid_actions` (which is
    /// exactly `["draw"]` on a draw turn).
    /// See `docs/protocol/DRAW27-GAME-STATE-PROTOCOL.md`.
    fn decide_draw(&mut self, _state: &GameState) -> Action {
        Action::stand_pat()
    }

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

    /// Called when the server resumes an in-flight match after a
    /// reconnect.
    ///
    /// On re-attach the server sends `reconnected` — NOT `match_start` —
    /// so [`Bot::on_match_start`] does not fire for a session that
    /// (re)joined a match already in progress. Override this to
    /// (re)learn match context: the message carries `match_id`, `seats`,
    /// and `game_config` in the same shape as `match_start`, plus the
    /// current `round_number` and `state` snapshot (see
    /// `docs/protocol/TRANSPORT-PROTOCOL.md` §8.15).
    ///
    /// Default impl is a no-op (chipzen-ai/chipzen-sdk#121).
    fn on_reconnected(&mut self, _msg: &Value) {}

    /// Called after each `turn_action` is sent, with your decision time
    /// in milliseconds.
    ///
    /// `latency_ms` is the wall-clock time [`Bot::decide`] took for the turn
    /// just sent. The platform enforces a per-turn timeout (default 5000ms,
    /// announced in `match_start.turn_timeout_ms`); logging this lets you
    /// catch a bot drifting toward the limit before it starts timing out.
    ///
    /// Default impl is a no-op. Override to record / log timings
    /// (chipzen-ai/chipzen-sdk#46).
    fn on_decision_latency(&mut self, _latency_ms: f64) {}
}
