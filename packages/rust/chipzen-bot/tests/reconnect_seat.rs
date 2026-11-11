//! Seat and button after a real reconnect (chipzen-ai/chipzen-sdk#199).
//!
//! The match server sends no `reconnected` frame. A reconnecting bot runs the
//! handshake again and the new session receives the next game frames, so it
//! never sees `match_start` (the seat) or the current hand's `round_start`
//! (the button). The SDK must then take the seat from `turn_request.seat` and
//! work out the button from the hand's blinds. Mirrors the Python SDK's
//! tests/test_reconnect_seat.py.

use async_trait::async_trait;
use chipzen_bot::{
    _run_session, Action, Bot, Error, GameState, MessageReader, MessageWriter, SessionContext,
};
use serde_json::{json, Value};
use std::sync::{Arc, Mutex};

struct ScriptedReader {
    messages: Vec<String>,
    index: usize,
}

#[async_trait]
impl MessageReader for ScriptedReader {
    async fn next(&mut self) -> Result<Option<String>, Error> {
        if self.index >= self.messages.len() {
            return Ok(None);
        }
        self.index += 1;
        Ok(Some(self.messages[self.index - 1].clone()))
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

#[derive(Default)]
struct SeatRecordingBot {
    seats: Vec<(i64, i64)>,
}

impl Bot for SeatRecordingBot {
    fn decide(&mut self, state: &GameState) -> Action {
        self.seats.push((state.your_seat, state.dealer_seat));
        if state.valid_actions.iter().any(|a| a == "check") {
            Action::Check
        } else {
            Action::Fold
        }
    }
}

fn hello() -> Value {
    json!({"type": "hello", "match_id": "m_test", "seq": 1, "capabilities": ["reconnect"]})
}

fn entry(seat: i64, action: &str, amount: i64) -> Value {
    json!({"seat": seat, "action": action, "amount": amount, "phase": "preflop"})
}

fn turn(seq: i64, seat: i64, hand: i64, stacks: Value, history: Vec<Value>) -> Value {
    json!({
        "type": "turn_request",
        "match_id": "m_test",
        "seq": seq,
        "seat": seat,
        "request_id": format!("req_{seq}"),
        "timeout_ms": 5000,
        "valid_actions": ["fold", "call", "raise"],
        "state": {
            "hand_number": hand,
            "phase": "preflop",
            "board": [],
            "your_hole_cards": ["Ah", "Kd"],
            "pot": 25,
            "your_stack": 995,
            "opponent_stacks": stacks,
            "to_call": 5,
            "min_raise": 20,
            "max_raise": 995,
            "action_history": history,
        },
    })
}

fn round_start(seq: i64, hand: i64, dealer: i64) -> Value {
    json!({
        "type": "round_start",
        "match_id": "m_test",
        "seq": seq,
        "round_id": format!("r_{hand}"),
        "round_number": hand,
        "state": {"hand_number": hand, "dealer_seat": dealer, "your_hole_cards": ["2c", "7d"]},
    })
}

async fn run(messages: Vec<Value>) -> (Vec<(i64, i64)>, Vec<Value>) {
    let mut reader = ScriptedReader {
        messages: messages.iter().map(Value::to_string).collect(),
        index: 0,
    };
    let mut writer = CapturingWriter::default();
    let mut bot = SeatRecordingBot::default();
    let context = SessionContext::new(
        "m_test".to_string(),
        Some("t".to_string()),
        None,
        "test".to_string(),
        "0".to_string(),
    );
    _run_session(&mut reader, &mut writer, &mut bot, &context)
        .await
        .unwrap();
    let sent = writer
        .sent
        .lock()
        .unwrap()
        .iter()
        .map(|s| serde_json::from_str(s).unwrap())
        .collect();
    (bot.seats, sent)
}

/// A 3-seat table: the bot is seat 2 and seat 1 has the button. The
/// reconnected session used to read seat 0 and button 0.
#[tokio::test]
async fn reconnected_session_reads_seat_and_button_without_match_start() {
    let history = vec![
        entry(2, "post_small_blind", 5),
        entry(0, "post_big_blind", 10),
        entry(1, "call", 10),
    ];
    let (seats, sent) = run(vec![
        hello(),
        // seq continues from the dropped connection
        turn(41, 2, 7, json!([990, 990]), history),
        round_start(45, 8, 2),
        turn(
            46,
            2,
            8,
            json!([995, 990]),
            vec![
                entry(0, "post_small_blind", 5),
                entry(1, "post_big_blind", 10),
            ],
        ),
    ])
    .await;
    assert_eq!(seats, vec![(2, 1), (2, 2)]);
    let answered: Vec<&Value> = sent
        .iter()
        .filter(|m| m["type"] == "turn_action")
        .map(|m| &m["request_id"])
        .collect();
    assert_eq!(answered, vec!["req_41", "req_46"]);
}

#[tokio::test]
async fn reconnected_heads_up_button_posts_the_small_blind() {
    let history = vec![
        entry(1, "post_small_blind", 5),
        entry(0, "post_big_blind", 10),
    ];
    let (seats, _) = run(vec![hello(), turn(9, 1, 3, json!([990]), history)]).await;
    assert_eq!(seats, vec![(1, 1)]);
}

/// POKER-GAME-STATE-PROTOCOL section 5.9's example: 6 seats, seats 1, 2 and 5
/// busted, button 0, so seat 3 posts the small blind.
#[tokio::test]
async fn reconnected_button_skips_busted_seats() {
    let history = vec![
        entry(3, "post_small_blind", 5),
        entry(4, "post_big_blind", 10),
    ];
    let stacks = json!([1500, 0, 0, 1200, 0]); // seats 0, 1, 2, 4, 5
    let (seats, _) = run(vec![hello(), turn(9, 3, 3, stacks, history)]).await;
    assert_eq!(seats, vec![(3, 0)]);
}

/// The small blind is the lowest live seat, so the button wraps to the last
/// live seat; seat 4 is all-in (stack 0) but has acted, so it is live.
#[tokio::test]
async fn reconnected_button_wraps_and_counts_all_in_seats() {
    let history = vec![
        entry(0, "post_small_blind", 5),
        entry(1, "post_big_blind", 10),
        entry(4, "raise", 300),
    ];
    let stacks = json!([990, 0, 0, 0]); // seats 1, 2, 3, 4
    let (seats, _) = run(vec![hello(), turn(9, 0, 3, stacks, history)]).await;
    assert_eq!(seats, vec![(0, 4)]);
}

/// Normal play: the roster seat and the `round_start` button are used as
/// before, whatever the blinds would suggest.
#[tokio::test]
async fn session_that_saw_match_start_and_round_start_is_unchanged() {
    let match_start = json!({
        "type": "match_start",
        "match_id": "m_test",
        "seq": 2,
        "seats": [{"seat": 0, "is_self": false}, {"seat": 1, "is_self": true}],
        "game_config": {},
    });
    let history = vec![
        entry(0, "post_small_blind", 5),
        entry(1, "post_big_blind", 10),
    ];
    let (seats, _) = run(vec![
        hello(),
        match_start,
        round_start(3, 1, 1),
        turn(4, 1, 1, json!([995]), history),
    ])
    .await;
    assert_eq!(seats, vec![(1, 1)]);
}

/// No Layer 1 seat and no blinds: today's fallback (seat 0, button 0).
#[tokio::test]
async fn no_seat_and_no_blinds_keeps_the_old_fallback() {
    let mut message = turn(9, 0, 3, json!([990]), vec![]);
    message.as_object_mut().unwrap().remove("seat");
    let (seats, _) = run(vec![hello(), message]).await;
    assert_eq!(seats, vec![(0, 0)]);
}
