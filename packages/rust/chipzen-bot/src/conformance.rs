//! Protocol-conformance harness — drives a `Bot` through one canned
//! handshake + hand + match_end against an in-process mock socket and
//! reports per-scenario verdicts.
//!
//! Mirrors the Python (`chipzen.conformance`) and JavaScript
//! (`chipzen-bot` / `runConformanceChecks`) harnesses — same scenario
//! shape, same severity model, same canned exchange — so a clean run
//! in any of the three SDKs means the upload pipeline will accept the
//! bot on protocol grounds. It does NOT mean the bot is good.

use crate::bot::Bot;
use crate::client::{MessageReader, MessageWriter, SessionContext, _run_session};
use crate::error::Error;
use async_trait::async_trait;
use serde_json::{json, Value};
use std::sync::{Arc, Mutex};

/// Severity of a single conformance verdict. Same shape as
/// `chipzen_sdk::Severity`; the CLI renders them uniformly.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Severity {
    Pass,
    Warn,
    Fail,
}

/// One conformance scenario's verdict.
#[derive(Debug, Clone)]
pub struct ConformanceCheck {
    pub severity: Severity,
    pub name: String,
    pub message: String,
}

/// Optional knobs for [`run_conformance_checks`].
#[derive(Debug, Clone)]
pub struct RunConformanceOptions {
    /// Per-scenario timeout. Default 10s — well above the platform's
    /// per-action 5-second budget but still bounded for CI use.
    pub timeout: std::time::Duration,
}

impl Default for RunConformanceOptions {
    fn default() -> Self {
        Self {
            timeout: std::time::Duration::from_secs(10),
        }
    }
}

const MATCH_ID: &str = "m_conformance_test";
const VALID_ACTION_KINDS: &[&str] = &["fold", "check", "call", "raise", "all_in"];

// ---------------------------------------------------------------------------
// Mock reader / writer
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
        self.sent
            .lock()
            .expect("CapturingWriter mutex poisoned")
            .push(payload);
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// Canned scripts
// ---------------------------------------------------------------------------

fn full_match_script() -> Vec<String> {
    let server_hello = json!({
        "type": "hello",
        "match_id": MATCH_ID,
        "seq": 1,
        "server_ts": "2026-04-13T14:30:05.123Z",
        "supported_versions": ["1.0"],
        "selected_version": "1.0",
        "game_type": "nlhe_6max",
    });
    let match_start = json!({
        "type": "match_start",
        "match_id": MATCH_ID,
        "seq": 2,
        "game_config": {
            "small_blind": 5,
            "big_blind": 10,
            "starting_stack": 1000,
        },
    });
    let round_start = json!({
        "type": "round_start",
        "match_id": MATCH_ID,
        "seq": 3,
        "round_id": "r_1",
        "round_number": 1,
        "state": { "hand_number": 1, "your_hole_cards": ["Ah", "Kd"] },
    });
    let turn_request = json!({
        "type": "turn_request",
        "match_id": MATCH_ID,
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
        "match_id": MATCH_ID,
        "seq": 5,
        "details": { "seat": 0, "action": "call", "amount": 5 },
    });
    let round_result = json!({
        "type": "round_result",
        "match_id": MATCH_ID,
        "seq": 6,
        "result": { "hand_number": 1, "winner_seats": [0], "pot": 40 },
    });
    let match_end = json!({
        "type": "match_end",
        "match_id": MATCH_ID,
        "seq": 7,
        "reason": "complete",
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

fn ctx() -> SessionContext {
    SessionContext {
        match_id: MATCH_ID.to_string(),
        token: Some("conformance".to_string()),
        ticket: None,
        client_name: "chipzen-sdk-conformance".to_string(),
        client_version: "0.0.0".to_string(),
    }
}

// ---------------------------------------------------------------------------
// Scenario evaluation
// ---------------------------------------------------------------------------

#[derive(Debug)]
struct ClassifyResult {
    ok: bool,
    message: String,
}

/// Validate a single payload the bot sent. ok=true with a non-fatal
/// note for messages that aren't `turn_action`; ok=false with a
/// diagnostic for anything malformed.
fn classify_turn_action(payload: &str) -> ClassifyResult {
    let msg: Value = match serde_json::from_str(payload) {
        Ok(v) => v,
        Err(e) => {
            return ClassifyResult {
                ok: false,
                message: format!("sent payload was not valid JSON: {e}"),
            }
        }
    };
    if msg.get("type").and_then(|v| v.as_str()) != Some("turn_action") {
        return ClassifyResult {
            ok: true,
            message: format!(
                "non-action message ({:?}) — ignored",
                msg.get("type").and_then(|v| v.as_str())
            ),
        };
    }
    if msg.get("request_id").and_then(|v| v.as_str()) != Some("req_1") {
        return ClassifyResult {
            ok: false,
            message: format!(
                "turn_action request_id {:?} did not echo the server's req_1 — \
                 the server uses request_id for correlation, idempotency, and \
                 action_rejected retries",
                msg.get("request_id")
            ),
        };
    }
    let action = msg.get("action").and_then(|v| v.as_str()).or_else(|| {
        msg.get("params")
            .and_then(|p| p.get("action"))
            .and_then(|v| v.as_str())
    });
    let Some(action) = action else {
        return ClassifyResult {
            ok: false,
            message: "turn_action missing `action` field".to_string(),
        };
    };
    if !VALID_ACTION_KINDS.contains(&action) {
        return ClassifyResult {
            ok: false,
            message: format!("turn_action action {action:?} is not in the legal set"),
        };
    }
    ClassifyResult {
        ok: true,
        message: format!("sent turn_action: action={action:?}"),
    }
}

async fn run_full_match_scenario<B: Bot>(
    mut bot: B,
    timeout: std::time::Duration,
) -> ConformanceCheck {
    let name = "connectivity_full_match".to_string();
    let mut reader = ScriptedReader {
        messages: full_match_script(),
        index: 0,
    };
    let mut writer = CapturingWriter::default();
    let context = ctx();

    let session_future = _run_session(&mut reader, &mut writer, &mut bot, &context);
    let result = tokio::time::timeout(timeout, session_future).await;

    match result {
        Err(_) => {
            return ConformanceCheck {
                severity: Severity::Fail,
                name,
                message: format!(
                    "bot did not complete the canned full-match exchange within {timeout:?} — \
                     either decide() is too slow or the bot is hung waiting on something"
                ),
            };
        }
        Ok(Err(e)) => {
            return ConformanceCheck {
                severity: Severity::Fail,
                name,
                message: format!("bot raised {e:?} during the canned exchange"),
            };
        }
        Ok(Ok(())) => {}
    }

    let sent = writer
        .sent
        .lock()
        .expect("CapturingWriter mutex poisoned")
        .clone();
    if sent.is_empty() {
        return ConformanceCheck {
            severity: Severity::Fail,
            name,
            message: "bot did not send any messages during the canned exchange — at minimum \
                     the client should have sent authenticate / hello / turn_action"
                .to_string(),
        };
    }

    let turn_actions: Vec<&String> = sent
        .iter()
        .filter(|payload| {
            serde_json::from_str::<Value>(payload)
                .ok()
                .and_then(|v| v.get("type").and_then(|t| t.as_str()).map(String::from))
                .as_deref()
                == Some("turn_action")
        })
        .collect();

    if turn_actions.is_empty() {
        return ConformanceCheck {
            severity: Severity::Fail,
            name,
            message: "bot completed the exchange but never sent a turn_action — decide() may \
                     have returned an unexpected value or the SDK's runner hit a fallback path"
                .to_string(),
        };
    }

    let verdict = classify_turn_action(turn_actions[0]);
    if !verdict.ok {
        return ConformanceCheck {
            severity: Severity::Fail,
            name,
            message: verdict.message,
        };
    }
    ConformanceCheck {
        severity: Severity::Pass,
        name,
        message: format!(
            "completed handshake + 1 hand + match_end; {}",
            verdict.message
        ),
    }
}

// ---------------------------------------------------------------------------
// Public entry
// ---------------------------------------------------------------------------

/// Drive `bot` through every conformance scenario and return per-check
/// verdicts. The same bot instance is consumed (passed by value) — match
/// the production usage shape where `run_bot` also takes ownership.
///
/// Currently runs one scenario (`connectivity_full_match`). More
/// scenarios may be added without breaking the return shape.
pub async fn run_conformance_checks<B: Bot>(
    bot: B,
    options: RunConformanceOptions,
) -> Vec<ConformanceCheck> {
    vec![run_full_match_scenario(bot, options.timeout).await]
}
