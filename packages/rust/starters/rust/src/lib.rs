//! Chipzen starter bot — your strategy lives here.
//!
//! Replace `decide` with your own. Everything else can stay as-is.
//!
//! `MyBot` is `pub` so the example conformance test in
//! [`tests/conformance.rs`](../tests/conformance.rs) can import it.
//! When you build the binary (`src/main.rs`), `cargo` finds this
//! library via the package's `[[bin]]` entry pulling it in.

use chipzen_bot::{Action, Bot, GameState};

/// Derive your seat's table position from the button.
///
/// The protocol is multiway-shaped: `opponent_stacks` is a LIST, so the table
/// size is `opponent_stacks.len() + 1`. Combined with `your_seat` and
/// `dealer_seat` (both already on the parsed `GameState`), that is everything
/// you need to know where you sit:
///
///     seats_after_button = (your_seat - dealer_seat).rem_euclid(num_players)
///
/// Heads-up is the special case the scaffold default below was written for: the
/// button posts the small blind and acts first preflop. See
/// docs/protocol/POKER-GAME-STATE-PROTOCOL.md section 5.9.
pub fn table_position(your_seat: i64, dealer_seat: i64, num_players: i64) -> &'static str {
    if num_players <= 1 {
        return "button";
    }
    let sab = (your_seat - dealer_seat).rem_euclid(num_players);
    if num_players == 2 {
        return if sab == 0 { "button_sb" } else { "big_blind" };
    }
    match sab {
        0 => "button",
        1 => "small_blind",
        2 => "big_blind",
        _ if sab == num_players - 1 => "cutoff",
        3 => "early",
        _ => "middle",
    }
}

pub struct MyBot;

impl Bot for MyBot {
    fn decide(&mut self, state: &GameState) -> Action {
        // Return one of: Action::Fold, Action::Check, Action::Call,
        // Action::Raise(amount), Action::AllIn. The chosen action's
        // wire-form must be in state.valid_actions; raises must satisfy
        // state.min_raise <= amount <= state.max_raise.
        //
        // Seat-count-aware: opponent_stacks is a LIST of every other seat
        // (length N-1), so this bot keeps running unchanged at a 3-6 player
        // table. your_seat + dealer_seat give your position. When you add real
        // strategy, iterate / aggregate opponent_stacks instead of assuming a
        // single opponent (reading opponent_stacks[0] sees only one neighbor).
        let num_players = state.opponent_stacks.len() as i64 + 1;
        let _position = table_position(state.your_seat, state.dealer_seat, num_players);

        if state.valid_actions.iter().any(|a| a == "check") {
            Action::Check
        } else {
            Action::Fold
        }
    }
}
