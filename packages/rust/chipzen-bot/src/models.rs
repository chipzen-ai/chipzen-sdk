//! Core data models — `Card`, `Action`, `GameState`.
//!
//! Field naming follows Rust's snake_case convention. The on-the-wire
//! JSON the protocol uses is also snake_case; the parsers in this
//! module bridge the two.

use crate::error::Error;
use serde_json::Value;
use std::str::FromStr;

// ---------------------------------------------------------------------------
// Card
// ---------------------------------------------------------------------------

/// A standard playing card.
///
/// `rank` is one of `2`-`9`, `T`, `J`, `Q`, `K`, `A`. `suit` is one of
/// `h` (hearts), `d` (diamonds), `c` (clubs), `s` (spades).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct Card {
    pub rank: char,
    pub suit: char,
}

const VALID_RANKS: &[char] = &[
    '2', '3', '4', '5', '6', '7', '8', '9', 'T', 'J', 'Q', 'K', 'A',
];
const VALID_SUITS: &[char] = &['h', 'd', 'c', 's'];

impl Card {
    pub fn new(rank: char, suit: char) -> Result<Self, Error> {
        if !VALID_RANKS.contains(&rank) {
            return Err(Error::Protocol(format!("invalid card rank: {rank:?}")));
        }
        if !VALID_SUITS.contains(&suit) {
            return Err(Error::Protocol(format!("invalid card suit: {suit:?}")));
        }
        Ok(Self { rank, suit })
    }
}

impl FromStr for Card {
    type Err = Error;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        let mut chars = s.chars();
        let rank = chars
            .next()
            .ok_or_else(|| Error::Protocol(format!("empty card string: {s:?}")))?;
        let suit = chars
            .next()
            .ok_or_else(|| Error::Protocol(format!("card string too short: {s:?}")))?;
        if chars.next().is_some() {
            return Err(Error::Protocol(format!(
                "card string too long: {s:?} (expected 2 chars)"
            )));
        }
        Card::new(rank, suit)
    }
}

impl std::fmt::Display for Card {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}{}", self.rank, self.suit)
    }
}

/// Parse a card from its 2-character wire form (e.g. `"Ah"`). Mirrors
/// the Python and JavaScript helpers of the same name; equivalent to
/// `s.parse::<Card>()`.
pub fn parse_card(s: &str) -> Result<Card, Error> {
    s.parse()
}

// ---------------------------------------------------------------------------
// Action
// ---------------------------------------------------------------------------

/// The five action kinds a bot can take. Matches the wire `action`
/// strings byte-for-byte.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ActionKind {
    Fold,
    Check,
    Call,
    Raise,
    AllIn,
}

impl ActionKind {
    pub fn as_str(&self) -> &'static str {
        match self {
            ActionKind::Fold => "fold",
            ActionKind::Check => "check",
            ActionKind::Call => "call",
            ActionKind::Raise => "raise",
            ActionKind::AllIn => "all_in",
        }
    }

    pub fn parse(s: &str) -> Option<Self> {
        match s {
            "fold" => Some(ActionKind::Fold),
            "check" => Some(ActionKind::Check),
            "call" => Some(ActionKind::Call),
            "raise" => Some(ActionKind::Raise),
            "all_in" => Some(ActionKind::AllIn),
            _ => None,
        }
    }
}

/// The action a bot returns from [`crate::Bot::decide`].
///
/// Construct via the variants directly — `Action::Fold`, `Action::Check`,
/// `Action::Call`, `Action::AllIn`, or `Action::Raise(amount)`. The
/// raise amount is a `u64` and must be the *target stack-to-pot total*
/// (the wire field is also called `amount`); the runtime clamps to
/// `state.min_raise..=state.max_raise` server-side.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Action {
    Fold,
    Check,
    Call,
    Raise(u64),
    AllIn,
}

impl Action {
    pub fn kind(&self) -> ActionKind {
        match self {
            Action::Fold => ActionKind::Fold,
            Action::Check => ActionKind::Check,
            Action::Call => ActionKind::Call,
            Action::Raise(_) => ActionKind::Raise,
            Action::AllIn => ActionKind::AllIn,
        }
    }

    /// Serialize to the two-layer `turn_action` payload shape the
    /// server expects. Returns `(action, params)` ready to drop into
    /// the outbound `turn_action` envelope.
    pub fn to_wire(&self) -> (&'static str, Value) {
        let action = self.kind().as_str();
        let params = match self {
            Action::Raise(amount) => serde_json::json!({ "amount": *amount }),
            _ => serde_json::json!({}),
        };
        (action, params)
    }
}

// ---------------------------------------------------------------------------
// GameState
// ---------------------------------------------------------------------------

/// One entry from `state.action_history`. Synthetic blind/ante entries
/// (`post_small_blind`, `post_big_blind`, `post_ante`) appear here too
/// — the server generates them; bots do not submit them.
#[derive(Debug, Clone)]
pub struct ActionHistoryEntry {
    pub seat: i64,
    pub action: String,
    pub amount: Option<i64>,
    pub is_timeout: Option<bool>,
}

/// Built from the server's `turn_request` message. The parser in
/// [`parse_game_state`] converts the wire-format snake_case to the
/// owned-string fields below.
///
/// Field semantics mirror the Python and JavaScript SDKs exactly so a
/// bot strategy translates 1:1 between languages.
#[derive(Debug, Clone)]
pub struct GameState {
    pub hand_number: i64,
    pub phase: String,
    pub hole_cards: Vec<Card>,
    pub board: Vec<Card>,
    pub pot: i64,
    pub your_stack: i64,
    pub opponent_stacks: Vec<i64>,
    pub your_seat: i64,
    pub dealer_seat: i64,
    pub to_call: i64,
    pub min_raise: i64,
    pub max_raise: i64,
    pub valid_actions: Vec<String>,
    pub action_history: Vec<ActionHistoryEntry>,
    pub round_id: String,
    pub request_id: String,
}

/// Parse a `turn_request` envelope into a [`GameState`].
///
/// The wire shape is documented in
/// `docs/protocol/POKER-GAME-STATE-PROTOCOL.md`. All fields default to
/// safe values when absent — but a real server always sends them.
pub fn parse_game_state(message: &Value) -> GameState {
    let state = message.get("state").cloned().unwrap_or(Value::Null);
    let state_obj = state.as_object();

    let hole_strs = state_obj
        .and_then(|o| o.get("your_hole_cards"))
        .and_then(|v| v.as_array());
    let board_strs = state_obj
        .and_then(|o| o.get("board"))
        .and_then(|v| v.as_array());

    let valid_actions = message
        .get("valid_actions")
        .and_then(|v| v.as_array())
        .or_else(|| {
            state_obj
                .and_then(|o| o.get("valid_actions"))
                .and_then(|v| v.as_array())
        })
        .map(|arr| {
            arr.iter()
                .filter_map(|v| v.as_str().map(String::from))
                .collect()
        })
        .unwrap_or_default();

    let action_history = state_obj
        .and_then(|o| o.get("action_history"))
        .and_then(|v| v.as_array())
        .map(|arr| arr.iter().map(parse_history_entry).collect())
        .unwrap_or_default();

    GameState {
        hand_number: state_obj
            .and_then(|o| o.get("hand_number"))
            .and_then(Value::as_i64)
            .unwrap_or(0),
        phase: state_obj
            .and_then(|o| o.get("phase"))
            .and_then(|v| v.as_str())
            .unwrap_or("preflop")
            .to_string(),
        hole_cards: hole_strs
            .map(|arr| {
                arr.iter()
                    .filter_map(|v| v.as_str())
                    .filter_map(|s| s.parse::<Card>().ok())
                    .collect()
            })
            .unwrap_or_default(),
        board: board_strs
            .map(|arr| {
                arr.iter()
                    .filter_map(|v| v.as_str())
                    .filter_map(|s| s.parse::<Card>().ok())
                    .collect()
            })
            .unwrap_or_default(),
        pot: state_obj
            .and_then(|o| o.get("pot"))
            .and_then(Value::as_i64)
            .unwrap_or(0),
        your_stack: state_obj
            .and_then(|o| o.get("your_stack"))
            .and_then(Value::as_i64)
            .unwrap_or(0),
        opponent_stacks: state_obj
            .and_then(|o| o.get("opponent_stacks"))
            .and_then(|v| v.as_array())
            .map(|arr| arr.iter().filter_map(Value::as_i64).collect())
            .unwrap_or_default(),
        your_seat: state_obj
            .and_then(|o| o.get("your_seat"))
            .and_then(Value::as_i64)
            .unwrap_or(0),
        dealer_seat: state_obj
            .and_then(|o| o.get("dealer_seat"))
            .and_then(Value::as_i64)
            .unwrap_or(0),
        to_call: state_obj
            .and_then(|o| o.get("to_call"))
            .and_then(Value::as_i64)
            .unwrap_or(0),
        min_raise: state_obj
            .and_then(|o| o.get("min_raise"))
            .and_then(Value::as_i64)
            .unwrap_or(0),
        max_raise: state_obj
            .and_then(|o| o.get("max_raise"))
            .and_then(Value::as_i64)
            .unwrap_or(0),
        valid_actions,
        action_history,
        round_id: message
            .get("round_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        request_id: message
            .get("request_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
    }
}

fn parse_history_entry(raw: &Value) -> ActionHistoryEntry {
    ActionHistoryEntry {
        seat: raw.get("seat").and_then(Value::as_i64).unwrap_or(0),
        action: raw
            .get("action")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        amount: raw.get("amount").and_then(Value::as_i64),
        is_timeout: raw.get("is_timeout").and_then(Value::as_bool),
    }
}
