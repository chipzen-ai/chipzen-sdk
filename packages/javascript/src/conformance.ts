/**
 * Protocol-conformance harness used by `chipzen-sdk validate --check-connectivity`.
 *
 * Mirrors the Python harness in `packages/python/src/chipzen/conformance.py`
 * — same scenario shape, same severity model, same canned protocol
 * exchange (handshake + one full hand + match_end). A clean run means
 * the upload pipeline will accept the bot on protocol grounds. It does
 * NOT mean the bot is good.
 *
 * Implementation: drives `_runSession` from `client.ts` against an
 * in-process mock WebSocket rather than spinning up a real server. The
 * `ws` transport is well-tested upstream; what we verify here is the
 * bot's own protocol handling, which is the only part the user's code
 * influences.
 */

import { _runSession, type SessionWebSocket, type AsyncMessageReader } from "./client.js";
import { type Bot } from "./bot.js";

export type Severity = "pass" | "warn" | "fail";

/** Single conformance scenario result. Same shape as `ValidationResult`. */
export interface ConformanceCheck {
  severity: Severity;
  name: string;
  message: string;
}

const MATCH_ID = "m_conformance_test";
const VALID_ACTION_KINDS = new Set(["fold", "check", "call", "raise", "all_in"]);

// ---------------------------------------------------------------------------
// Mock WebSocket — replays a scripted message sequence
// ---------------------------------------------------------------------------

/**
 * Scripted reader: serves a fixed list of messages then signals close
 * with `null`. Implements the same interface `_NodeWebSocketReader`
 * does so `_runSession` can drive it directly.
 */
class _ScriptedReader implements AsyncMessageReader {
  private index = 0;

  constructor(private readonly messages: string[]) {}

  async next(): Promise<string | null> {
    if (this.index >= this.messages.length) return null;
    return this.messages[this.index++] ?? null;
  }
}

/**
 * Capturing socket: records every payload the session loop sends so we
 * can verify the protocol envelopes after the scenario runs.
 */
class _CapturingSocket implements SessionWebSocket {
  readonly sent: string[] = [];
  send(data: string): void {
    this.sent.push(data);
  }
  close(): void {
    // no-op — the scripted reader controls the scenario lifetime.
  }
}

// ---------------------------------------------------------------------------
// Canned protocol messages — one full hand
// ---------------------------------------------------------------------------

function serverHello(): Record<string, unknown> {
  return {
    type: "hello",
    match_id: MATCH_ID,
    seq: 1,
    server_ts: "2026-04-13T14:30:05.123Z",
    supported_versions: ["1.0"],
    selected_version: "1.0",
    game_type: "nlhe_6max",
    capabilities: [],
  };
}

function matchStart(): Record<string, unknown> {
  return {
    type: "match_start",
    match_id: MATCH_ID,
    seq: 2,
    server_ts: "2026-04-13T14:30:06.000Z",
    seats: [
      { seat: 0, participant_id: "p0", display_name: "You", is_self: true },
      { seat: 1, participant_id: "p1", display_name: "Opp", is_self: false },
    ],
    game_config: {
      variant: "nlhe",
      starting_stack: 1000,
      small_blind: 5,
      big_blind: 10,
      ante: 0,
      total_hands: 0,
    },
    turn_timeout_ms: 5000,
  };
}

function roundStart(): Record<string, unknown> {
  return {
    type: "round_start",
    match_id: MATCH_ID,
    seq: 3,
    server_ts: "2026-04-13T14:30:07.000Z",
    round_id: "r_1",
    round_number: 1,
    state: {
      hand_number: 1,
      dealer_seat: 0,
      your_hole_cards: ["Ah", "Kd"],
      stacks: [995, 990],
      deck_commitment: "",
    },
  };
}

function turnRequest(): Record<string, unknown> {
  return {
    type: "turn_request",
    match_id: MATCH_ID,
    seq: 4,
    server_ts: "2026-04-13T14:30:07.500Z",
    seat: 0,
    request_id: "req_1",
    timeout_ms: 5000,
    valid_actions: ["fold", "call", "raise"],
    state: {
      hand_number: 1,
      phase: "preflop",
      board: [],
      your_hole_cards: ["Ah", "Kd"],
      pot: 15,
      your_stack: 995,
      opponent_stacks: [990],
      to_call: 5,
      min_raise: 20,
      max_raise: 995,
      action_history: [],
    },
  };
}

function turnResult(): Record<string, unknown> {
  return {
    type: "turn_result",
    match_id: MATCH_ID,
    seq: 5,
    server_ts: "2026-04-13T14:30:08.000Z",
    is_timeout: false,
    details: { seat: 0, action: "call", amount: 5 },
  };
}

function roundResult(): Record<string, unknown> {
  return {
    type: "round_result",
    match_id: MATCH_ID,
    seq: 6,
    server_ts: "2026-04-13T14:30:12.000Z",
    round_id: "r_1",
    round_number: 1,
    result: {
      hand_number: 1,
      winner_seats: [0],
      pot: 40,
      payouts: [{ seat: 0, amount: 40 }],
      showdown: [],
      action_history: [],
      stacks: [1020, 980],
      deck_commitment: "",
      deck_reveal: null,
    },
  };
}

function matchEnd(seq = 7): Record<string, unknown> {
  return {
    type: "match_end",
    match_id: MATCH_ID,
    seq,
    server_ts: "2026-04-13T14:35:00.000Z",
    reason: "complete",
    results: [
      { seat: 0, participant_id: "p0", rank: 1, score: 1020 },
      { seat: 1, participant_id: "p1", rank: 2, score: 980 },
    ],
  };
}

function turnRequestN(seq: number, requestId: string): Record<string, unknown> {
  return { ...turnRequest(), seq, request_id: requestId };
}

function turnResultN(seq: number): Record<string, unknown> {
  return { ...turnResult(), seq };
}

function phaseChange(seq: number, phase: string, board: string[]): Record<string, unknown> {
  return {
    type: "phase_change",
    match_id: MATCH_ID,
    seq,
    server_ts: "2026-04-13T14:30:09.000Z",
    state: { phase, board },
  };
}

function roundResultN(seq: number): Record<string, unknown> {
  return { ...roundResult(), seq };
}

/**
 * Server-side rejection of a previously-sent `turn_action`. Drives the
 * SDK's safe-fallback retry path. The SDK should respond with a
 * `turn_action` echoing this same `request_id` and a safe action
 * (`check` or `fold`) within `remaining_ms`.
 */
function actionRejected(
  seq: number,
  requestId = "req_1",
  remainingMs = 4000,
): Record<string, unknown> {
  return {
    type: "action_rejected",
    match_id: MATCH_ID,
    seq,
    server_ts: "2026-04-13T14:30:08.000Z",
    request_id: requestId,
    reason: "invalid_action",
    message: "action not in valid_actions",
    remaining_ms: remainingMs,
    valid_actions: ["check", "fold"],
  };
}

function fullMatchScript(): string[] {
  return [
    serverHello(),
    matchStart(),
    roundStart(),
    turnRequest(),
    turnResult(),
    roundResult(),
    matchEnd(),
  ].map((m) => JSON.stringify(m));
}

/**
 * Three turn_requests across preflop/flop/turn — exercises request_id
 * echo on every turn. The original full-match script only checks the
 * first action; a bug where the second or later action drops or
 * mangles the `request_id` would pass the basic harness silently.
 */
function multiTurnScript(): string[] {
  return [
    serverHello(),
    matchStart(),
    roundStart(),
    turnRequestN(4, "req_1"),
    turnResultN(5),
    phaseChange(6, "flop", ["2s", "7d", "Tc"]),
    turnRequestN(7, "req_2"),
    turnResultN(8),
    phaseChange(9, "turn", ["2s", "7d", "Tc", "Kh"]),
    turnRequestN(10, "req_3"),
    turnResultN(11),
    roundResultN(12),
    matchEnd(13),
  ].map((m) => JSON.stringify(m));
}

/**
 * One `turn_request` followed by `action_rejected` — exercises the
 * SDK's safe-fallback retry. The full-match script never delivers an
 * `action_rejected`, so the SDK's retry path goes untested in
 * conformance even though it's a routine production code path.
 */
function actionRejectedScript(): string[] {
  return [
    serverHello(),
    matchStart(),
    roundStart(),
    turnRequestN(4, "req_1"),
    actionRejected(5, "req_1"),
    turnResultN(6),
    roundResultN(7),
    matchEnd(8),
  ].map((m) => JSON.stringify(m));
}

/**
 * One `turn_request` followed by THREE consecutive `action_rejected`
 * messages. Catches a class of failure where a buggy SDK might enter
 * an infinite response loop or hang waiting for a non-rejection
 * message that never arrives. The SDK should be purely reactive: one
 * `turn_action` per incoming message, then exit cleanly on
 * `match_end`.
 */
function retryStormScript(): string[] {
  return [
    serverHello(),
    matchStart(),
    roundStart(),
    turnRequestN(4, "req_1"),
    actionRejected(5, "req_1"),
    actionRejected(6, "req_1"),
    actionRejected(7, "req_1"),
    turnResultN(8),
    roundResultN(9),
    matchEnd(10),
  ].map((m) => JSON.stringify(m));
}

// ---------------------------------------------------------------------------
// Scenario evaluation
// ---------------------------------------------------------------------------

interface ClassifyResult {
  ok: boolean;
  message: string;
}

/**
 * Validate a single payload the bot sent. Returns ok=true with a
 * non-fatal note for messages that aren't `turn_action`; returns
 * ok=false with a diagnostic for anything malformed.
 *
 * @param expectedRequestId The request_id the server sent for the turn
 *   this action is responding to. The SDK MUST echo this value back so
 *   the server can correlate, deduplicate, and route action_rejected
 *   retries.
 */
function classifyTurnAction(payload: string, expectedRequestId = "req_1"): ClassifyResult {
  let msg: Record<string, unknown>;
  try {
    msg = JSON.parse(payload) as Record<string, unknown>;
  } catch (err) {
    return { ok: false, message: `sent payload was not valid JSON: ${(err as Error).message}` };
  }
  if (msg.type !== "turn_action") {
    return { ok: true, message: `non-action message (${JSON.stringify(msg.type)}) — ignored` };
  }
  if (msg.request_id !== expectedRequestId) {
    return {
      ok: false,
      message:
        `turn_action request_id ${JSON.stringify(msg.request_id)} did not echo the ` +
        `server's ${JSON.stringify(expectedRequestId)} — the server uses request_id for ` +
        `correlation, idempotency, and action_rejected retries`,
    };
  }
  // The wire shape is `{type, action, params}` (see Action.toWire). Older
  // bots may also nest action inside params, so check both spots.
  const params = (msg.params as Record<string, unknown> | undefined) ?? {};
  const action = (msg.action as string | undefined) ?? (params.action as string | undefined);
  if (!action || !VALID_ACTION_KINDS.has(action)) {
    return {
      ok: false,
      message: `turn_action action ${JSON.stringify(action)} is not in the legal set`,
    };
  }
  return { ok: true, message: `sent turn_action: action=${JSON.stringify(action)}` };
}

/** Filter the captured-send buffer down to parsed `turn_action` payloads. */
function extractTurnActions(sent: string[]): Array<Record<string, unknown>> {
  const out: Array<Record<string, unknown>> = [];
  for (const payload of sent) {
    try {
      const parsed = JSON.parse(payload) as Record<string, unknown>;
      if (parsed.type === "turn_action") out.push(parsed);
    } catch {
      // ignore — non-JSON sends shouldn't happen in practice
    }
  }
  return out;
}

interface DriveResult {
  socket: _CapturingSocket;
  error: Error | null;
}

/**
 * Drive `_runSession` against `script` with a per-scenario timeout.
 * Returns the captured socket plus any error caught (timeout or
 * runtime exception). Callers inspect `socket.sent` for what the bot
 * emitted.
 *
 * Note on hung bots: `setTimeout` only fires when the event loop is
 * idle. A bot whose `decide()` busy-loops or calls a synchronous
 * blocking primitive (e.g. `Atomics.wait` on a sync int) starves the
 * event loop and prevents the timeout from firing. The Python SDK has
 * a daemon-thread watchdog for this; the JavaScript SDK does not yet
 * because the equivalent (a Worker thread) is heavier-weight and
 * deferred to a follow-up. Bots that block the loop will hang the
 * harness until process termination.
 */
async function driveSession(
  bot: Bot,
  script: string[],
  timeoutMs: number,
): Promise<DriveResult> {
  const reader = new _ScriptedReader(script);
  const socket = new _CapturingSocket();
  try {
    await withTimeout(
      _runSession(
        socket,
        bot,
        {
          matchId: MATCH_ID,
          token: "conformance",
          ticket: null,
          clientName: "chipzen-sdk-conformance",
          clientVersion: "0.0.0",
        },
        reader,
      ),
      timeoutMs,
    );
    return { socket, error: null };
  } catch (err) {
    return { socket, error: err as Error };
  }
}

async function withTimeout<T>(p: Promise<T>, timeoutMs: number): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(
      () => reject(new Error(`timed out after ${timeoutMs}ms`)),
      timeoutMs,
    );
  });
  try {
    return await Promise.race([p, timeout]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

async function runFullMatchScenario(
  bot: Bot,
  timeoutMs: number,
): Promise<ConformanceCheck> {
  const name = "connectivity_full_match";
  const { socket, error } = await driveSession(bot, fullMatchScript(), timeoutMs);

  if (error) {
    if (error.message.startsWith("timed out")) {
      return {
        severity: "fail",
        name,
        message:
          `bot did not complete the canned full-match exchange within ${timeoutMs}ms — ` +
          `either decide() is too slow or the bot is hung waiting on something`,
      };
    }
    return {
      severity: "fail",
      name,
      message: `bot raised ${error.constructor.name} during the canned exchange: ${error.message}`,
    };
  }

  if (socket.sent.length === 0) {
    return {
      severity: "fail",
      name,
      message:
        "bot did not send any messages during the canned exchange — at minimum the " +
        "client should have sent authenticate / hello / turn_action",
    };
  }

  const turnActions = extractTurnActions(socket.sent);
  if (turnActions.length === 0) {
    return {
      severity: "fail",
      name,
      message:
        "bot completed the exchange but never sent a turn_action — decide() may have " +
        "returned an unexpected value or the SDK's runner hit a fallback path",
    };
  }

  const verdict = classifyTurnAction(JSON.stringify(turnActions[0]));
  if (!verdict.ok) {
    return { severity: "fail", name, message: verdict.message };
  }
  return {
    severity: "pass",
    name,
    message: `completed handshake + 1 hand + match_end; ${verdict.message}`,
  };
}

/**
 * Drive three turn_requests and verify request_id is echoed correctly
 * on each. The full-match scenario only checks the first action; a
 * bug where the second-or-later action drops or rewrites the
 * `request_id` would slip through.
 */
async function runMultiTurnScenario(
  bot: Bot,
  timeoutMs: number,
): Promise<ConformanceCheck> {
  const name = "multi_turn_request_id_echo";
  const { socket, error } = await driveSession(bot, multiTurnScript(), timeoutMs);

  if (error) {
    if (error.message.startsWith("timed out")) {
      return {
        severity: "fail",
        name,
        message: `bot did not complete the multi-turn exchange within ${timeoutMs}ms`,
      };
    }
    return {
      severity: "fail",
      name,
      message: `bot raised ${error.constructor.name} during the multi-turn exchange: ${error.message}`,
    };
  }

  const turnActions = extractTurnActions(socket.sent);
  const expectedIds = ["req_1", "req_2", "req_3"];

  if (turnActions.length < expectedIds.length) {
    return {
      severity: "fail",
      name,
      message:
        `expected ${expectedIds.length} turn_actions across preflop/flop/turn, ` +
        `saw only ${turnActions.length} — bot stopped responding partway through the hand`,
    };
  }

  for (let i = 0; i < expectedIds.length; i++) {
    const expectedId = expectedIds[i]!;
    const verdict = classifyTurnAction(JSON.stringify(turnActions[i]), expectedId);
    if (!verdict.ok) {
      return {
        severity: "fail",
        name,
        message: `turn ${i + 1} of 3 failed: ${verdict.message}`,
      };
    }
  }

  return {
    severity: "pass",
    name,
    message:
      `all ${expectedIds.length} turn_actions echoed request_id correctly across ` +
      `preflop/flop/turn`,
  };
}

/**
 * Drive a turn_request followed by action_rejected and verify the SDK
 * retries safely. On rejection the SDK should send a second
 * `turn_action` echoing the same `request_id` and using a safe action
 * (`check` or `fold`).
 */
async function runActionRejectedScenario(
  bot: Bot,
  timeoutMs: number,
): Promise<ConformanceCheck> {
  const name = "action_rejected_recovery";
  const { socket, error } = await driveSession(bot, actionRejectedScript(), timeoutMs);

  if (error) {
    if (error.message.startsWith("timed out")) {
      return {
        severity: "fail",
        name,
        message:
          `bot did not complete the action_rejected scenario within ${timeoutMs}ms — ` +
          `the SDK's safe-fallback retry path may be hung`,
      };
    }
    return {
      severity: "fail",
      name,
      message: `bot raised ${error.constructor.name} during action_rejected handling: ${error.message}`,
    };
  }

  const turnActions = extractTurnActions(socket.sent);
  if (turnActions.length < 2) {
    return {
      severity: "fail",
      name,
      message:
        `expected 2 turn_actions (initial + safe-fallback retry), saw ${turnActions.length}; ` +
        `the SDK did not respond to the action_rejected message`,
    };
  }

  const retry = turnActions[1]!;
  if (retry.request_id !== "req_1") {
    return {
      severity: "fail",
      name,
      message:
        `safe-fallback retry used request_id ${JSON.stringify(retry.request_id)} instead of ` +
        `the original "req_1" — server-side correlation will fail`,
    };
  }

  const retryParams = (retry.params as Record<string, unknown> | undefined) ?? {};
  const retryAction =
    (retry.action as string | undefined) ?? (retryParams.action as string | undefined);
  if (retryAction !== "check" && retryAction !== "fold") {
    return {
      severity: "fail",
      name,
      message:
        `safe-fallback retry sent action ${JSON.stringify(retryAction)}; ` +
        `expected "check" or "fold" (the only universally-safe actions when valid_actions is unknown)`,
    };
  }

  return {
    severity: "pass",
    name,
    message:
      `action_rejected handled cleanly: original action sent, retry sent ${JSON.stringify(retryAction)} ` +
      `with original request_id`,
  };
}

/**
 * Drive a turn_request followed by THREE action_rejected messages
 * back-to-back. Catches a class of failure where a buggy SDK might
 * hang after the first rejection or enter an infinite send loop. The
 * SDK should respond reactively (one safe-fallback per rejection) and
 * exit cleanly when match_end arrives.
 */
async function runRetryStormScenario(
  bot: Bot,
  timeoutMs: number,
): Promise<ConformanceCheck> {
  const name = "retry_storm_bounded";
  const { socket, error } = await driveSession(bot, retryStormScript(), timeoutMs);

  if (error) {
    if (error.message.startsWith("timed out")) {
      return {
        severity: "fail",
        name,
        message:
          `bot did not complete the retry-storm scenario within ${timeoutMs}ms — ` +
          `the SDK may be stuck in a retry loop`,
      };
    }
    return {
      severity: "fail",
      name,
      message: `bot raised ${error.constructor.name} during retry-storm handling: ${error.message}`,
    };
  }

  const turnActions = extractTurnActions(socket.sent);
  // Expected: 1 initial + 3 retries = 4 turn_actions total. The SDK is
  // reactive: each action_rejected provokes exactly one retry.
  const expectedCount = 4;
  if (turnActions.length !== expectedCount) {
    return {
      severity: turnActions.length < expectedCount ? "fail" : "warn",
      name,
      message:
        `expected ${expectedCount} turn_actions (1 initial + 3 retries) under retry storm, ` +
        `saw ${turnActions.length} — the SDK's retry behavior may be unbounded or may have ` +
        `stopped responding`,
    };
  }

  return {
    severity: "pass",
    name,
    message:
      `SDK responded to all 3 action_rejected messages with safe-fallback retries ` +
      `(${expectedCount} turn_actions total) and exited cleanly on match_end`,
  };
}

// ---------------------------------------------------------------------------
// Public entry point
// ---------------------------------------------------------------------------

export interface RunConformanceOptions {
  /** Per-scenario timeout in milliseconds. Default 10s. */
  timeoutMs?: number;
}

type Scenario = (bot: Bot, timeoutMs: number) => Promise<ConformanceCheck>;

export const SCENARIOS: ReadonlyArray<{ name: string; fn: Scenario }> = [
  { name: "connectivity_full_match", fn: runFullMatchScenario },
  { name: "multi_turn_request_id_echo", fn: runMultiTurnScenario },
  { name: "action_rejected_recovery", fn: runActionRejectedScenario },
  { name: "retry_storm_bounded", fn: runRetryStormScenario },
];

/**
 * Run every conformance scenario against `bot` and return per-check
 * results. The same bot instance is reused across scenarios — matches
 * the user's production usage shape.
 *
 * Note: the JavaScript harness does not currently include a hard
 * wall-clock watchdog. A bot whose `decide()` blocks the event loop
 * (busy-loop, sync `Atomics.wait`) will hang the harness because
 * `setTimeout` cannot fire while the loop is starved. The Python SDK
 * uses a daemon thread for this; the JS equivalent is a Worker and is
 * deferred to a follow-up.
 */
export async function runConformanceChecks(
  bot: Bot,
  options: RunConformanceOptions = {},
): Promise<ConformanceCheck[]> {
  const timeoutMs = options.timeoutMs ?? 10_000;
  const results: ConformanceCheck[] = [];
  for (const { fn } of SCENARIOS) {
    results.push(await fn(bot, timeoutMs));
  }
  return results;
}
