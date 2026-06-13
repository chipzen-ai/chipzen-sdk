//! Retry / backoff policy for the WebSocket client.
//!
//! When the connection to the Chipzen server drops (TCP reset, heartbeat
//! miss, transient network failure, etc.) the SDK reconnects within the
//! server's reconnect grace window. The pacing of those reconnect attempts
//! is configurable via [`RetryPolicy`], accepted by both [`crate::run_bot`]
//! and [`crate::run_external_bot`].
//!
//! ```
//! use chipzen_bot::RetryPolicy;
//!
//! let policy = RetryPolicy::new(5, 500, 30_000, 2.0).unwrap();
//! assert_eq!(policy.backoff_ms(1), 500);
//! assert_eq!(policy.backoff_ms(2), 1000);
//! ```
//!
//! The default policy mirrors the Python SDK's spec: 5 attempts, 500ms
//! initial backoff, doubling each attempt, capped at 30 seconds. The
//! defaults are sensible for the typical home-network deployment; devs on
//! noisy connections may want a longer backoff or more attempts.
//!
//! Note: this policy controls **only** how reconnect attempts are paced.
//! The 30-second server-side grace window itself is unchanged; if the
//! reconnects burn through the window the session is considered lost and
//! the server terminates the match-side state.

use crate::error::Error;

/// Default maximum reconnect attempts after a drop.
pub const DEFAULT_MAX_RECONNECT_ATTEMPTS: u32 = 5;
/// Default delay before the first reconnect attempt, in milliseconds.
pub const DEFAULT_INITIAL_BACKOFF_MS: u64 = 500;
/// Default upper bound for any single backoff delay, in milliseconds.
pub const DEFAULT_MAX_BACKOFF_MS: u64 = 30_000;
/// Default exponential multiplier applied between attempts.
pub const DEFAULT_BACKOFF_MULTIPLIER: f64 = 2.0;

/// Backoff knobs applied to reconnect attempts.
///
/// Backoff progression for attempt `n` (1-indexed) is:
///
/// ```text
/// min(initial_backoff_ms * backoff_multiplier.powi(n - 1), max_backoff_ms)
/// ```
///
/// Examples (defaults):
///
/// ```text
/// attempt 1: 500 ms
/// attempt 2: 1000 ms
/// attempt 3: 2000 ms
/// attempt 4: 4000 ms
/// attempt 5: 8000 ms
/// attempt 6: 16000 ms  (would be next, but capped by attempts=5)
/// ```
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct RetryPolicy {
    /// Maximum number of reconnection attempts after a connection drop or
    /// heartbeat miss. `0` disables reconnection entirely (the first connect
    /// failure surfaces). Default `5`.
    pub max_reconnect_attempts: u32,
    /// Delay before the **first** reconnect attempt, in milliseconds.
    /// Default `500`.
    pub initial_backoff_ms: u64,
    /// Upper bound for any single backoff delay, in milliseconds. Must be
    /// `>= initial_backoff_ms`. Default `30_000` (30 seconds — matches the
    /// server-side grace window so a single backoff never exceeds the window
    /// itself).
    pub max_backoff_ms: u64,
    /// Exponential factor applied between attempts. `2.0` doubles the delay
    /// each attempt. Must be `>= 1.0`; `1.0` produces constant backoff.
    /// Default `2.0`.
    pub backoff_multiplier: f64,
}

impl Default for RetryPolicy {
    fn default() -> Self {
        Self {
            max_reconnect_attempts: DEFAULT_MAX_RECONNECT_ATTEMPTS,
            initial_backoff_ms: DEFAULT_INITIAL_BACKOFF_MS,
            max_backoff_ms: DEFAULT_MAX_BACKOFF_MS,
            backoff_multiplier: DEFAULT_BACKOFF_MULTIPLIER,
        }
    }
}

impl RetryPolicy {
    /// Construct a validated [`RetryPolicy`].
    ///
    /// Returns [`Error::Protocol`] if any knob is out of range. Mirrors the
    /// Python SDK's `__post_init__` validation so an invalid policy fails
    /// loudly at construction rather than producing surprising backoff.
    pub fn new(
        max_reconnect_attempts: u32,
        initial_backoff_ms: u64,
        max_backoff_ms: u64,
        backoff_multiplier: f64,
    ) -> Result<Self, Error> {
        if max_backoff_ms < initial_backoff_ms {
            return Err(Error::Protocol(format!(
                "max_backoff_ms must be >= initial_backoff_ms ({max_backoff_ms} < {initial_backoff_ms})"
            )));
        }
        if backoff_multiplier < 1.0 {
            return Err(Error::Protocol(format!(
                "backoff_multiplier must be >= 1.0, got {backoff_multiplier}"
            )));
        }
        Ok(Self {
            max_reconnect_attempts,
            initial_backoff_ms,
            max_backoff_ms,
            backoff_multiplier,
        })
    }

    /// Return the delay (in ms) to wait **before** the given attempt.
    ///
    /// `attempt` is 1-indexed: `attempt = 1` is the first reconnect after a
    /// drop, `attempt = 2` the second, etc. An `attempt` of 0 is clamped to 1
    /// (the first backoff) — the public contract is "give me the delay for
    /// the Nth retry" and there is no zeroth retry.
    pub fn backoff_ms(&self, attempt: u32) -> u64 {
        let exponent = attempt.saturating_sub(1);
        // Compute initial * multiplier.powi(exponent) in float, then clamp +
        // round down to an integer. Clamping before the cast keeps the cap
        // exact even when the float product overflows the cap by a fraction.
        let raw = self.initial_backoff_ms as f64 * self.backoff_multiplier.powi(exponent as i32);
        let capped = raw.min(self.max_backoff_ms as f64);
        if capped.is_finite() && capped >= 0.0 {
            capped as u64
        } else {
            self.max_backoff_ms
        }
    }
}

/// The default [`RetryPolicy`] used when a `run_*` entry point is called
/// without an explicit policy: 5 attempts, 500ms initial backoff doubling to
/// a 30s cap.
pub fn default_retry_policy() -> RetryPolicy {
    RetryPolicy::default()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_match_the_python_spec() {
        let p = RetryPolicy::default();
        assert_eq!(p.max_reconnect_attempts, 5);
        assert_eq!(p.initial_backoff_ms, 500);
        assert_eq!(p.max_backoff_ms, 30_000);
        assert_eq!(p.backoff_multiplier, 2.0);
    }

    #[test]
    fn backoff_progression_doubles_and_caps() {
        let p = RetryPolicy::default();
        assert_eq!(p.backoff_ms(1), 500);
        assert_eq!(p.backoff_ms(2), 1000);
        assert_eq!(p.backoff_ms(3), 2000);
        assert_eq!(p.backoff_ms(4), 4000);
        assert_eq!(p.backoff_ms(5), 8000);
        // Capped at 30_000 once the geometric series would exceed it.
        assert_eq!(p.backoff_ms(20), 30_000);
    }

    #[test]
    fn backoff_attempt_zero_clamps_to_first() {
        // attempt 0 is treated as the first backoff (no zeroth retry exists).
        assert_eq!(RetryPolicy::default().backoff_ms(0), 500);
    }

    #[test]
    fn constant_backoff_with_multiplier_one() {
        let p = RetryPolicy::new(3, 250, 1000, 1.0).unwrap();
        assert_eq!(p.backoff_ms(1), 250);
        assert_eq!(p.backoff_ms(5), 250);
    }

    #[test]
    fn rejects_max_below_initial() {
        let err = RetryPolicy::new(5, 1000, 500, 2.0).unwrap_err();
        assert!(format!("{err}").contains("max_backoff_ms must be >= initial_backoff_ms"));
    }

    #[test]
    fn rejects_multiplier_below_one() {
        let err = RetryPolicy::new(5, 500, 30_000, 0.5).unwrap_err();
        assert!(format!("{err}").contains("backoff_multiplier must be >= 1.0"));
    }

    #[test]
    fn zero_attempts_is_allowed() {
        // 0 disables reconnection — a valid policy, not an error.
        assert_eq!(
            RetryPolicy::new(0, 500, 30_000, 2.0)
                .unwrap()
                .max_reconnect_attempts,
            0
        );
    }
}
