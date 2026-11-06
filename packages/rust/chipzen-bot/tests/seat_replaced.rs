//! A match seat taken over by another connection of the same bot (#242),
//! against a real local WebSocket server, so the real reader's close-code
//! capture is what the session loop sees.
//!
//! The external-API runner's behaviour is covered in `tests/external.rs` with
//! a scripted transport; this file covers `run_bot` and its socket reader.

use chipzen_bot::{run_bot, Action, Bot, GameState, RetryPolicy, RunBotOptions};
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use std::borrow::Cow;
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::Arc;
use tokio::net::TcpListener;
use tokio_tungstenite::tungstenite::protocol::frame::coding::CloseCode;
use tokio_tungstenite::tungstenite::protocol::CloseFrame;
use tokio_tungstenite::tungstenite::Message;

struct NoopBot;
impl Bot for NoopBot {
    fn decide(&mut self, _state: &GameState) -> Action {
        Action::Fold
    }
}

/// Serve every connection: once its `authenticate` arrives, send `before`,
/// then close with `code`. Returns the URL and a connection counter.
async fn serve(before: Vec<Value>, code: u16) -> (String, Arc<AtomicU32>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let connections = Arc::new(AtomicU32::new(0));
    let counter = Arc::clone(&connections);
    tokio::spawn(async move {
        while let Ok((stream, _)) = listener.accept().await {
            counter.fetch_add(1, Ordering::SeqCst);
            let before = before.clone();
            tokio::spawn(async move {
                let mut ws = tokio_tungstenite::accept_async(stream).await.unwrap();
                while let Some(Ok(frame)) = ws.next().await {
                    let Message::Text(text) = frame else { continue };
                    let msg: Value = serde_json::from_str(&text).unwrap();
                    if msg["type"] == "authenticate" {
                        for frame in &before {
                            let _ = ws.send(Message::Text(frame.to_string())).await;
                        }
                        let _ = ws
                            .close(Some(CloseFrame {
                                code: CloseCode::from(code),
                                reason: Cow::Borrowed("replaced_by_new_connection"),
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
        format!("ws://127.0.0.1:{port}/ws/match/m-seat/bot"),
        connections,
    )
}

fn options() -> RunBotOptions {
    RunBotOptions {
        token: Some("tok".to_string()),
        retry_policy: RetryPolicy {
            max_reconnect_attempts: 3,
            initial_backoff_ms: 1,
            max_backoff_ms: 1,
            ..RetryPolicy::default()
        },
        ..RunBotOptions::default()
    }
}

#[tokio::test]
async fn run_bot_does_not_retry_a_4000_close() {
    let (url, connections) = serve(vec![], 4000).await;
    let end = run_bot(&url, NoopBot, options()).await.unwrap();
    assert!(end.is_none());
    assert_eq!(connections.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn run_bot_does_not_retry_the_error_frame_then_close() {
    let before = vec![
        json!({ "type": "hello", "selected_version": "1.0" }),
        json!({ "type": "error", "code": "replaced_by_new_connection" }),
    ];
    let (url, connections) = serve(before, 4000).await;
    let end = run_bot(&url, NoopBot, options()).await.unwrap();
    assert!(end.is_none());
    assert_eq!(connections.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn run_bot_still_retries_a_drop_before_the_server_hello() {
    // Any other close before the hello is a drop, retried per the policy: one
    // connect plus three retries, then the budget is spent.
    let (url, connections) = serve(vec![], 1011).await;
    let err = run_bot(&url, NoopBot, options()).await.unwrap_err();
    assert!(err.to_string().contains("retry budget exhausted"), "{err}");
    assert_eq!(connections.load(Ordering::SeqCst), 4);
}
