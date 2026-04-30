//! WebSocket client for the Chipzen two-layer protocol.
//!
//! The user-facing surface is [`run_bot`]. Internals (the session
//! loop, helper extractors, the `MessageReader`/`MessageWriter`
//! traits) are exported with an underscore prefix or behind
//! `#[doc(hidden)]` for the conformance harness — they are not part
//! of the supported API.

use crate::bot::Bot;
use crate::error::Error;
use crate::models::{parse_game_state, Action};
use async_trait::async_trait;
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use std::time::Duration;
use tokio_tungstenite::{
    connect_async,
    tungstenite::{Error as WsError, Message},
};

/// Protocol versions this client claims to support in the handshake.
pub const SUPPORTED_PROTOCOL_VERSIONS: &[&str] = &["1.0"];

const DEFAULT_CLIENT_NAME: &str = "chipzen-sdk";
const DEFAULT_CLIENT_VERSION: &str = env!("CARGO_PKG_VERSION");
const DEFAULT_MAX_RETRIES: u32 = 3;

/// Optional knobs for [`run_bot`]. Defaults match the platform's
/// expectations.
#[derive(Debug, Clone)]
pub struct RunBotOptions {
    /// Bot API token. Required for the `/bot` endpoint; empty is fine
    /// for local dev.
    pub token: Option<String>,
    /// Single-use ticket alternative to `token` (competitive
    /// endpoints).
    pub ticket: Option<String>,
    /// Match UUID. Auto-extracted from the URL if `None`.
    pub match_id: Option<String>,
    /// Client software name sent in the `hello` handshake.
    pub client_name: Option<String>,
    /// Client software version sent in the `hello` handshake.
    pub client_version: Option<String>,
    /// Number of reconnect attempts on unexpected disconnect.
    pub max_retries: u32,
}

impl Default for RunBotOptions {
    fn default() -> Self {
        Self {
            token: None,
            ticket: None,
            match_id: None,
            client_name: None,
            client_version: None,
            max_retries: DEFAULT_MAX_RETRIES,
        }
    }
}

/// Opaque per-session bag the session loop threads through. Public so
/// the conformance harness can construct one.
#[derive(Debug, Clone)]
pub struct SessionContext {
    pub match_id: String,
    pub token: Option<String>,
    pub ticket: Option<String>,
    pub client_name: String,
    pub client_version: String,
}

/// Connect a bot to the Chipzen server and play until the match ends.
///
/// Returns cleanly on `match_end`. Returns [`Error::RetriesExhausted`]
/// if the connection cannot be established after `max_retries`
/// attempts.
pub async fn run_bot<B: Bot>(url: &str, mut bot: B, options: RunBotOptions) -> Result<(), Error> {
    let match_id = options
        .match_id
        .clone()
        .unwrap_or_else(|| _extract_match_id(url));
    let ctx = SessionContext {
        match_id,
        token: options.token.clone(),
        ticket: options.ticket.clone(),
        client_name: options
            .client_name
            .clone()
            .unwrap_or_else(|| DEFAULT_CLIENT_NAME.to_string()),
        client_version: options
            .client_version
            .clone()
            .unwrap_or_else(|| DEFAULT_CLIENT_VERSION.to_string()),
    };

    let mut retries: u32 = 0;
    loop {
        let result: Result<(), Error> = async {
            let (ws_stream, _) = connect_async(url).await?;
            let (mut write_half, mut read_half) = ws_stream.split();
            let mut reader = WsReader {
                inner: &mut read_half,
            };
            let mut writer = WsWriter {
                inner: &mut write_half,
            };
            _run_session(&mut reader, &mut writer, &mut bot, &ctx).await
        }
        .await;

        match result {
            Ok(()) => return Ok(()),
            Err(err) => {
                retries += 1;
                if retries > options.max_retries {
                    return Err(Error::RetriesExhausted {
                        attempts: retries,
                        last_error: err.to_string(),
                    });
                }
                let backoff_secs = (1u64 << retries.min(3)).min(8);
                tokio::time::sleep(Duration::from_secs(backoff_secs)).await;
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Session loop — internal, exposed for the conformance harness
// ---------------------------------------------------------------------------

/// Pull-based async iterator over inbound messages. Real impl wraps
/// `tokio_tungstenite::WebSocketStream`; the conformance harness
/// provides a scripted impl in a future PR.
#[async_trait]
pub trait MessageReader: Send {
    /// Returns the next message as a UTF-8 string, or `None` if the
    /// underlying transport has closed cleanly. Errors should be
    /// surfaced through [`Error`] rather than this return type so the
    /// session loop can decide whether to retry.
    async fn next(&mut self) -> Result<Option<String>, Error>;
}

/// Push-based async sender for outbound messages.
#[async_trait]
pub trait MessageWriter: Send {
    async fn send(&mut self, payload: String) -> Result<(), Error>;
}

/// Drive a single connected session: handshake + message loop until
/// `match_end`. Public-but-hidden so the conformance harness in a
/// future PR can reuse it against a mock socket.
pub async fn _run_session<R, W, B>(
    reader: &mut R,
    writer: &mut W,
    bot: &mut B,
    ctx: &SessionContext,
) -> Result<(), Error>
where
    R: MessageReader,
    W: MessageWriter,
    B: Bot,
{
    // --- Layer 1 handshake ----------------------------------------------------
    let mut auth = json!({
        "type": "authenticate",
        "match_id": ctx.match_id,
        "client_name": ctx.client_name,
        "client_version": ctx.client_version,
    });
    if let Some(t) = ctx.token.as_deref().filter(|s| !s.is_empty()) {
        auth["token"] = Value::String(t.to_string());
    } else if let Some(t) = ctx.ticket.as_deref().filter(|s| !s.is_empty()) {
        auth["ticket"] = Value::String(t.to_string());
    } else {
        auth["token"] = Value::String(String::new());
    }
    writer.send(auth.to_string()).await?;

    let hello_raw = reader.next().await?.ok_or(Error::ConnectionClosed {
        context: "server hello",
    })?;
    let hello: Value = serde_json::from_str(&hello_raw)?;
    if hello.get("type").and_then(|v| v.as_str()) != Some("hello") {
        return Err(Error::Protocol(format!(
            "expected server hello, got {:?}",
            hello.get("type")
        )));
    }

    let client_hello = json!({
        "type": "hello",
        "match_id": ctx.match_id,
        "supported_versions": SUPPORTED_PROTOCOL_VERSIONS,
    });
    writer.send(client_hello.to_string()).await?;

    // --- Message loop ---------------------------------------------------------
    let mut last_seq: i64 = 0;
    while let Some(raw) = reader.next().await? {
        let msg: Value = match serde_json::from_str(&raw) {
            Ok(v) => v,
            // Malformed envelope — log + continue. Real production
            // deployments never emit invalid JSON; this is for
            // adversarial-input robustness.
            Err(_) => continue,
        };

        if let Some(seq) = msg.get("seq").and_then(Value::as_i64) {
            if seq <= last_seq {
                continue; // sequence regression / retransmit
            }
            last_seq = seq;
        }

        let mtype = msg.get("type").and_then(|v| v.as_str()).unwrap_or("");
        match mtype {
            "ping" => {
                let pong = json!({ "type": "pong", "match_id": ctx.match_id });
                writer.send(pong.to_string()).await?;
            }
            "match_start" => bot.on_match_start(&msg),
            "round_start" => bot.on_round_start(&msg),
            "phase_change" => bot.on_phase_change(&msg),
            "turn_result" => bot.on_turn_result(&msg),
            "round_result" => bot.on_round_result(&msg),
            "turn_request" => {
                let request_id = msg
                    .get("request_id")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let state = parse_game_state(&msg);
                let action = catch_decide(bot, &state, &msg);
                let (action_str, params) = action.to_wire();
                let payload = json!({
                    "type": "turn_action",
                    "match_id": ctx.match_id,
                    "request_id": request_id,
                    "action": action_str,
                    "params": params,
                });
                writer.send(payload.to_string()).await?;
            }
            "action_rejected" => {
                let request_id = msg
                    .get("request_id")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let valid_actions: Vec<String> = msg
                    .get("valid_actions")
                    .and_then(|v| v.as_array())
                    .map(|arr| {
                        arr.iter()
                            .filter_map(|v| v.as_str().map(String::from))
                            .collect()
                    })
                    .unwrap_or_else(|| vec!["fold".to_string()]);
                let fallback = _safe_fallback_action(&valid_actions);
                let (action_str, params) = fallback.to_wire();
                let payload = json!({
                    "type": "turn_action",
                    "match_id": ctx.match_id,
                    "request_id": request_id,
                    "action": action_str,
                    "params": params,
                });
                writer.send(payload.to_string()).await?;
            }
            "reconnected" => {
                if let Some(pending) = msg.get("pending_request") {
                    if pending.get("type").and_then(|v| v.as_str()) == Some("turn_request") {
                        let request_id = pending
                            .get("request_id")
                            .and_then(|v| v.as_str())
                            .unwrap_or("")
                            .to_string();
                        let state = parse_game_state(pending);
                        let action = catch_decide(bot, &state, pending);
                        let (action_str, params) = action.to_wire();
                        let payload = json!({
                            "type": "turn_action",
                            "match_id": ctx.match_id,
                            "request_id": request_id,
                            "action": action_str,
                            "params": params,
                        });
                        writer.send(payload.to_string()).await?;
                    }
                }
            }
            "match_end" => {
                let results = msg.get("results").cloned().unwrap_or_else(|| msg.clone());
                bot.on_match_end(&results);
                return Ok(());
            }
            "error" => {
                // Non-fatal — production deployments log; the SDK stays
                // quiet so user code controls the logging surface.
            }
            _ => {
                // Forward-compat: silently ignore unknown message types.
            }
        }
    }

    // Stream closed without match_end. Caller decides whether to retry.
    Ok(())
}

/// Run `bot.decide(state)` and substitute the safe-fallback action if
/// it returns something nonsensical for the current `valid_actions`.
/// Catching panics would require `std::panic::catch_unwind` which
/// requires `UnwindSafe`; instead we trust the validator to surface
/// panicking decides during development.
fn catch_decide<B: Bot>(bot: &mut B, state: &crate::models::GameState, msg: &Value) -> Action {
    let action = bot.decide(state);
    if !action_is_legal(&action, &state.valid_actions) {
        let valid = msg
            .get("valid_actions")
            .and_then(|v| v.as_array())
            .map(|arr| {
                arr.iter()
                    .filter_map(|v| v.as_str().map(String::from))
                    .collect::<Vec<_>>()
            })
            .unwrap_or_else(|| state.valid_actions.clone());
        return _safe_fallback_action(&valid);
    }
    action
}

fn action_is_legal(action: &Action, valid: &[String]) -> bool {
    let needed = action.kind().as_str();
    valid.iter().any(|v| v == needed)
}

/// Pick a safe action from the legal set: prefer `check`, fall back
/// to `fold`. Public-but-hidden so the conformance harness can use
/// the same logic when it doesn't have a real bot to drive.
pub fn _safe_fallback_action(valid_actions: &[String]) -> Action {
    if valid_actions.iter().any(|a| a == "check") {
        Action::Check
    } else {
        Action::Fold
    }
}

/// Pull `match_id` out of a Chipzen WebSocket URL. Path shape is
/// `.../ws/match/<match_id>/...`. Returns an empty string if the URL
/// doesn't match the expected pattern. Permissive on the inner shape
/// — server-side IDs may be UUIDs, shortened hashes, or namespaced
/// strings like `m_abc_123`.
pub fn _extract_match_id(url: &str) -> String {
    let needle = "/ws/match/";
    let Some(start) = url.find(needle) else {
        return String::new();
    };
    let after = &url[start + needle.len()..];
    let end = after.find(['/', '?', '#']).unwrap_or(after.len());
    after[..end].to_string()
}

// ---------------------------------------------------------------------------
// Real WebSocket adapters — bridge tokio-tungstenite to the trait surface
// ---------------------------------------------------------------------------

struct WsReader<'a, S>
where
    S: StreamExt<Item = Result<Message, WsError>> + Unpin,
{
    inner: &'a mut S,
}

#[async_trait]
impl<'a, S> MessageReader for WsReader<'a, S>
where
    S: StreamExt<Item = Result<Message, WsError>> + Unpin + Send,
{
    async fn next(&mut self) -> Result<Option<String>, Error> {
        loop {
            match self.inner.next().await {
                Some(Ok(Message::Text(t))) => return Ok(Some(t.to_string())),
                Some(Ok(Message::Ping(_))) => {
                    // Tungstenite auto-replies to control pings, but
                    // some peers send Ping as a Text-style heartbeat.
                    // Surface it so the session loop's `ping` handler
                    // can respond if needed.
                    continue;
                }
                Some(Ok(Message::Close(_))) | None => return Ok(None),
                Some(Ok(_)) => continue, // binary, pong, etc.
                Some(Err(e)) => return Err(Error::from(e)),
            }
        }
    }
}

struct WsWriter<'a, S>
where
    S: SinkExt<Message, Error = WsError> + Unpin,
{
    inner: &'a mut S,
}

#[async_trait]
impl<'a, S> MessageWriter for WsWriter<'a, S>
where
    S: SinkExt<Message, Error = WsError> + Unpin + Send,
{
    async fn send(&mut self, payload: String) -> Result<(), Error> {
        self.inner
            .send(Message::Text(payload))
            .await
            .map_err(Error::from)
    }
}
