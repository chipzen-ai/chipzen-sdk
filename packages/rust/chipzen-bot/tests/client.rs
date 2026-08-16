use async_trait::async_trait;
use chipzen_bot::{
    _extract_match_id, _run_session, _safe_fallback_action, Action, Bot, Error, FallbackAction,
    GameState, MessageReader, MessageWriter, SessionContext, SUPPORTED_PROTOCOL_VERSIONS,
};
use serde_json::{json, Value};
use std::sync::{Arc, Mutex};

#[test]
fn extract_match_id_handles_uuid_and_namespaced_ids() {
    assert_eq!(
        _extract_match_id("ws://localhost:8001/ws/match/abc-123-def/bot"),
        "abc-123-def"
    );
    assert_eq!(
        _extract_match_id("wss://api.chipzen.ai/ws/match/m_12_xyz/bot?token=foo"),
        "m_12_xyz"
    );
    assert_eq!(_extract_match_id("ws://localhost/no/match/here"), "");
    assert_eq!(_extract_match_id(""), "");
}

fn strings(actions: &[&str]) -> Vec<String> {
    actions.iter().map(|s| (*s).to_string()).collect()
}

#[test]
fn safe_fallback_prefers_check_then_fold() {
    let with_check = strings(&["fold", "check"]);
    assert_eq!(
        _safe_fallback_action(&with_check).unwrap(),
        FallbackAction::Typed(Action::Check)
    );

    let no_check = strings(&["fold", "call"]);
    assert_eq!(
        _safe_fallback_action(&no_check).unwrap(),
        FallbackAction::Typed(Action::Fold)
    );
}

#[test]
fn safe_fallback_degrades_to_first_valid_action() {
    // A variant table offers an action set this SDK doesn't model.
    // Answering check/fold here is rejected, retried, rejected again —
    // the reject/retry loop of chipzen-ai/Chipzen#4244. Echo the first
    // valid action instead (matches the Python SDK).
    assert_eq!(
        _safe_fallback_action(&strings(&["draw"])).unwrap(),
        FallbackAction::Raw("draw".to_string())
    );
    assert_eq!(
        _safe_fallback_action(&strings(&["place", "pass"])).unwrap(),
        FallbackAction::Raw("place".to_string())
    );
    // Modelled kinds that need no params still come back typed.
    assert_eq!(
        _safe_fallback_action(&strings(&["call", "raise"])).unwrap(),
        FallbackAction::Typed(Action::Call)
    );
    assert_eq!(
        _safe_fallback_action(&strings(&["all_in"])).unwrap(),
        FallbackAction::Typed(Action::AllIn)
    );
}

#[test]
fn safe_fallback_raw_action_serializes_verbatim_with_empty_params() {
    let (action, params) = _safe_fallback_action(&strings(&["draw"]))
        .unwrap()
        .to_wire();
    assert_eq!(action, "draw");
    assert_eq!(params, json!({}));
}

#[test]
fn safe_fallback_errors_on_empty_valid_actions() {
    // Nothing legal was offered: an explicit error, never a panic and
    // never a guessed `fold` that the server will just reject again.
    let err = _safe_fallback_action(&[]).expect_err("empty valid_actions must be an error");
    assert!(
        matches!(err, Error::Protocol(_)),
        "expected Error::Protocol, got: {err:?}"
    );
    assert!(
        format!("{err}").contains("valid_actions"),
        "error should name the offending field: {err}"
    );
}

#[test]
fn supported_protocol_versions_lists_at_least_v1() {
    assert!(SUPPORTED_PROTOCOL_VERSIONS.contains(&"1.0"));
}

// ---------------------------------------------------------------------------
// Mock reader / writer for driving _run_session through a canned script
// ---------------------------------------------------------------------------

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
        let msg = self.messages[self.index].clone();
        self.index += 1;
        Ok(Some(msg))
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

struct CallBot;
impl Bot for CallBot {
    fn decide(&mut self, state: &GameState) -> Action {
        if state.valid_actions.iter().any(|a| a == "call") {
            Action::Call
        } else if state.valid_actions.iter().any(|a| a == "check") {
            Action::Check
        } else {
            Action::Fold
        }
    }
}

fn ctx() -> SessionContext {
    SessionContext::new(
        "m_test".to_string(),
        Some("test-token".to_string()),
        None,
        "chipzen-sdk-test".to_string(),
        "0.0.0".to_string(),
    )
}

fn full_match_script() -> Vec<String> {
    let server_hello = json!({
        "type": "hello",
        "match_id": "m_test",
        "seq": 1,
        "supported_versions": ["1.0"],
        "selected_version": "1.0",
        "game_type": "nlhe_6max",
    });
    let match_start = json!({
        "type": "match_start",
        "match_id": "m_test",
        "seq": 2,
        "game_config": {
            "small_blind": 5,
            "big_blind": 10,
            "starting_stack": 1000,
        },
    });
    let round_start = json!({
        "type": "round_start",
        "match_id": "m_test",
        "seq": 3,
        "round_id": "r_1",
        "state": { "hand_number": 1, "your_hole_cards": ["Ah", "Kd"] },
    });
    let turn_request = json!({
        "type": "turn_request",
        "match_id": "m_test",
        "seq": 4,
        "request_id": "req_1",
        "valid_actions": ["fold", "call", "raise"],
        "state": {
            "hand_number": 1,
            "phase": "preflop",
            "your_hole_cards": ["Ah", "Kd"],
            "to_call": 5,
            "min_raise": 20,
            "max_raise": 995,
        },
    });
    let turn_result = json!({
        "type": "turn_result",
        "match_id": "m_test",
        "seq": 5,
        "details": { "seat": 0, "action": "call", "amount": 5 },
    });
    let round_result = json!({
        "type": "round_result",
        "match_id": "m_test",
        "seq": 6,
        "result": { "hand_number": 1, "winner_seats": [0], "pot": 40 },
    });
    let match_end = json!({
        "type": "match_end",
        "match_id": "m_test",
        "seq": 7,
        "reason": "complete",
        "results": [{"seat": 0, "rank": 1}, {"seat": 1, "rank": 2}],
    });
    [
        server_hello,
        match_start,
        round_start,
        turn_request,
        turn_result,
        round_result,
        match_end,
    ]
    .into_iter()
    .map(|v| v.to_string())
    .collect()
}

#[tokio::test]
async fn run_session_completes_canned_full_match() {
    let mut reader = ScriptedReader {
        messages: full_match_script(),
        index: 0,
    };
    let mut writer = CapturingWriter::default();
    let mut bot = CallBot;
    let context = ctx();

    let result = _run_session(&mut reader, &mut writer, &mut bot, &context).await;
    assert!(result.is_ok(), "session loop returned error: {result:?}");

    let sent = writer.sent.lock().unwrap().clone();
    assert!(
        sent.len() >= 3,
        "expected ≥3 sent messages, got {}",
        sent.len()
    );

    // First message must be authenticate with the token.
    let auth: Value = serde_json::from_str(&sent[0]).unwrap();
    assert_eq!(auth["type"], "authenticate");
    assert_eq!(auth["token"], "test-token");
    assert_eq!(auth["match_id"], "m_test");

    // Second must be client hello with supported_versions.
    let hello: Value = serde_json::from_str(&sent[1]).unwrap();
    assert_eq!(hello["type"], "hello");
    assert_eq!(hello["supported_versions"], json!(["1.0"]));

    // Third must be the turn_action echoing req_1 with action=call.
    let turn_action: Value = serde_json::from_str(&sent[2]).unwrap();
    assert_eq!(turn_action["type"], "turn_action");
    assert_eq!(turn_action["request_id"], "req_1");
    assert_eq!(turn_action["action"], "call");
}

#[tokio::test]
async fn run_session_substitutes_safe_fallback_when_decide_picks_invalid_action() {
    // Bot returns Action::Raise which isn't in valid_actions for the
    // canned turn_request below — session loop should swap in the
    // safe fallback (check, since "check" is in valid_actions) so the
    // exchange completes.
    struct BadBot;
    impl Bot for BadBot {
        fn decide(&mut self, _state: &GameState) -> Action {
            Action::Raise(99999)
        }
    }

    let server_hello = json!({"type": "hello", "match_id": "m_test", "seq": 1});
    let turn_request = json!({
        "type": "turn_request",
        "seq": 2,
        "request_id": "req_42",
        "valid_actions": ["fold", "check"],
        "state": {"phase": "preflop", "valid_actions": ["fold", "check"]},
    });
    let match_end = json!({"type": "match_end", "seq": 3, "reason": "complete"});
    let mut reader = ScriptedReader {
        messages: vec![
            server_hello.to_string(),
            turn_request.to_string(),
            match_end.to_string(),
        ],
        index: 0,
    };
    let mut writer = CapturingWriter::default();
    let mut bot = BadBot;
    let context = ctx();

    _run_session(&mut reader, &mut writer, &mut bot, &context)
        .await
        .unwrap();

    let sent = writer.sent.lock().unwrap().clone();
    let turn_action: Value = serde_json::from_str(&sent[2]).unwrap();
    assert_eq!(turn_action["action"], "check");
    assert_eq!(turn_action["request_id"], "req_42");
}

#[tokio::test]
async fn run_session_replies_to_action_rejected_with_safe_fallback() {
    let server_hello = json!({"type": "hello", "match_id": "m_test", "seq": 1});
    let action_rejected = json!({
        "type": "action_rejected",
        "seq": 2,
        "request_id": "req_99",
        "valid_actions": ["fold"],
        "reason": "bad_amount",
    });
    let match_end = json!({"type": "match_end", "seq": 3, "reason": "complete"});
    let mut reader = ScriptedReader {
        messages: vec![
            server_hello.to_string(),
            action_rejected.to_string(),
            match_end.to_string(),
        ],
        index: 0,
    };
    let mut writer = CapturingWriter::default();
    let mut bot = CallBot;
    let context = ctx();

    _run_session(&mut reader, &mut writer, &mut bot, &context)
        .await
        .unwrap();

    let sent = writer.sent.lock().unwrap().clone();
    let retry: Value = serde_json::from_str(&sent[2]).unwrap();
    assert_eq!(retry["type"], "turn_action");
    assert_eq!(retry["request_id"], "req_99");
    // valid_actions only had "fold" so safe-fallback picks fold.
    assert_eq!(retry["action"], "fold");
}

// ---------------------------------------------------------------------------
// Reconnected re-attach (chipzen-ai/chipzen-sdk#121)
// ---------------------------------------------------------------------------

/// Records the hook/decide order plus every `GameState` decided, so the
/// test can assert the replayed pending turn used the re-learned seat.
#[derive(Default)]
struct ReconnectRecordingBot {
    events: Vec<String>,
    states: Vec<GameState>,
    reconnected_msg: Option<Value>,
}

impl Bot for ReconnectRecordingBot {
    fn decide(&mut self, state: &GameState) -> Action {
        self.events.push("decide".to_string());
        self.states.push(state.clone());
        if state.valid_actions.iter().any(|a| a == "check") {
            Action::Check
        } else {
            Action::Fold
        }
    }
    fn on_match_end(&mut self, _results: &Value) {
        self.events.push("match_end".to_string());
    }
    fn on_reconnected(&mut self, msg: &Value) {
        self.events.push("reconnected".to_string());
        self.reconnected_msg = Some(msg.clone());
    }
}

/// chipzen-ai/chipzen-sdk#121 (Rust analog of #119): a session that
/// (re)attaches to an in-flight match sees `reconnected` instead of
/// `match_start` — it must still learn `your_seat` from the seats array
/// and surface match context via the `on_reconnected` hook, and the
/// replayed pending turn must be built with the re-learned seat (not
/// the your_seat=0 default).
#[tokio::test]
async fn run_session_reconnected_relearns_seat_and_fires_hook() {
    let server_hello = json!({"type": "hello", "match_id": "m_test", "seq": 1});
    let reconnected = json!({
        "type": "reconnected",
        "match_id": "m_test",
        "seq": 2,
        "round_number": 3,
        "match_state": "in_progress",
        "seats": [
            {"seat": 0, "participant_id": "p1", "display_name": "Opp", "is_self": false},
            {"seat": 1, "participant_id": "p0", "display_name": "You", "is_self": true},
        ],
        "game_config": {"small_blind": 5, "big_blind": 10, "starting_stack": 1000},
        "state": {},
        "pending_request": {
            "type": "turn_request",
            "match_id": "m_test",
            "request_id": "req_resume",
            "valid_actions": ["fold", "check"],
            // NOTE: no `your_seat` here — the turn state does not carry
            // it; it only exists in the seats roster.
            "state": {"hand_number": 7, "phase": "preflop"},
        },
    });
    let match_end = json!({"type": "match_end", "seq": 3, "reason": "complete"});
    let mut reader = ScriptedReader {
        messages: vec![
            server_hello.to_string(),
            reconnected.to_string(),
            match_end.to_string(),
        ],
        index: 0,
    };
    let mut writer = CapturingWriter::default();
    let mut bot = ReconnectRecordingBot::default();
    let context = ctx();

    _run_session(&mut reader, &mut writer, &mut bot, &context)
        .await
        .unwrap();

    // The hook fired with the full reconnected message, BEFORE the
    // pending turn was decided — so a bot can (re)learn match identity
    // in time.
    assert_eq!(bot.events, vec!["reconnected", "decide", "match_end"]);
    let msg = bot.reconnected_msg.as_ref().expect("hook message");
    assert_eq!(msg["match_id"], "m_test");

    // your_seat was re-learned from reconnected.seats (is_self at seat 1).
    assert_eq!(bot.states.len(), 1);
    assert_eq!(bot.states[0].your_seat, 1);

    // The pending request was answered like a fresh turn_request.
    let sent = writer.sent.lock().unwrap().clone();
    let turn_action: Value = serde_json::from_str(&sent[2]).unwrap();
    assert_eq!(turn_action["type"], "turn_action");
    assert_eq!(turn_action["request_id"], "req_resume");
    assert_eq!(turn_action["action"], "check");
}

// ---------------------------------------------------------------------------
// Variant-table reject/retry loop (chipzen-ai/Chipzen#4244)
// ---------------------------------------------------------------------------

/// Shared state for a mock server that validates every `turn_action`
/// against the table's `valid_actions` — the way the platform does.
struct VariantTable {
    /// Messages queued for the SDK to read.
    inbox: std::collections::VecDeque<String>,
    /// The only actions this table accepts.
    valid_actions: Vec<String>,
    /// Actions the table accepted.
    accepted: Vec<String>,
    /// How many `action_rejected` messages the table has sent.
    rejections: usize,
    /// Reject the first `turn_action` unconditionally, so the
    /// `action_rejected` retry path is exercised too.
    reject_first: bool,
    next_seq: i64,
}

/// Hard stop so a looping SDK ends the test instead of hanging. The
/// platform's equivalent is the auto-substitute streak limit, which
/// forfeits the match.
const REJECT_CAP: usize = 6;

impl VariantTable {
    fn new(valid_actions: &[&str]) -> Arc<Mutex<Self>> {
        let valid_actions = strings(valid_actions);
        let turn_request = json!({
            "type": "turn_request",
            "seq": 2,
            "request_id": "req_v1",
            "valid_actions": valid_actions,
            "state": {"phase": "draw", "valid_actions": valid_actions},
        });
        Arc::new(Mutex::new(VariantTable {
            inbox: [
                json!({"type": "hello", "match_id": "m_test", "seq": 1}).to_string(),
                turn_request.to_string(),
            ]
            .into_iter()
            .collect(),
            valid_actions,
            accepted: Vec::new(),
            rejections: 0,
            reject_first: true,
            next_seq: 3,
        }))
    }

    fn seq(&mut self) -> i64 {
        self.next_seq += 1;
        self.next_seq
    }

    fn queue_match_end(&mut self, reason: &str) {
        let seq = self.seq();
        self.inbox
            .push_back(json!({"type": "match_end", "seq": seq, "reason": reason}).to_string());
    }

    /// Rule the table applies to an incoming `turn_action`.
    fn handle_turn_action(&mut self, action: &str, request_id: &str) {
        let legal = self.valid_actions.iter().any(|a| a == action);
        let reject = std::mem::take(&mut self.reject_first) || !legal;
        if !reject {
            self.accepted.push(action.to_string());
            self.queue_match_end("complete");
            return;
        }
        self.rejections += 1;
        if self.rejections > REJECT_CAP {
            // The SDK is stuck in a reject/retry loop — the streak limit
            // would have forfeited the match by now.
            self.queue_match_end("auto_substitute_forfeit");
            return;
        }
        let seq = self.seq();
        self.inbox.push_back(
            json!({
                "type": "action_rejected",
                "seq": seq,
                "request_id": request_id,
                "reason": "invalid_action",
                "message": "action not in valid_actions",
                "remaining_ms": 4000,
                "valid_actions": self.valid_actions,
            })
            .to_string(),
        );
    }
}

struct TableReader(Arc<Mutex<VariantTable>>);

#[async_trait]
impl MessageReader for TableReader {
    async fn next(&mut self) -> Result<Option<String>, Error> {
        Ok(self.0.lock().unwrap().inbox.pop_front())
    }
}

struct TableWriter(Arc<Mutex<VariantTable>>);

#[async_trait]
impl MessageWriter for TableWriter {
    async fn send(&mut self, payload: String) -> Result<(), Error> {
        let msg: Value = serde_json::from_str(&payload).unwrap();
        if msg["type"] != "turn_action" {
            return Ok(());
        }
        let action = msg["action"].as_str().unwrap_or("").to_string();
        let request_id = msg["request_id"].as_str().unwrap_or("").to_string();
        self.0
            .lock()
            .unwrap()
            .handle_turn_action(&action, &request_id);
        Ok(())
    }
}

/// A plain NLHE bot: it only knows check/fold/call/raise, so at a variant
/// table every action it picks is illegal.
struct NlheBot;
impl Bot for NlheBot {
    fn decide(&mut self, state: &GameState) -> Action {
        if state.valid_actions.iter().any(|a| a == "check") {
            Action::Check
        } else {
            Action::Fold
        }
    }
}

/// An NLHE bot mis-seated at a variant table (`valid_actions == ["draw"]`)
/// used to answer `fold`, get rejected, answer `fold` again, and spin until
/// the auto-substitute streak limit killed the match. The safe fallback now
/// echoes the first valid action, so the exchange terminates after a single
/// rejection (chipzen-ai/Chipzen#4244).
#[tokio::test]
async fn run_session_does_not_loop_at_a_variant_table() {
    let table = VariantTable::new(&["draw"]);
    let mut reader = TableReader(Arc::clone(&table));
    let mut writer = TableWriter(Arc::clone(&table));
    let mut bot = NlheBot;
    let context = ctx();

    let end = _run_session(&mut reader, &mut writer, &mut bot, &context)
        .await
        .expect("variant table session must complete");

    let state = table.lock().unwrap();
    assert_eq!(
        state.rejections, 1,
        "expected exactly one rejection (forced) then a legal retry; \
         {} rejections means the SDK is looping on an illegal action",
        state.rejections
    );
    assert_eq!(
        state.accepted,
        vec!["draw".to_string()],
        "the safe fallback should echo the first valid action"
    );
    assert_eq!(
        end.expect("match_end payload")["reason"],
        "complete",
        "match ended in an auto-substitute forfeit"
    );
}

/// The same table, but the SDK is told nothing is legal. It must surface an
/// explicit protocol error rather than guessing an action that gets rejected
/// forever.
#[tokio::test]
async fn run_session_errors_when_no_action_is_valid() {
    let table = VariantTable::new(&[]);
    let mut reader = TableReader(Arc::clone(&table));
    let mut writer = TableWriter(Arc::clone(&table));
    let mut bot = NlheBot;
    let context = ctx();

    let err = _run_session(&mut reader, &mut writer, &mut bot, &context)
        .await
        .expect_err("an empty valid_actions list has no safe fallback");
    assert!(
        matches!(err, Error::Protocol(_)),
        "expected Error::Protocol, got: {err:?}"
    );
}

#[tokio::test]
async fn run_session_replies_to_ping_with_pong() {
    let server_hello = json!({"type": "hello", "match_id": "m_test", "seq": 1});
    let ping = json!({"type": "ping", "seq": 2});
    let match_end = json!({"type": "match_end", "seq": 3, "reason": "complete"});
    let mut reader = ScriptedReader {
        messages: vec![
            server_hello.to_string(),
            ping.to_string(),
            match_end.to_string(),
        ],
        index: 0,
    };
    let mut writer = CapturingWriter::default();
    let mut bot = CallBot;
    let context = ctx();

    _run_session(&mut reader, &mut writer, &mut bot, &context)
        .await
        .unwrap();

    let sent = writer.sent.lock().unwrap().clone();
    let pong: Value = serde_json::from_str(&sent[2]).unwrap();
    assert_eq!(pong["type"], "pong");
    assert_eq!(pong["match_id"], "m_test");
}

#[tokio::test]
async fn run_session_skips_malformed_envelope_and_continues() {
    let server_hello = json!({"type": "hello", "match_id": "m_test", "seq": 1});
    let garbage = "{this is not valid json".to_string();
    let match_end = json!({"type": "match_end", "seq": 3, "reason": "complete"});
    let mut reader = ScriptedReader {
        messages: vec![server_hello.to_string(), garbage, match_end.to_string()],
        index: 0,
    };
    let mut writer = CapturingWriter::default();
    let mut bot = CallBot;
    let context = ctx();

    let result = _run_session(&mut reader, &mut writer, &mut bot, &context).await;
    assert!(
        result.is_ok(),
        "expected garbage to be skipped, not bubble: {result:?}"
    );
}

// ---------------------------------------------------------------------------
// Lifecycle-hook safety (chipzen-ai/chipzen-sdk#80)
// ---------------------------------------------------------------------------

/// A bot whose `on_round_result` panics — the #80 failure shape.
struct PanickingHookBot;
impl Bot for PanickingHookBot {
    fn decide(&mut self, state: &GameState) -> Action {
        if state.valid_actions.iter().any(|a| a == "check") {
            Action::Check
        } else {
            Action::Fold
        }
    }
    fn on_round_result(&mut self, _msg: &Value) {
        panic!("winner key missing");
    }
}

fn crashing_hook_script() -> Vec<String> {
    let server_hello = json!({"type": "hello", "match_id": "m_test", "seq": 1});
    // Hook panics here.
    let round_result = json!({
        "type": "round_result",
        "seq": 2,
        "result": { "hand_number": 1, "winner_seats": [0], "pot": 40 },
    });
    // The bot must still answer the NEXT turn_request after the panic.
    let turn_request = json!({
        "type": "turn_request",
        "seq": 3,
        "request_id": "req_after_crash",
        "valid_actions": ["fold", "check"],
        "state": {"phase": "preflop", "valid_actions": ["fold", "check"]},
    });
    let match_end = json!({"type": "match_end", "seq": 4, "reason": "complete"});
    [server_hello, round_result, turn_request, match_end]
        .into_iter()
        .map(|v| v.to_string())
        .collect()
}

#[tokio::test]
async fn run_session_survives_panicking_lifecycle_hook() {
    let mut reader = ScriptedReader {
        messages: crashing_hook_script(),
        index: 0,
    };
    let mut writer = CapturingWriter::default();
    let mut bot = PanickingHookBot;
    let context = ctx(); // safe_mode = true

    let result = _run_session(&mut reader, &mut writer, &mut bot, &context).await;
    let end = result.expect("a panicking hook must not tear down the session");
    assert!(end.is_some(), "expected a clean match_end payload");

    // The next decide() after the panicking hook was still answered.
    let sent = writer.sent.lock().unwrap().clone();
    let turn_action: Value = serde_json::from_str(&sent[2]).unwrap();
    assert_eq!(turn_action["type"], "turn_action");
    assert_eq!(turn_action["request_id"], "req_after_crash");
    assert_eq!(turn_action["action"], "check");
}

#[tokio::test]
async fn run_session_propagates_hook_panic_when_safe_mode_off() {
    let mut reader = ScriptedReader {
        messages: crashing_hook_script(),
        index: 0,
    };
    let mut writer = CapturingWriter::default();
    let mut bot = PanickingHookBot;
    let mut context = ctx();
    context.safe_mode = false;

    let err = _run_session(&mut reader, &mut writer, &mut bot, &context)
        .await
        .expect_err("safe_mode=false must surface the hook panic");
    assert!(
        matches!(err, Error::BotDecision(_)),
        "expected Error::BotDecision, got: {err:?}"
    );
    let s = format!("{err}");
    assert!(s.contains("winner key missing"), "unexpected error: {s}");
}

#[tokio::test]
async fn run_session_errors_when_server_skips_hello() {
    let weird_first_message = json!({"type": "match_start", "seq": 1});
    let mut reader = ScriptedReader {
        messages: vec![weird_first_message.to_string()],
        index: 0,
    };
    let mut writer = CapturingWriter::default();
    let mut bot = CallBot;
    let context = ctx();

    let err = _run_session(&mut reader, &mut writer, &mut bot, &context)
        .await
        .expect_err("expected protocol error when server skips hello");
    let s = format!("{err}");
    assert!(s.contains("expected server hello"), "unexpected error: {s}");
}
