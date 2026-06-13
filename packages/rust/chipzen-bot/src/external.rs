//! External-API remote-play entry point: [`run_external_bot`].
//!
//! Where [`crate::run_bot`] connects a bot to a *known* match URL (the
//! containerized/upload path — the platform's executor hands the container
//! its `/ws/match/{match_id}/{participant_id}` URL), this module implements
//! the **external-API remote-play** path: a developer runs their bot on
//! their own machine, authenticates with a long-lived `cz_extbot_` token,
//! and the platform matches and dispatches them exactly like any other
//! competitor.
//!
//! The flow (documented in `docs/EXTERNAL-API-BOT-PROTOCOL.md`):
//!
//! ```text
//! lobby WS  /ws/external/bot/{bot_id}      (token in authenticate frame)
//!     -> "matched" notify (carries match_id + gateway_ws_url)
//!     -> per-match gateway WS  /ws/external/match/{mid}/{pid}
//!                               (token in Sec-WebSocket-Protocol header)
//!     -> two-layer bot handshake + game loop to match_end
//! ```
//!
//! The **match data plane is identical** to the containerized path, so the
//! game loop here reuses [`crate::_run_session`] verbatim — the only
//! external-API-specific code is the lobby connection and the per-match
//! gateway handshake. A developer writes ONE [`Bot`] and it works on both
//! paths.
//!
//! The lobby is held open for the bot's whole session and each `matched`
//! plays in its own task, so the lobby heartbeat is answered even while a
//! multi-minute match is in flight. This is what lets a single connection
//! serve a whole tournament (the bot is "checked in" via lobby presence and
//! matched once per round). Match-task ownership is hoisted above the lobby
//! session so a lobby reconnect doesn't kill in-flight matches, and a
//! dropped gateway socket reconnects and resumes via the server's
//! `reconnected` / `pending_request` frame.

use crate::bot::Bot;
use crate::client::{
    _run_session, default_user_agent, MessageReader, MessageWriter, SessionContext,
};
use crate::config::{load_chipzen_config, resolve_token, ChipzenConfig};
use crate::connect::{connect_to_chipzen, EnvName};
use crate::error::Error;
use crate::retry::RetryPolicy;
use async_trait::async_trait;
use serde_json::{json, Value};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::task::JoinSet;

/// Sentinel subprotocol that marks the `cz_extbot_` token in the
/// `Sec-WebSocket-Protocol` header (CZ issue 2932 moved the token off the
/// query string, where it leaked into proxy access logs). Must match the
/// value the platform's api gateway expects.
pub const BOT_TOKEN_SUBPROTOCOL: &str = "chipzen-bot-token";

/// How long the lobby loop blocks on a single `recv` before waking to
/// re-check the stop signal (milliseconds). Short enough that a stop is
/// honored promptly; long enough that the loop isn't a busy-wait. Stored in
/// an atomic so tests can shrink it (mirrors the Python suite's monkeypatch
/// of `_LOBBY_RECV_TIMEOUT_S`).
static LOBBY_RECV_TIMEOUT_MS: AtomicU64 = AtomicU64::new(2_000);

fn lobby_recv_timeout() -> Duration {
    Duration::from_millis(LOBBY_RECV_TIMEOUT_MS.load(Ordering::Relaxed))
}

/// Override the lobby recv timeout. Hidden test hook — not part of the
/// supported API.
#[doc(hidden)]
pub fn _set_lobby_recv_timeout_ms(ms: u64) {
    LOBBY_RECV_TIMEOUT_MS.store(ms, Ordering::Relaxed);
}

/// On teardown, how long to let still-in-flight matches finish before
/// cancelling them, so nothing is left orphaned.
const MATCH_DRAIN_GRACE: Duration = Duration::from_secs(5);

const DEFAULT_CLIENT_NAME: &str = "chipzen-sdk-rust";

/// Build the `Sec-WebSocket-Protocol` offer that carries the bot token:
/// `[sentinel, token]`. The sentinel marks "the next value is my bot
/// token"; the api gateway extracts the token from this header (so it never
/// appears in any access log / URL) and echoes the sentinel back on accept.
pub fn bot_token_subprotocols(token: &str) -> Vec<String> {
    vec![BOT_TOKEN_SUBPROTOCOL.to_string(), token.to_string()]
}

/// Strip any path/query from a URL, keeping scheme + host(:port) only.
/// Tolerates a bare `host:port` without a scheme (defaults to `wss`).
fn normalise_base(url: &str) -> String {
    let (scheme, rest) = match url.split_once("://") {
        Some((s, r)) => (s, r),
        None => ("wss", url),
    };
    // Authority ends at the first '/', '?' or '#'.
    let end = rest.find(['/', '?', '#']).unwrap_or(rest.len());
    let authority = &rest[..end];
    format!("{scheme}://{authority}")
}

/// Split a ws/wss URL into `(scheme, authority)` where authority is
/// `host[:port]`. Mirrors [`normalise_base`]'s parsing.
fn split_origin(url: &str) -> (&str, &str) {
    let (scheme, rest) = match url.split_once("://") {
        Some((s, r)) => (s, r),
        None => ("wss", url),
    };
    let end = rest.find(['/', '?', '#']).unwrap_or(rest.len());
    (scheme, &rest[..end])
}

/// Resolve the `matched.gateway_ws_url` path against the lobby origin.
///
/// The `matched` notification carries `gateway_ws_url` as a *path*
/// (`/ws/external/match/{mid}/{pid}`). The `cz_extbot_` token is NOT on the
/// query string — it travels in the `Sec-WebSocket-Protocol` header.
///
/// A server that returns a full URL is honored ONLY if it stays on the same
/// origin as the lobby and does not downgrade `wss` → `ws` — otherwise the bot
/// token would be sent to an attacker-/misconfig-supplied host (or in
/// cleartext). A relative path is re-anchored to the lobby origin, so it's
/// inherently same-origin.
///
/// # Errors
/// Returns [`Error::UntrustedGateway`] if `gateway_ws_path` is an absolute URL
/// on a different origin than the lobby, or downgrades a `wss` lobby to `ws`.
pub fn resolve_gateway_url(lobby_url: &str, gateway_ws_path: &str) -> Result<String, Error> {
    if gateway_ws_path.starts_with("ws://") || gateway_ws_path.starts_with("wss://") {
        let lobby_base = normalise_base(lobby_url);
        let (lobby_scheme, lobby_authority) = split_origin(&lobby_base);
        let (gw_scheme, gw_authority) = split_origin(gateway_ws_path);
        let downgrade = lobby_scheme == "wss" && gw_scheme != "wss";
        if gw_authority != lobby_authority || downgrade {
            return Err(Error::UntrustedGateway(format!(
                "{gateway_ws_path:?}: cross-origin or insecure relative to lobby \
                 {lobby_scheme}://{lobby_authority} (the bot token must not be sent to a \
                 different host or in cleartext)"
            )));
        }
        return Ok(gateway_ws_path.to_string());
    }
    Ok(format!("{}{}", normalise_base(lobby_url), gateway_ws_path))
}

/// Parse a WS frame into a JSON object (`{}` on non-object / bad JSON).
fn loads(raw: &str) -> Value {
    match serde_json::from_str::<Value>(raw) {
        Ok(v @ Value::Object(_)) => v,
        _ => json!({}),
    }
}

/// One per-match result collected during a session.
#[derive(Debug, Clone)]
pub struct MatchResult {
    /// The match id, when known (from the `match_end` payload).
    pub match_id: Option<String>,
    /// The full `match_end` payload on a clean finish, or `None` if the
    /// match was abandoned after exhausting the reconnect budget.
    pub end: Option<Value>,
}

/// Options for [`run_external_bot`]. Mirrors the Python kwargs.
#[derive(Debug, Clone, Default)]
pub struct RunExternalOptions {
    /// External-API bot UUID. Used to build the lobby URL when `url` is not
    /// given; falls back to `[external_api].bot_id` in `chipzen.toml`.
    pub bot_id: Option<String>,
    /// Target environment. `None` consults `$CHIPZEN_ENV` then defaults to
    /// `prod`.
    pub env: Option<EnvName>,
    /// Explicit full lobby URL (`wss://.../ws/external/bot/{bot_id}`).
    /// Overrides `bot_id` / `env` URL derivation when set.
    pub url: Option<String>,
    /// Long-lived `cz_extbot_` API token. Falls back to
    /// `[external_api].token` in `chipzen.toml`. Required.
    pub token: Option<String>,
    /// Pre-loaded config, to avoid a second filesystem stat.
    pub config: Option<ChipzenConfig>,
    /// Reconnect-pacing policy for both lobby drops and mid-match gateway
    /// drops. `None` uses [`RetryPolicy::default`].
    pub retry_policy: Option<RetryPolicy>,
    /// Sent in the per-match `hello` handshake. Defaults to
    /// `chipzen-sdk-rust`.
    pub client_name: Option<String>,
    /// Sent in the per-match `hello` handshake. Defaults to the crate
    /// version.
    pub client_version: Option<String>,
    /// When `true` (default), a panic in `decide()` is folded; `false`
    /// propagates it as a terminal [`Error::BotDecision`].
    pub safe_mode: Option<bool>,
    /// Stop after this many matches complete. `None` runs until the lobby
    /// closes / the bot is evicted. `Some(1)` plays a single challenge.
    pub max_matches: Option<u64>,
    /// Override the WS `User-Agent`. Defaults to `chipzen-sdk-rust/<version>`.
    pub user_agent: Option<String>,
}

// ---------------------------------------------------------------------------
// CLI helper — the Rust equivalent of `chipzen run-external <bot.py>`.
// ---------------------------------------------------------------------------

/// Parsed `run-external` flags, mirroring the Python CLI
/// (`--env`/`--token`/`--bot-id`/`--max-matches`/`--no-safe-mode`).
///
/// Rust cannot dynamically load a bot from a file the way Python's
/// `chipzen run-external my_bot.py` does — a Rust bot is compiled into a
/// binary. The equivalent is [`run_external_cli`]: wire it into your own
/// bot binary's `main` (the scaffolded starter does this for you) and it
/// loads `chipzen.toml`, resolves the env-aware lobby URL, and runs your
/// bot via [`run_external_bot`]. Build this struct however you like — from
/// `clap`, from env vars, or by hand — so this crate stays argument-parser
/// agnostic.
#[derive(Debug, Clone, Default)]
pub struct RunExternalArgs {
    /// `--env prod|staging|local`. `None` consults `$CHIPZEN_ENV` then
    /// defaults to `prod`.
    pub env: Option<EnvName>,
    /// `--token`. Overrides `[external_api].token` in `chipzen.toml`.
    pub token: Option<String>,
    /// `--bot-id`. Overrides `[external_api].bot_id`. Required when no
    /// `[external_api].url` is configured.
    pub bot_id: Option<String>,
    /// `--max-matches`. `None` runs until the lobby closes (tournament
    /// check-in); `Some(1)` plays a single challenge.
    pub max_matches: Option<u64>,
    /// `--no-safe-mode` sets this to `false` (let a `decide()` panic crash
    /// the process). Defaults to `true`.
    pub safe_mode: bool,
}

impl RunExternalArgs {
    /// A default args set with `safe_mode` on — the common case.
    pub fn new() -> Self {
        Self {
            env: None,
            token: None,
            bot_id: None,
            max_matches: None,
            safe_mode: true,
        }
    }
}

/// The Rust equivalent of the `chipzen run-external` CLI: resolve config +
/// env URL + token from `args` (and a discovered `chipzen.toml`), then run
/// `factory`'s bot via [`run_external_bot`].
///
/// Wire this into your bot binary's `main` — the scaffolded starter ships a
/// `run-external` subcommand that calls it. `factory` produces one bot per
/// match (`|| MyBot::default()`).
///
/// # Errors
///
/// Surfaces the same errors as [`run_external_bot`] (no token, no URL,
/// malformed config, or a terminal [`Error::BotDecision`] under
/// `safe_mode = false`).
pub async fn run_external_cli<B, F>(
    factory: F,
    args: RunExternalArgs,
) -> Result<Vec<MatchResult>, Error>
where
    B: Bot,
    F: Fn() -> B + Send + Sync + 'static,
{
    let options = RunExternalOptions {
        bot_id: args.bot_id,
        env: args.env,
        token: args.token,
        max_matches: args.max_matches,
        safe_mode: Some(args.safe_mode),
        ..Default::default()
    };
    run_external_bot(factory, options).await
}

// ---------------------------------------------------------------------------
// Transport seam — real impl wraps tokio-tungstenite; tests script it.
// ---------------------------------------------------------------------------

/// Opens lobby + gateway WebSocket connections. Abstracted so tests can
/// drive [`run_external_with_transport`] against scripted sockets, exactly
/// as the conformance harness drives [`_run_session`].
///
/// A connection is returned as a boxed [`MessageReader`] + [`MessageWriter`]
/// pair. Implementors must be `Send + Sync + 'static` (a single transport is
/// shared across the lobby loop and every spawned match task).
#[async_trait]
pub trait LobbyTransport: Send + Sync + 'static {
    /// Open the lobby WS at `url`, sending `user_agent` on the handshake.
    async fn connect_lobby(
        &self,
        url: &str,
        user_agent: &str,
    ) -> Result<(Box<dyn MessageReader>, Box<dyn MessageWriter>), Error>;

    /// Open the per-match gateway WS at `url`. `token` travels in the
    /// `Sec-WebSocket-Protocol` header (see [`bot_token_subprotocols`]).
    async fn connect_gateway(
        &self,
        url: &str,
        token: &str,
        user_agent: &str,
    ) -> Result<(Box<dyn MessageReader>, Box<dyn MessageWriter>), Error>;
}

/// Production transport: real tokio-tungstenite sockets.
struct WsTransport;

#[async_trait]
impl LobbyTransport for WsTransport {
    async fn connect_lobby(
        &self,
        url: &str,
        user_agent: &str,
    ) -> Result<(Box<dyn MessageReader>, Box<dyn MessageWriter>), Error> {
        ws_transport::connect(url, user_agent, None).await
    }

    async fn connect_gateway(
        &self,
        url: &str,
        token: &str,
        user_agent: &str,
    ) -> Result<(Box<dyn MessageReader>, Box<dyn MessageWriter>), Error> {
        ws_transport::connect(url, user_agent, Some(token)).await
    }
}

// ---------------------------------------------------------------------------
// Public entry point
// ---------------------------------------------------------------------------

/// Run a bot on the Chipzen external-API remote-play path.
///
/// Connects to the lobby, then plays every match the platform dispatches to
/// this bot (a single challenge, or every round of a tournament) until the
/// lobby closes, the bot is evicted, or `max_matches` matches complete.
///
/// `factory` produces one bot instance per match. Pass a closure that
/// returns a fresh bot (`|| MyBot::default()`) for correct per-match state
/// when matches may overlap; the same closure can capture-and-clone shared
/// config. Each match runs in its own task on its own gateway socket.
///
/// Returns one [`MatchResult`] per match played this session.
///
/// # Errors
///
/// * [`Error::Protocol`] if no token can be resolved, or neither `url` nor a
///   `bot_id` is available to build the lobby URL.
/// * [`Error::BotDecision`] if `decide()` panics under `safe_mode = false`.
pub async fn run_external_bot<B, F>(
    factory: F,
    options: RunExternalOptions,
) -> Result<Vec<MatchResult>, Error>
where
    B: Bot,
    F: Fn() -> B + Send + Sync + 'static,
{
    run_external_with_transport(factory, options, Arc::new(WsTransport)).await
}

/// Testable core of [`run_external_bot`], generic over the transport.
///
/// `run_external_bot` calls this with the real `WsTransport`; tests pass a
/// scripted transport to exercise the lobby/gateway/reconnect logic without
/// a server.
pub async fn run_external_with_transport<B, F, T>(
    factory: F,
    options: RunExternalOptions,
    transport: Arc<T>,
) -> Result<Vec<MatchResult>, Error>
where
    B: Bot,
    F: Fn() -> B + Send + Sync + 'static,
    T: LobbyTransport,
{
    let config = match options.config {
        Some(c) => Some(c),
        None => load_chipzen_config(None)?,
    };

    // --- Resolve lobby URL + token + retry policy --------------------------
    let (lobby_url, policy, resolved_token) = if let Some(url) = options.url {
        let policy = options.retry_policy.unwrap_or_default();
        let token = resolve_token(options.token.as_deref(), config.as_ref());
        (url, policy, token)
    } else {
        let bot_id = options
            .bot_id
            .clone()
            .or_else(|| config.as_ref().and_then(|c| c.bot_id.clone()));
        let Some(bot_id) = bot_id.filter(|s| !s.is_empty()) else {
            return Err(Error::Protocol(
                "run_external_bot() needs a lobby URL. Set url, or bot_id (or \
                 [external_api].bot_id / url in chipzen.toml)."
                    .to_string(),
            ));
        };
        let conn = connect_to_chipzen(&bot_id, options.env, options.retry_policy, config.clone())?;
        // An explicit token still wins over the config-file token.
        let token = match options.token.as_deref() {
            Some(t) => Some(t.to_string()),
            None => conn.token.clone(),
        };
        (conn.url, conn.retry_policy, token)
    };

    let Some(resolved_token) = resolved_token.filter(|t| !t.is_empty()) else {
        return Err(Error::Protocol(
            "run_external_bot() requires an external-API token (cz_extbot_...). Pass \
             token, or set [external_api].token in chipzen.toml."
                .to_string(),
        ));
    };

    let client_version = options
        .client_version
        .clone()
        .unwrap_or_else(|| env!("CARGO_PKG_VERSION").to_string());
    let user_agent = options
        .user_agent
        .clone()
        .unwrap_or_else(default_user_agent);
    let client_name = options
        .client_name
        .clone()
        .unwrap_or_else(|| DEFAULT_CLIENT_NAME.to_string());
    let safe_mode = options.safe_mode.unwrap_or(true);

    let session = Arc::new(SessionParams {
        token: resolved_token,
        client_name,
        client_version,
        safe_mode,
        user_agent,
        policy,
        max_matches: options.max_matches,
    });

    // Shared mutable session state — owned HERE, not by a single lobby
    // session, so in-flight matches survive a lobby reconnect.
    let results: Arc<Mutex<Vec<MatchResult>>> = Arc::new(Mutex::new(Vec::new()));
    let completed = Arc::new(AtomicU64::new(0));
    let stop = Arc::new(AtomicBool::new(false));
    let fatal: Arc<Mutex<Option<Error>>> = Arc::new(Mutex::new(None));
    let mut match_tasks: JoinSet<()> = JoinSet::new();

    // --- Lobby session loop with reconnect/backoff -------------------------
    let mut consecutive_failures: u32 = 0;
    let mut ever_connected = false;
    let mut giveup: Option<Error> = None;

    while !stop.load(Ordering::SeqCst) {
        let run = run_lobby_once(
            &transport,
            &lobby_url,
            &factory,
            &session,
            &results,
            &completed,
            &stop,
            &fatal,
            &mut match_tasks,
        )
        .await;

        match run {
            Ok(status) => {
                ever_connected = true;
                consecutive_failures = 0;
                if matches!(status, LobbyStatus::Stopped | LobbyStatus::Evicted)
                    || fatal.lock().unwrap().is_some()
                {
                    break;
                }
                // status == Closed: the lobby dropped. In-flight matches keep
                // playing on their own sockets; reconnect per the policy.
                consecutive_failures += 1;
                if consecutive_failures > session.policy.max_reconnect_attempts {
                    break;
                }
                let delay = session.policy.backoff_ms(consecutive_failures);
                tokio::time::sleep(Duration::from_millis(delay)).await;
            }
            Err(exc) => {
                // connect() itself failed — count it as a reconnect attempt.
                consecutive_failures += 1;
                if consecutive_failures > session.policy.max_reconnect_attempts {
                    // Only a hard error if we NEVER reached the lobby. If we
                    // connected and played, give up quietly and return results.
                    if !ever_connected {
                        giveup = Some(exc);
                    }
                    break;
                }
                let delay = session.policy.backoff_ms(consecutive_failures);
                tokio::time::sleep(Duration::from_millis(delay)).await;
            }
        }
    }

    // --- Teardown: never orphan an in-flight match task --------------------
    drain_and_cancel(&mut match_tasks).await;

    if let Some(err) = fatal.lock().unwrap().take() {
        return Err(err);
    }
    if let Some(err) = giveup {
        return Err(err);
    }

    let out = std::mem::take(&mut *results.lock().unwrap());
    Ok(out)
}

/// Immutable per-session parameters shared with every spawned match task.
struct SessionParams {
    token: String,
    client_name: String,
    client_version: String,
    safe_mode: bool,
    user_agent: String,
    policy: RetryPolicy,
    max_matches: Option<u64>,
}

/// Why a single lobby connection ended.
enum LobbyStatus {
    /// Caller's stop signal fired or `max_matches` reached — do not reconnect.
    Stopped,
    /// The lobby evicted us (a newer connection replaced this one).
    Evicted,
    /// The lobby connection closed unexpectedly — the caller may reconnect.
    Closed,
}

/// Hold ONE lobby connection and dispatch every `matched` it delivers.
///
/// Each `matched` is spawned into `match_tasks` (owned by the caller) so the
/// lobby heartbeat is answered during matches AND in-flight matches survive
/// a lobby reconnect — a match runs on its own gateway socket.
#[allow(clippy::too_many_arguments)]
async fn run_lobby_once<B, F, T>(
    transport: &Arc<T>,
    lobby_url: &str,
    factory: &F,
    session: &Arc<SessionParams>,
    results: &Arc<Mutex<Vec<MatchResult>>>,
    completed: &Arc<AtomicU64>,
    stop: &Arc<AtomicBool>,
    fatal: &Arc<Mutex<Option<Error>>>,
    match_tasks: &mut JoinSet<()>,
) -> Result<LobbyStatus, Error>
where
    B: Bot,
    F: Fn() -> B + Send + Sync + 'static,
    T: LobbyTransport,
{
    let (mut reader, mut writer) = transport
        .connect_lobby(lobby_url, &session.user_agent)
        .await?;

    // Lobby auth frame.
    writer
        .send(json!({ "type": "authenticate", "token": session.token }).to_string())
        .await?;

    while !stop.load(Ordering::SeqCst) {
        let recv = tokio::time::timeout(lobby_recv_timeout(), reader.next()).await;
        let raw = match recv {
            // Periodic wake to re-check the stop signal.
            Err(_elapsed) => continue,
            Ok(Ok(Some(raw))) => raw,
            // Clean close.
            Ok(Ok(None)) => return Ok(LobbyStatus::Closed),
            // Transport error → treat as a drop the caller may reconnect.
            Ok(Err(_)) => return Ok(LobbyStatus::Closed),
        };

        let msg = loads(&raw);
        match msg.get("type").and_then(|v| v.as_str()) {
            Some("ping") => {
                writer.send(json!({ "type": "pong" }).to_string()).await?;
            }
            Some("hello") => {
                // Lobby connected; no client hello on the lobby leg.
            }
            Some("matched") => {
                let gateway_path = msg
                    .get("gateway_ws_url")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                let match_id = msg
                    .get("match_id")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                // Untrusted gateway URL (cross-origin / downgrade) → skip this
                // match rather than send the token there.
                let gateway_url = match resolve_gateway_url(lobby_url, gateway_path) {
                    Ok(url) => url,
                    Err(_) => continue,
                };

                let bot = factory();
                let transport = Arc::clone(transport);
                let session = Arc::clone(session);
                let results = Arc::clone(results);
                let completed = Arc::clone(completed);
                let stop = Arc::clone(stop);
                let fatal = Arc::clone(fatal);

                match_tasks.spawn(async move {
                    let outcome =
                        play_one_match(&*transport, &gateway_url, &match_id, bot, &session).await;
                    record_match_outcome(
                        outcome, &match_id, &session, &results, &completed, &stop, &fatal,
                    );
                });
            }
            Some("evict") => return Ok(LobbyStatus::Evicted),
            _ => {
                // Ignore unknown lobby frame types.
            }
        }
    }

    if stop.load(Ordering::SeqCst) {
        Ok(LobbyStatus::Stopped)
    } else {
        Ok(LobbyStatus::Closed)
    }
}

/// Fold a finished match into the shared results + completed counter, and
/// flip the stop signal on a terminal `BotDecision` error or `max_matches`.
fn record_match_outcome(
    outcome: Result<Option<Value>, Error>,
    match_id: &str,
    session: &SessionParams,
    results: &Mutex<Vec<MatchResult>>,
    completed: &AtomicU64,
    stop: &AtomicBool,
    fatal: &Mutex<Option<Error>>,
) {
    match outcome {
        // safe_mode=false: a deterministic bot bug. Surface it (stop the
        // session + re-raise from run_external_bot) — the whole point of
        // safe_mode=false is a loud failure.
        Err(err @ Error::BotDecision(_)) => {
            let mut slot = fatal.lock().unwrap();
            if slot.is_none() {
                *slot = Some(err);
            }
            stop.store(true, Ordering::SeqCst);
            return;
        }
        Err(_other) => {
            // Record, never crash the lobby. (Connect/transport errors that
            // outlived the reconnect budget land here as the match abandoned.)
            results.lock().unwrap().push(MatchResult {
                match_id: Some(match_id.to_string()),
                end: None,
            });
        }
        Ok(end) => {
            let match_id = end
                .as_ref()
                .and_then(|e| e.get("match_id"))
                .and_then(|v| v.as_str())
                .map(String::from)
                .or_else(|| Some(match_id.to_string()));
            completed.fetch_add(1, Ordering::SeqCst);
            results.lock().unwrap().push(MatchResult { match_id, end });
        }
    }

    if let Some(max) = session.max_matches {
        if completed.load(Ordering::SeqCst) >= max {
            stop.store(true, Ordering::SeqCst);
        }
    }
}

/// Play one match end-to-end over the per-match gateway WS, reconnecting
/// across a mid-match drop.
///
/// Opens the gateway WS (token in the `Sec-WebSocket-Protocol` header) and
/// hands off to [`_run_session`] for the two-layer handshake + game loop.
/// If the socket drops before `match_end`, reconnects (bounded by the
/// policy) and lets the platform's reconnect-resume re-deliver the pending
/// turn — `_run_session` already consumes the server `reconnected` frame and
/// replays its `pending_request`, and the same `bot` carries its state
/// across the gap.
///
/// Returns the `match_end` payload, or `Ok(None)` if the match could not be
/// completed within the reconnect budget. A [`Error::BotDecision`] is
/// terminal and propagates immediately (never reconnect-retried).
async fn play_one_match<B, T>(
    transport: &T,
    gateway_url: &str,
    match_id: &str,
    mut bot: B,
    session: &SessionParams,
) -> Result<Option<Value>, Error>
where
    B: Bot,
    T: LobbyTransport,
{
    let ctx = SessionContext {
        match_id: match_id.to_string(),
        // The inner leg's auth token is the gateway's internal JWT; the
        // executor ignores the value we send (an empty token), but the
        // authenticate frame MUST be first.
        token: None,
        ticket: None,
        client_name: session.client_name.clone(),
        client_version: session.client_version.clone(),
        safe_mode: session.safe_mode,
    };

    let mut attempt: u32 = 0;
    loop {
        let connect = transport
            .connect_gateway(gateway_url, &session.token, &session.user_agent)
            .await;

        match connect {
            Ok((mut reader, mut writer)) => {
                match _run_session(&mut reader, &mut writer, &mut bot, &ctx).await {
                    // Clean match_end — done.
                    Ok(Some(end)) => return Ok(Some(end)),
                    // Socket closed without a match_end (a drop). Try to resume.
                    Ok(None) => {}
                    // Deterministic bot bug — terminal, never reconnect-retry.
                    Err(e @ Error::BotDecision(_)) => return Err(e),
                    // Transport/protocol error mid-session — try to resume.
                    Err(_) => {}
                }
            }
            // Connect failed — counts as a drop too.
            Err(Error::BotDecision(_)) => unreachable!("connect cannot raise BotDecision"),
            Err(_) => {}
        }

        attempt += 1;
        if attempt > session.policy.max_reconnect_attempts {
            return Ok(None); // reconnect budget exhausted — abandon
        }
        let delay = session.policy.backoff_ms(attempt);
        tokio::time::sleep(Duration::from_millis(delay)).await;
    }
}

/// Teardown: let still-in-flight matches finish for a short grace window,
/// then cancel any stragglers so no task is orphaned.
async fn drain_and_cancel(match_tasks: &mut JoinSet<()>) {
    if match_tasks.is_empty() {
        return;
    }
    // `join_next` until the grace window elapses; whatever's left is aborted.
    let drain = async { while match_tasks.join_next().await.is_some() {} };
    if tokio::time::timeout(MATCH_DRAIN_GRACE, drain)
        .await
        .is_err()
    {
        match_tasks.abort_all();
        while match_tasks.join_next().await.is_some() {}
    }
}

// ---------------------------------------------------------------------------
// Real WebSocket transport adapter
// ---------------------------------------------------------------------------

mod ws_transport {
    use super::{bot_token_subprotocols, MessageReader, MessageWriter};
    use crate::error::Error;
    use async_trait::async_trait;
    use futures_util::stream::{SplitSink, SplitStream};
    use futures_util::{SinkExt, StreamExt};
    use tokio::net::TcpStream;
    use tokio_tungstenite::{
        connect_async,
        tungstenite::client::IntoClientRequest,
        tungstenite::handshake::client::generate_key,
        tungstenite::http::header::{
            CONNECTION, SEC_WEBSOCKET_KEY, SEC_WEBSOCKET_PROTOCOL, SEC_WEBSOCKET_VERSION, UPGRADE,
            USER_AGENT,
        },
        tungstenite::Message,
        MaybeTlsStream, WebSocketStream,
    };

    type WsStream = WebSocketStream<MaybeTlsStream<TcpStream>>;

    /// Open a WS connection, attaching the `User-Agent` header and, when a
    /// `token` is supplied, the `Sec-WebSocket-Protocol` token offer.
    pub async fn connect(
        url: &str,
        user_agent: &str,
        token: Option<&str>,
    ) -> Result<(Box<dyn MessageReader>, Box<dyn MessageWriter>), Error> {
        let mut request = url.into_client_request().map_err(Error::from)?;
        let headers = request.headers_mut();
        if let Ok(value) = user_agent.parse() {
            headers.insert(USER_AGENT, value);
        }
        if let Some(token) = token {
            let offer = bot_token_subprotocols(token).join(", ");
            if let Ok(value) = offer.parse() {
                headers.insert(SEC_WEBSOCKET_PROTOCOL, value);
            }
            // `into_client_request` already set the upgrade/version/key
            // headers; ensure the standard handshake set is present in case a
            // future tungstenite changes defaults.
            headers
                .entry(SEC_WEBSOCKET_VERSION)
                .or_insert_with(|| "13".parse().expect("static header"));
            headers
                .entry(SEC_WEBSOCKET_KEY)
                .or_insert_with(|| generate_key().parse().expect("generated key"));
            headers
                .entry(CONNECTION)
                .or_insert_with(|| "Upgrade".parse().expect("static header"));
            headers
                .entry(UPGRADE)
                .or_insert_with(|| "websocket".parse().expect("static header"));
        }

        let (ws_stream, _) = connect_async(request).await?;
        let (write_half, read_half) = ws_stream.split();
        let reader: Box<dyn MessageReader> = Box::new(OwnedWsReader { inner: read_half });
        let writer: Box<dyn MessageWriter> = Box::new(OwnedWsWriter { inner: write_half });
        Ok((reader, writer))
    }

    struct OwnedWsReader {
        inner: SplitStream<WsStream>,
    }

    #[async_trait]
    impl MessageReader for OwnedWsReader {
        async fn next(&mut self) -> Result<Option<String>, Error> {
            loop {
                match self.inner.next().await {
                    Some(Ok(Message::Text(t))) => return Ok(Some(t.to_string())),
                    Some(Ok(Message::Ping(_))) => continue,
                    Some(Ok(Message::Close(_))) | None => return Ok(None),
                    Some(Ok(_)) => continue,
                    Some(Err(e)) => return Err(Error::from(e)),
                }
            }
        }
    }

    struct OwnedWsWriter {
        inner: SplitSink<WsStream, Message>,
    }

    #[async_trait]
    impl MessageWriter for OwnedWsWriter {
        async fn send(&mut self, payload: String) -> Result<(), Error> {
            self.inner
                .send(Message::Text(payload))
                .await
                .map_err(Error::from)
        }
    }
}
