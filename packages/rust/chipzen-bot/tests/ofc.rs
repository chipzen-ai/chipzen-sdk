//! Pineapple OFC in the Rust SDK (chipzen-ai/chipzen-sdk#176).
//!
//! Ports the OFC half of the Python SDK's `tests/test_variant_backcompat.py`
//! and the JavaScript SDK's `tests/ofc.test.ts` against the same frames: the
//! `ofc_*` fixtures below are the Python `_ofc_*` fixtures
//! (`packages/python/src/chipzen/conformance.py`) value for value. What is
//! pinned:
//!
//! - `parse_game_state` reads an OFC `turn_request`, the new fields default
//!   at a hold'em table, and a mistyped optional key degrades to its default.
//! - OFC carries `your_seat` / `dealer_seat` in the turn state, and the
//!   state's values win over the seat the SDK tracks (also on a reconnect).
//! - The OFC envelope is an additive superset of the hold'em one.
//! - `Action::place` puts the placements and the discard under `params`.
//! - `decide_placement` is defaulted (legal, not good) and never called by
//!   the SDK.
//! - The safe fallback at an OFC turn (`["place"]`, no check, no fold) is the
//!   protocol's default placement (OFC section 5.9), built from that turn's
//!   state: on a panic, on an illegal answer, and on an `action_rejected`.
//! - End to end: a bot that declares `ofc` plays an OFC match through the
//!   real session loop, including a full session (a whole hand, then a
//!   Fantasy Land hand) against a table that rejects illegal placements.
//!
//! Not ported: the JavaScript bad-card tests (the Rust parser has always
//! dropped a malformed card rather than failing) and the conformance
//! `classify` vocabulary tests (that helper is private in Rust).
//!
//! Authority: docs/protocol/OFC-GAME-STATE-PROTOCOL.md.

use async_trait::async_trait;
use chipzen_bot::{
    _ofc_default_placement, _run_session, _safe_fallback_action, _safe_fallback_action_for,
    parse_game_state, run_conformance_checks, Action, ActionKind, Bot, Card, Error, FallbackAction,
    GameState, MessageReader, MessageWriter, Placement, RunConformanceOptions, SessionContext,
    SCENARIO_NAMES,
};
use serde_json::{json, Map, Value};
use std::collections::{BTreeMap, VecDeque};
use std::sync::{Arc, Mutex};

const MATCH_ID: &str = "m_conformance_test";
const PHASE_SEQUENCE: [&str; 6] = [
    "deal1", "street2", "street3", "street4", "street5", "complete",
];
const ROW_SIZES: [(&str, i64); 3] = [("top", 3), ("middle", 5), ("bottom", 5)];

// ---------------------------------------------------------------------------
// Fixtures: the Python `_ofc_*` frames, value for value
// ---------------------------------------------------------------------------

/// The server `hello`'s `game` descriptor at an OFC table (OFC §1.1).
fn ofc_game_descriptor() -> Value {
    json!({
        "game_type": "ofc",
        "variant": "pineapple",
        "actions": ["place"],
        "phases": PHASE_SEQUENCE,
        "state_shape": "pineapple",
    })
}

/// The server `hello` at an OFC table. `with_descriptor` adds the `game`
/// descriptor a live OFC table sends; without it the frame is the Python
/// fixture's `_server_hello(game_type="ofc_pineapple")`.
fn ofc_server_hello(with_descriptor: bool) -> Value {
    let mut hello = json!({
        "type": "hello",
        "match_id": MATCH_ID,
        "seq": 1,
        "server_ts": "2026-04-13T14:30:05.123Z",
        "supported_versions": ["1.0"],
        "selected_version": "1.0",
        "game_type": "ofc_pineapple",
        "capabilities": [],
    });
    if with_descriptor {
        hello["game"] = ofc_game_descriptor();
    }
    hello
}

/// `match_start` for a heads-up Pineapple OFC table. `scoring` is the key to
/// read first; there is NO `betting_structure` key and no blinds.
fn ofc_match_start(seq: i64) -> Value {
    json!({
        "type": "match_start",
        "match_id": MATCH_ID,
        "seq": seq,
        "server_ts": "2026-04-13T14:30:06.000Z",
        "seats": [
            { "seat": 0, "participant_id": "p0", "display_name": "Opp", "is_self": false },
            { "seat": 1, "participant_id": "p1", "display_name": "You", "is_self": true },
        ],
        "game_config": {
            "variant": "pineapple",
            "starting_stack": 10000,
            "scoring": "points",
            "point_value": 50,
            "bank_points": 200,
            "rows": { "top": 3, "middle": 5, "bottom": 5 },
            "cards_per_seat": 17,
            "cards_placed": 13,
            "cards_per_street": [5, 3, 3, 3, 3],
            "fantasy_land": {
                "cards": 14,
                "progressive": false,
                "middle_stay": null,
                "entry_min_pair_rank": 12,
            },
            "min_seats": 2,
            "max_seats": 3,
            "rated_max_seats": 2,
            "escalation_unit": "point_value",
            "escalation_interval": 20,
            "num_players": 2,
        },
        "turn_timeout_ms": 5000,
    })
}

fn empty_seat(seat: i64) -> Value {
    json!({
        "seat": seat,
        "rows": { "top": [], "middle": [], "bottom": [] },
        "placed": 0,
        "royalties": { "top": 0, "middle": 0, "bottom": 0 },
        "fouled": false,
        "stack": 10000,
        "in_fantasy_land": false,
        "hidden": false,
    })
}

/// `round_start` for OFC: the opening set of five, plus the public seats.
fn ofc_round_start(seq: i64) -> Value {
    json!({
        "type": "round_start",
        "match_id": MATCH_ID,
        "seq": seq,
        "server_ts": "2026-04-13T14:30:07.000Z",
        "round_id": "r_1",
        "round_number": 1,
        "state": {
            "hand_number": 1,
            "dealer_seat": 0,
            "your_seat": 1,
            "your_hole_cards": ["2c", "3d", "4h", "5s", "6s"],
            "cards_to_place": ["2c", "3d", "4h", "5s", "6s"],
            "your_rows": { "top": [], "middle": [], "bottom": [] },
            "pot": 0,
            "post_blind_stacks": [10000, 10000],
            "point_value": 50,
            "in_fantasy_land": false,
            "phase_sequence": PHASE_SEQUENCE,
            "seats": [empty_seat(0), empty_seat(1)],
            "stacks": [10000, 10000],
            "deck_commitment": "",
        },
    })
}

/// `turn_request` for an OFC pineapple street: place 2, discard 1.
/// `valid_actions` is exactly `["place"]`; the SHAPE of a legal placement
/// lives in the state. `board` is always `[]` and the betting fields are 0.
fn ofc_turn_request(seq: i64, request_id: &str) -> Value {
    json!({
        "type": "turn_request",
        "match_id": MATCH_ID,
        "seq": seq,
        "server_ts": "2026-04-13T14:30:07.500Z",
        "seat": 1,
        "request_id": request_id,
        "timeout_ms": 5000,
        "valid_actions": ["place"],
        "state": {
            "hand_number": 1,
            "phase": "street2",
            "board": [],
            "your_hole_cards": ["9s", "Js", "Ks"],
            "pot": 0,
            "your_stack": 10000,
            "opponent_stacks": [10000],
            "to_call": 0,
            "min_raise": 0,
            "max_raise": 0,
            "dealer_seat": 0,
            "your_seat": 1,
            "your_rows": { "top": ["2c"], "middle": ["3d", "4h"], "bottom": ["5s", "6s"] },
            "opponent_rows": { "0": { "top": ["7c"], "middle": ["8d", "9h"], "bottom": ["Ts", "Jc"] } },
            "cards_to_place": ["9s", "Js", "Ks"],
            "place": 2,
            "must_discard": 1,
            "row_capacity": { "top": 2, "middle": 3, "bottom": 3 },
            "royalties": { "top": 0, "middle": 0, "bottom": 0 },
            "opponent_royalties": { "0": { "top": 0, "middle": 0, "bottom": 0 } },
            "point_value": 50,
            "in_fantasy_land": false,
            "phase_sequence": PHASE_SEQUENCE,
            "action_history": [
                // The opening-set mask: 2c->top, 3d->middle, 4h->middle,
                // 5s->bottom, 6s->bottom = 1 + (2<<2) + (2<<4) + (3<<6) + (3<<8).
                { "seat": 1, "action": "place", "amount": 1001, "phase": "deal1", "is_timeout": false },
            ],
        },
    })
}

/// `turn_result` for a placement: `amount` is the placement MASK.
fn ofc_turn_result(seq: i64) -> Value {
    json!({
        "type": "turn_result",
        "match_id": MATCH_ID,
        "seq": seq,
        "server_ts": "2026-04-13T14:30:08.000Z",
        "is_timeout": false,
        "details": { "seat": 1, "action": "place", "amount": 14 },
    })
}

/// `round_result` for OFC: the showdown entries carry the OFC breakdown.
fn ofc_round_result(seq: i64) -> Value {
    json!({
        "type": "round_result",
        "match_id": MATCH_ID,
        "seq": seq,
        "server_ts": "2026-04-13T14:30:12.000Z",
        "round_id": "r_1",
        "round_number": 1,
        "result": {
            "hand_number": 1,
            "winner_seats": [1],
            "pot": 300,
            "payouts": [{ "seat": 1, "amount": 300, "refund": 0 }],
            "showdown": [
                {
                    "seat": 0,
                    "hole_cards": ["7c", "7d", "7h", "9c", "9d", "9h", "2c", "2d", "Ac", "Ad", "Ah", "Kc", "Kd"],
                    "hand_rank": "Two Pair",
                    "best_hand": ["Ac", "Ad", "Ah", "Kc", "Kd"],
                    "rows": {
                        "top": ["7c", "7d", "7h"],
                        "middle": ["9c", "9d", "9h", "2c", "2d"],
                        "bottom": ["Ac", "Ad", "Ah", "Kc", "Kd"],
                    },
                    "row_royalties": { "top": 10, "middle": 12, "bottom": 0 },
                    "royalties": 22,
                    "fouled": false,
                    "complete": true,
                    "points": -6,
                    "net_chips": -300,
                    "fantasy_land": true,
                    "in_fantasy_land": false,
                },
                {
                    "seat": 1,
                    "hole_cards": ["2c", "3d", "4h", "5s", "6s", "9s", "Js", "Ks", "Qs", "Th", "8c", "7d", "5h"],
                    "hand_rank": "Flush",
                    "best_hand": ["Ks", "Qs", "Js", "9s", "5s"],
                    "rows": {
                        "top": ["2c", "3d", "4h"],
                        "middle": ["6s", "8c", "7d", "5h", "Th"],
                        "bottom": ["Ks", "Qs", "Js", "9s", "5s"],
                    },
                    "row_royalties": { "top": 0, "middle": 0, "bottom": 4 },
                    "royalties": 4,
                    "fouled": false,
                    "complete": true,
                    "points": 6,
                    "net_chips": 300,
                    "fantasy_land": false,
                    "in_fantasy_land": false,
                },
            ],
            "action_history": [],
            "stacks": [9700, 10300],
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

/// Handshake + one OFC placement turn + round_result + match_end.
fn ofc_script(with_descriptor: bool) -> Vec<Value> {
    vec![
        ofc_server_hello(with_descriptor),
        ofc_match_start(2),
        ofc_round_start(3),
        ofc_turn_request(4, "req_1"),
        ofc_turn_result(5),
        ofc_round_result(6),
        match_end(7),
    ]
}

/// The opening-set `turn_request` for the same hand as `ofc_turn_request`:
/// five cards dealt, place 5, discard 0, every row empty.
fn deal1_turn_request(seq: i64, request_id: &str) -> Value {
    let mut msg = ofc_turn_request(seq, request_id);
    let opening = ofc_round_start(3)["state"]["cards_to_place"].clone();
    let state = &mut msg["state"];
    state["phase"] = json!("deal1");
    state["your_hole_cards"] = opening.clone();
    state["cards_to_place"] = opening;
    state["your_rows"] = json!({ "top": [], "middle": [], "bottom": [] });
    state["opponent_rows"] = json!({ "0": { "top": [], "middle": [], "bottom": [] } });
    state["place"] = json!(5);
    state["must_discard"] = json!(0);
    state["row_capacity"] = json!({ "top": 3, "middle": 5, "bottom": 5 });
    state["action_history"] = json!([]);
    msg
}

const FANTASY_LAND_CARDS: [&str; 14] = [
    "2c", "3d", "4h", "5s", "6s", "7c", "8d", "9h", "Ts", "Jc", "Qd", "Kh", "As", "Ac",
];

/// A Fantasy Land turn (OFC §5.6): 14 cards, place 13, discard 1.
fn fantasy_land_turn_request() -> Value {
    let mut msg = deal1_turn_request(4, "req_1");
    let state = &mut msg["state"];
    state["your_hole_cards"] = json!(FANTASY_LAND_CARDS);
    state["cards_to_place"] = json!(FANTASY_LAND_CARDS);
    state["place"] = json!(13);
    state["must_discard"] = json!(1);
    state["in_fantasy_land"] = json!(true);
    state["phase_sequence"] = json!(["deal1", "complete"]);
    msg
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

fn rejection(seq: i64, request_id: &str) -> Value {
    json!({
        "type": "action_rejected",
        "match_id": MATCH_ID,
        "seq": seq,
        "request_id": request_id,
        "reason": "invalid_action",
        "valid_actions": ["place"],
    })
}

fn poison(message: &Value, key: &str, value: Value) -> Value {
    let mut copy = message.clone();
    copy["state"][key] = value;
    copy
}

fn parsed() -> GameState {
    parse_game_state(&ofc_turn_request(4, "req_1"))
}

fn strings(items: &[&str]) -> Vec<String> {
    items.iter().map(|s| s.to_string()).collect()
}

fn int_map(entries: &[(&str, i64)]) -> BTreeMap<String, i64> {
    entries.iter().map(|(k, v)| (k.to_string(), *v)).collect()
}

fn rows(top: &[&str], middle: &[&str], bottom: &[&str]) -> BTreeMap<String, Vec<String>> {
    BTreeMap::from([
        ("top".to_string(), strings(top)),
        ("middle".to_string(), strings(middle)),
        ("bottom".to_string(), strings(bottom)),
    ])
}

// ---------------------------------------------------------------------------
// Legality: OFC §3.5
// ---------------------------------------------------------------------------

fn discard_list(discard: &Value) -> Vec<String> {
    match discard {
        Value::String(card) => vec![card.clone()],
        Value::Array(cards) => cards
            .iter()
            .map(|c| c.as_str().unwrap_or_default().to_string())
            .collect(),
        _ => Vec::new(),
    }
}

/// Exactly `place` placements and `must_discard` discards, drawn from
/// `cards`, each card once, every named row within its free capacity
/// counting the other placements in the same action.
fn check_placement(
    params: &Value,
    cards: &[String],
    place: usize,
    must_discard: usize,
    capacity: &BTreeMap<String, i64>,
) -> Result<(), String> {
    let placements = params["placements"]
        .as_array()
        .ok_or_else(|| format!("no placements list in {params}"))?;
    let discard = discard_list(&params["discard"]);
    if placements.len() != place {
        return Err(format!("{} placements, want {place}", placements.len()));
    }
    if discard.len() != must_discard {
        return Err(format!("{} discards, want {must_discard}", discard.len()));
    }
    let mut used: Vec<String> = placements
        .iter()
        .map(|p| p["card"].as_str().unwrap_or_default().to_string())
        .chain(discard)
        .collect();
    let mut dealt = cards.to_vec();
    used.sort();
    dealt.sort();
    if used != dealt {
        return Err(format!("cards {used:?} are not the dealt {dealt:?}"));
    }
    let mut counts: BTreeMap<String, i64> = BTreeMap::new();
    for p in placements {
        *counts
            .entry(p["row"].as_str().unwrap_or_default().to_string())
            .or_default() += 1;
    }
    for (row, count) in &counts {
        match capacity.get(row) {
            Some(free) if count <= free => {}
            _ => {
                return Err(format!(
                    "{count} cards to row {row:?}, capacity {capacity:?}"
                ))
            }
        }
    }
    Ok(())
}

fn assert_legal(params: &Value, state: &GameState) {
    let place = usize::try_from(state.place).unwrap();
    let must_discard = usize::try_from(state.must_discard).unwrap();
    if let Err(why) = check_placement(
        params,
        &state.cards_to_place,
        place,
        must_discard,
        &state.row_capacity,
    ) {
        panic!("illegal placement {params}: {why}");
    }
}

fn row_counts(params: &Value) -> BTreeMap<String, i64> {
    let mut counts = BTreeMap::new();
    for p in params["placements"].as_array().unwrap() {
        *counts
            .entry(p["row"].as_str().unwrap().to_string())
            .or_default() += 1;
    }
    counts
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

/// An OFC bot: dispatches placement turns to `decide_placement` (the SDK
/// default).
#[derive(Default)]
struct OfcBot {
    seen: Vec<GameState>,
}

impl Bot for OfcBot {
    fn decide(&mut self, state: &GameState) -> Action {
        self.seen.push(state.clone());
        if state.valid_actions.iter().any(|a| a == "place") {
            return self.decide_placement(state);
        }
        if state.valid_actions.iter().any(|a| a == "check") {
            Action::Check
        } else {
            Action::Fold
        }
    }
}

/// Only implements `decide`.
struct LegacyBot;
impl Bot for LegacyBot {
    fn decide(&mut self, _state: &GameState) -> Action {
        Action::Fold
    }
}

struct Panicker;
impl Bot for Panicker {
    fn decide(&mut self, _state: &GameState) -> Action {
        panic!("boom");
    }
}

/// Answers every OFC turn with a well-formed `place` that puts every card in
/// the top row: the right action kind, so it is sent, and the table rejects
/// it (too many cards for the top row, no discard).
struct TopStacker;
impl Bot for TopStacker {
    fn decide(&mut self, state: &GameState) -> Action {
        Action::place(
            state.cards_to_place.iter().map(|card| (card, "top")),
            None::<&str>,
        )
    }
}

// ---------------------------------------------------------------------------
// 1. An OFC turn_request parses, and its new keys are readable
// ---------------------------------------------------------------------------

#[test]
fn an_ofc_turn_parses_with_board_carried_as_empty() {
    let state = parsed();
    assert!(state.board.is_empty());
    // Your pending cards are ALSO the Card-parsed hole_cards.
    let hole: Vec<String> = state.hole_cards.iter().map(Card::to_string).collect();
    assert_eq!(hole, state.cards_to_place);
}

#[test]
fn a_holdem_only_bot_decides_on_it_without_panicking() {
    // Wrong for the table, but WRONG is survivable and CRASHING is not.
    let mut bot = NlheOnlyBot::default();
    let action = bot.decide(&parsed());
    assert_ne!(action.kind(), ActionKind::Place);
    assert_eq!(bot.seen.len(), 1);
}

#[test]
fn the_ofc_keys_are_readable() {
    let state = parsed();
    assert_eq!(state.valid_actions, vec!["place"]);
    assert_eq!(state.cards_to_place, strings(&["9s", "Js", "Ks"]));
    assert_eq!(state.place, 2);
    assert_eq!(state.must_discard, 1);
    // place + must_discard == cards_to_place.len(), always.
    assert_eq!(
        usize::try_from(state.place + state.must_discard).unwrap(),
        state.cards_to_place.len()
    );
    assert_eq!(
        state.row_capacity,
        int_map(&[("top", 2), ("middle", 3), ("bottom", 3)])
    );
    assert_eq!(state.your_rows, rows(&["2c"], &["3d", "4h"], &["5s", "6s"]));
    // Seat index is a DECIMAL STRING key, because JSON object keys are.
    assert_eq!(state.opponent_rows["0"]["middle"], strings(&["8d", "9h"]));
    assert_eq!(
        state.royalties,
        int_map(&[("top", 0), ("middle", 0), ("bottom", 0)])
    );
    assert_eq!(state.opponent_royalties["0"]["top"], 0);
    assert_eq!(state.point_value, 50);
    assert!(!state.in_fantasy_land);
    assert_eq!(state.phase_sequence, strings(&PHASE_SEQUENCE));
    assert_eq!(state.phase, "street2");
    assert_eq!(state.action_history[0].action, "place");
    assert_eq!(state.action_history[0].amount, Some(1001));
}

#[test]
fn ofc_carries_its_own_seats_in_the_turn_state() {
    let state = parsed();
    assert_eq!((state.your_seat, state.dealer_seat), (1, 0));
}

#[test]
fn a_fantasy_land_turn_reads_as_14_cards_place_13_discard_1() {
    let state = parse_game_state(&fantasy_land_turn_request());
    assert_eq!(state.cards_to_place.len(), 14);
    assert_eq!(state.place, 13);
    assert_eq!(state.must_discard, 1);
    assert!(state.in_fantasy_land);
    assert_eq!(state.phase_sequence, strings(&["deal1", "complete"]));
}

#[test]
fn a_hidden_fantasy_land_board_reads_as_empty_rows_never_a_placeholder() {
    let mut msg = ofc_turn_request(4, "req_1");
    msg["state"]["opponent_rows"] = json!({ "0": { "top": [], "middle": [], "bottom": [] } });
    msg["state"]["opponent_royalties"] = json!({ "0": { "top": 0, "middle": 0, "bottom": 0 } });
    let state = parse_game_state(&msg);
    assert_eq!(
        state.opponent_rows,
        BTreeMap::from([("0".to_string(), rows(&[], &[], &[]))])
    );
    assert_eq!(
        state.opponent_royalties,
        BTreeMap::from([(
            "0".to_string(),
            int_map(&[("top", 0), ("middle", 0), ("bottom", 0)])
        )])
    );
}

#[test]
fn a_holdem_payload_leaves_every_ofc_field_at_its_default() {
    let state = parse_game_state(&holdem_turn_request());
    assert!(state.your_rows.is_empty());
    assert!(state.opponent_rows.is_empty());
    assert!(state.cards_to_place.is_empty());
    assert_eq!(state.place, 0);
    assert_eq!(state.must_discard, 0);
    assert!(state.row_capacity.is_empty());
    assert!(state.royalties.is_empty());
    assert!(state.opponent_royalties.is_empty());
    assert_eq!(state.point_value, 0);
    assert!(!state.in_fantasy_land);
    assert!(state.phase_sequence.is_empty());
}

#[test]
fn a_mistyped_optional_key_degrades_to_its_default() {
    // The variant keys are lenient on purpose (as in Python and JS): a
    // mistyped optional key must not become a session-killing failure.
    let base = ofc_turn_request(4, "req_1");
    let check = |key: &str, value: Value| parse_game_state(&poison(&base, key, value));

    assert!(check("your_rows", json!(["2c"])).your_rows.is_empty());
    assert_eq!(
        check("your_rows", json!({ "top": "2c" })).your_rows,
        BTreeMap::from([("top".to_string(), Vec::new())])
    );
    assert!(check("opponent_rows", json!([1])).opponent_rows.is_empty());
    assert_eq!(
        check("opponent_rows", json!({ "0": "x" })).opponent_rows,
        BTreeMap::from([("0".to_string(), BTreeMap::new())])
    );
    assert!(check("cards_to_place", json!("9s"))
        .cards_to_place
        .is_empty());
    assert_eq!(check("place", json!("2")).place, 0);
    assert_eq!(check("place", json!(true)).place, 0);
    assert_eq!(check("must_discard", Value::Null).must_discard, 0);
    assert_eq!(check("must_discard", json!(1.9)).must_discard, 1);
    assert!(check("row_capacity", json!([2, 3, 3]))
        .row_capacity
        .is_empty());
    assert_eq!(
        check("row_capacity", json!({ "top": "2", "middle": 3 })).row_capacity,
        int_map(&[("top", 0), ("middle", 3)])
    );
    assert!(check("royalties", json!("none")).royalties.is_empty());
    assert_eq!(
        check("opponent_royalties", json!({ "0": [1] })).opponent_royalties,
        BTreeMap::from([("0".to_string(), BTreeMap::new())])
    );
    assert_eq!(check("point_value", json!("50")).point_value, 0);
    assert!(!check("in_fantasy_land", json!("yes")).in_fantasy_land);
    assert!(check("phase_sequence", json!("deal1"))
        .phase_sequence
        .is_empty());
    // A non-string card is kept as its text, as Python's str() and
    // JavaScript's String() keep it: never a parse failure.
    assert_eq!(
        check("cards_to_place", json!(["9s", 5])).cards_to_place,
        strings(&["9s", "5"])
    );
}

// ---------------------------------------------------------------------------
// 2. No hold'em-era field is displaced; the envelope is an additive superset
// ---------------------------------------------------------------------------

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
fn ofc_zeroes_the_betting_fields_rather_than_dropping_them() {
    // Rule 2: a bot that computes a bet size from these computes zero, which
    // is correct. A bot that hits a MISSING key crashes, which is not.
    let raw = ofc_turn_request(4, "req_1");
    for key in ["pot", "to_call", "min_raise", "max_raise", "your_stack"] {
        assert!(raw["state"][key].is_i64(), "{key}");
    }
    assert!(raw["state"]["opponent_stacks"].is_array());
    let state = parsed();
    assert_eq!(
        (state.to_call, state.min_raise, state.max_raise, state.pot),
        (0, 0, 0, 0)
    );
    assert_eq!(state.your_stack, 10000);
    assert_eq!(state.opponent_stacks, vec![10000]);
    assert_eq!(state.request_id, "req_1");
}

#[test]
fn the_ofc_state_carries_every_holdem_key_with_the_same_type() {
    let nlhe = holdem_turn_request();
    let variant = ofc_turn_request(4, "req_1");
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
    // valid_actions is exactly ["place"]; the placement's shape lives in state.
    assert_eq!(variant["valid_actions"], json!(["place"]));
}

#[test]
fn the_config_says_scoring_points_and_carries_no_betting_keys() {
    let cfg = &ofc_match_start(2)["game_config"];
    assert_eq!(cfg["variant"], "pineapple");
    assert_eq!(cfg["scoring"], "points");
    for absent in ["betting_structure", "small_blind", "big_blind", "bet_cap"] {
        assert!(cfg.get(absent).is_none(), "{absent}");
    }
    // point_value is the ruled quantity; bank_points is its derivation.
    assert_eq!(cfg["point_value"], 50);
    assert_eq!(
        cfg["bank_points"].as_i64().unwrap(),
        cfg["starting_stack"].as_i64().unwrap() / cfg["point_value"].as_i64().unwrap()
    );
    // rows is an OBJECT of row name -> capacity; its keys are the row names.
    assert_eq!(cfg["rows"], json!({ "top": 3, "middle": 5, "bottom": 5 }));
    let total: i64 = cfg["rows"]
        .as_object()
        .unwrap()
        .values()
        .map(|v| v.as_i64().unwrap())
        .sum();
    assert_eq!(cfg["cards_placed"].as_i64().unwrap(), total);
    assert!(cfg.get("points_per_chip").is_none());
}

#[test]
fn the_game_descriptor_matches_ofc_section_1_1() {
    assert_eq!(
        ofc_game_descriptor(),
        json!({
            "game_type": "ofc",
            "variant": "pineapple",
            "actions": ["place"],
            "phases": ["deal1", "street2", "street3", "street4", "street5", "complete"],
            "state_shape": "pineapple",
        })
    );
}

#[test]
fn game_state_default_fills_the_ofc_fields_and_a_holdem_literal_still_compiles() {
    let state = GameState::default();
    assert!(state.cards_to_place.is_empty());
    assert_eq!(state.place, 0);
    assert!(state.row_capacity.is_empty());
    let literal = GameState {
        valid_actions: vec!["fold".to_string()],
        ..Default::default()
    };
    assert_eq!(NlheOnlyBot::default().decide(&literal), Action::Fold);
}

// ---------------------------------------------------------------------------
// 3. Action::place puts the placements and the discard under `params`
// ---------------------------------------------------------------------------

#[test]
fn place_emits_the_placements_and_the_discard_under_params() {
    assert_eq!(
        Action::place([("Ks", "bottom"), ("Js", "middle")], ["9s"]).to_wire(),
        (
            "place",
            json!({
                "placements": [
                    { "card": "Ks", "row": "bottom" },
                    { "card": "Js", "row": "middle" },
                ],
                "discard": ["9s"],
            })
        )
    );
}

#[test]
fn place_accepts_placements_cards_and_an_optional_discard() {
    let card: Card = "2c".parse().unwrap();
    let wire = Action::place(
        [Placement::new(card, "top"), ("3d", "middle").into()],
        Some("9s"),
    )
    .to_wire()
    .1;
    assert_eq!(wire["placements"][0], json!({ "card": "2c", "row": "top" }));
    assert_eq!(
        wire["placements"][1],
        json!({ "card": "3d", "row": "middle" })
    );
    assert_eq!(wire["discard"], json!(["9s"]));
}

#[test]
fn the_opening_set_discards_nothing() {
    let none: [&str; 0] = [];
    for action in [
        Action::place([("2c", "top")], None::<&str>),
        Action::place([("2c", "top")], none),
        Action::place([("2c", "top")], Vec::<String>::new()),
    ] {
        assert_eq!(action.to_wire().1["discard"], json!([]));
    }
}

#[test]
fn the_place_kind_round_trips_and_holdem_wire_shapes_are_unchanged() {
    assert_eq!(ActionKind::Place.as_str(), "place");
    assert_eq!(ActionKind::parse("place"), Some(ActionKind::Place));
    assert_eq!(
        Action::place([("Ks", "bottom")], None::<&str>).kind(),
        ActionKind::Place
    );
    assert_eq!(Action::Fold.to_wire(), ("fold", json!({})));
    assert_eq!(Action::Check.to_wire(), ("check", json!({})));
    assert_eq!(Action::Call.to_wire(), ("call", json!({})));
    assert_eq!(Action::AllIn.to_wire(), ("all_in", json!({})));
    assert_eq!(
        Action::Raise(200).to_wire(),
        ("raise", json!({ "amount": 200 }))
    );
}

#[test]
fn action_and_action_kind_matches_outside_the_crate_need_a_wildcard() {
    // Both enums are #[non_exhaustive]: a future game's action is a minor
    // release, not a break. This match only compiles with the `_` arm.
    let describe = |action: &Action| match action {
        Action::Fold => "fold",
        Action::Place { .. } => "place",
        _ => "other",
    };
    assert_eq!(describe(&Action::stand_pat()), "other");
    assert_eq!(
        describe(&Action::place([("Ks", "bottom")], ["9s"])),
        "place"
    );
}

// ---------------------------------------------------------------------------
// 4. decide() stays the single required method; decide_placement is defaulted
// ---------------------------------------------------------------------------

#[test]
fn decide_placement_defaults_to_a_legal_placement() {
    let state = parsed();
    let (kind, params) = LegacyBot.decide_placement(&state).to_wire();
    assert_eq!(kind, "place");
    assert_legal(&params, &state);
    // Bottom, then middle, then top; the leftovers are the discard.
    assert_eq!(
        params,
        json!({
            "placements": [
                { "card": "9s", "row": "bottom" },
                { "card": "Js", "row": "bottom" },
            ],
            "discard": ["Ks"],
        })
    );
}

#[test]
fn decide_placement_respects_a_full_row() {
    let mut msg = ofc_turn_request(4, "req_1");
    msg["state"]["row_capacity"] = json!({ "top": 0, "middle": 0, "bottom": 2 });
    let params = LegacyBot
        .decide_placement(&parse_game_state(&msg))
        .to_wire()
        .1;
    assert_eq!(row_counts(&params), int_map(&[("bottom", 2)]));
}

#[test]
fn decide_placement_is_legal_on_the_opening_set_and_a_fantasy_land_turn() {
    for msg in [deal1_turn_request(4, "req_1"), fantasy_land_turn_request()] {
        let state = parse_game_state(&msg);
        assert_legal(&LegacyBot.decide_placement(&state).to_wire().1, &state);
    }
}

#[tokio::test]
async fn decide_placement_is_not_called_by_the_session_loop() {
    #[derive(Default)]
    struct HookSpy {
        inner: NlheOnlyBot,
        hook_calls: usize,
    }
    impl Bot for HookSpy {
        fn decide(&mut self, state: &GameState) -> Action {
            self.inner.decide(state)
        }
        fn decide_placement(&mut self, _state: &GameState) -> Action {
            self.hook_calls += 1;
            Action::place([("9s", "bottom")], ["Js"])
        }
    }
    let mut bot = HookSpy::default();
    let played = play(&mut bot, &ofc_script(true), Some(&["ofc"])).await;
    assert!(played.end.is_some());
    assert_eq!(bot.inner.seen.len(), 1);
    assert_eq!(bot.hook_calls, 0);
}

// ---------------------------------------------------------------------------
// 5. The safe fallback at an OFC turn is the protocol's default placement
// ---------------------------------------------------------------------------

#[test]
fn the_default_placement_fills_the_roomiest_row_ties_toward_the_back() {
    // Capacity top 2, middle 3, bottom 3: 9s ties middle/bottom -> bottom;
    // then middle (3) beats bottom (2) -> Js to middle; Ks is the discard.
    assert_eq!(
        _ofc_default_placement(Some(&parsed())).to_wire().1,
        json!({
            "placements": [
                { "card": "9s", "row": "bottom" },
                { "card": "Js", "row": "middle" },
            ],
            "discard": ["Ks"],
        })
    );
}

#[test]
fn the_fallback_is_legal_on_every_kind_of_ofc_turn() {
    let place = strings(&["place"]);
    for msg in [
        ofc_turn_request(4, "req_1"),
        deal1_turn_request(4, "req_1"),
        fantasy_land_turn_request(),
    ] {
        let state = parse_game_state(&msg);
        let FallbackAction::Typed(action) =
            _safe_fallback_action_for(&place, Some(&state)).unwrap()
        else {
            panic!("an OFC fallback must be a typed placement");
        };
        assert_eq!(action.kind(), ActionKind::Place);
        assert_legal(&action.to_wire().1, &state);
    }
}

#[test]
fn without_a_state_the_fallback_is_an_empty_well_formed_placement() {
    let empty = (
        "place".to_string(),
        json!({ "placements": [], "discard": [] }),
    );
    assert_eq!(
        _safe_fallback_action(&strings(&["place"]))
            .unwrap()
            .to_wire(),
        empty
    );
    assert_eq!(
        _safe_fallback_action_for(&strings(&["place"]), None)
            .unwrap()
            .to_wire(),
        empty
    );
}

#[test]
fn the_fallback_keeps_check_fold_and_stand_pat_wherever_they_are_offered() {
    let state = parsed();
    let fallback =
        |valid: &[&str]| _safe_fallback_action_for(&strings(valid), Some(&state)).unwrap();
    assert_eq!(
        fallback(&["check", "raise"]),
        FallbackAction::Typed(Action::Check)
    );
    assert_eq!(
        fallback(&["fold", "call", "raise"]),
        FallbackAction::Typed(Action::Fold)
    );
    assert_eq!(
        fallback(&["draw"]),
        FallbackAction::Typed(Action::stand_pat())
    );
}

// ---------------------------------------------------------------------------
// 6. The fixtures match the spec and stay out of the grader
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
fn an_action_history_amount_is_a_placement_mask_that_decodes_to_your_rows() {
    let msg = ofc_turn_request(4, "req_1");
    let entry = &msg["state"]["action_history"][0];
    assert_eq!(entry["action"], "place");
    let mask = entry["amount"].as_u64().unwrap();
    let codes: Vec<u64> = (0..5).map(|i| (mask >> (2 * i)) & 0b11).collect();
    assert_eq!(codes, vec![1, 2, 2, 3, 3]); // top, middle, middle, bottom, bottom

    let row_name = |code: u64| ["", "top", "middle", "bottom"][code as usize];
    let dealt = ofc_round_start(3)["state"]["cards_to_place"].clone();
    let mut rebuilt = json!({ "top": [], "middle": [], "bottom": [] });
    for (card, code) in dealt.as_array().unwrap().iter().zip(&codes) {
        assert_ne!(*code, 0, "the opening set discards nothing");
        rebuilt[row_name(*code)]
            .as_array_mut()
            .unwrap()
            .push(card.clone());
    }
    assert_eq!(rebuilt, msg["state"]["your_rows"]);

    // A pineapple street DOES carry one code 0: place 2, discard 1.
    let details = &ofc_turn_result(5)["details"];
    assert_eq!(details["action"], "place");
    let mask = details["amount"].as_u64().unwrap();
    let codes: Vec<u64> = (0..3).map(|i| (mask >> (2 * i)) & 0b11).collect();
    assert_eq!(codes, vec![2, 3, 0]);
}

#[test]
fn the_showdown_points_and_chips_sum_to_zero_and_every_seat_appears() {
    let result = &ofc_round_result(6)["result"];
    let showdown = result["showdown"].as_array().unwrap();
    let sum = |key: &str| -> i64 { showdown.iter().map(|e| e[key].as_i64().unwrap()).sum() };
    assert_eq!(sum("points"), 0);
    assert_eq!(sum("net_chips"), 0);
    let mut seats: Vec<i64> = showdown
        .iter()
        .map(|e| e["seat"].as_i64().unwrap())
        .collect();
    seats.sort();
    assert_eq!(seats, vec![0, 1]);
    // A discard never appears, not even in its owner's own entry.
    for e in showdown {
        assert_eq!(e["hole_cards"].as_array().unwrap().len(), 13);
    }
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

fn ctx(supported_games: Option<&[&str]>) -> SessionContext {
    SessionContext {
        supported_games: supported_games.map(|g| g.iter().map(|s| s.to_string()).collect()),
        ..SessionContext::new(
            MATCH_ID.to_string(),
            Some("t".to_string()),
            None,
            "test".to_string(),
            "0.0.0".to_string(),
        )
    }
}

async fn play<B: Bot>(bot: &mut B, frames: &[Value], supported_games: Option<&[&str]>) -> Played {
    let mut reader = ScriptedReader::new(frames);
    let mut writer = CapturingWriter::default();
    let end = _run_session(&mut reader, &mut writer, bot, &ctx(supported_games))
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

/// Handshake + a hand with two placement turns (deal1, then street2) +
/// match_end.
fn two_street_script() -> Vec<Value> {
    let mut first_result = ofc_turn_result(0);
    first_result["seq"] = json!(5);
    let mut second_result = ofc_turn_result(0);
    second_result["seq"] = json!(7);
    vec![
        ofc_server_hello(true),
        ofc_match_start(2),
        ofc_round_start(3),
        deal1_turn_request(4, "req_1"),
        first_result,
        ofc_turn_request(6, "req_2"),
        second_result,
        ofc_round_result(8),
        match_end(9),
    ]
}

// ---------------------------------------------------------------------------
// 7. End to end: a Rust bot that declares ofc plays an OFC match
// ---------------------------------------------------------------------------

#[tokio::test]
async fn a_bot_that_declares_ofc_plays_the_fixture_hand_end_to_end() {
    // With the live game descriptor, and with the Python fixture's hello (no
    // descriptor), which a client reads as poker and parses the same way.
    for with_descriptor in [true, false] {
        let mut bot = OfcBot::default();
        let played = play(
            &mut bot,
            &ofc_script(with_descriptor),
            Some(&["poker", "ofc"]),
        )
        .await;

        assert_eq!(played.end.as_ref().unwrap()["type"], "match_end");
        assert_eq!(
            played.hello().unwrap()["supported_games"],
            json!(["poker", "ofc"])
        );
        let actions = played.turn_actions();
        assert_eq!(actions.len(), 1);
        assert_eq!(
            (actions[0].0.as_str(), actions[0].1.as_str()),
            ("req_1", "place")
        );
        assert_legal(&actions[0].2, &bot.seen[0]);
        assert_eq!(bot.seen[0].your_seat, 1);
    }
}

#[tokio::test]
async fn a_bot_plays_the_opening_set_and_a_pineapple_street_each_answer_legal() {
    let mut bot = OfcBot::default();
    let played = play(&mut bot, &two_street_script(), Some(&["ofc"])).await;

    assert_eq!(played.end.as_ref().unwrap()["type"], "match_end");
    let actions = played.turn_actions();
    let ids: Vec<(&str, &str)> = actions
        .iter()
        .map(|(id, action, _)| (id.as_str(), action.as_str()))
        .collect();
    assert_eq!(ids, vec![("req_1", "place"), ("req_2", "place")]);
    let seen: Vec<(&str, i64, i64)> = bot
        .seen
        .iter()
        .map(|s| (s.phase.as_str(), s.place, s.must_discard))
        .collect();
    assert_eq!(seen, vec![("deal1", 5, 0), ("street2", 2, 1)]);
    for (i, (_, _, params)) in actions.iter().enumerate() {
        assert_legal(params, &bot.seen[i]);
    }
}

#[tokio::test]
async fn a_holdem_answer_on_an_ofc_turn_is_replaced_by_the_default_placement() {
    // The hold'em bot answers `fold`, which an OFC turn does not offer. The
    // Rust session substitutes the safe fallback for an illegal action
    // before sending (the JS SDK sends it and retries on the rejection).
    let mut bot = NlheOnlyBot::default();
    let played = play(&mut bot, &two_street_script(), None).await;
    assert!(played.end.is_some());
    let actions = played.turn_actions();
    assert_eq!(actions.len(), 2);
    for (i, (_, action, params)) in actions.iter().enumerate() {
        assert_eq!(action, "place");
        assert_legal(params, &bot.seen[i]);
    }
}

#[tokio::test]
async fn a_panic_on_an_ofc_turn_sends_the_default_placement_not_a_fold() {
    let played = play(&mut Panicker, &ofc_script(true), Some(&["ofc"])).await;
    assert_eq!(
        played.turn_actions()[0],
        (
            "req_1".to_string(),
            "place".to_string(),
            json!({
                "placements": [
                    { "card": "9s", "row": "bottom" },
                    { "card": "Js", "row": "middle" },
                ],
                "discard": ["Ks"],
            })
        )
    );
}

#[tokio::test]
async fn an_action_rejected_on_an_ofc_turn_retries_with_the_default_placement() {
    let played = play(
        &mut TopStacker,
        &[
            ofc_server_hello(true),
            ofc_match_start(2),
            ofc_turn_request(3, "req_1"),
            rejection(4, "req_1"),
        ],
        Some(&["ofc"]),
    )
    .await;
    let actions = played.turn_actions();
    assert_eq!(actions.len(), 2);
    // The bot's own (illegal) answer went out, then the legal default.
    assert_eq!(row_counts(&actions[0].2), int_map(&[("top", 3)]));
    assert_eq!(
        (actions[1].0.as_str(), actions[1].1.as_str()),
        ("req_1", "place")
    );
    assert_legal(&actions[1].2, &parsed());
    assert_eq!(
        actions[1].2,
        _ofc_default_placement(Some(&parsed())).to_wire().1
    );
}

#[tokio::test]
async fn an_action_rejected_for_a_turn_never_seen_sends_an_empty_placement() {
    let played = play(
        &mut TopStacker,
        &[
            ofc_server_hello(true),
            ofc_match_start(2),
            ofc_turn_request(3, "req_1"),
            rejection(4, "req_other"),
        ],
        Some(&["ofc"]),
    )
    .await;
    assert_eq!(
        played.turn_actions()[1],
        (
            "req_other".to_string(),
            "place".to_string(),
            json!({ "placements": [], "discard": [] })
        )
    );
}

#[tokio::test]
async fn the_ofc_turn_states_own_seats_win_over_the_tracked_ones() {
    // match_start seats the bot in seat 0 and round_start puts the button on
    // seat 1; the OFC turn state says your_seat 1, dealer_seat 0, and wins.
    let mut start = ofc_match_start(2);
    start["seats"][0]["is_self"] = json!(true);
    start["seats"][1]["is_self"] = json!(false);
    let mut round = ofc_round_start(3);
    round["state"]["dealer_seat"] = json!(1);
    let mut bot = OfcBot::default();
    play(
        &mut bot,
        &[
            ofc_server_hello(true),
            start,
            round,
            ofc_turn_request(4, "req_1"),
            match_end(5),
        ],
        Some(&["ofc"]),
    )
    .await;
    assert_eq!((bot.seen[0].your_seat, bot.seen[0].dealer_seat), (1, 0));
}

#[tokio::test]
async fn on_a_reconnect_the_ofc_states_own_your_seat_wins_over_the_relearned_seat() {
    let mut bot = OfcBot::default();
    let played = play(
        &mut bot,
        &[
            ofc_server_hello(true),
            json!({
                "type": "reconnected",
                "match_id": MATCH_ID,
                "seq": 2,
                "round_number": 1,
                "seats": [{ "seat": 0, "participant_id": "p1", "is_self": true }],
                "pending_request": ofc_turn_request(3, "req_1"),
            }),
            rejection(4, "req_1"),
        ],
        Some(&["ofc"]),
    )
    .await;
    let seats: Vec<(i64, i64)> = bot
        .seen
        .iter()
        .map(|s| (s.your_seat, s.dealer_seat))
        .collect();
    assert_eq!(seats, vec![(1, 0)]);
    // The pending turn is remembered for an action_rejected retry, too.
    let actions = played.turn_actions();
    let ids: Vec<(&str, &str)> = actions
        .iter()
        .map(|(id, action, _)| (id.as_str(), action.as_str()))
        .collect();
    assert_eq!(ids, vec![("req_1", "place"), ("req_1", "place")]);
    assert_legal(&actions[1].2, &parsed());
}

#[tokio::test]
async fn on_a_reconnect_a_state_without_your_seat_takes_the_relearned_seat() {
    let mut pending = ofc_turn_request(3, "req_1");
    pending["state"]
        .as_object_mut()
        .unwrap()
        .remove("your_seat");
    let mut bot = OfcBot::default();
    play(
        &mut bot,
        &[
            ofc_server_hello(true),
            json!({
                "type": "reconnected",
                "match_id": MATCH_ID,
                "seq": 2,
                "round_number": 1,
                "seats": [{ "seat": 1, "participant_id": "p1", "is_self": true }],
                "pending_request": pending,
            }),
        ],
        Some(&["ofc"]),
    )
    .await;
    let seats: Vec<i64> = bot.seen.iter().map(|s| s.your_seat).collect();
    assert_eq!(seats, vec![1]);
}

// ---------------------------------------------------------------------------
// 8. A full session against a table that enforces OFC §3.5
// ---------------------------------------------------------------------------

/// One placement turn the table will deal.
#[derive(Clone)]
struct Turn {
    hand: i64,
    phase: &'static str,
    cards: Vec<String>,
    place: usize,
    discard: usize,
    fantasy: bool,
}

/// Hand 1: the opening set and four pineapple streets (5 + 4 x 2 = 13
/// placed). Hand 2: a Fantasy Land hand, 14 cards, one turn, place 13, with
/// the button frozen on seat 0 (OFC §5.6).
fn full_session_turns() -> Vec<Turn> {
    let turn = |phase: &'static str, cards: &[&str], place: usize, discard: usize| Turn {
        hand: 1,
        phase,
        cards: strings(cards),
        place,
        discard,
        fantasy: false,
    };
    vec![
        turn("deal1", &["2c", "3d", "4h", "5s", "6s"], 5, 0),
        turn("street2", &["9s", "Js", "Ks"], 2, 1),
        turn("street3", &["7c", "8d", "Th"], 2, 1),
        turn("street4", &["Qc", "Ad", "2h"], 2, 1),
        turn("street5", &["3s", "4c", "5d"], 2, 1),
        Turn {
            hand: 2,
            phase: "deal1",
            cards: strings(&FANTASY_LAND_CARDS),
            place: 13,
            discard: 1,
            fantasy: true,
        },
    ]
}

fn empty_rows() -> BTreeMap<String, Vec<String>> {
    rows(&[], &[], &[])
}

fn capacity_of(board: &BTreeMap<String, Vec<String>>) -> BTreeMap<String, i64> {
    ROW_SIZES
        .iter()
        .map(|(row, size)| (row.to_string(), size - board[*row].len() as i64))
        .collect()
}

fn phase_sequence_for(turn: &Turn) -> Value {
    if turn.fantasy {
        json!(["deal1", "complete"])
    } else {
        json!(PHASE_SEQUENCE)
    }
}

fn round_start_for(turn: &Turn) -> Value {
    let mut msg = ofc_round_start(0);
    msg["round_id"] = json!(format!("r_{}", turn.hand));
    msg["round_number"] = json!(turn.hand);
    let state = &mut msg["state"];
    state["hand_number"] = json!(turn.hand);
    state["your_hole_cards"] = json!(turn.cards);
    state["cards_to_place"] = json!(turn.cards);
    state["in_fantasy_land"] = json!(turn.fantasy);
    state["phase_sequence"] = phase_sequence_for(turn);
    msg
}

fn turn_request_for(turn: &Turn, request_id: &str, board: &BTreeMap<String, Vec<String>>) -> Value {
    let mut msg = ofc_turn_request(0, request_id);
    let state = &mut msg["state"];
    state["hand_number"] = json!(turn.hand);
    state["phase"] = json!(turn.phase);
    state["your_hole_cards"] = json!(turn.cards);
    state["cards_to_place"] = json!(turn.cards);
    state["your_rows"] = json!(board);
    state["place"] = json!(turn.place);
    state["must_discard"] = json!(turn.discard);
    state["row_capacity"] = json!(capacity_of(board));
    state["in_fantasy_land"] = json!(turn.fantasy);
    state["phase_sequence"] = phase_sequence_for(turn);
    state["action_history"] = json!([]);
    msg
}

/// A scripted OFC table: deals `turns` in order and REJECTS an illegal
/// placement (OFC §3.5) with `action_rejected`, as the live server does.
struct OfcTable {
    inbox: VecDeque<String>,
    seq: i64,
    pending: VecDeque<Turn>,
    current: Option<(String, Turn)>,
    board: BTreeMap<String, Vec<String>>,
    next_request: usize,
    accepted: Vec<(String, Value)>,
    rejections: usize,
    finished_boards: Vec<BTreeMap<String, Vec<String>>>,
    client_frames: Vec<Value>,
}

const REJECT_CAP: usize = 12;

impl OfcTable {
    fn new(turns: Vec<Turn>) -> Arc<Mutex<Self>> {
        let mut table = OfcTable {
            inbox: VecDeque::new(),
            seq: 0,
            pending: turns.into(),
            current: None,
            board: empty_rows(),
            next_request: 0,
            accepted: Vec::new(),
            rejections: 0,
            finished_boards: Vec::new(),
            client_frames: Vec::new(),
        };
        table.push(ofc_server_hello(true));
        table.push(ofc_match_start(0));
        table.deal_next(None);
        Arc::new(Mutex::new(table))
    }

    fn push(&mut self, mut frame: Value) {
        self.seq += 1;
        frame["seq"] = json!(self.seq);
        self.inbox.push_back(frame.to_string());
    }

    fn finish_hand(&mut self) {
        self.finished_boards.push(self.board.clone());
        self.push(ofc_round_result(0));
    }

    fn deal_next(&mut self, previous_hand: Option<i64>) {
        let Some(turn) = self.pending.pop_front() else {
            if previous_hand.is_some() {
                self.finish_hand();
            }
            self.push(match_end(0));
            return;
        };
        if previous_hand != Some(turn.hand) {
            if previous_hand.is_some() {
                self.finish_hand();
            }
            self.board = empty_rows();
            self.push(round_start_for(&turn));
        }
        self.next_request += 1;
        let request_id = format!("req_{}", self.next_request);
        let frame = turn_request_for(&turn, &request_id, &self.board);
        self.push(frame);
        self.current = Some((request_id, turn));
    }

    fn handle(&mut self, msg: Value) {
        if msg["type"] != "turn_action" {
            self.client_frames.push(msg);
            return;
        }
        let (request_id, turn) = self
            .current
            .clone()
            .expect("a turn_action with no turn outstanding");
        assert_eq!(msg["request_id"], request_id.as_str());
        let params = msg["params"].clone();
        let verdict = if msg["action"] == "place" {
            check_placement(
                &params,
                &turn.cards,
                turn.place,
                turn.discard,
                &capacity_of(&self.board),
            )
        } else {
            Err(format!("{} is not place", msg["action"]))
        };
        match verdict {
            Ok(()) => {
                for p in params["placements"].as_array().unwrap() {
                    self.board
                        .get_mut(p["row"].as_str().unwrap())
                        .unwrap()
                        .push(p["card"].as_str().unwrap().to_string());
                }
                self.accepted.push((request_id, params));
                self.current = None;
                self.push(ofc_turn_result(0));
                self.deal_next(Some(turn.hand));
            }
            Err(why) => {
                self.rejections += 1;
                assert!(self.rejections <= REJECT_CAP, "reject/retry loop: {why}");
                let frame = json!({
                    "type": "action_rejected",
                    "match_id": MATCH_ID,
                    "request_id": request_id,
                    "reason": "invalid_action",
                    "message": why,
                    "valid_actions": ["place"],
                    "row_capacity": capacity_of(&self.board),
                });
                self.push(frame);
            }
        }
    }
}

struct TableReader(Arc<Mutex<OfcTable>>);

#[async_trait]
impl MessageReader for TableReader {
    async fn next(&mut self) -> Result<Option<String>, Error> {
        Ok(self.0.lock().unwrap().inbox.pop_front())
    }
}

struct TableWriter(Arc<Mutex<OfcTable>>);

#[async_trait]
impl MessageWriter for TableWriter {
    async fn send(&mut self, payload: String) -> Result<(), Error> {
        let msg: Value = serde_json::from_str(&payload).unwrap();
        self.0.lock().unwrap().handle(msg);
        Ok(())
    }
}

/// Play `full_session_turns()` at an [`OfcTable`]; returns the table.
async fn play_full_session<B: Bot>(bot: &mut B) -> Arc<Mutex<OfcTable>> {
    let table = OfcTable::new(full_session_turns());
    let mut reader = TableReader(Arc::clone(&table));
    let mut writer = TableWriter(Arc::clone(&table));
    let end = _run_session(&mut reader, &mut writer, bot, &ctx(Some(&["poker", "ofc"])))
        .await
        .expect("an OFC session must complete");
    assert_eq!(end.expect("match_end")["reason"], "complete");
    table
}

fn assert_boards_complete(table: &OfcTable) {
    assert_eq!(table.finished_boards.len(), 2);
    for board in &table.finished_boards {
        let sizes: Vec<(String, usize)> = board.iter().map(|(r, c)| (r.clone(), c.len())).collect();
        assert_eq!(
            sizes,
            vec![
                ("bottom".to_string(), 5),
                ("middle".to_string(), 5),
                ("top".to_string(), 3),
            ]
        );
    }
}

#[tokio::test]
async fn a_bot_that_declares_ofc_plays_a_full_session() {
    let mut bot = OfcBot::default();
    let table = play_full_session(&mut bot).await;
    let table = table.lock().unwrap();

    let hello = table
        .client_frames
        .iter()
        .find(|m| m["type"] == "hello")
        .unwrap();
    assert_eq!(hello["supported_games"], json!(["poker", "ofc"]));

    // Every answer was legal the first time: the table never rejected one.
    assert_eq!(table.rejections, 0);
    let ids: Vec<&str> = table.accepted.iter().map(|(id, _)| id.as_str()).collect();
    assert_eq!(
        ids,
        vec!["req_1", "req_2", "req_3", "req_4", "req_5", "req_6"]
    );
    assert_boards_complete(&table);

    // The bot walked the whole hand, then a Fantasy Land hand.
    let seen: Vec<(i64, &str, i64, i64, bool)> = bot
        .seen
        .iter()
        .map(|s| {
            (
                s.hand_number,
                s.phase.as_str(),
                s.place,
                s.must_discard,
                s.in_fantasy_land,
            )
        })
        .collect();
    assert_eq!(
        seen,
        vec![
            (1, "deal1", 5, 0, false),
            (1, "street2", 2, 1, false),
            (1, "street3", 2, 1, false),
            (1, "street4", 2, 1, false),
            (1, "street5", 2, 1, false),
            (2, "deal1", 13, 1, true),
        ]
    );
    assert_eq!(bot.seen[5].phase_sequence, strings(&["deal1", "complete"]));
    // The button froze into the Fantasy Land hand; the seat never moved.
    assert!(bot
        .seen
        .iter()
        .all(|s| (s.your_seat, s.dealer_seat) == (1, 0)));
    // Rows carried forward: the last pineapple street saw 11 cards down.
    let placed: usize = bot.seen[4].your_rows.values().map(Vec::len).sum();
    assert_eq!(placed, 11);
}

#[tokio::test]
async fn a_panicking_bot_completes_a_full_session_on_the_default_placement() {
    let table = play_full_session(&mut Panicker).await;
    let table = table.lock().unwrap();
    assert_eq!(table.rejections, 0);
    assert_eq!(table.accepted.len(), 6);
    assert_boards_complete(&table);
}

#[tokio::test]
async fn a_rejected_placement_is_retried_with_the_default_on_every_turn() {
    // Every TopStacker answer is rejected (OFC §3.5: rejected, not clamped);
    // each retry is the default placement built from the rejected turn's
    // state, which the table accepts.
    let table = play_full_session(&mut TopStacker).await;
    let table = table.lock().unwrap();
    assert_eq!(table.rejections, 6);
    assert_eq!(table.accepted.len(), 6);
    assert_boards_complete(&table);
}
