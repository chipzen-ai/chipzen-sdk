/**
 * Lobby liveness for {@link runExternalBot} (chipzen-ai/chipzen-sdk#136).
 *
 * The server pings the lobby every 15 seconds. When the host sleeps, the
 * server times the bot out and closes the lobby, but the local socket can
 * still look open for a long time after waking, so the SDK never noticed and
 * never reconnected. A lobby that hears nothing at all for
 * `lobbySilenceTimeoutMs` is treated as dead: the socket is dropped without a
 * close handshake and the lobby reconnects with the retry policy's backoff.
 *
 * Mirrors the Python SDK's `_LobbyLiveness` in `chipzen.external`.
 */

/**
 * Default for `runExternalBot({ lobbySilenceTimeoutMs })`: how long the lobby
 * may go without receiving ANY frame before the SDK treats the socket as dead
 * and reconnects. The server pings every 15s, so 45s is three missed
 * heartbeats in a row.
 */
export const DEFAULT_LOBBY_SILENCE_TIMEOUT_MS = 45_000;

/** The two clocks {@link LobbyLiveness} reads, in milliseconds. Injectable for tests. */
export interface LivenessClocks {
  /** A monotonic clock (`performance.now()`). */
  monotonic(): number;
  /** The wall clock (`Date.now()`). */
  wall(): number;
}

const SYSTEM_CLOCKS: LivenessClocks = {
  monotonic: () => performance.now(),
  wall: () => Date.now(),
};

/**
 * Tracks how long the lobby has gone without a frame from the server.
 *
 * Measures the gap on both the monotonic clock and the wall clock and uses the
 * larger. The monotonic clock does not advance while macOS or Linux is asleep,
 * so after a long sleep it reports a gap of a few seconds; the wall clock does
 * advance, so the first check after waking sees the real gap. A wall clock
 * stepped backwards only shrinks its own reading, and the monotonic reading
 * still covers that case. A big forward step can cause one early reconnect,
 * which is harmless.
 */
export class LobbyLiveness {
  private mono = 0;
  private wallAt = 0;

  /**
   * @param timeoutMs Silence that counts as a dead lobby, or `null` to never expire.
   * @param clocks Clock sources (tests inject fakes).
   */
  constructor(
    readonly timeoutMs: number | null,
    private readonly clocks: LivenessClocks = SYSTEM_CLOCKS,
  ) {
    this.mark();
  }

  /** Record that a frame just arrived. */
  mark(): void {
    this.mono = this.clocks.monotonic();
    this.wallAt = this.clocks.wall();
  }

  /** Milliseconds since the last frame (the larger of the two clocks). */
  silentForMs(): number {
    return Math.max(this.clocks.monotonic() - this.mono, this.clocks.wall() - this.wallAt);
  }

  /** True when a timeout is set and the silence has reached it. */
  expired(): boolean {
    return this.timeoutMs !== null && this.silentForMs() >= this.timeoutMs;
  }
}

/**
 * Check a `lobbySilenceTimeoutMs` option. `undefined` takes the default and
 * `null` turns the check off; anything else must be a number above zero.
 *
 * @throws RangeError on zero, a negative number or `NaN`.
 */
export function resolveLobbySilenceTimeoutMs(value: number | null | undefined): number | null {
  if (value === undefined) return DEFAULT_LOBBY_SILENCE_TIMEOUT_MS;
  if (value === null) return null;
  if (!(value > 0)) {
    throw new RangeError(`lobbySilenceTimeoutMs must be > 0 ms or null, got ${value}`);
  }
  return value;
}

/** The warning logged when a silent lobby is dropped. */
export function _lobbySilenceLog(silentMs: number): string {
  return (
    `chipzen-sdk: lobby: nothing from the server for ${(silentMs / 1000).toFixed(1)}s ` +
    "(the host may have slept, or the connection is half-open); reconnecting"
  );
}
