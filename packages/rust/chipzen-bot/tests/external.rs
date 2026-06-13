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
struct ScriptedReader {
    frames: Vec<Option<String>>,
    idx: usize,
    block_when_done: bool,
}

#[async_trait]
impl MessageReader for ScriptedReader {
    async fn next(&mut self) -> Result<Option<String>, Error> {
        if self.idx < self.frames.len() {
            let frame = self.frames[self.idx].clone();
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
    );
    assert_eq!(out, "wss://staging.chipzen.ai/ws/external/match/m1/p1");
}

#[test]
fn test_resolve_gateway_url_passes_through_absolute_url() {
    let full = "wss://other.example/ws/external/match/m1/p1";
    assert_eq!(
        resolve_gateway_url("wss://staging.chipzen.ai/x", full),
        full
    );
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
