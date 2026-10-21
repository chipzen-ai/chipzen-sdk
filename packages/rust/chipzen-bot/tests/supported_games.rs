//! The client's `supported_games` declaration (chipzen-ai/chipzen-sdk#172).
//! Mirrors the Python SDK's `tests/test_supported_games.py` and the
//! JavaScript SDK's `tests/supported-games.test.ts`.
//!
//! A bot may declare the games it can actually play; the declaration rides
//! the Layer 1 client `hello` as `supported_games`, a list of `game_type`
//! strings (`docs/protocol/LAYER2-COMMON.md` section 2). The platform reads
//! an **absent** declaration as "poker only", which is what every deployed
//! bot relies on, so this file pins two things at once:
//!
//! - declaring something puts exactly that list on the wire, under exactly
//!   that key, on the client `hello` (nowhere else); and
//! - declaring nothing leaves the `hello` frame **byte-identical** to the one
//!   the SDK has always sent: same keys, same order, same JSON text.
//!
//! The second is the load-bearing one. The server closes the socket on any
//! handshake key outside its allowlist, and it reads an absent list ("assume
//! poker") and an empty one ("supports nothing") as different answers.
//!
//! Both runners are covered: `run_bot` (the container runner) against a real
//! local WebSocket server, and `run_external_bot` over a scripted transport.

use async_trait::async_trait;
use chipzen_bot::{
    _run_session, _set_lobby_recv_timeout_ms, run_bot, run_external_with_transport, Action, Bot,
    Error, GameState, LobbyTransport, MessageReader, MessageWriter, RetryPolicy, RunBotOptions,
    RunExternalArgs, RunExternalOptions, SessionContext,
};
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Map, Value};
use std::collections::HashSet;
use std::sync::{Arc, Mutex};
use tokio::net::TcpListener;
use tokio_tungstenite::tungstenite::Message;

const MATCH_ID: &str = "m_test_abc123";

/// The platform's handshake field allowlist (`ALLOWED_HANDSHAKE_FIELDS`), the
/// same set the Python and JavaScript tests pin. A `hello` carrying any key
/// outside it is closed before the bot is seated.
const SERVER_ALLOWED_HANDSHAKE_FIELDS: &[&str] = &[
    "type",
    "match_id",
    "ticket",
    "token",
    "supported_versions",
    "client_name",
    "client_version",
    "supported_games",
];

/// The exact client `hello` text the Rust SDK has always sent (keys sorted,
/// as the `serde_json::json!` map it was built from serialized them). Frozen
/// as a literal string so a change in key ORDER fails this test too.
const FROZEN_HELLO_JSON: &str = r#"{"client_name":"chipzen-sdk-test","client_version":"0.2.0","match_id":"m_test_abc123","supported_versions":["1.0"],"type":"hello"}"#;

fn games(list: &[&str]) -> Vec<String> {
    list.iter().map(|g| (*g).to_string()).collect()
}

/// A poker table's server hello: no `game` descriptor.
fn server_hello() -> Value {
    json!({
        "type": "hello",
        "match_id": MATCH_ID,
        "seq": 1,
        "supported_versions": ["1.0"],
        "selected_version": "1.0",
        "game_type": "nlhe_6max",
        "capabilities": [],
    })
}

fn match_end() -> Value {
    json!({
        "type": "match_end",
        "match_id": MATCH_ID,
        "seq": 2,
        "reason": "complete",
        "results": [],
    })
}

struct NoopBot;
impl Bot for NoopBot {
    fn decide(&mut self, _state: &GameState) -> Action {
        Action::Fold
    }
}

/// Replays the given frames, then reports the socket closed (or parks, for
/// a lobby leg that stays open).
struct ScriptedReader {
    frames: Vec<String>,
    idx: usize,
    block_when_done: bool,
}

impl ScriptedReader {
    fn new(frames: Vec<Value>, block_when_done: bool) -> Self {
        Self {
            frames: frames.iter().map(Value::to_string).collect(),
            idx: 0,
            block_when_done,
        }
    }
}

#[async_trait]
impl MessageReader for ScriptedReader {
    async fn next(&mut self) -> Result<Option<String>, Error> {
        if let Some(frame) = self.frames.get(self.idx).cloned() {
            self.idx += 1;
            return Ok(Some(frame));
        }
        if self.block_when_done {
            // Park until the lobby's recv-timeout re-checks its stop signal.
            std::future::pending::<()>().await;
        }
        Ok(None)
    }
}

/// Keeps every frame the client sent as the raw text that went on the wire.
#[derive(Clone, Default)]
struct RawCapturingWriter {
    sent: Arc<Mutex<Vec<String>>>,
}

#[async_trait]
impl MessageWriter for RawCapturingWriter {
    async fn send(&mut self, payload: String) -> Result<(), Error> {
        self.sent.lock().unwrap().push(payload);
        Ok(())
    }
}

fn historical_ctx() -> SessionContext {
    SessionContext::new(
        MATCH_ID.to_string(),
        Some("test_token".to_string()),
        None,
        "chipzen-sdk-test".to_string(),
        "0.2.0".to_string(),
    )
}

fn session_ctx(supported_games: Option<Vec<String>>) -> SessionContext {
    SessionContext {
        supported_games,
        ..historical_ctx()
    }
}

/// Run one handshake through `_run_session` and return the raw frames sent.
async fn handshake(ctx: &SessionContext) -> Vec<String> {
    let mut reader = ScriptedReader::new(vec![server_hello()], false);
    let mut writer = RawCapturingWriter::default();
    _run_session(&mut reader, &mut writer, &mut NoopBot, ctx)
        .await
        .unwrap();
    let sent = writer.sent.lock().unwrap().clone();
    sent
}

async fn hello_text(supported_games: Option<Vec<String>>) -> String {
    handshake(&session_ctx(supported_games)).await[1].clone()
}

fn parse(text: &str) -> Map<String, Value> {
    serde_json::from_str::<Value>(text)
        .unwrap()
        .as_object()
        .unwrap()
        .clone()
}

// ---------------------------------------------------------------------------
// Default: nothing declared -> nothing on the wire
// ---------------------------------------------------------------------------

#[tokio::test]
async fn default_hello_is_byte_identical_to_the_frozen_frame() {
    let hello = hello_text(None).await;
    assert_eq!(hello, FROZEN_HELLO_JSON);
    assert!(!hello.contains("supported_games"));
}

#[tokio::test]
async fn session_context_new_declares_nothing() {
    // The historical constructor leaves the field `None`, the same as
    // explicitly passing `None`.
    let ctx = historical_ctx();
    assert_eq!(ctx.supported_games, None);
    assert_eq!(handshake(&ctx).await, handshake(&session_ctx(None)).await);
}

#[test]
fn every_options_default_declares_nothing() {
    assert_eq!(RunBotOptions::default().supported_games, None);
    assert_eq!(RunExternalOptions::default().supported_games, None);
    assert_eq!(RunExternalArgs::new().supported_games, None);
    assert_eq!(RunExternalArgs::default().supported_games, None);
}

// ---------------------------------------------------------------------------
// Declaring
// ---------------------------------------------------------------------------

#[tokio::test]
async fn declared_list_rides_the_hello_and_not_the_authenticate_frame() {
    let sent = handshake(&session_ctx(Some(games(&["poker", "draw27"])))).await;
    let authenticate = parse(&sent[0]);
    let hello = parse(&sent[1]);

    assert_eq!(authenticate["type"], "authenticate");
    assert!(!authenticate.contains_key("supported_games"));

    assert_eq!(hello["type"], "hello");
    assert_eq!(hello["supported_games"], json!(["poker", "draw27"]));
}

#[tokio::test]
async fn declared_list_is_appended_last_leaving_the_prior_bytes_untouched() {
    let hello = hello_text(Some(games(&["poker", "draw27"]))).await;
    let prefix = FROZEN_HELLO_JSON.strip_suffix('}').unwrap();
    assert_eq!(
        hello,
        format!(r#"{prefix},"supported_games":["poker","draw27"]}}"#)
    );
}

#[tokio::test]
async fn hello_keys_stay_inside_the_server_handshake_allowlist() {
    let allowed: HashSet<&str> = SERVER_ALLOWED_HANDSHAKE_FIELDS.iter().copied().collect();
    for declared in [None, Some(games(&["poker", "draw27", "ofc"]))] {
        let hello = parse(&hello_text(declared).await);
        for key in hello.keys() {
            assert!(allowed.contains(key.as_str()), "{key} is not allowlisted");
        }
    }
}

#[tokio::test]
async fn empty_list_is_sent_verbatim_rather_than_dropped() {
    // `[]` means "I support nothing". Dropping it would tell the server
    // "assume poker", the opposite of what the caller said.
    let hello = hello_text(Some(Vec::new())).await;
    assert!(hello.ends_with(r#","supported_games":[]}"#), "{hello}");
}

// ---------------------------------------------------------------------------
// The container runner: run_bot against a real local WebSocket server
// ---------------------------------------------------------------------------

/// Serve one scripted match on a local port. The task resolves with the raw
/// client hello text.
async fn serve_one_match() -> (String, tokio::task::JoinHandle<String>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let server = tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let mut ws = tokio_tungstenite::accept_async(stream).await.unwrap();
        let mut hello = String::new();
        while let Some(Ok(frame)) = ws.next().await {
            let Message::Text(text) = frame else { continue };
            let text = text.to_string();
            match parse(&text)["type"].as_str() {
                Some("authenticate") => {
                    ws.send(Message::Text(server_hello().to_string()))
                        .await
                        .unwrap();
                }
                Some("hello") => {
                    hello = text;
                    ws.send(Message::Text(match_end().to_string()))
                        .await
                        .unwrap();
                    break;
                }
                _ => {}
            }
        }
        hello
    });
    (
        format!("ws://127.0.0.1:{port}/ws/match/{MATCH_ID}/bot"),
        server,
    )
}

fn run_bot_options(supported_games: Option<Vec<String>>) -> RunBotOptions {
    RunBotOptions {
        token: Some("tok".to_string()),
        client_name: Some("chipzen-sdk-test".to_string()),
        client_version: Some("0.2.0".to_string()),
        retry_policy: RetryPolicy {
            max_reconnect_attempts: 0,
            ..RetryPolicy::default()
        },
        supported_games,
        ..RunBotOptions::default()
    }
}

#[tokio::test]
async fn run_bot_carries_a_declared_poker_and_draw27_list() {
    let (url, server) = serve_one_match().await;
    let end = run_bot(
        &url,
        NoopBot,
        run_bot_options(Some(games(&["poker", "draw27"]))),
    )
    .await
    .unwrap();
    assert_eq!(end.unwrap()["type"], "match_end");
    let hello = server.await.unwrap();
    assert_eq!(parse(&hello)["supported_games"], json!(["poker", "draw27"]));
}

#[tokio::test]
async fn run_bot_default_hello_is_the_frozen_frame() {
    let (url, server) = serve_one_match().await;
    run_bot(&url, NoopBot, run_bot_options(None)).await.unwrap();
    assert_eq!(server.await.unwrap(), FROZEN_HELLO_JSON);
}

// ---------------------------------------------------------------------------
// The external-API runner threads the same declaration to every match
// ---------------------------------------------------------------------------

/// A lobby that dispatches one match, and a gateway that plays it empty.
/// Keeps the raw frames sent on the gateway leg.
#[derive(Default)]
struct OneMatchTransport {
    gateway_sent: Arc<Mutex<Vec<String>>>,
}

#[async_trait]
impl LobbyTransport for OneMatchTransport {
    async fn connect_lobby(
        &self,
        _url: &str,
        _user_agent: &str,
    ) -> Result<(Box<dyn MessageReader>, Box<dyn MessageWriter>), Error> {
        let frames = vec![
            json!({ "type": "hello", "endpoint": "lobby" }),
            json!({
                "type": "matched",
                "match_id": MATCH_ID,
                "participant_id": "p1",
                "gateway_ws_url": format!("/ws/external/match/{MATCH_ID}/p1"),
                "rated": false,
            }),
        ];
        Ok((
            Box::new(ScriptedReader::new(frames, true)),
            Box::new(RawCapturingWriter::default()),
        ))
    }

    async fn connect_gateway(
        &self,
        _url: &str,
        _token: &str,
        _user_agent: &str,
    ) -> Result<(Box<dyn MessageReader>, Box<dyn MessageWriter>), Error> {
        let writer = RawCapturingWriter {
            sent: Arc::clone(&self.gateway_sent),
        };
        Ok((
            Box::new(ScriptedReader::new(
                vec![server_hello(), match_end()],
                false,
            )),
            Box::new(writer),
        ))
    }
}

async fn external_client_hello(supported_games: Option<Vec<String>>) -> Map<String, Value> {
    _set_lobby_recv_timeout_ms(5);
    let transport = Arc::new(OneMatchTransport::default());
    let options = RunExternalOptions {
        url: Some("wss://example.test/ws/external/bot/b1".to_string()),
        token: Some("cz_extbot_x".to_string()),
        max_matches: Some(1),
        supported_games,
        ..Default::default()
    };
    let results = run_external_with_transport(|| NoopBot, options, Arc::clone(&transport))
        .await
        .unwrap();
    assert_eq!(results.len(), 1);
    let sent = transport.gateway_sent.lock().unwrap().clone();
    sent.iter()
        .map(|s| parse(s.as_str()))
        .find(|m| m["type"] == "hello")
        .expect("no client hello on the gateway leg")
}

#[tokio::test]
async fn run_external_bot_forwards_the_declaration_to_the_match() {
    let hello = external_client_hello(Some(games(&["poker", "draw27"]))).await;
    assert_eq!(hello["supported_games"], json!(["poker", "draw27"]));
}

#[tokio::test]
async fn run_external_bot_declares_nothing_by_default() {
    let hello = external_client_hello(None).await;
    assert!(!hello.contains_key("supported_games"));
}
