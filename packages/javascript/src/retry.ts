/**
 * Retry / backoff policy for the WebSocket client.
 *
 * When the connection to the Chipzen server drops (TCP reset, heartbeat
 * miss, transient network failure, etc.) the SDK reconnects within the
 * server's reconnect grace window. The pacing of those reconnect attempts
 * is configurable via {@link RetryPolicy}, accepted by both `runBot` and
 * `runExternalBot`.
 *
 * The default policy mirrors the Python SDK's spec (External-API Issue 26):
 * 5 attempts, 500ms initial backoff, doubling each attempt, capped at
 * 30 seconds. The defaults are sensible for the typical home-network
 * deployment; devs on noisy connections may want a longer backoff or
 * more attempts.
 *
 * Note: this policy controls **only** how reconnect attempts are paced.
 * The 30-second server-side grace window itself is unchanged; if the
 * reconnects burn through the window the session is considered lost and
 * the server terminates the match-side state.
 */

/** Backoff knobs accepted by {@link RetryPolicy}. All fields are optional. */
export interface RetryPolicyOptions {
  /**
   * Maximum number of reconnection attempts after a connection drop or
   * heartbeat miss. Must be `>= 0`. `0` disables reconnection entirely
   * (the first connect failure raises). Default `5`.
   */
  maxReconnectAttempts?: number;
  /**
   * Delay before the **first** reconnect attempt, in milliseconds. Must
   * be `>= 0`. Default `500`.
   */
  initialBackoffMs?: number;
  /**
   * Upper bound for any single backoff delay, in milliseconds. Must be
   * `>= initialBackoffMs`. Default `30000` (matches the server-side grace
   * window so a single backoff never exceeds the window itself).
   */
  maxBackoffMs?: number;
  /**
   * Exponential factor applied between attempts. `2.0` doubles the delay
   * each attempt. Must be `>= 1.0`; `1.0` produces constant backoff.
   * Default `2.0`.
   */
  backoffMultiplier?: number;
}

/**
 * Backoff knobs applied to reconnect attempts.
 *
 * Backoff progression for attempt `n` (1-indexed) is:
 *
 *     min(initialBackoffMs * backoffMultiplier ** (n - 1), maxBackoffMs)
 *
 * Examples (defaults):
 *
 *     attempt 1:   500 ms
 *     attempt 2:  1000 ms
 *     attempt 3:  2000 ms
 *     attempt 4:  4000 ms
 *     attempt 5:  8000 ms
 *     attempt 6: 16000 ms  (would be next, but capped by attempts=5)
 */
export class RetryPolicy {
  readonly maxReconnectAttempts: number;
  readonly initialBackoffMs: number;
  readonly maxBackoffMs: number;
  readonly backoffMultiplier: number;

  constructor(options: RetryPolicyOptions = {}) {
    const maxReconnectAttempts = options.maxReconnectAttempts ?? 5;
    const initialBackoffMs = options.initialBackoffMs ?? 500;
    const maxBackoffMs = options.maxBackoffMs ?? 30_000;
    const backoffMultiplier = options.backoffMultiplier ?? 2.0;

    if (maxReconnectAttempts < 0) {
      throw new Error(`maxReconnectAttempts must be >= 0, got ${maxReconnectAttempts}`);
    }
    if (initialBackoffMs < 0) {
      throw new Error(`initialBackoffMs must be >= 0, got ${initialBackoffMs}`);
    }
    if (maxBackoffMs < initialBackoffMs) {
      throw new Error(
        `maxBackoffMs must be >= initialBackoffMs (${maxBackoffMs} < ${initialBackoffMs})`,
      );
    }
    if (backoffMultiplier < 1.0) {
      throw new Error(`backoffMultiplier must be >= 1.0, got ${backoffMultiplier}`);
    }

    this.maxReconnectAttempts = maxReconnectAttempts;
    this.initialBackoffMs = initialBackoffMs;
    this.maxBackoffMs = maxBackoffMs;
    this.backoffMultiplier = backoffMultiplier;
  }

  /**
   * Return the delay (in ms) to wait **before** the given attempt.
   *
   * @param attempt 1-indexed attempt number. `attempt=1` is the first
   *   reconnect after a drop, `attempt=2` the second, etc.
   * @returns The delay in milliseconds, capped at `maxBackoffMs`.
   * @throws Error if `attempt < 1`.
   */
  backoffMs(attempt: number): number {
    if (attempt < 1) {
      throw new Error(`attempt must be >= 1, got ${attempt}`);
    }
    // Compute initial * multiplier ** (attempt - 1) then clamp + floor.
    // Clamping before the floor keeps the cap exact even when the float
    // product overflows the cap by a fraction.
    const raw = this.initialBackoffMs * this.backoffMultiplier ** (attempt - 1);
    return Math.floor(Math.min(raw, this.maxBackoffMs));
  }
}

/**
 * The default {@link RetryPolicy} used when `runBot` / `runExternalBot`
 * is called without an explicit `retryPolicy` argument.
 */
export const DEFAULT_RETRY_POLICY = new RetryPolicy();
