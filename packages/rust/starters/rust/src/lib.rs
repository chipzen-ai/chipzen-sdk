//! Chipzen starter bot — your strategy lives here.
//!
//! Replace `decide` with your own. Everything else can stay as-is.
//!
//! `MyBot` is `pub` so the example conformance test in
//! [`tests/conformance.rs`](../tests/conformance.rs) can import it.
//! When you build the binary (`src/main.rs`), `cargo` finds this
//! library via the package's `[[bin]]` entry pulling it in.

use chipzen_bot::{Action, Bot, GameState};

/// The seats still in the match, in seat order.
///
/// A busted seat stays in `opponent_stacks` with a stack of 0, so the list's
/// length counts chairs, not players. A seat all-in this hand also shows 0, but
/// has an entry in `action_history`. A busted seat has neither.
pub fn live_seats(state: &GameState) -> Vec<i64> {
    let others = (0..=state.opponent_stacks.len() as i64).filter(|seat| *seat != state.your_seat);
    let mut live: Vec<i64> = others
        .zip(state.opponent_stacks.iter())
        .filter(|(seat, stack)| {
            **stack > 0 || state.action_history.iter().any(|entry| entry.seat == *seat)
        })
        .map(|(seat, _)| seat)
        .collect();
    live.push(state.your_seat);
    live.push(state.dealer_seat);
    live.sort_unstable();
    live.dedup();
    live
}

/// Derive your seat's table position from the button, among the seats still in.
///
/// Heads-up is the special case the scaffold default below was written for: with
/// two players left, at a table of any size, the button posts the small blind and
/// acts first preflop. See docs/protocol/POKER-GAME-STATE-PROTOCOL.md section 5.9.
pub fn table_position(state: &GameState) -> &'static str {
    let live = live_seats(state);
    let num_players = live.len() as i64;
    if num_players <= 1 {
        return "button";
    }
    let index_of = |seat: i64| live.iter().position(|s| *s == seat).unwrap_or(0) as i64;
    let sab = (index_of(state.your_seat) - index_of(state.dealer_seat)).rem_euclid(num_players);
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
        // table. table_position counts the seats still in. When you add real
        // strategy, iterate / aggregate opponent_stacks instead of assuming a
        // single opponent (reading opponent_stacks[0] sees only one neighbor).
        let _position = table_position(state);

        if state.valid_actions.iter().any(|a| a == "check") {
            Action::Check
        } else {
            Action::Fold
        }
    }
}
