use async_trait::async_trait;
use chipzen_bot::{
    Action, Bot, Error, GameState, MessageReader, MessageWriter, SessionContext, _extract_match_id,
    _run_session, _safe_fallback_action, SUPPORTED_PROTOCOL_VERSIONS,
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

#[test]
fn safe_fallback_prefers_check_then_fold() {
    let with_check = vec!["fold".to_string(), "check".to_string()];
    assert!(matches!(_safe_fallback_action(&with_check), Action::Check));

    let no_check = vec!["fold".to_string()];
    assert!(matches!(_safe_fallback_action(&no_check), Action::Fold));

    // Empty (shouldn't happen in practice) — still returns fold.
    assert!(matches!(_safe_fallback_action(&[]), Action::Fold));
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
    SessionContext {
        match_id: "m_test".to_string(),
        token: Some("test-token".to_string()),
        ticket: None,
        client_name: "chipzen-sdk-test".to_string(),
        client_version: "0.0.0".to_string(),
    }
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
