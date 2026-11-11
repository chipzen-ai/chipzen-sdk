//! Lobby liveness: a lobby that stops sending frames is reconnected (#136).
//!
//! The server pings the lobby every 15s. When the host sleeps, the server
//! times the bot out and closes the lobby, but the local socket can stay
//! "open" for a long time after the host wakes, so the SDK never noticed and
//! never reconnected. `run_external_bot` now treats total silence for
//! `lobby_silence_timeout` as a dead lobby and reconnects with the retry
//! policy's backoff. Mirrors the Python SDK's `test_external_liveness.py`.
//!
//! The silent-lobby test uses a raw TCP server that sends `hello` and then
//! never answers anything (not even a close frame) while keeping the TCP
//! connection up. The other socket tests use a real `tokio-tungstenite`
//! server. The clock and sleep cases are unit tests in `src/lobby_liveness.rs`.

use chipzen_bot::{
    run_external_bot, Action, Bot, Error, GameState, LobbySilenceTimeout, RetryPolicy,
    RunExternalOptions,
};
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::oneshot;
use tokio_tungstenite::tungstenite::handshake::derive_accept_key;
use tokio_tungstenite::tungstenite::Message;

struct FoldBot;
impl Bot for FoldBot {
    fn decide(&mut self, _state: &GameState) -> Action {
        Action::Fold
    }
}

fn fast_policy() -> RetryPolicy {
    RetryPolicy::new(3, 10, 10, 2.0).unwrap()
}

fn options(url: String, timeout: LobbySilenceTimeout) -> RunExternalOptions {
    // Wake the lobby loop often so a short silence timeout is noticed quickly.
    // Every test in this binary wants the same value.
    chipzen_bot::_set_lobby_recv_timeout_ms(20);
    RunExternalOptions {
        url: Some(url),
        token: Some("cz_extbot_x".to_string()),
        retry_policy: Some(fast_policy()),
        lobby_silence_timeout: timeout,
        ..Default::default()
    }
}

/// One unmasked server-to-client text frame (payload under 126 bytes).
fn ws_text_frame(msg: &Value) -> Vec<u8> {
    let payload = msg.to_string().into_bytes();
    assert!(payload.len() < 126);
    let mut frame = vec![0x81, payload.len() as u8];
    frame.extend(payload);
    frame
}

/// Complete the WebSocket opening handshake by hand.
async fn accept_upgrade(sock: &mut TcpStream) {
    let mut request = Vec::new();
    let mut buf = [0u8; 4096];
    while !request.windows(4).any(|w| w == b"\r\n\r\n") {
        let n = sock.read(&mut buf).await.unwrap();
        assert!(n > 0, "client closed during the handshake");
        request.extend_from_slice(&buf[..n]);
    }
    let text = String::from_utf8_lossy(&request);
    let key = text
        .lines()
        .find_map(|line| {
            let (name, value) = line.split_once(':')?;
            name.eq_ignore_ascii_case("sec-websocket-key")
                .then(|| value.trim().to_string())
        })
        .expect("Sec-WebSocket-Key header");
    let accept = derive_accept_key(key.as_bytes());
    let response = format!(
        "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\
         Sec-WebSocket-Accept: {accept}\r\n\r\n"
    );
    sock.write_all(response.as_bytes()).await.unwrap();
}

#[tokio::test]
async fn test_silent_lobby_is_reconnected() {
    // The first connection completes the upgrade, sends `hello` and then never
    // answers anything again: no pings, no pong, and no reply to a close
    // frame, as a socket the server already closed while the host slept.
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let connects = Arc::new(AtomicU32::new(0));
    let (dropped_tx, dropped_rx) = oneshot::channel::<()>();
    let counter = Arc::clone(&connects);
    tokio::spawn(async move {
        let mut dropped_tx = Some(dropped_tx);
        while let Ok((mut sock, _)) = listener.accept().await {
            let n = counter.fetch_add(1, Ordering::SeqCst) + 1;
            let dropped_tx = if n == 1 { dropped_tx.take() } else { None };
            tokio::spawn(async move {
                accept_upgrade(&mut sock).await;
                let mut buf = [0u8; 4096];
                let _ = sock.read(&mut buf).await; // the client's authenticate frame
                let hello = json!({"type": "hello", "endpoint": "lobby"});
                sock.write_all(&ws_text_frame(&hello)).await.unwrap();
                if n > 1 {
                    // The reconnect: end the session cleanly with an evict.
                    let _ = sock
                        .write_all(&ws_text_frame(&json!({"type": "evict"})))
                        .await;
                    let _ = sock.write_all(&[0x88, 0x02, 0x03, 0xe8]).await; // close 1000
                    let _ = sock.shutdown().await;
                    return;
                }
                // Dead from here: read and ignore everything until the client
                // drops the TCP connection.
                while matches!(sock.read(&mut buf).await, Ok(n) if n > 0) {}
                if let Some(tx) = dropped_tx {
                    let _ = tx.send(());
                }
            });
        }
    });

    let started = Instant::now();
    let results = tokio::time::timeout(
        Duration::from_secs(20),
        run_external_bot(
            || FoldBot,
            options(
                format!("ws://127.0.0.1:{port}/ws/external/bot/b1"),
                LobbySilenceTimeout::After(Duration::from_millis(400)),
            ),
        ),
    )
    .await
    .expect("run_external_bot did not finish")
    .unwrap();
    let elapsed = started.elapsed();
    tokio::time::timeout(Duration::from_secs(5), dropped_rx)
        .await
        .expect("the silent lobby's TCP connection was never dropped")
        .unwrap();

    assert_eq!(connects.load(Ordering::SeqCst), 2); // dropped and reconnected
    assert!(results.is_empty());
    // Dropped, not closed with a handshake the dead peer never answers.
    assert!(elapsed < Duration::from_secs(5), "took {elapsed:?}");
}

/// Serve every connection with `script` once it is accepted. Returns the
/// lobby URL and a connection counter.
async fn serve<F, Fut>(script: F) -> (String, Arc<AtomicU32>)
where
    F: Fn(tokio_tungstenite::WebSocketStream<TcpStream>) -> Fut + Send + Sync + 'static,
    Fut: std::future::Future<Output = ()> + Send + 'static,
{
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let connects = Arc::new(AtomicU32::new(0));
    let counter = Arc::clone(&connects);
    let script = Arc::new(script);
    tokio::spawn(async move {
        while let Ok((stream, _)) = listener.accept().await {
            counter.fetch_add(1, Ordering::SeqCst);
            let script = Arc::clone(&script);
            tokio::spawn(async move {
                let ws = tokio_tungstenite::accept_async(stream).await.unwrap();
                script(ws).await;
            });
        }
    });
    (
        format!("ws://127.0.0.1:{port}/ws/external/bot/b1"),
        connects,
    )
}

async fn next_json(ws: &mut tokio_tungstenite::WebSocketStream<TcpStream>) -> Option<Value> {
    while let Some(Ok(frame)) = ws.next().await {
        if let Message::Text(text) = frame {
            return serde_json::from_str(&text).ok();
        }
    }
    None
}

#[tokio::test]
async fn test_pinging_lobby_is_not_reconnected() {
    let pongs = Arc::new(AtomicU32::new(0));
    let counter = Arc::clone(&pongs);
    let (url, connects) = serve(move |mut ws| {
        let pongs = Arc::clone(&counter);
        async move {
            next_json(&mut ws).await; // authenticate
            let hello = json!({"type": "hello", "endpoint": "lobby"});
            ws.send(Message::Text(hello.to_string())).await.unwrap();
            for _ in 0..8 {
                // 8 x 100ms = 4x the 200ms silence timeout.
                tokio::time::sleep(Duration::from_millis(100)).await;
                let ping = json!({"type": "ping"}).to_string();
                ws.send(Message::Text(ping)).await.unwrap();
                if next_json(&mut ws).await == Some(json!({"type": "pong"})) {
                    pongs.fetch_add(1, Ordering::SeqCst);
                }
            }
            let evict = json!({"type": "evict"}).to_string();
            let _ = ws.send(Message::Text(evict)).await;
            while let Some(Ok(_)) = ws.next().await {}
        }
    })
    .await;

    tokio::time::timeout(
        Duration::from_secs(15),
        run_external_bot(
            || FoldBot,
            options(url, LobbySilenceTimeout::After(Duration::from_millis(200))),
        ),
    )
    .await
    .expect("run_external_bot did not finish")
    .unwrap();

    assert_eq!(connects.load(Ordering::SeqCst), 1);
    assert_eq!(pongs.load(Ordering::SeqCst), 8);
}

#[tokio::test]
async fn test_silence_check_can_be_turned_off() {
    // `LobbySilenceTimeout::Off` keeps the old behavior: a quiet lobby is left
    // alone (the server ends this one with an evict after a quiet spell).
    let (url, connects) = serve(|mut ws| async move {
        next_json(&mut ws).await; // authenticate
        let hello = json!({"type": "hello", "endpoint": "lobby"});
        ws.send(Message::Text(hello.to_string())).await.unwrap();
        tokio::time::sleep(Duration::from_millis(600)).await;
        let evict = json!({"type": "evict"}).to_string();
        let _ = ws.send(Message::Text(evict)).await;
        while let Some(Ok(_)) = ws.next().await {}
    })
    .await;

    tokio::time::timeout(
        Duration::from_secs(15),
        run_external_bot(|| FoldBot, options(url, LobbySilenceTimeout::Off)),
    )
    .await
    .expect("run_external_bot did not finish")
    .unwrap();

    assert_eq!(connects.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn test_zero_silence_timeout_is_rejected() {
    let err = run_external_bot(
        || FoldBot,
        options(
            "ws://127.0.0.1:1/ws/external/bot/b1".to_string(),
            LobbySilenceTimeout::After(Duration::ZERO),
        ),
    )
    .await
    .unwrap_err();
    assert!(matches!(err, Error::Protocol(ref m) if m.contains("lobby_silence_timeout")));
}

#[test]
fn test_default_is_three_missed_heartbeats() {
    // The server pings every 15s; the default must tolerate two missed pings.
    assert_eq!(
        chipzen_bot::DEFAULT_LOBBY_SILENCE_TIMEOUT,
        Duration::from_secs(45)
    );
    assert_eq!(
        RunExternalOptions::default().lobby_silence_timeout,
        LobbySilenceTimeout::Default
    );
}
