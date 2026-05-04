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
use crate::client::{_run_session, MessageReader, MessageWriter, SessionContext};
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

fn server_hello() -> Value {
    json!({
        "type": "hello",
        "match_id": MATCH_ID,
        "seq": 1,
        "server_ts": "2026-04-13T14:30:05.123Z",
        "supported_versions": ["1.0"],
        "selected_version": "1.0",
        "game_type": "nlhe_6max",
    })
}

fn match_start() -> Value {
    json!({
        "type": "match_start",
        "match_id": MATCH_ID,
        "seq": 2,
        "game_config": {
            "small_blind": 5,
            "big_blind": 10,
            "starting_stack": 1000,
        },
    })
}

fn round_start() -> Value {
    json!({
        "type": "round_start",
        "match_id": MATCH_ID,
        "seq": 3,
        "round_id": "r_1",
        "round_number": 1,
        "state": { "hand_number": 1, "your_hole_cards": ["Ah", "Kd"] },
    })
}

fn turn_request_n(seq: u64, request_id: &str) -> Value {
    json!({
        "type": "turn_request",
        "match_id": MATCH_ID,
        "seq": seq,
        "request_id": request_id,
        "valid_actions": ["fold", "call", "raise"],
        "state": {
            "hand_number": 1,
            "phase": "preflop",
            "your_hole_cards": ["Ah", "Kd"],
            "to_call": 5,
            "min_raise": 20,
            "max_raise": 995,
        },
    })
}

fn turn_result_n(seq: u64) -> Value {
    json!({
        "type": "turn_result",
        "match_id": MATCH_ID,
        "seq": seq,
        "details": { "seat": 0, "action": "call", "amount": 5 },
    })
}

fn phase_change(seq: u64, phase: &str, board: &[&str]) -> Value {
    json!({
        "type": "phase_change",
        "match_id": MATCH_ID,
        "seq": seq,
        "state": { "phase": phase, "board": board },
    })
}

fn round_result_n(seq: u64) -> Value {
    json!({
        "type": "round_result",
        "match_id": MATCH_ID,
        "seq": seq,
        "result": { "hand_number": 1, "winner_seats": [0], "pot": 40 },
    })
}

fn match_end_n(seq: u64) -> Value {
    json!({
        "type": "match_end",
        "match_id": MATCH_ID,
        "seq": seq,
        "reason": "complete",
    })
}

/// Server-side rejection of a previously-sent `turn_action`. Drives
/// the SDK's safe-fallback retry path. The SDK should respond with a
/// `turn_action` echoing this same `request_id` and a safe action
/// (`check` or `fold`) within `remaining_ms`.
fn action_rejected(seq: u64, request_id: &str) -> Value {
    json!({
        "type": "action_rejected",
        "match_id": MATCH_ID,
        "seq": seq,
        "request_id": request_id,
        "reason": "invalid_action",
        "message": "action not in valid_actions",
        "remaining_ms": 4000,
        "valid_actions": ["check", "fold"],
    })
}

fn full_match_script() -> Vec<String> {
    [
        server_hello(),
        match_start(),
        round_start(),
        turn_request_n(4, "req_1"),
        turn_result_n(5),
        round_result_n(6),
        match_end_n(7),
    ]
    .into_iter()
    .map(|v| v.to_string())
    .collect()
}

/// Three turn_requests across preflop/flop/turn — exercises request_id
/// echo on every turn. The original full-match script only checks the
/// first action; a bug where the second-or-later action drops or
/// rewrites the `request_id` would slip through.
fn multi_turn_script() -> Vec<String> {
    [
        server_hello(),
        match_start(),
        round_start(),
        turn_request_n(4, "req_1"),
        turn_result_n(5),
        phase_change(6, "flop", &["2s", "7d", "Tc"]),
        turn_request_n(7, "req_2"),
        turn_result_n(8),
        phase_change(9, "turn", &["2s", "7d", "Tc", "Kh"]),
        turn_request_n(10, "req_3"),
        turn_result_n(11),
        round_result_n(12),
        match_end_n(13),
    ]
    .into_iter()
    .map(|v| v.to_string())
    .collect()
}

/// One turn_request followed by an action_rejected — exercises the
/// SDK's safe-fallback retry path. The full-match script never
/// delivers an action_rejected, so the SDK's retry path goes untested
/// in conformance even though it's a routine production code path.
fn action_rejected_script() -> Vec<String> {
    [
        server_hello(),
        match_start(),
        round_start(),
        turn_request_n(4, "req_1"),
        action_rejected(5, "req_1"),
        turn_result_n(6),
        round_result_n(7),
        match_end_n(8),
    ]
    .into_iter()
    .map(|v| v.to_string())
    .collect()
}

/// One turn_request followed by THREE consecutive action_rejected
/// messages. Catches a class of failure where a buggy SDK might enter
/// an infinite response loop or hang waiting for a non-rejection
/// message that never arrives. The SDK should be purely reactive: one
/// safe-fallback `turn_action` per rejection, then exit cleanly on
/// `match_end`.
fn retry_storm_script() -> Vec<String> {
    [
        server_hello(),
        match_start(),
        round_start(),
        turn_request_n(4, "req_1"),
        action_rejected(5, "req_1"),
        action_rejected(6, "req_1"),
        action_rejected(7, "req_1"),
        turn_result_n(8),
        round_result_n(9),
        match_end_n(10),
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
///
/// `expected_request_id` is the request_id the server sent for the
/// turn this action is responding to. The SDK MUST echo it back so
/// the server can correlate, deduplicate, and route action_rejected
/// retries.
fn classify_turn_action(payload: &str, expected_request_id: &str) -> ClassifyResult {
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
    if msg.get("request_id").and_then(|v| v.as_str()) != Some(expected_request_id) {
        return ClassifyResult {
            ok: false,
            message: format!(
                "turn_action request_id {:?} did not echo the server's {expected_request_id:?} — \
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

/// Filter the captured-send buffer down to parsed `turn_action`
/// payloads. The string form is preserved alongside the parsed value
/// so callers can re-pass the original string to
/// [`classify_turn_action`] without re-serializing.
fn extract_turn_actions(sent: &[String]) -> Vec<(String, Value)> {
    sent.iter()
        .filter_map(|payload| {
            let parsed: Value = serde_json::from_str(payload).ok()?;
            if parsed.get("type").and_then(|t| t.as_str()) == Some("turn_action") {
                Some((payload.clone(), parsed))
            } else {
                None
            }
        })
        .collect()
}

/// Outcome of `drive_session` — either the captured writer if the
/// session completed cleanly, or a `Severity::Fail` diagnostic if the
/// timeout fired or the inner future returned an error.
enum DriveOutcome {
    Completed(Vec<String>),
    Failed { fail_message: String },
}

async fn drive_session<B: Bot>(
    bot: &mut B,
    script: Vec<String>,
    timeout: std::time::Duration,
) -> DriveOutcome {
    let mut reader = ScriptedReader {
        messages: script,
        index: 0,
    };
    let mut writer = CapturingWriter::default();
    let context = ctx();

    let session_future = _run_session(&mut reader, &mut writer, bot, &context);
    let result = tokio::time::timeout(timeout, session_future).await;

    match result {
        Err(_) => DriveOutcome::Failed {
            fail_message: format!(
                "did not complete within {timeout:?} — either decide() is too slow or \
                 the bot is hung waiting on something"
            ),
        },
        Ok(Err(e)) => DriveOutcome::Failed {
            fail_message: format!("session returned {e:?}"),
        },
        Ok(Ok(())) => {
            let sent = writer
                .sent
                .lock()
                .expect("CapturingWriter mutex poisoned")
                .clone();
            DriveOutcome::Completed(sent)
        }
    }
}

async fn run_full_match_scenario<B: Bot>(
    bot: &mut B,
    timeout: std::time::Duration,
) -> ConformanceCheck {
    let name = "connectivity_full_match".to_string();
    let sent = match drive_session(bot, full_match_script(), timeout).await {
        DriveOutcome::Failed { fail_message } => {
            return ConformanceCheck {
                severity: Severity::Fail,
                name,
                message: format!("full-match scenario {fail_message}"),
            }
        }
        DriveOutcome::Completed(sent) => sent,
    };

    if sent.is_empty() {
        return ConformanceCheck {
            severity: Severity::Fail,
            name,
            message: "bot did not send any messages during the canned exchange — at minimum \
                     the client should have sent authenticate / hello / turn_action"
                .to_string(),
        };
    }

    let turn_actions = extract_turn_actions(&sent);
    if turn_actions.is_empty() {
        return ConformanceCheck {
            severity: Severity::Fail,
            name,
            message: "bot completed the exchange but never sent a turn_action — decide() may \
                     have returned an unexpected value or the SDK's runner hit a fallback path"
                .to_string(),
        };
    }

    let (raw, _) = &turn_actions[0];
    let verdict = classify_turn_action(raw, "req_1");
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

/// Drive three turn_requests and verify request_id is echoed correctly
/// on each. The full-match scenario only checks the first action; a
/// bug where the second-or-later action drops or rewrites the
/// `request_id` would slip through.
async fn run_multi_turn_scenario<B: Bot>(
    bot: &mut B,
    timeout: std::time::Duration,
) -> ConformanceCheck {
    let name = "multi_turn_request_id_echo".to_string();
    let sent = match drive_session(bot, multi_turn_script(), timeout).await {
        DriveOutcome::Failed { fail_message } => {
            return ConformanceCheck {
                severity: Severity::Fail,
                name,
                message: format!("multi-turn scenario {fail_message}"),
            }
        }
        DriveOutcome::Completed(sent) => sent,
    };

    let turn_actions = extract_turn_actions(&sent);
    let expected_ids = ["req_1", "req_2", "req_3"];

    if turn_actions.len() < expected_ids.len() {
        return ConformanceCheck {
            severity: Severity::Fail,
            name,
            message: format!(
                "expected {} turn_actions across preflop/flop/turn, saw only {} — \
                 bot stopped responding partway through the hand",
                expected_ids.len(),
                turn_actions.len(),
            ),
        };
    }

    for (i, expected_id) in expected_ids.iter().enumerate() {
        let (raw, _) = &turn_actions[i];
        let verdict = classify_turn_action(raw, expected_id);
        if !verdict.ok {
            return ConformanceCheck {
                severity: Severity::Fail,
                name,
                message: format!("turn {} of 3 failed: {}", i + 1, verdict.message),
            };
        }
    }

    ConformanceCheck {
        severity: Severity::Pass,
        name,
        message: format!(
            "all {} turn_actions echoed request_id correctly across preflop/flop/turn",
            expected_ids.len()
        ),
    }
}

/// Drive a turn_request followed by an action_rejected and verify the
/// SDK retries safely. On rejection the SDK should send a second
/// `turn_action` echoing the same `request_id` and using a safe
/// action (`check` or `fold`).
async fn run_action_rejected_scenario<B: Bot>(
    bot: &mut B,
    timeout: std::time::Duration,
) -> ConformanceCheck {
    let name = "action_rejected_recovery".to_string();
    let sent = match drive_session(bot, action_rejected_script(), timeout).await {
        DriveOutcome::Failed { fail_message } => {
            return ConformanceCheck {
                severity: Severity::Fail,
                name,
                message: format!("action_rejected scenario {fail_message}"),
            }
        }
        DriveOutcome::Completed(sent) => sent,
    };

    let turn_actions = extract_turn_actions(&sent);
    if turn_actions.len() < 2 {
        return ConformanceCheck {
            severity: Severity::Fail,
            name,
            message: format!(
                "expected 2 turn_actions (initial + safe-fallback retry), saw {}; \
                 the SDK did not respond to the action_rejected message",
                turn_actions.len()
            ),
        };
    }

    let (_, retry) = &turn_actions[1];
    let retry_request_id = retry.get("request_id").and_then(|v| v.as_str()).unwrap_or("");
    if retry_request_id != "req_1" {
        return ConformanceCheck {
            severity: Severity::Fail,
            name,
            message: format!(
                "safe-fallback retry used request_id {retry_request_id:?} instead of \
                 the original \"req_1\" — server-side correlation will fail"
            ),
        };
    }

    let retry_action = retry.get("action").and_then(|v| v.as_str()).or_else(|| {
        retry
            .get("params")
            .and_then(|p| p.get("action"))
            .and_then(|v| v.as_str())
    });
    let Some(action) = retry_action else {
        return ConformanceCheck {
            severity: Severity::Fail,
            name,
            message: "safe-fallback retry was missing the `action` field".to_string(),
        };
    };
    if action != "check" && action != "fold" {
        return ConformanceCheck {
            severity: Severity::Fail,
            name,
            message: format!(
                "safe-fallback retry sent action {action:?}; expected \"check\" or \"fold\" \
                 (the only universally-safe actions when valid_actions is unknown)"
            ),
        };
    }

    ConformanceCheck {
        severity: Severity::Pass,
        name,
        message: format!(
            "action_rejected handled cleanly: original action sent, retry sent {action:?} \
             with original request_id"
        ),
    }
}

/// Drive a turn_request followed by THREE action_rejected messages
/// back-to-back. Catches a class of failure where a buggy SDK might
/// hang after the first rejection or enter an infinite send loop. The
/// SDK should respond reactively (one safe-fallback per rejection)
/// and exit cleanly when match_end arrives.
async fn run_retry_storm_scenario<B: Bot>(
    bot: &mut B,
    timeout: std::time::Duration,
) -> ConformanceCheck {
    let name = "retry_storm_bounded".to_string();
    let sent = match drive_session(bot, retry_storm_script(), timeout).await {
        DriveOutcome::Failed { fail_message } => {
            return ConformanceCheck {
                severity: Severity::Fail,
                name,
                message: format!("retry-storm scenario {fail_message}"),
            }
        }
        DriveOutcome::Completed(sent) => sent,
    };

    let turn_actions = extract_turn_actions(&sent);
    // Expected: 1 initial + 3 retries = 4 turn_actions total. The SDK
    // is reactive: each action_rejected provokes exactly one retry.
    let expected_count = 4;
    if turn_actions.len() != expected_count {
        let severity = if turn_actions.len() < expected_count {
            Severity::Fail
        } else {
            Severity::Warn
        };
        return ConformanceCheck {
            severity,
            name,
            message: format!(
                "expected {expected_count} turn_actions (1 initial + 3 retries) under \
                 retry storm, saw {} — the SDK's retry behavior may be unbounded or \
                 may have stopped responding",
                turn_actions.len()
            ),
        };
    }

    ConformanceCheck {
        severity: Severity::Pass,
        name,
        message: format!(
            "SDK responded to all 3 action_rejected messages with safe-fallback retries \
             ({expected_count} turn_actions total) and exited cleanly on match_end"
        ),
    }
}

// ---------------------------------------------------------------------------
// Public entry
// ---------------------------------------------------------------------------

/// The set of conformance scenario names registered with
/// [`run_conformance_checks`]. Listed in the order they're executed.
/// Useful for downstream tooling that wants to enumerate scenarios
/// without parsing CLI output.
pub const SCENARIO_NAMES: &[&str] = &[
    "connectivity_full_match",
    "multi_turn_request_id_echo",
    "action_rejected_recovery",
    "retry_storm_bounded",
];

/// Drive `bot` through every conformance scenario and return per-check
/// verdicts. The bot instance is consumed (passed by value) — matches
/// the production usage shape where `run_bot` also takes ownership.
///
/// Note on hung bots: the timeout uses `tokio::time::timeout` which
/// cancels at await points inside the session loop. A bot whose
/// `decide()` synchronously busy-loops (or calls a long-blocking
/// non-async function) starves the tokio runtime task and prevents
/// the timeout from firing on time. The Python SDK has a daemon-thread
/// hard watchdog for this; the Rust equivalent (running decide in
/// `tokio::task::spawn_blocking`) is more invasive and deferred to a
/// follow-up. Bots that block their task will hang the harness.
pub async fn run_conformance_checks<B: Bot>(
    mut bot: B,
    options: RunConformanceOptions,
) -> Vec<ConformanceCheck> {
    vec![
        run_full_match_scenario(&mut bot, options.timeout).await,
        run_multi_turn_scenario(&mut bot, options.timeout).await,
        run_action_rejected_scenario(&mut bot, options.timeout).await,
        run_retry_storm_scenario(&mut bot, options.timeout).await,
    ]
}
