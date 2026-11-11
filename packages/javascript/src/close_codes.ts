/**
 * Match-socket close codes that end the match for this bot (no reconnect).
 *
 * A match socket that closes without a `match_end` is normally a drop: the
 * SDK reconnects within the retry policy and the platform resumes the seat.
 * Some 4xxx closes are not drops. They say the server will not take this seat
 * back on this match, whatever the client does, so reconnecting only loops
 * until the retry budget runs out (`docs/protocol/TRANSPORT-PROTOCOL.md`
 * section 15, `docs/ERROR-CODES.md`).
 *
 * On a code in {@link TERMINAL_CLOSE_CODES} the SDK stops at once: it logs one
 * line with the code and the server's reason and throws
 * {@link ProtocolCloseError}, which carries both. `4013` (no mutually
 * supported protocol version) is the first the platform sends through to
 * remote bots.
 *
 * Left out on purpose, so they stay a drop and are retried as before:
 *
 * - `4000`: seat taken over by another connection; it has its own path
 *   (`SeatReplacedError`).
 * - `4001` / `4002`: the external-API gateway sends these itself, and a
 *   `4002` can mean the executor is not registered yet, a race a retry fixes.
 * - `4004`, `4007`, `4008`, `4009`, `4011`: each has a use where a reconnect
 *   can succeed (for example `4011 duplicate_participant` while the old
 *   socket is still being reaped, or a connect rate limit that passes).
 *
 * Normal closes (`1000` / `1001`) and closes with no code are unchanged.
 */

/** Symbolic names from TRANSPORT-PROTOCOL.md section 15 for the terminal codes. */
export const TERMINAL_CLOSE_NAMES: Readonly<Record<number, string>> = Object.freeze({
  4003: "match_full",
  4005: "match_ended",
  4006: "participant_not_found",
  4010: "reconnect_expired",
  4012: "responsible_gaming",
  4013: "protocol_mismatch",
});

/** Close codes after which the SDK does not reconnect the match socket. */
export const TERMINAL_CLOSE_CODES: ReadonlySet<number> = new Set(
  Object.keys(TERMINAL_CLOSE_NAMES).map(Number),
);

/**
 * The server closed the match socket with a terminal 4xxx code. The match is
 * over for this bot: reconnecting would be refused the same way. `code` is
 * the close code and `reason` the server's close reason (`""` when it sent
 * none), for example `4013` and `"No mutually supported protocol version"`.
 */
export class ProtocolCloseError extends Error {
  readonly code: number;
  readonly reason: string;

  constructor(code: number, reason: string = "") {
    const label = `${code} ${TERMINAL_CLOSE_NAMES[code] ?? ""}`.trim();
    super(`server closed the match socket with ${label}: ${reason || "no reason given"}`);
    this.name = "ProtocolCloseError";
    this.code = code;
    this.reason = reason;
  }
}

/** The {@link ProtocolCloseError} for a close code + reason, or `null` if not terminal. */
export function _protocolCloseFor(
  code: number | undefined,
  reason: string | undefined,
): ProtocolCloseError | null {
  if (code === undefined || !TERMINAL_CLOSE_CODES.has(code)) return null;
  return new ProtocolCloseError(code, reason ?? "");
}

/** The one line logged when a terminal close ends a match. */
export function _protocolCloseLog(matchId: string, err: ProtocolCloseError): string {
  return `chipzen-sdk: match ${matchId}: ${err.message}; not reconnecting`;
}
