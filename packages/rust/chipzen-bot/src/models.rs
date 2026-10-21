//! Core data models — `Card`, `Action`, `GameState`.
//!
//! Field naming follows Rust's snake_case convention. The on-the-wire
//! JSON the protocol uses is also snake_case; the parsers in this
//! module bridge the two.

use crate::error::Error;
use serde_json::Value;
use std::collections::BTreeMap;
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

/// The action kinds a bot can take. Matches the wire `action` strings
/// byte-for-byte. `Draw` is 2-7 Triple Draw's only draw-phase action
/// (`docs/protocol/DRAW27-GAME-STATE-PROTOCOL.md` section 4.1).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ActionKind {
    Fold,
    Check,
    Call,
    Raise,
    AllIn,
    Draw,
}

impl ActionKind {
    pub fn as_str(&self) -> &'static str {
        match self {
            ActionKind::Fold => "fold",
            ActionKind::Check => "check",
            ActionKind::Call => "call",
            ActionKind::Raise => "raise",
            ActionKind::AllIn => "all_in",
            ActionKind::Draw => "draw",
        }
    }

    pub fn parse(s: &str) -> Option<Self> {
        match s {
            "fold" => Some(ActionKind::Fold),
            "check" => Some(ActionKind::Check),
            "call" => Some(ActionKind::Call),
            "raise" => Some(ActionKind::Raise),
            "all_in" => Some(ActionKind::AllIn),
            "draw" => Some(ActionKind::Draw),
            _ => None,
        }
    }
}

/// One entry of a 2-7 Triple Draw discard list
/// (`docs/protocol/DRAW27-GAME-STATE-PROTOCOL.md` section 3.5).
///
/// The server accepts either a **card string**, matched against your own
/// holding, or a **0-based position** into `state.hole_cards`. Build one
/// with `.into()` from a `&str` / `String` / [`Card`] / `&Card` (a card)
/// or a `usize` (a position); [`Action::discard`] does that for you.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum DiscardItem {
    /// A card string, e.g. `"Ah"`. Sent verbatim.
    Card(String),
    /// A 0-based position into your holding.
    Position(usize),
}

impl DiscardItem {
    fn to_value(&self) -> Value {
        match self {
            DiscardItem::Card(card) => Value::String(card.clone()),
            DiscardItem::Position(pos) => Value::from(*pos),
        }
    }
}

impl From<&str> for DiscardItem {
    fn from(card: &str) -> Self {
        DiscardItem::Card(card.to_string())
    }
}

impl From<String> for DiscardItem {
    fn from(card: String) -> Self {
        DiscardItem::Card(card)
    }
}

impl From<Card> for DiscardItem {
    fn from(card: Card) -> Self {
        DiscardItem::Card(card.to_string())
    }
}

impl From<&Card> for DiscardItem {
    fn from(card: &Card) -> Self {
        DiscardItem::Card(card.to_string())
    }
}

impl From<usize> for DiscardItem {
    fn from(position: usize) -> Self {
        DiscardItem::Position(position)
    }
}

/// The action a bot returns from [`crate::Bot::decide`].
///
/// Construct the hold'em actions via the variants directly —
/// `Action::Fold`, `Action::Check`, `Action::Call`, `Action::AllIn`, or
/// `Action::Raise(amount)`. The raise amount is a `u64` and must be the
/// *target stack-to-pot total* (the wire field is also called `amount`);
/// the runtime clamps to `state.min_raise..=state.max_raise` server-side.
///
/// 2-7 Triple Draw's `draw` action is `Action::Draw(discards)`, built with
/// [`Action::discard`], [`Action::discard_positions`] or
/// [`Action::stand_pat`]. It is legal only on a draw turn
/// (`state.is_draw_phase`, where `valid_actions` is exactly `["draw"]`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Action {
    Fold,
    Check,
    Call,
    Raise(u64),
    AllIn,
    /// 2-7 Triple Draw: discard these cards (an empty list is a stand pat).
    Draw(Vec<DiscardItem>),
}

impl Action {
    pub fn kind(&self) -> ActionKind {
        match self {
            Action::Fold => ActionKind::Fold,
            Action::Check => ActionKind::Check,
            Action::Call => ActionKind::Call,
            Action::Raise(_) => ActionKind::Raise,
            Action::AllIn => ActionKind::AllIn,
            Action::Draw(_) => ActionKind::Draw,
        }
    }

    /// 2-7 Triple Draw: the `draw` action.
    ///
    /// `cards` is what to throw away: card strings matched against your
    /// own holding (`["Ah", "Kd"]`), [`Card`]s (`&state.hole_cards[..2]`),
    /// or 0-based positions (`[0usize, 3]`, or see
    /// [`Action::discard_positions`]). At most `state.max_discard` entries.
    /// An empty list is a **stand pat**.
    ///
    /// Emits `{"action": "draw", "params": {"discard": [...]}}`. The server
    /// is forgiving: unknown cards, out-of-range positions and duplicates
    /// are dropped, and an over-long list is cut to `max_discard`
    /// (DRAW27-GAME-STATE-PROTOCOL.md sections 3.5 and 5.5).
    pub fn discard<I, T>(cards: I) -> Action
    where
        I: IntoIterator<Item = T>,
        T: Into<DiscardItem>,
    {
        Action::Draw(cards.into_iter().map(Into::into).collect())
    }

    /// 2-7 Triple Draw: discard by 0-based position into
    /// `state.hole_cards`. The same as [`Action::discard`] with positions.
    pub fn discard_positions<I: IntoIterator<Item = usize>>(positions: I) -> Action {
        Action::Draw(positions.into_iter().map(DiscardItem::Position).collect())
    }

    /// 2-7 Triple Draw: keep all five cards. The same as an empty
    /// [`Action::discard`]. A stand pat is a real action, not a no-op: it
    /// appears in `action_history` with `amount: 0` and passes the turn.
    pub fn stand_pat() -> Action {
        Action::Draw(Vec::new())
    }

    /// Serialize to the two-layer `turn_action` payload shape the
    /// server expects. Returns `(action, params)` ready to drop into
    /// the outbound `turn_action` envelope.
    pub fn to_wire(&self) -> (&'static str, Value) {
        let action = self.kind().as_str();
        let params = match self {
            Action::Raise(amount) => serde_json::json!({ "amount": *amount }),
            Action::Draw(discards) => serde_json::json!({
                "discard": discards.iter().map(DiscardItem::to_value).collect::<Vec<_>>(),
            }),
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
///
/// The 2-7 Triple Draw fields at the end
/// (`docs/protocol/DRAW27-GAME-STATE-PROTOCOL.md` section 3.3) are always
/// filled by [`parse_game_state`] and hold their defaults (`false`, `0`,
/// empty) at a hold'em table. `GameState` implements [`Default`], so a
/// hand-built state can name the fields it cares about and finish with
/// `..Default::default()`.
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

    // --- 2-7 Triple Draw (`game_config.variant == "27tripledraw"`) ---
    /// `true` iff this turn owes a DRAW rather than a betting decision. The
    /// one dial to branch on: a betting action in a draw phase (or `draw`
    /// in a betting phase) is an error.
    pub is_draw_phase: bool,
    /// `1`-`3` inside a draw round, `0` in every other phase.
    pub draw_number: i64,
    /// Draw rounds still to come, counting one in progress. `3` during
    /// `draw1`, `0` from `bet3` on.
    pub draws_remaining: i64,
    /// The largest discard legal right now (`5` in a draw phase, `0`
    /// outside one). The bound lives here, NOT in `valid_actions`, which on
    /// a draw turn is exactly `["draw"]`.
    pub max_discard: i64,
    /// Your own per-round draw counts so far; a stand pat contributes `0`.
    pub your_draw_counts: Vec<i64>,
    /// Seat index **as a decimal string** (JSON object keys are strings) to
    /// that seat's per-round draw counts. Excludes you. Draw counts are
    /// public; opponents' cards and discards never appear.
    pub opponent_draw_counts: BTreeMap<String, Vec<i64>>,
}

impl Default for GameState {
    /// The state [`parse_game_state`] builds from an empty `turn_request`
    /// (`phase` is `"preflop"`, every number `0`, every list empty).
    fn default() -> Self {
        parse_game_state(&Value::Null)
    }
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
        // --- 2-7 Triple Draw (absent at a hold'em table -> defaults) ---
        is_draw_phase: as_bool(state_obj.and_then(|o| o.get("is_draw_phase"))),
        draw_number: as_int(state_obj.and_then(|o| o.get("draw_number"))),
        draws_remaining: as_int(state_obj.and_then(|o| o.get("draws_remaining"))),
        max_discard: as_int(state_obj.and_then(|o| o.get("max_discard"))),
        your_draw_counts: as_int_list(state_obj.and_then(|o| o.get("your_draw_counts"))),
        opponent_draw_counts: state_obj
            .and_then(|o| o.get("opponent_draw_counts"))
            .and_then(Value::as_object)
            .map(|seats| {
                seats
                    .iter()
                    .map(|(seat, counts)| (seat.clone(), as_int_list(Some(counts))))
                    .collect()
            })
            .unwrap_or_default(),
    }
}

// ---------------------------------------------------------------------------
// Lenient coercion for the OPTIONAL variant keys
// ---------------------------------------------------------------------------
//
// The variant keys are parsed leniently on purpose, as in the Python and
// JavaScript SDKs: they are new, optional keys, and a hard failure on them
// would turn "the server sent a field shape I did not expect" into "every
// deployed bot at this table dies". A missing or mistyped key degrades to
// its default.

/// An integer, truncating a float (Python's `int(4.7) == 4`); anything
/// else (a string, a bool, `null`) is `0`.
fn as_int(value: Option<&Value>) -> i64 {
    match value {
        Some(Value::Number(n)) => n
            .as_i64()
            .or_else(|| {
                n.as_f64()
                    .filter(|f| f.is_finite())
                    .map(|f| f.trunc() as i64)
            })
            .unwrap_or(0),
        _ => 0,
    }
}

fn as_bool(value: Option<&Value>) -> bool {
    value.and_then(Value::as_bool).unwrap_or(false)
}

/// A list of integers, each coerced by [`as_int`]; a non-list is `[]`.
fn as_int_list(value: Option<&Value>) -> Vec<i64> {
    value
        .and_then(Value::as_array)
        .map(|arr| arr.iter().map(|v| as_int(Some(v))).collect())
        .unwrap_or_default()
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
