//! A terminal 4xxx close on a match socket (4013 first) ends the match, with
//! no reconnect (Chipzen #7008).
//!
//! The external-API gateway now passes the executor's 4013 version refusal
//! through to the bot instead of masking it as `match_end` + 1000. Before this
//! change the SDK read that close as a drop and reconnected until the retry
//! budget ran out. These tests run a real local WebSocket server, so the real
//! reader's close capture is what the session loop sees. The external-API
//! runner is covered in `tests/external.rs` with a scripted transport. They
//! mirror the Python and JavaScript tests.

use chipzen_bot::{
    is_terminal_close, run_bot, Action, Bot, Error, GameState, RetryPolicy, RunBotOptions,
    TERMINAL_CLOSE_CODES,
};
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use std::borrow::Cow;
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::Arc;
use tokio::net::TcpListener;
use tokio_tungstenite::tungstenite::protocol::frame::coding::CloseCode;
use tokio_tungstenite::tungstenite::protocol::CloseFrame;
use tokio_tungstenite::tungstenite::Message;

const NO_MUTUAL: &str = "No mutually supported protocol version";

struct NoopBot;
impl Bot for NoopBot {
    fn decide(&mut self, _state: &GameState) -> Action {
        Action::Fold
    }
}

/// When the server closes: right after `authenticate`, or after the bot's
/// `hello` (the server sends its own hello first).
#[derive(Clone, Copy)]
enum At {
    Authenticate,
    ClientHello,
}

/// Serve every connection: close with `code` and `reason` at `at`. Returns
/// the URL and a connection counter.
async fn serve(at: At, code: u16, reason: &'static str) -> (String, Arc<AtomicU32>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let connections = Arc::new(AtomicU32::new(0));
    let counter = Arc::clone(&connections);
    tokio::spawn(async move {
        while let Ok((stream, _)) = listener.accept().await {
            counter.fetch_add(1, Ordering::SeqCst);
            tokio::spawn(async move {
                let mut ws = tokio_tungstenite::accept_async(stream).await.unwrap();
                while let Some(Ok(frame)) = ws.next().await {
                    let Message::Text(text) = frame else { continue };
                    let msg: Value = serde_json::from_str(&text).unwrap();
                    let close_now = match (at, msg["type"].as_str()) {
                        (At::Authenticate, Some("authenticate")) => true,
                        (At::ClientHello, Some("authenticate")) => {
                            let hello = json!({ "type": "hello", "selected_version": "1.0" });
                            let _ = ws.send(Message::Text(hello.to_string())).await;
                            false
                        }
                        (At::ClientHello, Some("hello")) => true,
                        _ => false,
                    };
                    if close_now {
                        let _ = ws
                            .close(Some(CloseFrame {
                                code: CloseCode::from(code),
                                reason: Cow::Borrowed(reason),
                            }))
                            .await;
                        // Drain until the client's close completes the handshake.
                        while let Some(Ok(_)) = ws.next().await {}
                        break;
                    }
                }
            });
        }
    });
    (
        format!("ws://127.0.0.1:{port}/ws/match/m-live/bot"),
        connections,
    )
}

fn options() -> RunBotOptions {
    RunBotOptions {
        token: Some("tok".to_string()),
        retry_policy: RetryPolicy {
            max_reconnect_attempts: 2,
            initial_backoff_ms: 1,
            max_backoff_ms: 1,
            ..RetryPolicy::default()
        },
        ..RunBotOptions::default()
    }
}

#[test]
fn terminal_codes_are_pinned() {
    // Widening this set changes what a deployed bot does on that close; the
    // gateway's pass-through list may only include codes in here.
    assert_eq!(TERMINAL_CLOSE_CODES, &[4003, 4005, 4006, 4010, 4012, 4013]);
    assert!(is_terminal_close(4013));
    assert!(!is_terminal_close(4000));
    assert!(!is_terminal_close(4011));
    assert!(!is_terminal_close(1000));
}

#[test]
fn the_error_names_the_code_and_the_reason() {
    let err = Error::ProtocolClose {
        code: 4013,
        reason: NO_MUTUAL.to_string(),
    };
    assert_eq!(
        err.to_string(),
        format!("server closed the match socket with 4013 protocol_mismatch: {NO_MUTUAL}")
    );
    let bare = Error::ProtocolClose {
        code: 4010,
        reason: String::new(),
    };
    assert!(bare
        .to_string()
        .ends_with("4010 reconnect_expired: no reason given"));
}

#[tokio::test]
async fn run_bot_stops_on_a_4013_before_the_server_hello() {
    let (url, connections) = serve(At::Authenticate, 4013, NO_MUTUAL).await;
    let err = run_bot(&url, NoopBot, options()).await.unwrap_err();
    match err {
        Error::ProtocolClose { code, reason } => {
            assert_eq!(code, 4013);
            assert_eq!(reason, NO_MUTUAL);
        }
        other => panic!("expected ProtocolClose, got {other:?}"),
    }
    assert_eq!(connections.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn run_bot_stops_on_a_4013_after_its_own_hello() {
    let reason = "Missing or invalid supported_versions";
    let (url, connections) = serve(At::ClientHello, 4013, reason).await;
    let err = run_bot(&url, NoopBot, options()).await.unwrap_err();
    assert!(
        matches!(&err, Error::ProtocolClose { code: 4013, reason: r } if r == reason),
        "{err:?}"
    );
    assert_eq!(connections.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn run_bot_stops_on_every_terminal_code() {
    for &code in TERMINAL_CLOSE_CODES {
        let (url, connections) = serve(At::Authenticate, code, "why").await;
        let err = run_bot(&url, NoopBot, options()).await.unwrap_err();
        assert!(
            matches!(&err, Error::ProtocolClose { code: c, .. } if *c == code),
            "{code}: {err:?}"
        );
        assert_eq!(connections.load(Ordering::SeqCst), 1, "{code}");
    }
}

#[tokio::test]
async fn run_bot_still_retries_other_error_closes() {
    for code in [1011, 4001, 4002, 4008, 4011] {
        let (url, connections) = serve(At::Authenticate, code, "x").await;
        let err = run_bot(&url, NoopBot, options()).await.unwrap_err();
        assert!(
            matches!(err, Error::RetriesExhausted { .. }),
            "{code}: {err:?}"
        );
        // One connect plus two retries, as before.
        assert_eq!(connections.load(Ordering::SeqCst), 3, "{code}");
    }
}

#[tokio::test]
async fn a_normal_close_after_the_handshake_is_unchanged() {
    for code in [1000, 1001] {
        let (url, connections) = serve(At::ClientHello, code, "").await;
        let end = run_bot(&url, NoopBot, options()).await.unwrap();
        assert!(end.is_none(), "{code}");
        assert_eq!(connections.load(Ordering::SeqCst), 1, "{code}");
    }
}
