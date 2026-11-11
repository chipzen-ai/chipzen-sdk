//! Lobby liveness for [`crate::run_external_bot`] (chipzen-ai/chipzen-sdk#136).
//!
//! The server pings the lobby every 15 seconds. When the host sleeps, the
//! server times the bot out and closes the lobby, but the local socket can
//! still look open for a long time after waking, so the SDK never noticed and
//! never reconnected. A lobby that hears nothing at all for the
//! [`LobbySilenceTimeout`] is treated as dead: the socket is dropped without a
//! close handshake and the lobby reconnects with the retry policy's backoff.
//!
//! Mirrors the Python SDK's `_LobbyLiveness` in `chipzen.external`.

use crate::error::Error;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

/// The default lobby silence timeout: 45 seconds. The server pings every 15
/// seconds, so this is three missed heartbeats in a row.
pub const DEFAULT_LOBBY_SILENCE_TIMEOUT: Duration = Duration::from_secs(45);

/// How long the lobby may go without receiving any frame before
/// [`crate::run_external_bot`] treats the socket as dead, drops it and
/// reconnects with the retry policy's backoff.
///
/// This is what notices a lobby the server closed while the host was asleep.
/// In-flight matches are not affected: they play on their own sockets. Sends
/// nothing new on the wire.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum LobbySilenceTimeout {
    /// [`DEFAULT_LOBBY_SILENCE_TIMEOUT`] (45 seconds).
    #[default]
    Default,
    /// A custom timeout. Must be above zero.
    After(Duration),
    /// No check: a silent lobby is left open.
    Off,
}

impl LobbySilenceTimeout {
    /// The timeout to apply, or `None` when the check is off.
    ///
    /// # Errors
    /// [`Error::Protocol`] for `After(Duration::ZERO)`.
    pub(crate) fn resolve(self) -> Result<Option<Duration>, Error> {
        match self {
            Self::Default => Ok(Some(DEFAULT_LOBBY_SILENCE_TIMEOUT)),
            Self::After(d) if d.is_zero() => Err(Error::Protocol(
                "lobby_silence_timeout must be above zero, or LobbySilenceTimeout::Off".to_string(),
            )),
            Self::After(d) => Ok(Some(d)),
            Self::Off => Ok(None),
        }
    }
}

/// The two clocks [`LobbyLiveness`] reads, each as time since its own origin.
/// Injectable so the unit tests can simulate sleep and clock steps.
pub(crate) trait LivenessClocks: Send + Sync {
    /// A monotonic clock ([`Instant`]).
    fn monotonic(&self) -> Duration;
    /// The wall clock ([`SystemTime`]).
    fn wall(&self) -> Duration;
}

/// The real clocks.
pub(crate) struct SystemClocks {
    origin: Instant,
}

impl SystemClocks {
    pub(crate) fn new() -> Self {
        Self {
            origin: Instant::now(),
        }
    }
}

impl LivenessClocks for SystemClocks {
    fn monotonic(&self) -> Duration {
        self.origin.elapsed()
    }

    fn wall(&self) -> Duration {
        // A wall clock before 1970 reads as zero; the monotonic reading still
        // covers the gap.
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or(Duration::ZERO)
    }
}

/// Tracks how long the lobby has gone without a frame from the server.
///
/// Measures the gap on both the monotonic clock and the wall clock and uses
/// the larger. The monotonic clock does not advance while macOS or Linux is
/// asleep, so after a long sleep it reports a gap of a few seconds; the wall
/// clock does advance, so the first check after waking sees the real gap. A
/// wall clock stepped backwards only shrinks its own reading (to zero, never
/// below), and the monotonic reading still covers that case. A big forward
/// step can cause one early reconnect, which is harmless.
pub(crate) struct LobbyLiveness<C: LivenessClocks = SystemClocks> {
    timeout: Option<Duration>,
    clocks: C,
    mono: Duration,
    wall: Duration,
}

impl LobbyLiveness<SystemClocks> {
    /// A tracker on the real clocks. `None` never expires.
    pub(crate) fn new(timeout: Option<Duration>) -> Self {
        Self::with_clocks(timeout, SystemClocks::new())
    }
}

impl<C: LivenessClocks> LobbyLiveness<C> {
    pub(crate) fn with_clocks(timeout: Option<Duration>, clocks: C) -> Self {
        let mono = clocks.monotonic();
        let wall = clocks.wall();
        Self {
            timeout,
            clocks,
            mono,
            wall,
        }
    }

    /// Record that a frame just arrived.
    pub(crate) fn mark(&mut self) {
        self.mono = self.clocks.monotonic();
        self.wall = self.clocks.wall();
    }

    /// Time since the last frame (the larger of the two clocks).
    pub(crate) fn silent_for(&self) -> Duration {
        let mono = self.clocks.monotonic().saturating_sub(self.mono);
        let wall = self.clocks.wall().saturating_sub(self.wall);
        mono.max(wall)
    }

    /// True when a timeout is set and the silence has reached it.
    pub(crate) fn expired(&self) -> bool {
        self.timeout.is_some_and(|t| self.silent_for() >= t)
    }
}

/// The warning printed when a silent lobby is dropped.
pub(crate) fn lobby_silence_log(silent_for: Duration) -> String {
    format!(
        "chipzen-sdk: lobby: nothing from the server for {:.1}s (the host may have slept, or \
         the connection is half-open); reconnecting",
        silent_for.as_secs_f64()
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, Mutex};

    /// Stand-ins for the monotonic and wall clocks.
    #[derive(Clone)]
    struct FakeClocks(Arc<Mutex<(Duration, Duration)>>);

    impl FakeClocks {
        fn new() -> Self {
            Self(Arc::new(Mutex::new((
                Duration::from_secs(1_000),
                Duration::from_secs(1_700_000_000),
            ))))
        }
        fn advance(&self, d: Duration) {
            let mut c = self.0.lock().unwrap();
            c.0 += d;
            c.1 += d;
        }
        fn wall_forward(&self, d: Duration) {
            self.0.lock().unwrap().1 += d;
        }
        fn wall_back(&self, d: Duration) {
            self.0.lock().unwrap().1 -= d;
        }
    }

    impl LivenessClocks for FakeClocks {
        fn monotonic(&self) -> Duration {
            self.0.lock().unwrap().0
        }
        fn wall(&self) -> Duration {
            self.0.lock().unwrap().1
        }
    }

    const T45: Option<Duration> = Some(Duration::from_secs(45));

    #[test]
    fn expires_after_the_timeout() {
        let clocks = FakeClocks::new();
        let live = LobbyLiveness::with_clocks(T45, clocks.clone());
        clocks.advance(Duration::from_secs(44));
        assert!(!live.expired());
        clocks.advance(Duration::from_secs(1));
        assert!(live.expired());
    }

    #[test]
    fn a_frame_resets_the_silence() {
        let clocks = FakeClocks::new();
        let mut live = LobbyLiveness::with_clocks(T45, clocks.clone());
        clocks.advance(Duration::from_secs(40));
        live.mark();
        clocks.advance(Duration::from_secs(40));
        assert!(!live.expired());
        assert_eq!(live.silent_for(), Duration::from_secs(40));
    }

    #[test]
    fn host_sleep_is_seen_through_the_wall_clock() {
        // macOS and Linux stop the monotonic clock while asleep; the wall
        // clock keeps going, so the first check after waking sees the gap.
        let clocks = FakeClocks::new();
        let live = LobbyLiveness::with_clocks(T45, clocks.clone());
        clocks.wall_forward(Duration::from_secs(45 * 60)); // asleep: monotonic did not move
        clocks.advance(Duration::from_secs(2)); // the first poll after waking
        assert!(live.expired());
        assert_eq!(live.silent_for(), Duration::from_secs(45 * 60 + 2));
    }

    #[test]
    fn wall_clock_stepped_back_still_expires_on_monotonic() {
        let clocks = FakeClocks::new();
        let live = LobbyLiveness::with_clocks(T45, clocks.clone());
        clocks.wall_back(Duration::from_secs(3_600)); // NTP steps the wall clock back an hour
        clocks.advance(Duration::from_secs(45));
        assert!(live.expired());
    }

    #[test]
    fn off_never_expires() {
        let clocks = FakeClocks::new();
        let live = LobbyLiveness::with_clocks(None, clocks.clone());
        clocks.advance(Duration::from_secs(10_000));
        assert!(!live.expired());
    }

    #[test]
    fn real_clocks_start_at_zero_silence() {
        let live = LobbyLiveness::new(T45);
        assert!(live.silent_for() < Duration::from_secs(5));
        assert!(!live.expired());
    }

    #[test]
    fn default_is_three_missed_heartbeats() {
        // The server pings every 15s; the default must tolerate two missed pings.
        assert_eq!(DEFAULT_LOBBY_SILENCE_TIMEOUT, Duration::from_secs(45));
        assert_eq!(
            LobbySilenceTimeout::default().resolve().unwrap(),
            Some(Duration::from_secs(45))
        );
    }

    #[test]
    fn resolve_maps_each_setting() {
        let d = Duration::from_millis(250);
        assert_eq!(LobbySilenceTimeout::After(d).resolve().unwrap(), Some(d));
        assert_eq!(LobbySilenceTimeout::Off.resolve().unwrap(), None);
        let err = LobbySilenceTimeout::After(Duration::ZERO)
            .resolve()
            .unwrap_err();
        assert!(err.to_string().contains("lobby_silence_timeout"));
    }

    #[test]
    fn the_log_line_names_the_gap() {
        let line = lobby_silence_log(Duration::from_millis(45_500));
        assert!(line.contains("nothing from the server for 45.5s"));
    }
}
