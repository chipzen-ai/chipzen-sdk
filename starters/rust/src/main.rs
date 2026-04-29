//! Chipzen starter bot — tight-aggressive preflop, check-call postflop.
//!
//! Implements the Chipzen two-layer protocol:
//!   Layer 1 (Transport): docs/protocol/TRANSPORT-PROTOCOL.md
//!   Layer 2 (Poker):     docs/protocol/POKER-GAME-STATE-PROTOCOL.md
//!
//! Usage:  cargo run -- ws://localhost:8001/ws/match/{match_id}/bot
//! Env:    CHIPZEN_WS_URL    — WebSocket URL (alternative to CLI arg)
//!         CHIPZEN_TOKEN     — Bot API token (for /bot endpoints)
//!         CHIPZEN_TICKET    — Single-use ticket (for competitive endpoints)
//!         CHIPZEN_MATCH_ID  — Match UUID (auto-extracted from URL if omitted)

use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use std::collections::HashSet;
use tokio_tungstenite::{connect_async, tungstenite::Message};

const PROTOCOL_VERSIONS: &[&str] = &["1.0"];
const CLIENT_NAME: &str = "chipzen-starter-rust";
const CLIENT_VERSION: &str = "0.2.0";

fn log(msg: &str) {
    eprintln!("[bot] {msg}");
}

/// Strong preflop hands: pairs 77+, broadways, suited aces.
fn strong_hands() -> HashSet<&'static str> {
    [
        "AA", "KK", "QQ", "JJ", "TT", "99", "88", "77", "AKs", "AKo", "AQs", "AQo", "AJs", "ATs",
        "KQs", "KQo", "KJs", "QJs", "JTs",
    ]
    .into_iter()
    .collect()
}

/// Convert ["Ah","Kd"] to "AKo" shorthand (rank-ordered, 's'/'o' for suit).
fn hand_key(cards: &[String]) -> String {
    if cards.len() != 2 {
        return String::new();
    }
    let order = "23456789TJQKA";
    let (mut r0, mut s0) = (
        &cards[0][..cards[0].len() - 1],
        &cards[0][cards[0].len() - 1..],
    );
    let (mut r1, mut s1) = (
        &cards[1][..cards[1].len() - 1],
        &cards[1][cards[1].len() - 1..],
    );
    if order.find(r0).unwrap_or(0) < order.find(r1).unwrap_or(0) {
        std::mem::swap(&mut r0, &mut r1);
        std::mem::swap(&mut s0, &mut s1);
    }
    if r0 == r1 {
        format!("{r0}{r1}")
    } else {
        format!("{r0}{r1}{}", if s0 == s1 { "s" } else { "o" })
    }
}

/// A decided action, ready to be serialized into `turn_action.action`/`params`.
struct Decision {
    action: &'static str,
    amount: Option<i64>,
}

impl Decision {
    fn to_params(&self) -> Value {
        match self.amount {
            Some(amt) => json!({ "amount": amt }),
            None => json!({}),
        }
    }
}

fn has(valid: &[String], a: &str) -> bool {
    valid.iter().any(|v| v == a)
}

/// Pick an action from a Layer 2 turn_request state.
fn decide(state: &Value, valid: &[String]) -> Decision {
    let phase = state
        .get("phase")
        .and_then(|v| v.as_str())
        .unwrap_or("preflop");
    let to_call = state.get("to_call").and_then(|v| v.as_i64()).unwrap_or(0);
    let pot = state.get("pot").and_then(|v| v.as_i64()).unwrap_or(0);
    let min_raise = state.get("min_raise").and_then(|v| v.as_i64()).unwrap_or(0);
    let max_raise = state.get("max_raise").and_then(|v| v.as_i64()).unwrap_or(0);

    let hole: Vec<String> = state
        .get("your_hole_cards")
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|x| x.as_str().map(String::from))
                .collect()
        })
        .unwrap_or_default();
    let key = hand_key(&hole);
    let strong = strong_hands();

    if phase == "preflop" {
        if strong.contains(key.as_str()) && has(valid, "raise") && min_raise > 0 {
            let amount = if max_raise > 0 {
                min_raise.max(1).min(max_raise)
            } else {
                min_raise
            };
            log(&format!("Preflop raise with {key} to {amount}"));
            return Decision {
                action: "raise",
                amount: Some(amount),
            };
        }
        if to_call > 0 && has(valid, "call") {
            log(&format!("Preflop call ({key})"));
            return Decision {
                action: "call",
                amount: None,
            };
        }
        if has(valid, "check") {
            return Decision {
                action: "check",
                amount: None,
            };
        }
        return Decision {
            action: "fold",
            amount: None,
        };
    }

    // Postflop: check free, call small, fold large
    if has(valid, "check") {
        return Decision {
            action: "check",
            amount: None,
        };
    }
    if has(valid, "call") && to_call <= pot / 2 {
        log(&format!("Postflop call {to_call} into {pot}"));
        return Decision {
            action: "call",
            amount: None,
        };
    }
    if has(valid, "fold") {
        return Decision {
            action: "fold",
            amount: None,
        };
    }
    Decision {
        action: "check",
        amount: None,
    }
}

/// Extract match UUID from a `/ws/.../match/{match_id}/...` URL.
fn extract_match_id(url: &str) -> String {
    let trimmed = url.trim_end_matches('/');
    let parts: Vec<&str> = trimmed.split('/').collect();
    for (i, p) in parts.iter().enumerate() {
        if *p == "match" && i + 1 < parts.len() {
            return parts[i + 1].to_string();
        }
    }
    "unknown".to_string()
}

type BoxError = Box<dyn std::error::Error + Send + Sync>;

#[tokio::main]
async fn main() -> Result<(), BoxError> {
    let url = std::env::args()
        .nth(1)
        .or_else(|| std::env::var("CHIPZEN_WS_URL").ok());
    let url = match url {
        Some(u) => u,
        None => {
            eprintln!("Usage: chipzen-starter-bot <ws_url>");
            eprintln!("Env:   CHIPZEN_WS_URL, CHIPZEN_TOKEN, CHIPZEN_TICKET");
            std::process::exit(1);
        }
    };

    let token = std::env::var("CHIPZEN_TOKEN").ok();
    let ticket = std::env::var("CHIPZEN_TICKET").ok();
    let match_id = std::env::var("CHIPZEN_MATCH_ID").unwrap_or_else(|_| extract_match_id(&url));

    log(&format!("Connecting to {url}"));
    let (ws_stream, _) = connect_async(&url).await?;
    let (mut write, mut read) = ws_stream.split();
    log("Connected!");

    // --- Layer 1 handshake --------------------------------------------------
    // 1. Send authenticate (must be the first bot message).
    let mut auth = json!({ "type": "authenticate", "match_id": match_id });
    if let Some(t) = token.as_deref().filter(|s| !s.is_empty()) {
        auth["token"] = Value::String(t.to_string());
    } else if let Some(t) = ticket.as_deref().filter(|s| !s.is_empty()) {
        auth["ticket"] = Value::String(t.to_string());
    } else {
        // Sidecar / localhost dev may accept empty token.
        auth["token"] = Value::String(String::new());
    }
    write.send(Message::Text(auth.to_string().into())).await?;

    // 2. Receive server hello.
    let server_hello: Value = loop {
        match read.next().await {
            Some(Ok(Message::Text(t))) => break serde_json::from_str(&t)?,
            Some(Ok(Message::Ping(p))) => {
                write.send(Message::Pong(p)).await?;
            }
            Some(Ok(_)) => continue,
            Some(Err(e)) => return Err(Box::new(e) as BoxError),
            None => return Ok(()),
        }
    };
    if server_hello.get("type").and_then(|v| v.as_str()) != Some("hello") {
        log(&format!(
            "Expected 'hello' from server, got {:?}",
            server_hello.get("type")
        ));
        return Ok(());
    }
    log(&format!(
        "Server hello: version={} game_type={}",
        server_hello
            .get("selected_version")
            .and_then(|v| v.as_str())
            .unwrap_or("?"),
        server_hello
            .get("game_type")
            .and_then(|v| v.as_str())
            .unwrap_or("?"),
    ));

    // 3. Send client hello.
    let client_hello = json!({
        "type": "hello",
        "match_id": match_id,
        "supported_versions": PROTOCOL_VERSIONS,
        "client_name": CLIENT_NAME,
        "client_version": CLIENT_VERSION,
    });
    write
        .send(Message::Text(client_hello.to_string().into()))
        .await?;

    // --- Main message loop -------------------------------------------------
    while let Some(frame) = read.next().await {
        let text = match frame? {
            Message::Text(t) => t,
            Message::Ping(p) => {
                write.send(Message::Pong(p)).await?;
                continue;
            }
            Message::Close(_) => break,
            _ => continue,
        };
        let msg: Value = match serde_json::from_str(&text) {
            Ok(v) => v,
            Err(_) => continue, // malformed, skip
        };
        let mtype = msg.get("type").and_then(|v| v.as_str()).unwrap_or("");

        match mtype {
            "ping" => {
                // Heartbeat: must respond within 5000ms.
                let pong = json!({ "type": "pong", "match_id": match_id });
                write.send(Message::Text(pong.to_string().into())).await?;
            }
            "match_start" => {
                let cfg = msg.get("game_config").cloned().unwrap_or(json!({}));
                log(&format!(
                    "Match start: blinds {}/{} stacks {}",
                    cfg.get("small_blind").and_then(|v| v.as_i64()).unwrap_or(0),
                    cfg.get("big_blind").and_then(|v| v.as_i64()).unwrap_or(0),
                    cfg.get("starting_stack")
                        .and_then(|v| v.as_i64())
                        .unwrap_or(0),
                ));
            }
            "round_start" => {
                let state = msg.get("state").cloned().unwrap_or(json!({}));
                log(&format!(
                    "Hand {} dealt: {}",
                    state
                        .get("hand_number")
                        .and_then(|v| v.as_i64())
                        .unwrap_or(0),
                    state.get("your_hole_cards").cloned().unwrap_or(json!([])),
                ));
            }
            "turn_request" => {
                let state = msg.get("state").cloned().unwrap_or(json!({}));
                let valid: Vec<String> = msg
                    .get("valid_actions")
                    .and_then(|v| v.as_array())
                    .map(|arr| {
                        arr.iter()
                            .filter_map(|x| x.as_str().map(String::from))
                            .collect()
                    })
                    .unwrap_or_default();
                let request_id = msg
                    .get("request_id")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let d = decide(&state, &valid);
                log(&format!(
                    "Turn h{} {}: {} {}",
                    state
                        .get("hand_number")
                        .and_then(|v| v.as_i64())
                        .unwrap_or(0),
                    state.get("phase").and_then(|v| v.as_str()).unwrap_or("?"),
                    d.action,
                    d.amount.map(|a| a.to_string()).unwrap_or_default(),
                ));
                let action = json!({
                    "type": "turn_action",
                    "match_id": match_id,
                    "request_id": request_id,  // MUST echo
                    "action": d.action,
                    "params": d.to_params(),
                });
                write.send(Message::Text(action.to_string().into())).await?;
            }
            "action_rejected" => {
                // Retry within remaining_ms using the SAME request_id.
                let request_id = msg
                    .get("request_id")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                log(&format!(
                    "Action rejected ({}): {} — {}ms remaining",
                    msg.get("reason").and_then(|v| v.as_str()).unwrap_or("?"),
                    msg.get("message").and_then(|v| v.as_str()).unwrap_or(""),
                    msg.get("remaining_ms")
                        .and_then(|v| v.as_i64())
                        .unwrap_or(0),
                ));
                let action = json!({
                    "type": "turn_action",
                    "match_id": match_id,
                    "request_id": request_id,
                    "action": "check",
                    "params": {},
                });
                write.send(Message::Text(action.to_string().into())).await?;
            }
            "turn_result" => {
                let d = msg.get("details").cloned().unwrap_or(json!({}));
                log(&format!(
                    "Turn result seat={} action={} amount={}",
                    d.get("seat").and_then(|v| v.as_i64()).unwrap_or(-1),
                    d.get("action").and_then(|v| v.as_str()).unwrap_or("?"),
                    d.get("amount").and_then(|v| v.as_i64()).unwrap_or(0),
                ));
            }
            "phase_change" => {
                let s = msg.get("state").cloned().unwrap_or(json!({}));
                log(&format!(
                    "Phase -> {} board={}",
                    s.get("phase").and_then(|v| v.as_str()).unwrap_or("?"),
                    s.get("board").cloned().unwrap_or(json!([])),
                ));
            }
            "round_result" => {
                let r = msg.get("result").cloned().unwrap_or(json!({}));
                log(&format!(
                    "Hand {} over: winners={} pot={}",
                    r.get("hand_number").and_then(|v| v.as_i64()).unwrap_or(0),
                    r.get("winner_seats").cloned().unwrap_or(json!([])),
                    r.get("pot").and_then(|v| v.as_i64()).unwrap_or(0),
                ));
            }
            "action_timeout" => {
                log(&format!(
                    "Timed out; server auto-applied: {}",
                    msg.get("auto_action")
                        .and_then(|v| v.as_str())
                        .unwrap_or("?"),
                ));
            }
            "session_control" => {
                log(&format!(
                    "Session control: {} ({})",
                    msg.get("action").and_then(|v| v.as_str()).unwrap_or("?"),
                    msg.get("reason").and_then(|v| v.as_str()).unwrap_or("?"),
                ));
            }
            "error" => {
                log(&format!(
                    "Error [{}]: {}",
                    msg.get("code").and_then(|v| v.as_str()).unwrap_or("?"),
                    msg.get("message").and_then(|v| v.as_str()).unwrap_or(""),
                ));
            }
            "match_end" => {
                log(&format!(
                    "Match ended: {}",
                    msg.get("reason").and_then(|v| v.as_str()).unwrap_or("?"),
                ));
                break;
            }
            _ => {
                // Forward-compat: silently ignore unknown message types.
            }
        }
    }

    log("Disconnected");
    Ok(())
}
