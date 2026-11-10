//! Tests for the external-API remote-play path (`run_external_bot`),
//! mirroring the Python suite `tests/test_external.py`.
//!
//! The websocket layer is faked: a scripted [`LobbyTransport`] routes lobby
//! vs gateway connections and replays canned frames, so the whole
//! lobby → matched → gateway → match_end flow (plus reconnect-resume) runs
//! without a server — the Rust analogue of the Python suite's monkeypatched
//! `websockets.connect`.

use async_trait::async_trait;
use chipzen_bot::{
    _set_lobby_recv_timeout_ms, bot_token_subprotocols, resolve_gateway_url,
    run_external_with_transport, Action, Bot, ChipzenConfig, EnvName, Error, GameState,
    LobbyTransport, MatchResult, MessageReader, MessageWriter, RetryPolicy, RunExternalOptions,
    BOT_TOKEN_SUBPROTOCOL,
};
use serde_json::{json, Value};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::sync::Semaphore;
use tokio::time::Instant;

const LOBBY_URL: &str = "wss://staging.chipzen.ai/ws/external/bot/test-bot-uuid";

// ---------------------------------------------------------------------------
// Bots
// ---------------------------------------------------------------------------

/// Records lifecycle hooks + decision latencies; checks when it can.
#[derive(Clone, Default)]
struct CollectBot {
    events: Arc<Mutex<Vec<String>>>,
    latencies: Arc<Mutex<Vec<f64>>>,
}

impl Bot for CollectBot {
    fn decide(&mut self, state: &GameState) -> Action {
        self.events.lock().unwrap().push("decide".into());
        if state.valid_actions.iter().any(|a| a == "check") {
            Action::Check
        } else {
            Action::Fold
        }
    }
    fn on_match_start(&mut self, _msg: &Value) {
        self.events.lock().unwrap().push("match_start".into());
    }
    fn on_match_end(&mut self, _msg: &Value) {
        self.events.lock().unwrap().push("match_end".into());
    }
    fn on_decision_latency(&mut self, latency_ms: f64) {
        self.latencies.lock().unwrap().push(latency_ms);
    }
}

struct CrashBot;
impl Bot for CrashBot {
    fn decide(&mut self, _state: &GameState) -> Action {
        panic!("boom");
    }
}

// ---------------------------------------------------------------------------
// Canned frames
// ---------------------------------------------------------------------------

fn server_hello() -> Value {
    json!({ "type": "hello", "selected_version": "1.0", "game_type": "nlhe_6max" })
}

/// The lobby's server hello (endpoint=lobby); no client hello on the lobby leg.
fn server_hello_lobby() -> Value {
    json!({ "type": "hello", "endpoint": "lobby" })
}

fn match_start() -> Value {
    json!({
        "type": "match_start",
        "match_id": "m1",
        "seats": [{"seat": 0, "is_self": true}, {"seat": 1, "is_self": false}],
        "turn_timeout_ms": 5000,
    })
}

fn turn_request() -> Value {
    json!({
        "type": "turn_request",
        "match_id": "m1",
        "request_id": "req_1",
        "valid_actions": ["fold", "call", "raise"],
        "state": {
            "phase": "preflop",
            "board": [],
            "your_hole_cards": ["Ah", "Kd"],
            "pot": 15,
            "to_call": 5,
            "min_raise": 20,
            "max_raise": 995,
        },
    })
}

fn match_end() -> Value {
    json!({ "type": "match_end", "match_id": "m1", "reason": "complete" })
}

fn full_match() -> Vec<Value> {
    vec![server_hello(), match_start(), turn_request(), match_end()]
}

fn matched() -> Value {
    json!({
        "type": "matched",
        "match_id": "m1",
        "participant_id": "p1",
        "gateway_ws_url": "/ws/external/match/m1/p1",
        "rated": false,
    })
}

// ---------------------------------------------------------------------------
// Scripted transport
// ---------------------------------------------------------------------------

/// Lobby reader: replays scripted frames, then parks forever (the loop's
/// recv-timeout wakes it to re-check the stop signal). A `None` entry in the
/// script simulates a socket drop (returns `Ok(None)` = clean close).
///
/// A few entries are test steps rather than frames (see [`close_with`],
/// [`hold`], [`release`] and [`wait_ms`]).
struct ScriptedReader {
    frames: Vec<Option<String>>,
    idx: usize,
    block_when_done: bool,
    close_code: Option<u16>,
    /// The gate [`hold`] waits on and [`release`] opens (shared per transport).
    gate: Arc<Semaphore>,
    /// The end of the [`wait_ms`] step in progress. Kept on the reader so a
    /// wait cut short by the lobby's recv timeout resumes, not restarts.
    wait_until: Option<Instant>,
}

/// A gateway script entry: the server closes the socket with this code.
fn close_with(code: u16) -> Value {
    json!({ "__close": code })
}

/// A gateway script entry: a healthy mid-match socket that sends nothing
/// more until a lobby script runs [`release`].
fn hold() -> Value {
    json!({ "__hold": true })
}

/// A lobby script entry: lets a [`hold`] gateway go on.
fn release() -> Value {
    json!({ "__release": true })
}

/// A lobby script entry: deliver nothing for `ms` milliseconds, so the match
/// tasks run (and, with a 1 ms backoff, reconnect or give up) meanwhile.
fn wait_ms(ms: u64) -> Value {
    json!({ "__wait_ms": ms })
}

#[async_trait]
impl MessageReader for ScriptedReader {
    async fn next(&mut self) -> Result<Option<String>, Error> {
        while self.idx < self.frames.len() {
            let frame = self.frames[self.idx].clone();
            let step = frame
                .as_deref()
                .and_then(|f| serde_json::from_str::<Value>(f).ok())
                .unwrap_or(Value::Null);
            if let Some(code) = step.get("__close").and_then(Value::as_u64) {
                self.close_code = Some(code as u16);
                self.idx = self.frames.len(); // nothing arrives after a close
                return Ok(None);
            }
            if step.get("__hold").is_some() {
                self.gate.acquire().await.unwrap().forget();
                self.idx += 1;
                continue;
            }
            if step.get("__release").is_some() {
                self.gate.add_permits(1);
                self.idx += 1;
                continue;
            }
            if let Some(ms) = step.get("__wait_ms").and_then(Value::as_u64) {
                let until = *self
                    .wait_until
                    .get_or_insert_with(|| Instant::now() + Duration::from_millis(ms));
                tokio::time::sleep_until(until).await;
                self.wait_until = None;
                self.idx += 1;
                continue;
            }
            self.idx += 1;
            // `None` in the script = drop.
            return Ok(frame);
        }
        if self.block_when_done {
            // Park until the session's recv-timeout cancels us and the loop
            // re-checks `stop`. Mirrors the Python fake's `sleep(3600)`.
            std::future::pending::<()>().await;
            unreachable!();
        }
        Ok(None)
    }

    fn close_code(&self) -> Option<u16> {
        self.close_code
    }
}

#[derive(Clone, Default)]
struct CapturingWriter {
    sent: Arc<Mutex<Vec<Value>>>,
}

#[async_trait]
impl MessageWriter for CapturingWriter {
    async fn send(&mut self, payload: String) -> Result<(), Error> {
        let parsed: Value = serde_json::from_str(&payload).unwrap_or(Value::Null);
        self.sent.lock().unwrap().push(parsed);
        Ok(())
    }
}

#[derive(Clone)]
struct Calls {
    lobby_urls: Arc<Mutex<Vec<String>>>,
    gateway_urls: Arc<Mutex<Vec<String>>>,
    subprotocols: Arc<Mutex<Vec<Vec<String>>>>,
    user_agents: Arc<Mutex<Vec<String>>>,
    lobby_sent: Arc<Mutex<Vec<Value>>>,
}

impl Calls {
    fn new() -> Self {
        Self {
            lobby_urls: Arc::new(Mutex::new(Vec::new())),
            gateway_urls: Arc::new(Mutex::new(Vec::new())),
            subprotocols: Arc::new(Mutex::new(Vec::new())),
            user_agents: Arc::new(Mutex::new(Vec::new())),
            lobby_sent: Arc::new(Mutex::new(Vec::new())),
        }
    }
}

/// Scripted transport: pops a fresh lobby/gateway script per connect, so
/// successive connects can behave differently (reconnect tests). The shared
/// `lobby_sent` buffer aggregates everything sent on every lobby leg.
struct ScriptedTransport {
    lobby_scripts: Mutex<std::vec::IntoIter<Vec<Option<Value>>>>,
    gateway_scripts: Mutex<std::vec::IntoIter<Vec<Value>>>,
    calls: Calls,
    gate: Arc<Semaphore>,
}

impl ScriptedTransport {
    fn new(
        lobby_scripts: Vec<Vec<Option<Value>>>,
        gateway_scripts: Vec<Vec<Value>>,
    ) -> (Arc<Self>, Calls) {
        let calls = Calls::new();
        let t = Arc::new(Self {
            lobby_scripts: Mutex::new(lobby_scripts.into_iter()),
            gateway_scripts: Mutex::new(gateway_scripts.into_iter()),
            calls: calls.clone(),
            gate: Arc::new(Semaphore::new(0)),
        });
        (t, calls)
    }
}

#[async_trait]
impl LobbyTransport for ScriptedTransport {
    async fn connect_lobby(
        &self,
        url: &str,
        user_agent: &str,
    ) -> Result<(Box<dyn MessageReader>, Box<dyn MessageWriter>), Error> {
        self.calls.lobby_urls.lock().unwrap().push(url.to_string());
        self.calls
            .user_agents
            .lock()
            .unwrap()
            .push(user_agent.to_string());
        let script = self
            .lobby_scripts
            .lock()
            .unwrap()
            .next()
            .expect("ran out of lobby scripts");
        let frames = script
            .into_iter()
            .map(|f| f.map(|v| v.to_string()))
            .collect();
        let reader = ScriptedReader {
            frames,
            idx: 0,
            block_when_done: true,
            close_code: None,
            gate: Arc::clone(&self.gate),
            wait_until: None,
        };
        // The writer aggregates into the shared lobby_sent buffer.
        let writer = CapturingWriter {
            sent: Arc::clone(&self.calls.lobby_sent),
        };
        Ok((Box::new(reader), Box::new(writer)))
    }

    async fn connect_gateway(
        &self,
        url: &str,
        token: &str,
        user_agent: &str,
    ) -> Result<(Box<dyn MessageReader>, Box<dyn MessageWriter>), Error> {
        self.calls
            .gateway_urls
            .lock()
            .unwrap()
            .push(url.to_string());
        self.calls
            .subprotocols
            .lock()
            .unwrap()
            .push(bot_token_subprotocols(token));
        self.calls
            .user_agents
            .lock()
            .unwrap()
            .push(user_agent.to_string());
        let script = self
            .gateway_scripts
            .lock()
            .unwrap()
            .next()
            .expect("ran out of gateway scripts");
        let frames = script.into_iter().map(|v| Some(v.to_string())).collect();
        let reader = ScriptedReader {
            frames,
            idx: 0,
            block_when_done: false, // gateway closes cleanly when exhausted
            close_code: None,
            gate: Arc::clone(&self.gate),
            wait_until: None,
        };
        let writer = CapturingWriter::default();
        Ok((Box::new(reader), Box::new(writer)))
    }
}

/// Build a transport with a single lobby script and a default-or-given
/// gateway script. `None` entries in the lobby script are drops.
fn install(
    lobby_frames: Vec<Option<Value>>,
    gateway_frames: Option<Vec<Value>>,
) -> (Arc<ScriptedTransport>, Calls) {
    _set_lobby_recv_timeout_ms(5);
    ScriptedTransport::new(
        vec![lobby_frames],
        vec![gateway_frames.unwrap_or_else(full_match)],
    )
}

/// Helper: run with the bot factory + options + transport.
async fn run<B, F>(
    factory: F,
    options: RunExternalOptions,
    transport: Arc<ScriptedTransport>,
) -> Result<Vec<MatchResult>, Error>
where
    B: Bot,
    F: Fn() -> B + Send + Sync + 'static,
{
    run_external_with_transport(factory, options, transport).await
}

fn opts_with_token() -> RunExternalOptions {
    RunExternalOptions {
        url: Some(LOBBY_URL.to_string()),
        token: Some("cz_extbot_x".to_string()),
        ..Default::default()
    }
}

// ---------------------------------------------------------------------------
// URL helpers (pure)
// ---------------------------------------------------------------------------

#[test]
fn test_bot_token_subprotocols() {
    assert_eq!(
        bot_token_subprotocols("cz_extbot_x"),
        vec![BOT_TOKEN_SUBPROTOCOL.to_string(), "cz_extbot_x".to_string()]
    );
}

#[test]
fn test_resolve_gateway_url_joins_path_to_lobby_origin() {
    let out = resolve_gateway_url(
        "wss://staging.chipzen.ai/ws/external/bot/abc",
        "/ws/external/match/m1/p1",
    )
    .unwrap();
    assert_eq!(out, "wss://staging.chipzen.ai/ws/external/match/m1/p1");
}

#[test]
fn test_resolve_gateway_url_passes_through_same_origin_absolute_url() {
    let full = "wss://staging.chipzen.ai/ws/external/match/m1/p1";
    assert_eq!(
        resolve_gateway_url("wss://staging.chipzen.ai/x", full).unwrap(),
        full
    );
}

#[test]
fn test_resolve_gateway_url_rejects_cross_origin() {
    // The bot token must not follow a redirect to another host.
    let err = resolve_gateway_url(
        "wss://staging.chipzen.ai/x",
        "wss://attacker.example/ws/external/match/m1/p1",
    )
    .unwrap_err();
    assert!(matches!(err, chipzen_bot::Error::UntrustedGateway(_)));
}

#[test]
fn test_resolve_gateway_url_rejects_wss_to_ws_downgrade() {
    let err = resolve_gateway_url(
        "wss://staging.chipzen.ai/x",
        "ws://staging.chipzen.ai/ws/external/match/m1/p1",
    )
    .unwrap_err();
    assert!(matches!(err, chipzen_bot::Error::UntrustedGateway(_)));
}

// ---------------------------------------------------------------------------
// Connection resolution + validation
// ---------------------------------------------------------------------------

#[tokio::test]
async fn test_requires_a_token() {
    let cfg = ChipzenConfig {
        token: None,
        ..Default::default()
    };
    let opts = RunExternalOptions {
        url: Some(LOBBY_URL.to_string()),
        config: Some(cfg),
        ..Default::default()
    };
    let (transport, _) = install(vec![], None);
    let err = run(CollectBot::default, opts, transport)
        .await
        .expect_err("expected a token error");
    assert!(
        format!("{err}").contains("requires an external-API token"),
        "unexpected error: {err}"
    );
}

#[tokio::test]
async fn test_requires_url_or_bot_id() {
    let cfg = ChipzenConfig {
        token: Some("cz_extbot_x".to_string()),
        bot_id: None,
        ..Default::default()
    };
    let opts = RunExternalOptions {
        config: Some(cfg),
        ..Default::default()
    };
    let (transport, _) = install(vec![], None);
    let err = run(CollectBot::default, opts, transport)
        .await
        .expect_err("expected a url/bot_id error");
    assert!(
        format!("{err}").contains("needs a lobby URL"),
        "unexpected error: {err}"
    );
}

#[tokio::test]
async fn test_token_from_config_when_no_kwarg() {
    let cfg = ChipzenConfig {
        token: Some("cz_extbot_from_config".to_string()),
        ..Default::default()
    };
    let opts = RunExternalOptions {
        url: Some(LOBBY_URL.to_string()),
        config: Some(cfg),
        max_matches: Some(1),
        ..Default::default()
    };
    let (transport, calls) = install(vec![Some(server_hello_lobby()), Some(matched())], None);
    run(CollectBot::default, opts, transport).await.unwrap();
    let auth = &calls.lobby_sent.lock().unwrap()[0];
    assert_eq!(auth["type"], "authenticate");
    assert_eq!(auth["token"], "cz_extbot_from_config");
}

#[tokio::test]
async fn test_explicit_token_overrides_config() {
    let cfg = ChipzenConfig {
        token: Some("cz_extbot_config".to_string()),
        ..Default::default()
    };
    let opts = RunExternalOptions {
        url: Some(LOBBY_URL.to_string()),
        token: Some("cz_extbot_explicit".to_string()),
        config: Some(cfg),
        max_matches: Some(1),
        ..Default::default()
    };
    let (transport, calls) = install(vec![Some(server_hello_lobby()), Some(matched())], None);
    run(CollectBot::default, opts, transport).await.unwrap();
    assert_eq!(
        calls.lobby_sent.lock().unwrap()[0]["token"],
        "cz_extbot_explicit"
    );
}

#[tokio::test]
async fn test_bot_id_plus_env_builds_lobby_url() {
    let cfg = ChipzenConfig {
        token: Some("cz_extbot_x".to_string()),
        ..Default::default()
    };
    let opts = RunExternalOptions {
        bot_id: Some("abc".to_string()),
        env: Some(EnvName::Staging),
        config: Some(cfg),
        max_matches: Some(1),
        ..Default::default()
    };
    let (transport, calls) = install(vec![Some(server_hello_lobby()), Some(matched())], None);
    run(CollectBot::default, opts, transport).await.unwrap();
    assert_eq!(
        calls.lobby_urls.lock().unwrap()[0],
        "wss://staging.chipzen.ai/ws/external/bot/abc"
    );
}

// ---------------------------------------------------------------------------
// Lobby -> gateway happy path
// ---------------------------------------------------------------------------

#[tokio::test]
async fn test_plays_one_match_end_to_end() {
    let bot = CollectBot::default();
    let events = Arc::clone(&bot.events);
    let latencies = Arc::clone(&bot.latencies);
    let opts = RunExternalOptions {
        max_matches: Some(1),
        ..opts_with_token()
    };
    let (transport, calls) = install(vec![Some(server_hello_lobby()), Some(matched())], None);
    // Reuse the SAME bot instance across matches (factory clones the handle).
    let results = run(move || bot.clone(), opts, transport).await.unwrap();

    assert_eq!(results.len(), 1);
    assert_eq!(results[0].end.as_ref().unwrap()["reason"], "complete");
    assert_eq!(results[0].match_id.as_deref(), Some("m1"));

    assert_eq!(
        *events.lock().unwrap(),
        vec!["match_start", "decide", "match_end"]
    );
    assert_eq!(latencies.lock().unwrap().len(), 1);

    // Gateway leg carried the token in the Sec-WebSocket-Protocol offer.
    assert_eq!(
        calls.subprotocols.lock().unwrap()[0],
        vec![BOT_TOKEN_SUBPROTOCOL.to_string(), "cz_extbot_x".to_string()]
    );
    // Non-default UA.
    assert!(calls.user_agents.lock().unwrap()[0].starts_with("chipzen-sdk-rust/"));
}

#[tokio::test]
async fn test_lobby_answers_ping_with_pong() {
    let opts = RunExternalOptions {
        max_matches: Some(1),
        ..opts_with_token()
    };
    let (transport, calls) = install(
        vec![
            Some(server_hello_lobby()),
            Some(json!({ "type": "ping" })),
            Some(matched()),
        ],
        None,
    );
    run(CollectBot::default, opts, transport).await.unwrap();
    let sent_types: Vec<String> = calls
        .lobby_sent
        .lock()
        .unwrap()
        .iter()
        .map(|m| m["type"].as_str().unwrap_or("").to_string())
        .collect();
    assert!(
        sent_types.iter().any(|t| t == "pong"),
        "no pong in {sent_types:?}"
    );
}

#[tokio::test]
async fn test_evict_ends_session_with_no_match() {
    let (transport, _) = install(
        vec![Some(server_hello_lobby()), Some(json!({ "type": "evict" }))],
        None,
    );
    let results = run(CollectBot::default, opts_with_token(), transport)
        .await
        .unwrap();
    assert!(results.is_empty());
}

#[tokio::test]
async fn test_max_matches_stops_after_one() {
    let opts = RunExternalOptions {
        max_matches: Some(1),
        ..opts_with_token()
    };
    let (transport, _) = install(vec![Some(server_hello_lobby()), Some(matched())], None);
    let results = run(CollectBot::default, opts, transport).await.unwrap();
    assert_eq!(results.len(), 1);
}

// ---------------------------------------------------------------------------
// safe_mode
// ---------------------------------------------------------------------------

#[tokio::test]
async fn test_safe_mode_false_propagates_bot_error() {
    let opts = RunExternalOptions {
        safe_mode: Some(false),
        max_matches: Some(1),
        ..opts_with_token()
    };
    let (transport, _) = install(vec![Some(server_hello_lobby()), Some(matched())], None);
    let err = run(|| CrashBot, opts, transport)
        .await
        .expect_err("expected BotDecision error");
    assert!(matches!(err, Error::BotDecision(_)), "got {err:?}");
}

#[tokio::test]
async fn test_safe_mode_true_folds_bot_error() {
    let opts = RunExternalOptions {
        safe_mode: Some(true),
        max_matches: Some(1),
        ..opts_with_token()
    };
    let (transport, _) = install(vec![Some(server_hello_lobby()), Some(matched())], None);
    let results = run(|| CrashBot, opts, transport).await.unwrap();
    assert_eq!(results.len(), 1);
    assert_eq!(results[0].end.as_ref().unwrap()["reason"], "complete");
}

// ---------------------------------------------------------------------------
// Reconnect behavior (gateway mid-match drop + lobby drop)
// ---------------------------------------------------------------------------

#[tokio::test]
async fn test_gateway_reconnects_and_resumes() {
    // First gateway connect drops mid-match (no match_end); the SDK reconnects
    // and the second connect plays to match_end. Same bot instance is reused.
    _set_lobby_recv_timeout_ms(5);
    let policy = RetryPolicy::new(5, 1, 1, 2.0).unwrap();
    let (transport, calls) = ScriptedTransport::new(
        vec![vec![Some(server_hello_lobby()), Some(matched())]],
        vec![
            vec![server_hello(), match_start(), turn_request()], // drops, no match_end
            vec![server_hello(), match_end()],                   // reconnect → completes
        ],
    );
    let opts = RunExternalOptions {
        retry_policy: Some(policy),
        max_matches: Some(1),
        ..opts_with_token()
    };
    let results = run(CollectBot::default, opts, transport).await.unwrap();
    assert_eq!(calls.gateway_urls.lock().unwrap().len(), 2); // reconnected once
    assert_eq!(results.len(), 1);
    assert_eq!(results[0].end.as_ref().unwrap()["reason"], "complete");
}

#[tokio::test]
async fn test_gateway_reconnect_budget_exhausted_abandons_match() {
    // Gateway never reaches match_end. After max_reconnect_attempts the match
    // is abandoned (end=None) rather than hanging or looping forever.
    _set_lobby_recv_timeout_ms(5);
    let policy = RetryPolicy::new(2, 1, 1, 2.0).unwrap();
    let (transport, calls) = ScriptedTransport::new(
        vec![vec![Some(server_hello_lobby()), Some(matched())]],
        vec![
            vec![server_hello(), match_start()], // initial
            vec![server_hello(), match_start()], // retry 1
            vec![server_hello(), match_start()], // retry 2
        ],
    );
    let opts = RunExternalOptions {
        retry_policy: Some(policy),
        max_matches: Some(1),
        ..opts_with_token()
    };
    let results = run(CollectBot::default, opts, transport).await.unwrap();
    assert_eq!(calls.gateway_urls.lock().unwrap().len(), 3); // initial + 2 retries
    assert_eq!(results.len(), 1);
    assert!(results[0].end.is_none());
}

#[tokio::test]
async fn test_lobby_reconnects_after_close() {
    // The lobby socket drops after connecting; the SDK reconnects the lobby
    // and then plays the match it's dispatched on the second session.
    _set_lobby_recv_timeout_ms(5);
    let policy = RetryPolicy::new(5, 1, 1, 2.0).unwrap();
    let (transport, calls) = ScriptedTransport::new(
        vec![
            vec![Some(server_hello_lobby()), None], // connects, then drops
            vec![Some(server_hello_lobby()), Some(matched())], // reconnect → a match
        ],
        vec![full_match()],
    );
    let opts = RunExternalOptions {
        retry_policy: Some(policy),
        max_matches: Some(1),
        ..opts_with_token()
    };
    let results = run(CollectBot::default, opts, transport).await.unwrap();
    assert_eq!(calls.lobby_urls.lock().unwrap().len(), 2); // lobby reconnected
    assert_eq!(results.len(), 1);
    assert_eq!(results[0].end.as_ref().unwrap()["reason"], "complete");
}

// ---------------------------------------------------------------------------
// A match seat taken over by another connection of the same bot (#242)
// ---------------------------------------------------------------------------
//
// A newer socket for the same seat takes the match over (chipzen-ai/Chipzen#6734).
// The old socket gets an `error` frame (`replaced_by_new_connection`) and then a
// 4000 close. Reconnecting would take the seat back, and two live copies of one
// bot would trade it forever, so the match's task ends instead. (The one log
// line goes to stderr; the Python and JavaScript suites assert its text.)

fn matched_for(match_id: &str, resume: bool) -> Value {
    json!({
        "type": "matched",
        "match_id": match_id,
        "participant_id": "p1",
        "gateway_ws_url": format!("/ws/external/match/{match_id}/p1"),
        "rated": false,
        "resume": resume,
    })
}

fn replaced_error_frame() -> Value {
    json!({
        "type": "error",
        "code": "replaced_by_new_connection",
        "message": "a newer connection for this seat replaced this one",
    })
}

/// Run with a first lobby session that dispatches `first` and then drops (the
/// reconnect backoff lets the match task settle), and a second that sends
/// `second`. A trailing evict ends the session either way.
async fn run_two_lobby_sessions(
    first: Vec<Value>,
    second: Vec<Value>,
    gateways: Vec<Vec<Value>>,
) -> (Vec<MatchResult>, Calls) {
    _set_lobby_recv_timeout_ms(5);
    let mut lobby1 = vec![Some(server_hello_lobby())];
    lobby1.extend(first.into_iter().map(Some));
    lobby1.push(None);
    let mut lobby2 = vec![Some(server_hello_lobby())];
    lobby2.extend(second.into_iter().map(Some));
    lobby2.push(Some(json!({ "type": "evict" })));
    let (transport, calls) = ScriptedTransport::new(vec![lobby1, lobby2], gateways);
    let opts = RunExternalOptions {
        retry_policy: Some(RetryPolicy::new(5, 1, 1, 2.0).unwrap()),
        max_matches: Some(1),
        ..opts_with_token()
    };
    let results = run(CollectBot::default, opts, transport).await.unwrap();
    (results, calls)
}

fn gateway_ids(calls: &Calls) -> Vec<String> {
    calls
        .gateway_urls
        .lock()
        .unwrap()
        .iter()
        .map(|u| u.rsplit("/match/").next().unwrap().to_string())
        .collect()
}

#[tokio::test]
async fn test_a_4000_close_ends_the_match_without_reconnecting() {
    let (results, calls) = run_two_lobby_sessions(
        vec![matched()],
        vec![],
        vec![vec![
            server_hello(),
            match_start(),
            turn_request(),
            close_with(4000),
        ]],
    )
    .await;
    // A reconnect would open a second gateway (and run out of scripts).
    assert_eq!(gateway_ids(&calls), vec!["m1/p1"]);
    // Not a finished match, and no bogus result entry.
    assert!(results.is_empty());
}

#[tokio::test]
async fn test_the_error_frame_then_close_ends_the_match_without_reconnecting() {
    let (results, calls) = run_two_lobby_sessions(
        vec![matched()],
        vec![],
        vec![vec![
            server_hello(),
            match_start(),
            replaced_error_frame(),
            close_with(4000),
        ]],
    )
    .await;
    assert_eq!(gateway_ids(&calls), vec!["m1/p1"]);
    assert!(results.is_empty());
}

#[tokio::test]
async fn test_the_error_frame_before_the_hello_ends_the_match_without_reconnecting() {
    // Taken over during the handshake: the error frame comes in place of the hello.
    let (results, calls) = run_two_lobby_sessions(
        vec![matched()],
        vec![],
        vec![vec![replaced_error_frame(), close_with(4000)]],
    )
    .await;
    assert_eq!(gateway_ids(&calls), vec!["m1/p1"]);
    assert!(results.is_empty());
}

#[tokio::test]
async fn test_other_drops_still_reconnect() {
    for drop in [None, Some(1011), Some(4001)] {
        _set_lobby_recv_timeout_ms(5);
        let mut first = vec![server_hello(), match_start(), turn_request()];
        if let Some(code) = drop {
            first.push(close_with(code));
        }
        let (transport, calls) = ScriptedTransport::new(
            vec![vec![Some(server_hello_lobby()), Some(matched())]],
            vec![first, vec![server_hello(), match_end()]],
        );
        let opts = RunExternalOptions {
            retry_policy: Some(RetryPolicy::new(5, 1, 1, 2.0).unwrap()),
            max_matches: Some(1),
            ..opts_with_token()
        };
        let results = run(CollectBot::default, opts, transport).await.unwrap();
        assert_eq!(calls.gateway_urls.lock().unwrap().len(), 2, "{drop:?}");
        assert_eq!(results[0].end.as_ref().unwrap()["reason"], "complete");
    }
}

#[tokio::test]
async fn test_after_a_takeover_a_new_match_plays_and_the_old_one_is_not_retaken() {
    // The reconnected lobby re-sends matched for m1 (the bot still holds that
    // seat, on the other connection): taking it would steal it back. m2 is a
    // new match and plays.
    let (results, calls) = run_two_lobby_sessions(
        vec![matched_for("m1", false)],
        vec![matched_for("m1", true), matched_for("m2", false)],
        vec![
            vec![server_hello(), match_start(), close_with(4000)],
            full_match(),
        ],
    )
    .await;
    assert_eq!(gateway_ids(&calls), vec!["m1/p1", "m2/p1"]);
    assert_eq!(results.len(), 1);
    assert_eq!(results[0].end.as_ref().unwrap()["reason"], "complete");
}

// ---------------------------------------------------------------------------
// A repeat `matched` for a match still being played (#241)
// ---------------------------------------------------------------------------
//
// On lobby connect the platform re-sends `matched` (`resume: true`) for every
// match the bot holds a seat in, so that a RESTARTED process can pick its match
// back up. A process that only lost its lobby socket is still playing that match
// on its own gateway socket; opening a second one gets `duplicate_participant`.
// Mirrors the Python and JavaScript tests added for #230.

/// A full match whose gateway stays open, healthy, before `match_end` until
/// a lobby script runs [`release`].
fn held_full_match() -> Vec<Value> {
    vec![
        server_hello(),
        match_start(),
        turn_request(),
        hold(),
        match_end(),
    ]
}

/// A bot factory that keeps every bot it builds (each clone shares its
/// events), so a test can count them.
#[allow(clippy::type_complexity)]
fn counting_factory() -> (
    Arc<Mutex<Vec<CollectBot>>>,
    impl Fn() -> CollectBot + Send + Sync + 'static,
) {
    let made: Arc<Mutex<Vec<CollectBot>>> = Arc::new(Mutex::new(Vec::new()));
    let sink = Arc::clone(&made);
    let factory = move || {
        let bot = CollectBot::default();
        sink.lock().unwrap().push(bot.clone());
        bot
    };
    (made, factory)
}

fn evict() -> Value {
    json!({ "type": "evict" })
}

/// The reported sequence: the lobby drops mid-match, the reconnected lobby
/// re-dispatches the match, and the bot must stay on the gateway it has.
async fn lobby_reconnect_mid_match_keeps_the_one_gateway(max_matches: Option<u64>) {
    _set_lobby_recv_timeout_ms(5);
    let (transport, calls) = ScriptedTransport::new(
        vec![
            vec![Some(server_hello_lobby()), Some(matched()), None],
            vec![
                Some(server_hello_lobby()),
                Some(matched_for("m1", true)),
                Some(release()), // finish the match
                Some(wait_ms(50)),
                Some(evict()),
            ],
        ],
        vec![held_full_match()],
    );
    let opts = RunExternalOptions {
        retry_policy: Some(RetryPolicy::new(5, 1, 1, 2.0).unwrap()),
        max_matches,
        ..opts_with_token()
    };
    let (made, factory) = counting_factory();
    let results = run(factory, opts, transport).await.unwrap();

    assert_eq!(calls.lobby_urls.lock().unwrap().len(), 2); // the lobby did reconnect
    assert_eq!(gateway_ids(&calls), vec!["m1/p1"]); // ...and no second gateway opened
    let made = made.lock().unwrap();
    assert_eq!(made.len(), 1); // no bot built for the repeat frame
    assert_eq!(results.len(), 1); // the ignored frame is not a played match
    assert_eq!(results[0].end.as_ref().unwrap()["reason"], "complete");
    // The original match played out.
    assert_eq!(
        made[0].events.lock().unwrap().last().map(String::as_str),
        Some("match_end")
    );
}

#[tokio::test]
async fn test_lobby_reconnect_mid_match_keeps_the_one_gateway_one_match() {
    lobby_reconnect_mid_match_keeps_the_one_gateway(Some(1)).await;
}

#[tokio::test]
async fn test_lobby_reconnect_mid_match_keeps_the_one_gateway_long_running() {
    lobby_reconnect_mid_match_keeps_the_one_gateway(None).await;
}

#[tokio::test]
async fn test_repeat_matched_on_the_same_lobby_socket_is_ignored() {
    _set_lobby_recv_timeout_ms(5);
    let (transport, calls) = ScriptedTransport::new(
        vec![vec![
            Some(server_hello_lobby()),
            Some(matched()),
            Some(matched()),
            Some(release()),
            Some(wait_ms(50)),
        ]],
        vec![held_full_match()],
    );
    let opts = RunExternalOptions {
        max_matches: Some(1),
        ..opts_with_token()
    };
    let (made, factory) = counting_factory();
    let results = run(factory, opts, transport).await.unwrap();
    assert_eq!(gateway_ids(&calls), vec!["m1/p1"]);
    assert_eq!(made.lock().unwrap().len(), 1);
    assert_eq!(results.len(), 1);
    assert_eq!(results[0].end.as_ref().unwrap()["reason"], "complete");
}

#[tokio::test]
async fn test_new_match_id_during_a_live_match_starts_normally() {
    // Overlapping matches are supported: only the SAME id is a repeat.
    _set_lobby_recv_timeout_ms(5);
    let (transport, calls) = ScriptedTransport::new(
        vec![vec![
            Some(server_hello_lobby()),
            Some(matched_for("m1", false)),
            Some(matched_for("m2", false)),
            Some(wait_ms(50)),
            Some(release()), // finish the held match
            Some(wait_ms(50)),
            Some(evict()), // ends the session even if m2 was ignored
        ]],
        // Whichever match connects first gets the held gateway; m1 is live
        // when m2 arrives either way, as its task has not even started.
        vec![held_full_match(), full_match()],
    );
    let opts = RunExternalOptions {
        max_matches: Some(2),
        ..opts_with_token()
    };
    let (made, factory) = counting_factory();
    let results = run(factory, opts, transport).await.unwrap();
    let mut ids = gateway_ids(&calls);
    ids.sort();
    assert_eq!(ids, vec!["m1/p1", "m2/p1"]);
    assert_eq!(made.lock().unwrap().len(), 2);
    assert_eq!(results.len(), 2);
}

#[tokio::test]
async fn test_matched_after_the_task_gave_up_plays_again() {
    // "Still playing" means a live task. If this session abandoned the match
    // (reconnect budget spent) and the platform still holds the seat, a later
    // re-dispatch must get a fresh attempt, not be ignored.
    _set_lobby_recv_timeout_ms(5);
    let (transport, calls) = ScriptedTransport::new(
        vec![vec![
            Some(server_hello_lobby()),
            Some(matched()),
            Some(wait_ms(100)), // the match drops, retries once, gives up
            Some(matched_for("m1", true)),
            Some(wait_ms(100)),
            Some(evict()), // ends the session even if the repeat was ignored
        ]],
        vec![
            vec![server_hello(), match_start()], // drops
            vec![server_hello(), match_start()], // retry drops: abandoned
            full_match(),                        // the re-dispatch plays to match_end
        ],
    );
    let opts = RunExternalOptions {
        retry_policy: Some(RetryPolicy::new(1, 1, 1, 2.0).unwrap()),
        max_matches: Some(2),
        ..opts_with_token()
    };
    let results = run(CollectBot::default, opts, transport).await.unwrap();
    assert_eq!(calls.gateway_urls.lock().unwrap().len(), 3);
    let ends: Vec<Option<&str>> = results
        .iter()
        .map(|r| r.end.as_ref().map(|e| e["reason"].as_str().unwrap()))
        .collect();
    assert_eq!(ends, vec![None, Some("complete")]);
}
