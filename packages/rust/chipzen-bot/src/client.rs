//! WebSocket client for the Chipzen two-layer protocol.
//!
//! The user-facing surface is [`run_bot`]. Internals (the session
//! loop, helper extractors, the `MessageReader`/`MessageWriter`
//! traits) are exported with an underscore prefix or behind
//! `#[doc(hidden)]` for the conformance harness — they are not part
//! of the supported API.

use crate::bot::Bot;
use crate::error::Error;
use crate::models::{parse_game_state, Action, GameState};
use crate::retry::RetryPolicy;
use async_trait::async_trait;
use futures_util::{SinkExt, StreamExt};
use serde::Serialize;
use serde_json::{json, Value};
use std::panic::AssertUnwindSafe;
use tokio_tungstenite::{
    connect_async,
    tungstenite::{client::IntoClientRequest, http::header::USER_AGENT, Error as WsError, Message},
};

/// Protocol versions this client claims to support in the handshake.
pub const SUPPORTED_PROTOCOL_VERSIONS: &[&str] = &["1.0"];

const DEFAULT_CLIENT_NAME: &str = "chipzen-sdk-rust";
const DEFAULT_CLIENT_VERSION: &str = env!("CARGO_PKG_VERSION");

/// Default `User-Agent` header sent on the WebSocket handshake. A
/// non-default UA also clears the platform's Cloudflare bot-fight rule
/// (chipzen-ai/chipzen-sdk#46).
pub fn default_user_agent() -> String {
    format!("chipzen-sdk-rust/{DEFAULT_CLIENT_VERSION}")
}

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
    /// Client software version sent in the `hello` handshake. Defaults to
    /// the crate version (chipzen-ai/chipzen-sdk#41).
    pub client_version: Option<String>,
    /// Reconnect-pacing policy (attempt cap + exponential backoff).
    pub retry_policy: RetryPolicy,
    /// When `true` (default), a panic in `decide()` is caught and folded —
    /// a transient bug won't forfeit a competitive match. Set `false` for
    /// dev/eval so the first panic propagates as [`Error::BotDecision`] and
    /// exits non-zero (chipzen-ai/chipzen-sdk#52).
    pub safe_mode: bool,
    /// Override the WS `User-Agent` header. Defaults to
    /// `chipzen-sdk-rust/<version>` (chipzen-ai/chipzen-sdk#46).
    pub user_agent: Option<String>,
    /// The games this client can actually play, declared in the client
    /// `hello` as `supported_games`: a list of `game_type` strings
    /// (`"poker"`, `"draw27"`, `"ofc"`). See
    /// `docs/protocol/LAYER2-COMMON.md` section 2.
    ///
    /// `None` (the default) **omits the field entirely**, and the platform
    /// reads an absent declaration as "poker only", so the default `hello`
    /// is byte-identical to earlier releases. An empty list is sent as is
    /// and means "supports nothing". At a non-poker table a client that has
    /// not declared the seat's game is rejected before it is seated (close
    /// `4002`). Declaring a `game_type` asserts you implement everything in
    /// that game's Layer 2 spec, so do not declare a game your `decide()`
    /// cannot play. This release plays `poker`, `draw27` (2-7 Triple Draw:
    /// branch on `state.is_draw_phase`, see [`crate::Bot::decide_draw`]) and
    /// `ofc` (Pineapple OFC: branch on `"place"` in `state.valid_actions`,
    /// see [`crate::Bot::decide_placement`]).
    pub supported_games: Option<Vec<String>>,
}

impl Default for RunBotOptions {
    fn default() -> Self {
        Self {
            token: None,
            ticket: None,
            match_id: None,
            client_name: None,
            client_version: None,
            retry_policy: RetryPolicy::default(),
            safe_mode: true,
            user_agent: None,
            supported_games: None,
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
    /// When `false`, a panicking `decide()` surfaces as
    /// [`Error::BotDecision`] instead of being folded to a safe action.
    pub safe_mode: bool,
    /// The `game_type`s declared on the client `hello` as
    /// `supported_games`. `None` omits the field (see
    /// [`RunBotOptions::supported_games`]).
    pub supported_games: Option<Vec<String>>,
}

impl SessionContext {
    /// A context with `safe_mode` on — the common case. Lets existing
    /// callers (tests, conformance harness) construct a context with the
    /// historical field set without naming `safe_mode` explicitly.
    pub fn new(
        match_id: String,
        token: Option<String>,
        ticket: Option<String>,
        client_name: String,
        client_version: String,
    ) -> Self {
        Self {
            match_id,
            token,
            ticket,
            client_name,
            client_version,
            safe_mode: true,
            supported_games: None,
        }
    }
}

/// Connect a bot to the Chipzen server and play until the match ends.
///
/// Returns the `match_end` payload on a clean finish, or `None` if the
/// connection closed without a clean `match_end` after exhausting the
/// retry budget. Returns [`Error::RetriesExhausted`] if the connection
/// cannot be established after `retry_policy.max_reconnect_attempts`
/// attempts, or [`Error::BotDecision`] if `decide()` panics under
/// `safe_mode = false`.
pub async fn run_bot<B: Bot>(
    url: &str,
    mut bot: B,
    options: RunBotOptions,
) -> Result<Option<Value>, Error> {
    let match_id = options
        .match_id
        .clone()
        .unwrap_or_else(|| _extract_match_id(url));
    let client_version = options
        .client_version
        .clone()
        .unwrap_or_else(|| DEFAULT_CLIENT_VERSION.to_string());
    let user_agent = options
        .user_agent
        .clone()
        .unwrap_or_else(default_user_agent);
    let ctx = SessionContext {
        match_id,
        token: options.token.clone(),
        ticket: options.ticket.clone(),
        client_name: options
            .client_name
            .clone()
            .unwrap_or_else(|| DEFAULT_CLIENT_NAME.to_string()),
        client_version,
        safe_mode: options.safe_mode,
        supported_games: options.supported_games.clone(),
    };

    let policy = options.retry_policy;
    let max_attempts = policy.max_reconnect_attempts;

    let mut retries: u32 = 0;
    loop {
        let result: Result<Option<Value>, Error> = async {
            let request = build_handshake_request(url, &user_agent)?;
            let (ws_stream, _) = connect_async(request).await?;
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
            Ok(end) => return Ok(end),
            // A deterministic bot bug (safe_mode off) — terminal, not a
            // transient disconnect. Do not reconnect-retry; propagate so the
            // process exits non-zero.
            Err(e @ Error::BotDecision(_)) => return Err(e),
            Err(err) => {
                retries += 1;
                if retries > max_attempts {
                    return Err(Error::RetriesExhausted {
                        attempts: retries,
                        last_error: err.to_string(),
                    });
                }
                let backoff_ms = policy.backoff_ms(retries);
                tokio::time::sleep(std::time::Duration::from_millis(backoff_ms)).await;
            }
        }
    }
}

/// Build the tungstenite handshake request for `url`, attaching the
/// `User-Agent` header. tokio-tungstenite does not set a UA by default;
/// some hosts (Cloudflare bot-fight) reject the empty UA.
fn build_handshake_request(
    url: &str,
    user_agent: &str,
) -> Result<tokio_tungstenite::tungstenite::handshake::client::Request, Error> {
    let mut request = url.into_client_request().map_err(Error::from)?;
    if let Ok(value) = user_agent.parse() {
        request.headers_mut().insert(USER_AGENT, value);
    }
    Ok(request)
}

// ---------------------------------------------------------------------------
// Session loop — internal, exposed for the conformance harness
// ---------------------------------------------------------------------------

/// Pull-based async iterator over inbound messages. Real impl wraps
/// `tokio_tungstenite::WebSocketStream`; the conformance harness
/// provides a scripted impl.
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

// Boxed trait objects forward to their inner impl so callers (e.g. the
// external-API transport, which returns `Box<dyn ...>`) can hand them to
// the generic [`_run_session`] without re-wrapping.
#[async_trait]
impl MessageReader for Box<dyn MessageReader> {
    async fn next(&mut self) -> Result<Option<String>, Error> {
        (**self).next().await
    }
}

#[async_trait]
impl MessageWriter for Box<dyn MessageWriter> {
    async fn send(&mut self, payload: String) -> Result<(), Error> {
        (**self).send(payload).await
    }
}

/// Drive a single connected session: handshake + message loop until
/// `match_end`. Public-but-hidden so the conformance harness (and the
/// external-API gateway leg) can reuse it against any transport.
///
/// Returns the `match_end` payload (the full envelope) on a clean
/// finish, or `None` if the socket closed without a `match_end` (a drop
/// the caller may reconnect through). Returns [`Error::BotDecision`] if
/// `decide()` panics under `ctx.safe_mode = false`.
pub async fn _run_session<R, W, B>(
    reader: &mut R,
    writer: &mut W,
    bot: &mut B,
    ctx: &SessionContext,
) -> Result<Option<Value>, Error>
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

    // LAYER2-COMMON section 2: branch on `state_shape` before parsing any
    // state. An NLHE hello carries no `game` descriptor (shape `None`), so
    // every poker table keeps the path it has always taken. A shape this
    // release cannot parse ends the session (as in the Python and JavaScript
    // SDKs) rather than being read as NLHE.
    match state_shape_of(&hello) {
        Err(detail) => {
            eprintln!("chipzen-sdk: server hello has a malformed game descriptor: {detail}");
            return Ok(None);
        }
        Ok(shape) if !POKER_STATE_SHAPES.contains(&shape) => {
            eprintln!(
                "chipzen-sdk: cannot parse state_shape {shape:?}: this SDK release does not \
                 know it, so it cannot play this table"
            );
            return Ok(None);
        }
        Ok(_) => {}
    }

    let client_hello = ClientHello {
        client_name: &ctx.client_name,
        client_version: &ctx.client_version,
        match_id: &ctx.match_id,
        supported_versions: SUPPORTED_PROTOCOL_VERSIONS,
        kind: "hello",
        supported_games: ctx.supported_games.as_deref(),
    };
    writer.send(serde_json::to_string(&client_hello)?).await?;

    // --- Message loop ---------------------------------------------------------
    let mut last_seq: i64 = 0;
    // Seat context, tracked as the Python loop does (the Rust analog of
    // chipzen-ai/chipzen-sdk#194): a hold'em or 27TD turn state carries
    // neither `your_seat` nor `dealer_seat`, so the seat is taken from
    // `match_start` (or `reconnected`) and the button from each
    // `round_start`. A turn state that does carry them (OFC's does) wins.
    let mut seats = Seats::default();
    // The last turn this session answered, kept so an `action_rejected` for
    // it can be retried with a fallback built from that turn's state (an OFC
    // default placement needs the cards and the row capacity).
    let mut last_turn: Option<(String, GameState)> = None;
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
            "match_start" => {
                if let Some(seat) = self_seat(&msg) {
                    seats.your_seat = seat;
                }
                call_hook("on_match_start", ctx.safe_mode, || bot.on_match_start(&msg))?
            }
            "round_start" => {
                if let Some(dealer) = msg
                    .get("state")
                    .and_then(|s| s.get("dealer_seat"))
                    .and_then(Value::as_i64)
                {
                    seats.dealer_seat = dealer;
                }
                call_hook("on_round_start", ctx.safe_mode, || bot.on_round_start(&msg))?
            }
            "phase_change" => call_hook("on_phase_change", ctx.safe_mode, || {
                bot.on_phase_change(&msg)
            })?,
            "turn_result" => {
                call_hook("on_turn_result", ctx.safe_mode, || bot.on_turn_result(&msg))?
            }
            "round_result" => call_hook("on_round_result", ctx.safe_mode, || {
                bot.on_round_result(&msg)
            })?,
            "turn_request" => {
                let request_id = msg
                    .get("request_id")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let state = parse_turn(&msg, &seats);
                let (action, latency_ms) = decide_timed(bot, &state, &msg, ctx.safe_mode)?;
                last_turn = Some((request_id.clone(), state));
                send_turn_action(writer, &ctx.match_id, &request_id, action).await?;
                call_hook("on_decision_latency", ctx.safe_mode, || {
                    bot.on_decision_latency(latency_ms)
                })?;
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
                // A rejection of a turn this session never saw has no state:
                // an OFC fallback is then an empty placement, which the
                // server rejects and replaces with its own default.
                let rejected_state = last_turn
                    .as_ref()
                    .filter(|(id, _)| *id == request_id)
                    .map(|(_, state)| state);
                let fallback = _safe_fallback_action_for(&valid_actions, rejected_state)?;
                send_turn_action(writer, &ctx.match_id, &request_id, fallback).await?;
            }
            "reconnected" => {
                // Mid-session after a reconnect. Re-learn seat context from
                // `reconnected.seats` (same schema as `match_start.seats`,
                // TRANSPORT-PROTOCOL §8.15) exactly as a `match_start` would
                // deliver it: on a re-attach this session never saw
                // `match_start`, so without this the replayed pending turn
                // below would be decided with your_seat=0
                // (chipzen-ai/chipzen-sdk#121, the Rust analog of #119).
                // The button is kept from the last `round_start` seen.
                if let Some(seat) = self_seat(&msg) {
                    seats.your_seat = seat;
                }
                call_hook("on_reconnected", ctx.safe_mode, || bot.on_reconnected(&msg))?;
                // Replay the pending request as if it were a fresh
                // turn_request so the bot acts on it, with the seat
                // re-learned from the reconnected frame (the pending state
                // itself does not carry `your_seat`).
                if let Some(pending) = msg.get("pending_request") {
                    if pending.get("type").and_then(|v| v.as_str()) == Some("turn_request") {
                        let request_id = pending
                            .get("request_id")
                            .and_then(|v| v.as_str())
                            .unwrap_or("")
                            .to_string();
                        let state = parse_turn(pending, &seats);
                        let (action, latency_ms) =
                            decide_timed(bot, &state, pending, ctx.safe_mode)?;
                        last_turn = Some((request_id.clone(), state));
                        send_turn_action(writer, &ctx.match_id, &request_id, action).await?;
                        call_hook("on_decision_latency", ctx.safe_mode, || {
                            bot.on_decision_latency(latency_ms)
                        })?;
                    }
                }
            }
            "match_end" => {
                let results = msg.get("results").cloned().unwrap_or_else(|| msg.clone());
                call_hook("on_match_end", ctx.safe_mode, || bot.on_match_end(&results))?;
                return Ok(Some(msg));
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
    Ok(None)
}

/// The `state_shape`s this release parses with [`parse_game_state`]: `None`
/// is a server `hello` with no `game` descriptor, which means NLHE,
/// `"27tripledraw"` is 2-7 Triple Draw and `"pineapple"` is Pineapple OFC.
/// Any other shape is refused rather than read as hold'em.
const POKER_STATE_SHAPES: &[Option<&str>] = &[None, Some("27tripledraw"), Some("pineapple")];

/// The `state_shape` a server `hello` advertises: `Ok(None)` when it has no
/// `game` descriptor (poker), `Err` when the descriptor is present but does
/// not carry a string `state_shape`.
fn state_shape_of(hello: &Value) -> Result<Option<&str>, String> {
    match hello.get("game") {
        None | Some(Value::Null) => Ok(None),
        Some(game) => match game.get("state_shape") {
            Some(Value::String(shape)) => Ok(Some(shape.as_str())),
            other => Err(format!(
                "game.state_shape is {}, not a string",
                other.map_or_else(|| "missing".to_string(), Value::to_string)
            )),
        },
    }
}

/// Seat context the session loop carries into each turn.
#[derive(Debug, Default)]
struct Seats {
    /// The bot's seat, from `match_start` / `reconnected`.
    your_seat: i64,
    /// The button, from the latest `round_start`.
    dealer_seat: i64,
}

/// `seats[].seat` of the entry flagged `is_self` in a `match_start` or
/// `reconnected` frame (TRANSPORT-PROTOCOL §8.15: the same schema).
fn self_seat(msg: &Value) -> Option<i64> {
    msg.get("seats")?
        .as_array()?
        .iter()
        .find(|s| s.get("is_self").and_then(Value::as_bool).unwrap_or(false))?
        .get("seat")?
        .as_i64()
}

/// Parse a `turn_request` with the session's seat context filled in where
/// the turn state does not carry it (a hold'em or 27TD state never does; an
/// OFC state does, and its own values win).
fn parse_turn(msg: &Value, seats: &Seats) -> GameState {
    let mut state = parse_game_state(msg);
    let raw_int = |key: &str| {
        msg.get("state")
            .and_then(|s| s.get(key))
            .and_then(Value::as_i64)
    };
    if raw_int("your_seat").is_none() {
        state.your_seat = seats.your_seat;
    }
    if raw_int("dealer_seat").is_none() {
        state.dealer_seat = seats.dealer_seat;
    }
    state
}

/// The Layer 1 client `hello` frame.
///
/// The first five fields are declared in the exact order the frame has
/// always gone on the wire (the `json!` map it replaced serialized its keys
/// sorted), so a client that declares nothing sends byte-identical text to
/// earlier releases, whatever `serde_json` features the final binary
/// enables. `supported_games` (chipzen-ai/chipzen-sdk#172) is appended
/// **last** and only when declared: the platform reads an absent
/// declaration as "poker only", which is what every deployed bot relies
/// on, while an empty list means "supports nothing". `supported_games` is
/// on the server's handshake field allowlist; any other key closes the
/// socket.
#[derive(Serialize)]
struct ClientHello<'a> {
    client_name: &'a str,
    client_version: &'a str,
    match_id: &'a str,
    supported_versions: &'a [&'a str],
    #[serde(rename = "type")]
    kind: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    supported_games: Option<&'a [String]>,
}

/// Send a `turn_action` envelope echoing `request_id`.
async fn send_turn_action<W: MessageWriter>(
    writer: &mut W,
    match_id: &str,
    request_id: &str,
    action: FallbackAction,
) -> Result<(), Error> {
    let (action_str, params) = action.to_wire();
    let payload = json!({
        "type": "turn_action",
        "match_id": match_id,
        "request_id": request_id,
        "action": action_str,
        "params": params,
    });
    writer.send(payload.to_string()).await
}

/// Run `bot.decide(state)` with timing + safe_mode handling, returning
/// `(action, latency_ms)`.
///
/// A panic in `decide()` is caught. Under `safe_mode` it is folded to a
/// safe-fallback action (so a transient bug doesn't forfeit a competitive
/// match); under `safe_mode = false` it surfaces as [`Error::BotDecision`]
/// so the caller treats it as terminal. If `decide()` returns an action
/// that isn't legal for the current `valid_actions`, the safe fallback is
/// substituted regardless of `safe_mode` (mirrors the existing Rust + JS
/// behavior).
///
/// Errors from [`_safe_fallback_action`] (an empty `valid_actions`)
/// propagate: there is nothing legal left to send.
fn decide_timed<B: Bot>(
    bot: &mut B,
    state: &GameState,
    msg: &Value,
    safe_mode: bool,
) -> Result<(FallbackAction, f64), Error> {
    let start = std::time::Instant::now();
    let outcome = std::panic::catch_unwind(AssertUnwindSafe(|| bot.decide(state)));
    let latency_ms = start.elapsed().as_secs_f64() * 1000.0;

    let action = match outcome {
        Ok(action) => action,
        Err(payload) => {
            let detail = panic_message(payload.as_ref());
            if !safe_mode {
                return Err(Error::BotDecision(detail));
            }
            // safe_mode: fold the panic into a safe action so the match
            // continues. valid_actions drives check-vs-fold-vs-first-valid.
            return Ok((
                _safe_fallback_action_for(&state.valid_actions, Some(state))?,
                latency_ms,
            ));
        }
    };

    if action_is_legal(&action, &state.valid_actions) {
        Ok((FallbackAction::Typed(action), latency_ms))
    } else {
        let valid = msg
            .get("valid_actions")
            .and_then(|v| v.as_array())
            .map(|arr| {
                arr.iter()
                    .filter_map(|v| v.as_str().map(String::from))
                    .collect::<Vec<_>>()
            })
            .unwrap_or_else(|| state.valid_actions.clone());
        Ok((_safe_fallback_action_for(&valid, Some(state))?, latency_ms))
    }
}

/// Invoke a bot lifecycle hook with the same safe-mode guard as
/// [`decide_timed`] (chipzen-ai/chipzen-sdk#80).
///
/// A panic in a lifecycle/stats hook must never tear down the WS session:
/// historically it unwound through the session loop, the reconnect path
/// obscured the real failure, and the bot zombied into an auto-substitute
/// forfeit. The panic is caught and reported loudly on stderr (the default
/// panic hook has already printed the panic location/backtrace); the
/// session continues. Under `safe_mode = false` (dev/eval) it surfaces as
/// [`Error::BotDecision`] so the bug is terminal — mirroring `decide()`.
fn call_hook<F: FnOnce()>(name: &str, safe_mode: bool, hook: F) -> Result<(), Error> {
    match std::panic::catch_unwind(AssertUnwindSafe(hook)) {
        Ok(()) => Ok(()),
        Err(payload) => {
            let detail = panic_message(payload.as_ref());
            eprintln!(
                "chipzen-sdk: bot {name}() panicked; ignoring it and continuing the session \
                 (chipzen-ai/chipzen-sdk#80): {detail}"
            );
            if safe_mode {
                Ok(())
            } else {
                Err(Error::BotDecision(detail))
            }
        }
    }
}

/// Best-effort extraction of a panic message from the `catch_unwind`
/// payload (the common `&str` / `String` cases; otherwise a generic note).
fn panic_message(payload: &(dyn std::any::Any + Send)) -> String {
    if let Some(s) = payload.downcast_ref::<&str>() {
        (*s).to_string()
    } else if let Some(s) = payload.downcast_ref::<String>() {
        s.clone()
    } else {
        "decide() panicked".to_string()
    }
}

fn action_is_legal(action: &Action, valid: &[String]) -> bool {
    let needed = action.kind().as_str();
    valid.iter().any(|v| v == needed)
}

/// A safe-fallback selection resolved from the server's `valid_actions`.
///
/// `Typed` carries one of the [`Action`]s this SDK models. `Raw` is the
/// escape hatch for an action string it does *not* model — an unmodelled
/// variant table's action, or a `raise` whose amount the fallback path
/// cannot know. A `Raw` action is echoed verbatim on the wire with empty
/// `params`, exactly mirroring the Python SDK's
/// `Action(action=valid_actions[0])`.
///
/// Kept as a separate fallback-only type: an action kind [`Action`] does
/// not model yet still needs an answer, and bots only ever *return* an
/// [`Action`]. (2-7 Triple Draw's `draw` and Pineapple OFC's `place` are
/// modelled, so their fallbacks are a typed stand pat and a typed default
/// placement.)
#[doc(hidden)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FallbackAction {
    /// One of the action kinds this SDK models.
    Typed(Action),
    /// An action string echoed verbatim, with empty `params`.
    Raw(String),
}

impl FallbackAction {
    /// Serialize to the `(action, params)` pair of the `turn_action`
    /// payload — the owned-string counterpart of [`Action::to_wire`].
    pub fn to_wire(&self) -> (String, Value) {
        match self {
            FallbackAction::Typed(action) => {
                let (name, params) = action.to_wire();
                (name.to_string(), params)
            }
            FallbackAction::Raw(name) => (name.clone(), json!({})),
        }
    }
}

/// Pick a safe action from the legal set: prefer `check`, else `fold`,
/// else a **stand pat** on a 2-7 Triple Draw draw turn (`["draw"]`, where
/// the stand pat is also the server's own timeout substitute), else an
/// **empty placement** on a Pineapple OFC turn (`["place"]`; with no turn
/// state there is nothing to place, so the server applies its own default,
/// see [`_safe_fallback_action_for`]), else the **first valid action** the
/// server offered. Public-but-hidden so the conformance harness can use the
/// same logic when it doesn't have a real bot to drive.
///
/// This mirrors the Python SDK's `_safe_fallback_action`
/// (`packages/python/src/chipzen/client.py`) so both SDKs degrade
/// identically. The first-valid branch matters when the bot is seated at a
/// table whose action set it does not model (a variant table offering only
/// `["draw"]`): blindly answering `check`/`fold` there is rejected, retried,
/// rejected again, and the bot spins until the auto-substitute streak limit
/// kills the match (chipzen-ai/Chipzen#4244). Taking that branch is logged
/// at warn level on stderr — degrading silently is worse than degrading
/// loudly.
///
/// # Errors
///
/// [`Error::Protocol`] when `valid_actions` is empty: the server offered
/// nothing legal, so there is no action to fall back to. An explicit error
/// beats guessing `fold` and having it rejected forever.
pub fn _safe_fallback_action(valid_actions: &[String]) -> Result<FallbackAction, Error> {
    _safe_fallback_action_for(valid_actions, None)
}

/// [`_safe_fallback_action`] with the turn's state, which a Pineapple OFC
/// turn (`["place"]`, no check, no fold) needs: there the fallback is the
/// protocol's default placement built from `state` (see
/// [`_ofc_default_placement`]). The session passes the state of the turn
/// being answered, and on an `action_rejected` the state of the turn it
/// rejects (`None` for a turn this session never saw).
pub fn _safe_fallback_action_for(
    valid_actions: &[String],
    state: Option<&GameState>,
) -> Result<FallbackAction, Error> {
    if valid_actions.iter().any(|a| a == "check") {
        return Ok(FallbackAction::Typed(Action::Check));
    }
    if valid_actions.iter().any(|a| a == "fold") {
        return Ok(FallbackAction::Typed(Action::Fold));
    }
    // A 2-7 Triple Draw draw turn offers exactly `["draw"]`: no check, no
    // fold. A stand pat is legal in every draw phase and is the protocol's
    // timeout default (DRAW27-GAME-STATE-PROTOCOL.md section 5.5).
    if valid_actions.iter().any(|a| a == "draw") {
        return Ok(FallbackAction::Typed(Action::stand_pat()));
    }
    // A Pineapple OFC turn offers exactly `["place"]`. The protocol's default
    // placement is also the server's own timeout substitute
    // (OFC-GAME-STATE-PROTOCOL.md section 5.9).
    if valid_actions.iter().any(|a| a == "place") {
        return Ok(FallbackAction::Typed(_ofc_default_placement(state)));
    }

    let Some(first) = valid_actions.first() else {
        return Err(Error::Protocol(
            "no safe fallback action available: the server offered an empty valid_actions list"
                .to_string(),
        ));
    };

    eprintln!(
        "chipzen-sdk: neither `check` nor `fold` is legal here (valid_actions={valid_actions:?}); \
         falling back to the first valid action {first:?}. This bot does not model the action set \
         of the table it is seated at (chipzen-ai/Chipzen#4244)."
    );

    Ok(match first.as_str() {
        // `check` / `fold` are handled above; the remaining modelled kinds
        // that need no params map to a typed action.
        "call" => FallbackAction::Typed(Action::Call),
        "all_in" => FallbackAction::Typed(Action::AllIn),
        // Everything else — an unmodelled variant action, or `raise` whose
        // amount this path cannot know — is echoed verbatim, as Python does.
        _ => FallbackAction::Raw(first.clone()),
    })
}

/// Pineapple OFC: the protocol's default placement
/// (OFC-GAME-STATE-PROTOCOL.md section 5.9), which is also what the server
/// substitutes for a seat that times out: each card goes to the row with
/// the most remaining capacity, ties toward the back (`bottom`, then
/// `middle`, then `top`). The first `state.place` of `state.cards_to_place`
/// are placed and the next `state.must_discard` are discarded, so the result
/// is legal for any state the server sends.
///
/// Without a state (an `action_rejected` for a turn this session never saw)
/// it is an empty placement, which the server rejects and then applies its
/// own default for, as it would for no answer at all.
pub fn _ofc_default_placement(state: Option<&GameState>) -> Action {
    let Some(state) = state else {
        return Action::place(Vec::<(String, String)>::new(), Vec::<String>::new());
    };
    let mut capacity = state.row_capacity.clone();
    // Back to front, then any row name this release has not heard of.
    let mut order: Vec<String> = ["bottom", "middle", "top"]
        .into_iter()
        .filter(|name| capacity.contains_key(*name))
        .map(String::from)
        .collect();
    for name in capacity.keys() {
        if !order.contains(name) {
            order.push(name.clone());
        }
    }

    let cards = &state.cards_to_place;
    let place = usize::try_from(state.place).unwrap_or(0).min(cards.len());
    let mut placements: Vec<(String, String)> = Vec::new();
    for card in &cards[..place] {
        let mut best: Option<&String> = None;
        for row in &order {
            let free = capacity.get(row).copied().unwrap_or(0);
            let best_free = best.and_then(|b| capacity.get(b).copied()).unwrap_or(0);
            // Strictly greater, so a tie keeps the row nearer the back.
            if free > best_free {
                best = Some(row);
            }
        }
        let Some(best) = best else { break };
        placements.push((card.clone(), best.clone()));
        if let Some(free) = capacity.get_mut(best) {
            *free -= 1;
        }
    }
    let discard_end = place
        .saturating_add(usize::try_from(state.must_discard).unwrap_or(0))
        .min(cards.len());
    Action::place(placements, &cards[place..discard_end])
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
