//! 2-7 Triple Draw in the Rust SDK (chipzen-ai/chipzen-sdk#174).
//!
//! Ports the 27TD half of the Python SDK's `tests/test_variant_backcompat.py`
//! and the JavaScript SDK's `tests/draw27.test.ts` against the same frames:
//! the `draw27_*` fixtures below are the Python `_draw27_*` fixtures
//! (`packages/python/src/chipzen/conformance.py`) value for value. What is
//! pinned:
//!
//! - `parse_game_state` reads a 27TD `turn_request` (draw turn and betting
//!   turn), and the new fields default at a hold'em table; a mistyped
//!   optional key degrades to its default.
//! - The 27TD envelope is an additive superset of the hold'em one.
//! - `Action::discard` / `Action::stand_pat` put the discard under `params`.
//! - `decide_draw` is defaulted (stand pat) and never called by the SDK.
//! - The safe fallback on a draw turn is a stand pat, not a fold.
//! - The session routes on the server hello's `state_shape`: `27tripledraw`
//!   is played, while `pineapple` (until #176) and unknown shapes end the
//!   session before the client `hello`.
//! - The seat comes from `match_start` and the button from each
//!   `round_start` (a 27TD turn state carries neither).
//! - End to end: a bot that declares `draw27` plays a 27TD match through the
//!   real session loop, with the `game` descriptor a live table sends.
//!
//! Authority: docs/protocol/DRAW27-GAME-STATE-PROTOCOL.md.

use async_trait::async_trait;
use chipzen_bot::{
    _run_session, _safe_fallback_action, parse_game_state, run_conformance_checks, Action,
    ActionKind, Bot, Card, DiscardItem, Error, FallbackAction, GameState, MessageReader,
    MessageWriter, RunConformanceOptions, SessionContext, SCENARIO_NAMES,
};
use serde_json::{json, Map, Value};
use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

const MATCH_ID: &str = "m_conformance_test";

// ---------------------------------------------------------------------------
// Fixtures: the Python `_draw27_*` frames, value for value
// ---------------------------------------------------------------------------

/// The server `hello`'s `game` descriptor at a 27TD table (DRAW27 §1.1).
fn draw27_game_descriptor() -> Value {
    json!({
        "game_type": "draw27",
        "variant": "27tripledraw",
        "actions": ["call", "check", "draw", "fold", "raise"],
        "phases": ["predraw", "draw1", "bet1", "draw2", "bet2", "draw3", "bet3", "showdown", "complete"],
        "state_shape": "27tripledraw",
    })
}

/// The server `hello`. `game` adds a `game` descriptor, as a live variant
/// table sends; without one the frame is the Python fixture's
/// `_server_hello(game_type="draw27")`.
fn server_hello(game_type: &str, game: Option<Value>) -> Value {
    let mut hello = json!({
        "type": "hello",
        "match_id": MATCH_ID,
        "seq": 1,
        "server_ts": "2026-04-13T14:30:05.123Z",
        "supported_versions": ["1.0"],
        "selected_version": "1.0",
        "game_type": game_type,
        "capabilities": [],
    });
    if let Some(game) = game {
        hello["game"] = game;
    }
    hello
}

fn draw27_server_hello(with_descriptor: bool) -> Value {
    server_hello("draw27", with_descriptor.then(draw27_game_descriptor))
}

/// `match_start` for a heads-up 27TD table: FIXED LIMIT, four independent
/// sizes, no `total_hands`. The bot sits in seat `your_seat`.
fn draw27_match_start_in_seat(seq: i64, your_seat: i64) -> Value {
    json!({
        "type": "match_start",
        "match_id": MATCH_ID,
        "seq": seq,
        "server_ts": "2026-04-13T14:30:06.000Z",
        "seats": [
            { "seat": 0, "participant_id": "p0", "display_name": if your_seat == 0 { "You" } else { "Opp" }, "is_self": your_seat == 0 },
            { "seat": 1, "participant_id": "p1", "display_name": if your_seat == 1 { "You" } else { "Opp" }, "is_self": your_seat == 1 },
        ],
        "game_config": {
            "variant": "27tripledraw",
            "starting_stack": 10000,
            "betting_structure": "fixed_limit",
            "small_blind": 50,
            "big_blind": 100,
            "small_bet": 100,
            "big_bet": 200,
            "ante": 0,
            "bet_cap": 5,
            "num_betting_rounds": 4,
            "num_draws": 3,
            "cards_per_player": 5,
            "escalation_unit": "small_bet",
            "escalation_interval": 10,
            "num_players": 2,
        },
        "turn_timeout_ms": 5000,
    })
}

fn draw27_match_start(seq: i64) -> Value {
    draw27_match_start_in_seat(seq, 0)
}

/// `round_start` for 27TD: five cards, and the new `draws_remaining`.
fn draw27_round_start_with_button(seq: i64, hand_number: i64, dealer_seat: i64) -> Value {
    json!({
        "type": "round_start",
        "match_id": MATCH_ID,
        "seq": seq,
        "server_ts": "2026-04-13T14:30:07.000Z",
        "round_id": format!("r_{hand_number}"),
        "round_number": hand_number,
        "state": {
            "hand_number": hand_number,
            "dealer_seat": dealer_seat,
            "your_hole_cards": ["Ah", "Kd", "9s", "7c", "3h"],
            "pot": 150,
            "post_blind_stacks": [9950, 9900],
            "draws_remaining": 3,
            "stacks": [10000, 10000],
            "deck_commitment": "",
        },
    })
}

fn draw27_round_start(seq: i64) -> Value {
    draw27_round_start_with_button(seq, 1, 0)
}

/// `turn_request` for 27TD: a draw turn, else a betting turn. One shape
/// covers both; `is_draw_phase` says which. In a draw phase `valid_actions`
/// is exactly `["draw"]` and the BOUND on the discard (`max_discard`) lives
/// in the state. `board` is permanently `[]`.
fn draw27_turn_request(seq: i64, request_id: &str, draw_phase: bool) -> Value {
    let mut state = json!({
        "hand_number": 1,
        "board": [],
        "your_hole_cards": ["Ah", "Kd", "9s", "7c", "3h"],
        "pot": 400,
        "your_stack": 9800,
        "opponent_stacks": [9800],
        "your_draw_counts": [],
        "opponent_draw_counts": { "1": [] },
        "action_history": [
            { "seat": 0, "action": "post_small_blind", "amount": 50, "phase": "predraw", "is_timeout": false },
            { "seat": 1, "action": "post_big_blind", "amount": 100, "phase": "predraw", "is_timeout": false },
        ],
    });
    let phase_bits = if draw_phase {
        json!({
            "phase": "draw1",
            "to_call": 0,
            "min_raise": 0,
            "max_raise": 0,
            "is_draw_phase": true,
            "draw_number": 1,
            "draws_remaining": 3,
            "max_discard": 5,
        })
    } else {
        // Fixed limit: min_raise == max_raise, always. One legal size.
        json!({
            "phase": "bet1",
            "to_call": 100,
            "min_raise": 200,
            "max_raise": 200,
            "is_draw_phase": false,
            "draw_number": 0,
            "draws_remaining": 2,
            "max_discard": 0,
        })
    };
    for (k, v) in phase_bits.as_object().unwrap() {
        state[k] = v.clone();
    }
    json!({
        "type": "turn_request",
        "match_id": MATCH_ID,
        "seq": seq,
        "server_ts": "2026-04-13T14:30:07.500Z",
        "seat": 0,
        "request_id": request_id,
        "timeout_ms": 5000,
        "valid_actions": if draw_phase { json!(["draw"]) } else { json!(["fold", "call", "raise"]) },
        "state": state,
    })
}

/// `turn_result` for a draw. Per DRAW27 §3.6, `details.amount` here is the
/// server-normalized discard MASK (bit i = hand position i replaced), not
/// the count: `2` is position 1 alone. The public COUNT is what
/// `action_history` carries (§3.4). (The Python fixture's docstring calls
/// this value a count; the frame is the same and the protocol wins.)
fn draw27_turn_result(seq: i64) -> Value {
    json!({
        "type": "turn_result",
        "match_id": MATCH_ID,
        "seq": seq,
        "server_ts": "2026-04-13T14:30:08.000Z",
        "is_timeout": false,
        "details": { "seat": 0, "action": "draw", "amount": 2 },
    })
}

/// `round_result` for 27TD: the envelope is unchanged; showdown is not.
/// `hand_rank` is a dash-joined rank string, and the LOWEST hand wins.
fn draw27_round_result(seq: i64) -> Value {
    json!({
        "type": "round_result",
        "match_id": MATCH_ID,
        "seq": seq,
        "server_ts": "2026-04-13T14:30:12.000Z",
        "round_id": "r_1",
        "round_number": 1,
        "result": {
            "hand_number": 1,
            "winner_seats": [0],
            "pot": 800,
            "payouts": [{ "seat": 0, "amount": 800 }],
            "showdown": [
                {
                    "seat": 0,
                    "hole_cards": ["8s", "6h", "4d", "3c", "2h"],
                    "hand_rank": "8-6-4-3-2",
                    "best_hand": ["8s", "6h", "4d", "3c", "2h"],
                },
                {
                    "seat": 1,
                    "hole_cards": ["Js", "9h", "6d", "4c", "2s"],
                    "hand_rank": "J-9-6-4-2",
                    "best_hand": ["Js", "9h", "6d", "4c", "2s"],
                },
            ],
            "action_history": [],
            "stacks": [10400, 9600],
            "deck_commitment": "",
            "deck_reveal": null,
        },
    })
}

fn match_end(seq: i64) -> Value {
    json!({
        "type": "match_end",
        "match_id": MATCH_ID,
        "seq": seq,
        "server_ts": "2026-04-13T14:35:00.000Z",
        "reason": "complete",
        "results": [
            { "seat": 0, "participant_id": "p0", "rank": 1, "score": 1020 },
            { "seat": 1, "participant_id": "p1", "rank": 2, "score": 980 },
        ],
    })
}

/// Handshake + one 27TD hand (a draw turn AND a betting turn) + match_end.
fn draw27_script(with_descriptor: bool) -> Vec<Value> {
    vec![
        draw27_server_hello(with_descriptor),
        draw27_match_start(2),
        draw27_round_start(3),
        draw27_turn_request(4, "req_1", true),
        draw27_turn_result(5),
        draw27_turn_request(6, "req_2", false),
        draw27_round_result(7),
        match_end(8),
    ]
}

/// The hold'em `turn_request` fixture (Python `_turn_request`).
fn holdem_turn_request() -> Value {
    json!({
        "type": "turn_request",
        "match_id": MATCH_ID,
        "seq": 4,
        "server_ts": "2026-04-13T14:30:07.500Z",
        "seat": 0,
        "request_id": "req_1",
        "timeout_ms": 5000,
        "valid_actions": ["fold", "call", "raise"],
        "state": {
            "hand_number": 1,
            "phase": "preflop",
            "board": [],
            "your_hole_cards": ["Ah", "Kd"],
            "pot": 15,
            "your_stack": 995,
            "opponent_stacks": [990],
            "to_call": 5,
            "min_raise": 20,
            "max_raise": 995,
            "action_history": [],
        },
    })
}

fn poison(message: &Value, key: &str, value: Value) -> Value {
    let mut copy = message.clone();
    copy["state"][key] = value;
    copy
}

// ---------------------------------------------------------------------------
// Session harness
// ---------------------------------------------------------------------------

struct ScriptedReader {
    frames: Vec<String>,
    idx: usize,
}

impl ScriptedReader {
    fn new(frames: &[Value]) -> Self {
        Self {
            frames: frames.iter().map(Value::to_string).collect(),
            idx: 0,
        }
    }
}

#[async_trait]
impl MessageReader for ScriptedReader {
    async fn next(&mut self) -> Result<Option<String>, Error> {
        let frame = self.frames.get(self.idx).cloned();
        self.idx += 1;
        Ok(frame)
    }
}

#[derive(Clone, Default)]
struct CapturingWriter {
    sent: Arc<Mutex<Vec<String>>>,
}

#[async_trait]
impl MessageWriter for CapturingWriter {
    async fn send(&mut self, payload: String) -> Result<(), Error> {
        self.sent.lock().unwrap().push(payload);
        Ok(())
    }
}

struct Played {
    end: Option<Value>,
    sent: Vec<Map<String, Value>>,
}

impl Played {
    fn hello(&self) -> Option<&Map<String, Value>> {
        self.sent.iter().find(|m| m["type"] == "hello")
    }

    /// `(request_id, action, params)` of every `turn_action` sent.
    fn turn_actions(&self) -> Vec<(String, String, Value)> {
        self.sent
            .iter()
            .filter(|m| m["type"] == "turn_action")
            .map(|m| {
                (
                    m["request_id"].as_str().unwrap().to_string(),
                    m["action"].as_str().unwrap().to_string(),
                    m["params"].clone(),
                )
            })
            .collect()
    }
}

async fn play<B: Bot>(bot: &mut B, frames: &[Value], supported_games: Option<&[&str]>) -> Played {
    let ctx = SessionContext {
        supported_games: supported_games.map(|g| g.iter().map(|s| s.to_string()).collect()),
        ..SessionContext::new(
            MATCH_ID.to_string(),
            Some("t".to_string()),
            None,
            "test".to_string(),
            "0.0.0".to_string(),
        )
    };
    let mut reader = ScriptedReader::new(frames);
    let mut writer = CapturingWriter::default();
    let end = _run_session(&mut reader, &mut writer, bot, &ctx)
        .await
        .expect("session must not error");
    let sent = writer
        .sent
        .lock()
        .unwrap()
        .iter()
        .map(|s| {
            serde_json::from_str::<Value>(s)
                .unwrap()
                .as_object()
                .unwrap()
                .clone()
        })
        .collect();
    Played { end, sent }
}

// ---------------------------------------------------------------------------
// Bots
// ---------------------------------------------------------------------------

/// A bot written against hold'em and nothing else; records what it was handed.
#[derive(Default)]
struct NlheOnlyBot {
    seen: Vec<GameState>,
}

impl Bot for NlheOnlyBot {
    fn decide(&mut self, state: &GameState) -> Action {
        self.seen.push(state.clone());
        if state.valid_actions.iter().any(|a| a == "check") {
            Action::Check
        } else if state.to_call > 0 && state.to_call * 3 < state.pot {
            Action::Call
        } else {
            Action::Fold
        }
    }
}

/// A 27TD bot: draws two on a draw turn, raises when it can on a betting turn.
#[derive(Default)]
struct Draw27Bot {
    seen: Vec<GameState>,
}

impl Bot for Draw27Bot {
    fn decide(&mut self, state: &GameState) -> Action {
        self.seen.push(state.clone());
        if state.is_draw_phase {
            return self.decide_draw(state);
        }
        let has = |a: &str| state.valid_actions.iter().any(|v| v == a);
        if has("raise") {
            Action::Raise(state.min_raise as u64)
        } else if has("check") {
            Action::Check
        } else {
            Action::Call
        }
    }

    fn decide_draw(&mut self, state: &GameState) -> Action {
        // Throw the two highest cards (lowball keeps the low ones).
        let take = usize::try_from(state.max_discard).unwrap_or(0);
        Action::discard(["Ah", "Kd"].into_iter().take(take))
    }
}

/// Only implements `decide`.
struct LegacyBot;
impl Bot for LegacyBot {
    fn decide(&mut self, _state: &GameState) -> Action {
        Action::Fold
    }
}

fn parsed(draw_phase: bool) -> GameState {
    parse_game_state(&draw27_turn_request(4, "req_1", draw_phase))
}

// ---------------------------------------------------------------------------
// 1. A 27TD turn_request parses, and its new keys are readable
// ---------------------------------------------------------------------------

#[test]
fn both_turn_kinds_parse_with_board_carried_as_empty() {
    for draw_phase in [true, false] {
        let state = parsed(draw_phase);
        assert!(state.board.is_empty());
        assert_eq!(state.hole_cards.len(), 5);
    }
}

#[test]
fn a_holdem_only_bot_decides_on_either_turn_without_panicking() {
    // Wrong for the table, but WRONG is survivable and CRASHING is not.
    for draw_phase in [true, false] {
        let mut bot = NlheOnlyBot::default();
        let action = bot.decide(&parsed(draw_phase));
        assert_ne!(action.kind(), ActionKind::Draw);
        assert_eq!(bot.seen.len(), 1);
    }
}

#[test]
fn the_draw_turn_keys_are_readable() {
    let state = parsed(true);
    assert!(state.is_draw_phase);
    assert_eq!(state.draw_number, 1);
    assert_eq!(state.draws_remaining, 3);
    // The BOUND on the discard lives in state, not in valid_actions.
    assert_eq!(state.max_discard, 5);
    assert_eq!(state.valid_actions, vec!["draw"]);
    assert!(state.your_draw_counts.is_empty());
    // Seat index is a DECIMAL STRING key, because JSON object keys are.
    assert_eq!(
        state.opponent_draw_counts,
        BTreeMap::from([("1".to_string(), vec![])])
    );
    assert_eq!(state.phase, "draw1");
}

#[test]
fn a_betting_turn_is_fixed_limit() {
    let state = parsed(false);
    assert!(!state.is_draw_phase);
    assert_eq!(state.min_raise, 200);
    assert_eq!(state.max_raise, 200);
    assert_eq!(state.max_discard, 0);
    assert!(state.valid_actions.iter().any(|a| a == "raise"));
    assert_eq!(state.phase, "bet1");
}

#[test]
fn draw_counts_are_parsed_per_seat() {
    let mut msg = draw27_turn_request(4, "req_1", true);
    msg["state"]["your_draw_counts"] = json!([2, 0]);
    msg["state"]["opponent_draw_counts"] = json!({ "1": [3, 1], "2": [0] });
    let state = parse_game_state(&msg);
    assert_eq!(state.your_draw_counts, vec![2, 0]);
    assert_eq!(
        state.opponent_draw_counts,
        BTreeMap::from([("1".to_string(), vec![3, 1]), ("2".to_string(), vec![0])])
    );
}

#[test]
fn a_27td_turn_state_carries_no_your_seat_or_dealer_seat() {
    // The seat comes from match_start, the button from round_start; see the
    // seat-tracking tests below.
    let msg = draw27_turn_request(4, "req_1", true);
    let state = msg["state"].as_object().unwrap();
    assert!(!state.contains_key("your_seat"));
    assert!(!state.contains_key("dealer_seat"));
}

#[test]
fn a_holdem_payload_leaves_every_27td_field_at_its_default() {
    let state = parse_game_state(&holdem_turn_request());
    assert!(!state.is_draw_phase);
    assert_eq!(state.draw_number, 0);
    assert_eq!(state.draws_remaining, 0);
    assert_eq!(state.max_discard, 0);
    assert!(state.your_draw_counts.is_empty());
    assert!(state.opponent_draw_counts.is_empty());
}

#[test]
fn a_mistyped_optional_key_degrades_to_its_default() {
    // The variant keys are lenient on purpose (as in Python and JS): a
    // mistyped optional key must not become a session-killing failure.
    let base = draw27_turn_request(4, "req_1", true);
    let check = |key: &str, value: Value| parse_game_state(&poison(&base, key, value));

    assert!(!check("is_draw_phase", json!("yes")).is_draw_phase);
    assert_eq!(check("draw_number", json!("1")).draw_number, 0);
    assert_eq!(check("max_discard", Value::Null).max_discard, 0);
    assert_eq!(check("max_discard", json!(4.7)).max_discard, 4);
    assert!(check("your_draw_counts", json!("2"))
        .your_draw_counts
        .is_empty());
    assert_eq!(
        check("your_draw_counts", json!([1, "x", true])).your_draw_counts,
        vec![1, 0, 0]
    );
    assert!(check("opponent_draw_counts", json!([1]))
        .opponent_draw_counts
        .is_empty());
    assert_eq!(
        check("opponent_draw_counts", json!({ "1": "x" })).opponent_draw_counts,
        BTreeMap::from([("1".to_string(), vec![])])
    );
}

// ---------------------------------------------------------------------------
// 2. No hold'em-era field is displaced; the envelope is an additive superset
// ---------------------------------------------------------------------------

#[test]
fn a_draw_phase_zeroes_the_raise_bounds_rather_than_dropping_them() {
    // Rule 2: the six numeric fields stay present and numeric.
    let msg = draw27_turn_request(4, "req_1", true);
    for key in ["pot", "to_call", "min_raise", "max_raise", "your_stack"] {
        assert!(msg["state"][key].is_i64(), "{key}");
    }
    let state = parsed(true);
    assert_eq!(state.min_raise, 0);
    assert_eq!(state.max_raise, 0);
    assert_eq!(state.pot, 400);
    assert_eq!(state.your_stack, 9800);
    assert_eq!(state.opponent_stacks, vec![9800]);
    assert_eq!(state.action_history.len(), 2);
    assert_eq!(state.action_history[1].action, "post_big_blind");
    assert_eq!(state.request_id, "req_1");
}

fn json_kind(v: &Value) -> &'static str {
    match v {
        Value::Null => "null",
        Value::Bool(_) => "bool",
        Value::Number(_) => "number",
        Value::String(_) => "string",
        Value::Array(_) => "array",
        Value::Object(_) => "object",
    }
}

#[test]
fn the_27td_state_carries_every_holdem_key_with_the_same_type() {
    let nlhe = holdem_turn_request();
    for draw_phase in [true, false] {
        let variant = draw27_turn_request(4, "req_1", draw_phase);
        for (key, value) in nlhe["state"].as_object().unwrap() {
            let got = variant["state"].get(key).unwrap_or_else(|| panic!("{key}"));
            assert_eq!(json_kind(got), json_kind(value), "{key}");
        }
        for key in [
            "type",
            "match_id",
            "seq",
            "seat",
            "request_id",
            "timeout_ms",
        ] {
            assert_eq!(json_kind(&variant[key]), json_kind(&nlhe[key]), "{key}");
        }
        // valid_actions stays a flat array of strings; the bounds live in state.
        assert!(variant["valid_actions"]
            .as_array()
            .unwrap()
            .iter()
            .all(Value::is_string));
    }
}

#[test]
fn the_config_is_fixed_limit_with_four_independent_sizes() {
    let cfg = &draw27_match_start(2)["game_config"];
    assert_eq!(cfg["variant"], "27tripledraw");
    assert_eq!(cfg["betting_structure"], "fixed_limit");
    for key in ["small_blind", "big_blind", "small_bet", "big_bet"] {
        assert!(cfg[key].is_i64(), "{key}");
    }
    assert_eq!(cfg["bet_cap"], 5);
    assert_eq!(cfg["num_draws"], 3);
    assert_eq!(cfg["cards_per_player"], 5);
    assert!(cfg.get("total_hands").is_none());
}

#[test]
fn game_state_default_is_the_empty_parse_and_fills_the_27td_fields() {
    let state = GameState::default();
    assert_eq!(state.phase, "preflop");
    assert!(!state.is_draw_phase);
    assert_eq!(state.max_discard, 0);
    assert!(state.opponent_draw_counts.is_empty());
    // A hand-built hold'em-era state compiles with `..Default::default()`.
    let literal = GameState {
        valid_actions: vec!["fold".to_string()],
        ..Default::default()
    };
    assert_eq!(LegacyBot.decide(&literal), Action::Fold);
}

// ---------------------------------------------------------------------------
// 3. Action::discard / Action::stand_pat put the discard under `params`
// ---------------------------------------------------------------------------

#[test]
fn discard_emits_the_card_strings_under_params() {
    assert_eq!(
        Action::discard(["Ah", "Kd"]).to_wire(),
        ("draw", json!({ "discard": ["Ah", "Kd"] }))
    );
}

#[test]
fn discard_accepts_cards_and_positions() {
    let state = parsed(true);
    assert_eq!(
        Action::discard(&state.hole_cards[..2]).to_wire(),
        ("draw", json!({ "discard": ["Ah", "Kd"] }))
    );
    let card: Card = "9s".parse().unwrap();
    assert_eq!(
        Action::discard([card]).to_wire().1,
        json!({ "discard": ["9s"] })
    );
    assert_eq!(
        Action::discard_positions([0, 3]).to_wire(),
        ("draw", json!({ "discard": [0, 3] }))
    );
    assert_eq!(
        Action::discard([0usize, 3]),
        Action::discard_positions([0, 3])
    );
    // A mixed list is legal on the wire.
    assert_eq!(
        Action::discard([DiscardItem::from("Ah"), DiscardItem::Position(4)])
            .to_wire()
            .1,
        json!({ "discard": ["Ah", 4] })
    );
}

#[test]
fn stand_pat_is_an_empty_discard_and_a_real_action() {
    let empty: [&str; 0] = [];
    assert_eq!(
        Action::stand_pat().to_wire(),
        ("draw", json!({ "discard": [] }))
    );
    assert_eq!(Action::stand_pat(), Action::discard(empty));
    assert_eq!(Action::stand_pat().kind(), ActionKind::Draw);
    assert_ne!(Action::stand_pat(), Action::discard(["Ah"]));
}

#[test]
fn the_draw_kind_round_trips_and_holdem_wire_shapes_are_unchanged() {
    assert_eq!(ActionKind::Draw.as_str(), "draw");
    assert_eq!(ActionKind::parse("draw"), Some(ActionKind::Draw));
    assert_eq!(Action::Fold.to_wire(), ("fold", json!({})));
    assert_eq!(Action::Check.to_wire(), ("check", json!({})));
    assert_eq!(Action::Call.to_wire(), ("call", json!({})));
    assert_eq!(Action::AllIn.to_wire(), ("all_in", json!({})));
    assert_eq!(
        Action::Raise(200).to_wire(),
        ("raise", json!({ "amount": 200 }))
    );
}

// ---------------------------------------------------------------------------
// 4. decide() stays the single required method; decide_draw is defaulted
// ---------------------------------------------------------------------------

#[test]
fn decide_draw_defaults_to_a_stand_pat() {
    assert_eq!(LegacyBot.decide_draw(&parsed(true)), Action::stand_pat());
}

#[tokio::test]
async fn decide_draw_is_not_called_by_the_session_loop() {
    #[derive(Default)]
    struct HookSpy {
        inner: NlheOnlyBot,
        hook_calls: usize,
    }
    impl Bot for HookSpy {
        fn decide(&mut self, state: &GameState) -> Action {
            self.inner.decide(state)
        }
        fn decide_draw(&mut self, _state: &GameState) -> Action {
            self.hook_calls += 1;
            Action::stand_pat()
        }
    }
    let mut bot = HookSpy::default();
    let played = play(&mut bot, &draw27_script(true), Some(&["draw27"])).await;
    assert!(played.end.is_some());
    assert_eq!(bot.inner.seen.len(), 2);
    assert_eq!(bot.hook_calls, 0);
}

// ---------------------------------------------------------------------------
// 5. The fixtures match the spec and stay out of the grader
// ---------------------------------------------------------------------------

#[tokio::test]
async fn the_conformance_grader_is_unchanged() {
    assert_eq!(
        SCENARIO_NAMES,
        &[
            "connectivity_full_match",
            "multi_turn_request_id_echo",
            "action_rejected_recovery",
            "retry_storm_bounded",
        ]
    );
    let results = run_conformance_checks(LegacyBot, RunConformanceOptions::default()).await;
    assert_eq!(results.len(), 4);
}

#[test]
fn a_draws_turn_result_amount_is_the_discard_mask() {
    // DRAW27 §3.6: bit i set = hand position i replaced; 2 is position 1.
    let details = &draw27_turn_result(5)["details"];
    assert_eq!(details["action"], "draw");
    let mask = details["amount"].as_u64().unwrap();
    let positions: Vec<u64> = (0..5).filter(|i| (mask >> i) & 1 == 1).collect();
    assert_eq!(positions, vec![1]);
}

#[test]
fn the_showdown_rank_is_a_dash_joined_string_and_the_low_hand_wins() {
    let result = &draw27_round_result(6)["result"];
    let showdown = result["showdown"].as_array().unwrap();
    assert_eq!(showdown[0]["hand_rank"], "8-6-4-3-2");
    assert!(showdown
        .iter()
        .all(|e| e["hand_rank"].as_str().unwrap().contains('-')));
    assert_eq!(result["winner_seats"], json!([0]));
}

// ---------------------------------------------------------------------------
// 6. End to end: a Rust bot that declares draw27 plays a 27TD match
// ---------------------------------------------------------------------------

#[tokio::test]
async fn a_bot_that_declares_draw27_plays_a_hand_end_to_end() {
    // With the live game descriptor, and with the Python fixture's hello
    // (no descriptor), which a client reads as poker and parses the same way.
    for with_descriptor in [true, false] {
        let mut bot = Draw27Bot::default();
        let played = play(
            &mut bot,
            &draw27_script(with_descriptor),
            Some(&["poker", "draw27"]),
        )
        .await;

        assert_eq!(played.end.as_ref().unwrap()["type"], "match_end");
        assert_eq!(
            played.hello().unwrap()["supported_games"],
            json!(["poker", "draw27"])
        );
        assert_eq!(
            played.turn_actions(),
            vec![
                (
                    "req_1".to_string(),
                    "draw".to_string(),
                    json!({ "discard": ["Ah", "Kd"] })
                ),
                (
                    "req_2".to_string(),
                    "raise".to_string(),
                    json!({ "amount": 200 })
                ),
            ]
        );
        // The bot saw a draw turn and then a betting turn.
        let kinds: Vec<bool> = bot.seen.iter().map(|s| s.is_draw_phase).collect();
        assert_eq!(kinds, vec![true, false]);
    }
}

#[tokio::test]
async fn a_panic_on_a_draw_turn_sends_a_stand_pat_not_a_fold() {
    struct Panicker;
    impl Bot for Panicker {
        fn decide(&mut self, _state: &GameState) -> Action {
            panic!("boom");
        }
    }
    let played = play(&mut Panicker, &draw27_script(true), Some(&["draw27"])).await;
    let actions = played.turn_actions();
    assert_eq!(
        actions[0],
        (
            "req_1".to_string(),
            "draw".to_string(),
            json!({ "discard": [] })
        )
    );
    // The betting turn (fold, call, raise) still falls back to fold.
    assert_eq!(actions[1].1, "fold");
}

#[tokio::test]
async fn a_holdem_answer_on_a_draw_turn_is_replaced_by_a_stand_pat() {
    // The hold'em bot answers `fold`, which a draw turn does not offer. The
    // Rust session substitutes the safe fallback for an illegal action
    // before sending (the JS SDK sends it and retries on the rejection).
    let mut bot = NlheOnlyBot::default();
    let played = play(&mut bot, &draw27_script(true), Some(&["draw27"])).await;
    let actions = played.turn_actions();
    assert_eq!(
        actions[0],
        (
            "req_1".to_string(),
            "draw".to_string(),
            json!({ "discard": [] })
        )
    );
    assert_eq!(bot.seen.len(), 2);
}

#[tokio::test]
async fn an_action_rejected_on_a_draw_turn_retries_with_a_stand_pat() {
    let mut bot = Draw27Bot::default();
    let played = play(
        &mut bot,
        &[
            draw27_server_hello(true),
            draw27_match_start(2),
            draw27_turn_request(3, "req_1", true),
            json!({
                "type": "action_rejected",
                "match_id": MATCH_ID,
                "seq": 4,
                "request_id": "req_1",
                "reason": "invalid_action",
                "valid_actions": ["draw"],
            }),
        ],
        Some(&["draw27"]),
    )
    .await;
    let actions = played.turn_actions();
    assert_eq!(actions.len(), 2);
    assert_eq!(actions[0].2, json!({ "discard": ["Ah", "Kd"] }));
    assert_eq!(
        actions[1],
        (
            "req_1".to_string(),
            "draw".to_string(),
            json!({ "discard": [] })
        )
    );
}

#[test]
fn safe_fallback_keeps_check_and_fold_wherever_they_are_offered() {
    let v = |a: &[&str]| a.iter().map(|s| s.to_string()).collect::<Vec<_>>();
    assert_eq!(
        _safe_fallback_action(&v(&["draw"])).unwrap(),
        FallbackAction::Typed(Action::stand_pat())
    );
    assert_eq!(
        _safe_fallback_action(&v(&["draw"])).unwrap().to_wire(),
        ("draw".to_string(), json!({ "discard": [] }))
    );
    assert_eq!(
        _safe_fallback_action(&v(&["fold", "call", "raise"])).unwrap(),
        FallbackAction::Typed(Action::Fold)
    );
    assert_eq!(
        _safe_fallback_action(&v(&["check", "raise"])).unwrap(),
        FallbackAction::Typed(Action::Check)
    );
}

// ---------------------------------------------------------------------------
// 7. state_shape routing (LAYER2-COMMON §2)
// ---------------------------------------------------------------------------

/// A table whose hello carries `game` and is then followed by a full 27TD
/// hand. A refused table must never reach `decide()` nor send a `hello`.
async fn refused(game: Value) {
    let mut frames = draw27_script(true);
    frames[0] = server_hello("whatever", Some(game.clone()));
    let mut bot = NlheOnlyBot::default();
    let played = play(&mut bot, &frames, Some(&["poker", "draw27", "ofc"])).await;
    assert!(played.end.is_none(), "{game}");
    assert!(bot.seen.is_empty(), "{game}");
    let types: Vec<&Value> = played.sent.iter().map(|m| &m["type"]).collect();
    assert_eq!(types, vec!["authenticate"], "{game}");
}

#[tokio::test]
async fn an_ofc_table_is_refused_until_ofc_parity_lands() {
    refused(json!({ "game_type": "ofc", "variant": "pineapple", "state_shape": "pineapple" }))
        .await;
}

#[tokio::test]
async fn an_unknown_state_shape_is_refused_not_read_as_holdem() {
    refused(json!({ "game_type": "bigbadbots", "state_shape": "bigbadbots" })).await;
    refused(json!({ "game_type": "chess", "state_shape": "chess" })).await;
}

#[tokio::test]
async fn a_malformed_game_descriptor_is_refused() {
    refused(json!({ "game_type": "draw27" })).await;
    refused(json!({ "state_shape": 5 })).await;
    refused(json!("27tripledraw")).await;
}

#[tokio::test]
async fn a_null_game_descriptor_is_poker() {
    let mut frames = draw27_script(true);
    frames[0]["game"] = Value::Null;
    let mut bot = Draw27Bot::default();
    let played = play(&mut bot, &frames, None).await;
    assert!(played.end.is_some());
    assert_eq!(bot.seen.len(), 2);
}

// ---------------------------------------------------------------------------
// 8. Seat tracking: seat from match_start, button from each round_start
// ---------------------------------------------------------------------------

#[tokio::test]
async fn the_seat_comes_from_match_start_and_the_button_from_each_round_start() {
    // The bot sits in seat 1; the button moves from seat 0 to seat 1 between
    // hands. A 27TD turn state carries neither, so a session that did not
    // track them would hand decide() your_seat=0, dealer_seat=0 every turn.
    let mut bot = Draw27Bot::default();
    let played = play(
        &mut bot,
        &[
            draw27_server_hello(true),
            draw27_match_start_in_seat(2, 1),
            draw27_round_start_with_button(3, 1, 0),
            draw27_turn_request(4, "req_1", true),
            draw27_round_start_with_button(5, 2, 1),
            draw27_turn_request(6, "req_2", false),
            match_end(7),
        ],
        Some(&["draw27"]),
    )
    .await;
    assert!(played.end.is_some());
    let seats: Vec<(i64, i64)> = bot
        .seen
        .iter()
        .map(|s| (s.your_seat, s.dealer_seat))
        .collect();
    assert_eq!(seats, vec![(1, 0), (1, 1)]);
}

#[tokio::test]
async fn the_same_tracking_applies_at_a_holdem_table() {
    let mut turn = holdem_turn_request();
    turn["seq"] = json!(4);
    let mut bot = NlheOnlyBot::default();
    play(
        &mut bot,
        &[
            server_hello("nlhe_6max", None),
            draw27_match_start_in_seat(2, 1),
            draw27_round_start_with_button(3, 1, 1),
            turn,
            match_end(5),
        ],
        None,
    )
    .await;
    assert_eq!(bot.seen.len(), 1);
    assert_eq!((bot.seen[0].your_seat, bot.seen[0].dealer_seat), (1, 1));
}

#[tokio::test]
async fn a_seat_carried_by_the_turn_state_wins() {
    let mut turn = draw27_turn_request(4, "req_1", true);
    turn["state"]["your_seat"] = json!(0);
    turn["state"]["dealer_seat"] = json!(0);
    let mut bot = Draw27Bot::default();
    play(
        &mut bot,
        &[
            draw27_server_hello(true),
            draw27_match_start_in_seat(2, 1),
            draw27_round_start_with_button(3, 1, 1),
            turn,
            match_end(5),
        ],
        Some(&["draw27"]),
    )
    .await;
    assert_eq!((bot.seen[0].your_seat, bot.seen[0].dealer_seat), (0, 0));
}

#[tokio::test]
async fn a_reconnect_keeps_the_button_and_relearns_the_seat() {
    // A `reconnected` frame re-seats the bot in seat 1 and replays a
    // pending draw turn; the button from the last `round_start` this
    // session saw is kept.
    let mut bot = Draw27Bot::default();
    let played = play(
        &mut bot,
        &[
            draw27_server_hello(true),
            draw27_round_start_with_button(2, 1, 1),
            json!({
                "type": "reconnected",
                "match_id": MATCH_ID,
                "seq": 3,
                "seats": draw27_match_start_in_seat(0, 1)["seats"].clone(),
                "game_config": draw27_match_start(0)["game_config"].clone(),
                "round_number": 1,
                "pending_request": draw27_turn_request(0, "req_p", true),
            }),
            match_end(4),
        ],
        Some(&["draw27"]),
    )
    .await;
    assert_eq!(played.turn_actions()[0].0, "req_p");
    assert_eq!((bot.seen[0].your_seat, bot.seen[0].dealer_seat), (1, 1));
}
