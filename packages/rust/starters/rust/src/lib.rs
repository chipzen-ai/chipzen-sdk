//! Chipzen starter bot — your strategy lives here.
//!
//! Replace `decide` with your own. Everything else can stay as-is.
//!
//! `MyBot` is `pub` so the example conformance test in
//! [`tests/conformance.rs`](../tests/conformance.rs) can import it.
//! When you build the binary (`src/main.rs`), `cargo` finds this
//! library via the package's `[[bin]]` entry pulling it in.

use chipzen_bot::{Action, Bot, GameState};

pub struct MyBot;

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
