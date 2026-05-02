//! Reference Chipzen bot — non-trivial demonstration (Rust port of
//! `examples/reference-bot/bot.py`).
//!
//! Intentionally simple but **competent** — the goal is to show, in
//! one file, that the protocol carries real strategy state cleanly:
//!
//!   - Per-match state via `on_match_start` (seat assignment).
//!   - Per-hand state via `on_round_start` (reset trackers each hand).
//!   - Live observation via `on_turn_result` (count opponent
//!     aggression in the current hand).
//!   - Branching on `state.phase` for preflop vs postflop.
//!   - Heuristic hand-strength bucketing using `state.hole_cards`.
//!   - Made-hand detection from `state.hole_cards` + `state.board`.
//!   - Action history awareness via `self.opponent_raises_this_hand`.
//!   - Strict `state.valid_actions` checking — this bot will never
//!     return an action the server hasn't offered.
//!
//! The strategy is **not strong** — it folds too much, doesn't bluff,
//! ignores pot odds, has no postflop draw recognition, and uses a
//! crude rank-bucket model. That's fine: the point is to show that a
//! bot author **can** express real logic against the SDK, not that
//! the bot itself is competitive.
//!
//! If you're starting your own bot, scaffold one with
//! `chipzen-sdk init <name>` instead — that gives you a thin starter
//! with the IP-protected `cargo build --release` Dockerfile.

use chipzen_bot::{run_bot, Action, Bot, Card, GameState, RunBotOptions};
use serde_json::Value;
use std::collections::HashMap;

// ---------------------------------------------------------------------------
// Card / hand helpers — pure functions, no SDK state
// ---------------------------------------------------------------------------

const RANKS: [char; 13] = [
    '2', '3', '4', '5', '6', '7', '8', '9', 'T', 'J', 'Q', 'K', 'A',
];

fn rank_index(r: char) -> usize {
    RANKS.iter().position(|&x| x == r).unwrap_or(0)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Bucket {
    Premium,
    Strong,
    Medium,
    Weak,
}

/// Coarse preflop hand bucket. Crude on purpose; demonstrates that
/// `state.hole_cards` is shaped correctly for range-table-style work.
fn preflop_bucket(hole_cards: &[Card]) -> Bucket {
    if hole_cards.len() != 2 {
        return Bucket::Weak;
    }
    let r1 = hole_cards[0].rank;
    let r2 = hole_cards[1].rank;
    let suited = hole_cards[0].suit == hole_cards[1].suit;
    let (high, low) = if rank_index(r1) >= rank_index(r2) {
        (r1, r2)
    } else {
        (r2, r1)
    };

    // Pocket pairs
    if r1 == r2 {
        if matches!(r1, 'J' | 'Q' | 'K' | 'A') {
            return Bucket::Premium;
        }
        if matches!(r1, '9' | 'T') {
            return Bucket::Strong;
        }
        return Bucket::Medium;
    }

    // AK
    if (high == 'A' && low == 'K') || (high == 'K' && low == 'A') {
        return Bucket::Premium;
    }

    // Broadways with an ace
    if high == 'A' && matches!(low, 'Q' | 'J' | 'T') {
        return if suited { Bucket::Strong } else { Bucket::Medium };
    }

    // KQ, KJ
    if high == 'K' && matches!(low, 'Q' | 'J') {
        return if suited { Bucket::Strong } else { Bucket::Medium };
    }

    // Connected broadways
    if matches!(high, 'Q' | 'J') && matches!(low, 'T' | '9') {
        return if suited { Bucket::Strong } else { Bucket::Medium };
    }

    // Weak ace (any)
    if high == 'A' {
        return Bucket::Medium;
    }

    Bucket::Weak
}

/// Crude category of the best 7-card holding so far.
///
/// Returns:
///   0 — no pair (high card only).
///   1 — one pair.
///   2 — two pair.
///   3 — three of a kind or better.
fn made_hand_class(hole_cards: &[Card], board: &[Card]) -> u8 {
    let mut counts: HashMap<char, u8> = HashMap::new();
    for c in hole_cards.iter().chain(board.iter()) {
        *counts.entry(c.rank).or_insert(0) += 1;
    }
    let mut sorted: Vec<u8> = counts.values().copied().collect();
    sorted.sort_unstable_by(|a, b| b.cmp(a));
    if sorted.is_empty() {
        return 0;
    }
    if sorted[0] >= 3 {
        return 3;
    }
    if sorted.len() >= 2 && sorted[0] == 2 && sorted[1] == 2 {
        return 2;
    }
    if sorted[0] == 2 {
        return 1;
    }
    0
}

/// Return `target` clamped to `[min_raise, max_raise]`, or `None` if
/// raising is illegal at this turn (the SDK reports `min_raise == 0`
/// and `max_raise == 0` in that case).
fn bounded_raise(target: i64, state: &GameState) -> Option<u64> {
    if state.min_raise == 0 || state.max_raise == 0 {
        return None;
    }
    let target = target.max(state.min_raise).min(state.max_raise);
    Some(target as u64)
}

/// Count raise / all_in actions by anyone other than `my_seat`.
fn opponent_raises_in_history(state: &GameState, my_seat: Option<i64>) -> u32 {
    let mut n = 0;
    for entry in &state.action_history {
        if Some(entry.seat) == my_seat {
            continue;
        }
        if entry.action == "raise" || entry.action == "all_in" {
            n += 1;
        }
    }
    n
}

// ---------------------------------------------------------------------------
// The bot
// ---------------------------------------------------------------------------

struct ReferenceBot {
    /// Per-match state. Set by `on_match_start` from the seats array.
    my_seat: Option<i64>,
    /// Per-hand state — reset by `on_round_start`.
    opponent_raises_this_hand: u32,
}

impl ReferenceBot {
    fn new() -> Self {
        Self {
            my_seat: None,
            opponent_raises_this_hand: 0,
        }
    }

    fn decide_preflop(&self, state: &GameState, valid: &[String]) -> Action {
        let bucket = preflop_bucket(&state.hole_cards);
        let has = |a: &str| valid.iter().any(|v| v == a);

        // Premium: open-raise to ~3x BB if we can, otherwise call.
        if bucket == Bucket::Premium {
            let target = bounded_raise(state.min_raise.saturating_mul(3), state);
            if let (Some(t), true) = (target, has("raise")) {
                return Action::Raise(t);
            }
            if has("call") {
                return Action::Call;
            }
            return if has("check") { Action::Check } else { Action::Fold };
        }

        // Strong: raise unopened pots, otherwise call cheap.
        if bucket == Bucket::Strong {
            if state.to_call == 0 && has("raise") {
                if let Some(t) = bounded_raise(state.min_raise.saturating_mul(2), state) {
                    return Action::Raise(t);
                }
            }
            if has("call") && state.to_call <= state.your_stack / 10 {
                return Action::Call;
            }
            return if has("check") { Action::Check } else { Action::Fold };
        }

        // Medium: only call free / very cheap.
        if bucket == Bucket::Medium {
            if has("check") {
                return Action::Check;
            }
            if has("call") && state.to_call <= state.your_stack / 30 {
                return Action::Call;
            }
            return Action::Fold;
        }

        // Weak: check / fold.
        if has("check") {
            Action::Check
        } else {
            Action::Fold
        }
    }

    fn decide_postflop(&self, state: &GameState, valid: &[String], opp_aggression: u32) -> Action {
        let klass = made_hand_class(&state.hole_cards, &state.board);
        let has = |a: &str| valid.iter().any(|v| v == a);

        // Two pair or better: bet 2/3 pot if we can, else call.
        if klass >= 2 {
            if has("raise") && opp_aggression == 0 {
                let target = bounded_raise((state.pot * 2) / 3, state);
                if let Some(t) = target {
                    return Action::Raise(t);
                }
            }
            if has("call") {
                return Action::Call;
            }
            return if has("check") { Action::Check } else { Action::Fold };
        }

        // One pair: check, call small bets, fold to pressure.
        if klass == 1 {
            if has("check") {
                return Action::Check;
            }
            if has("call") && state.to_call <= state.pot / 3 {
                return Action::Call;
            }
            return Action::Fold;
        }

        // Nothing made: check or fold. (No bluffs in the reference bot.)
        if has("check") {
            Action::Check
        } else {
            Action::Fold
        }
    }
}

impl Bot for ReferenceBot {
    fn decide(&mut self, state: &GameState) -> Action {
        let history_raises = opponent_raises_in_history(state, self.my_seat);
        let opp_aggression = self.opponent_raises_this_hand.max(history_raises);

        let chosen = if state.phase == "preflop" {
            self.decide_preflop(state, &state.valid_actions)
        } else {
            self.decide_postflop(state, &state.valid_actions, opp_aggression)
        };

        eprintln!(
            "[reference-bot] decide hand={} phase={} legal={} opp_aggro={} action={:?}",
            state.hand_number,
            state.phase,
            if state.valid_actions.is_empty() {
                "-".to_string()
            } else {
                state.valid_actions.join(",")
            },
            opp_aggression,
            chosen,
        );
        chosen
    }

    fn on_match_start(&mut self, msg: &Value) {
        if let Some(seats) = msg.get("seats").and_then(|s| s.as_array()) {
            for seat in seats {
                if seat.get("is_self").and_then(|v| v.as_bool()) == Some(true) {
                    if let Some(s) = seat.get("seat").and_then(|v| v.as_i64()) {
                        self.my_seat = Some(s);
                    }
                    break;
                }
            }
        }
        eprintln!("[reference-bot] match_start my_seat={:?}", self.my_seat);
    }

    fn on_round_start(&mut self, _msg: &Value) {
        self.opponent_raises_this_hand = 0;
    }

    fn on_turn_result(&mut self, msg: &Value) {
        let details = msg.get("details");
        let seat = details
            .and_then(|d| d.get("seat"))
            .and_then(|v| v.as_i64());
        if seat == self.my_seat {
            return;
        }
        let action = details.and_then(|d| d.get("action")).and_then(|v| v.as_str());
        if matches!(action, Some("raise") | Some("all_in")) {
            self.opponent_raises_this_hand += 1;
        }
    }
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

#[tokio::main]
async fn main() -> Result<(), chipzen_bot::Error> {
    let url = std::env::args()
        .nth(1)
        .or_else(|| std::env::var("CHIPZEN_WS_URL").ok())
        .unwrap_or_else(|| {
            eprintln!("error: CHIPZEN_WS_URL not set and no URL passed");
            std::process::exit(2);
        });

    eprintln!("[reference-bot] reference-bot ready; connecting to {url}");
    run_bot(
        &url,
        ReferenceBot::new(),
        RunBotOptions {
            token: std::env::var("CHIPZEN_TOKEN").ok(),
            ticket: std::env::var("CHIPZEN_TICKET").ok(),
            client_name: Some("reference-bot".to_string()),
            client_version: Some("0.2.0".to_string()),
            ..Default::default()
        },
    )
    .await
}
