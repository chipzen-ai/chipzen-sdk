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

function matchEnd(): Record<string, unknown> {
  return {
    type: "match_end",
    match_id: MATCH_ID,
    seq: 7,
    server_ts: "2026-04-13T14:35:00.000Z",
    reason: "complete",
    results: [
      { seat: 0, participant_id: "p0", rank: 1, score: 1020 },
      { seat: 1, participant_id: "p1", rank: 2, score: 980 },
    ],
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
 */
function classifyTurnAction(payload: string): ClassifyResult {
  let msg: Record<string, unknown>;
  try {
    msg = JSON.parse(payload) as Record<string, unknown>;
  } catch (err) {
    return { ok: false, message: `sent payload was not valid JSON: ${(err as Error).message}` };
  }
  if (msg.type !== "turn_action") {
    return { ok: true, message: `non-action message (${JSON.stringify(msg.type)}) — ignored` };
  }
  if (msg.request_id !== "req_1") {
    return {
      ok: false,
      message:
        `turn_action request_id ${JSON.stringify(msg.request_id)} did not echo the ` +
        `server's req_1 — the server uses request_id for correlation, idempotency, ` +
        `and action_rejected retries`,
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
  const reader = new _ScriptedReader(fullMatchScript());
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
  } catch (err) {
    const e = err as Error;
    if (e.message.startsWith("timed out")) {
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
      message: `bot raised ${e.constructor.name} during the canned exchange: ${e.message}`,
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

  const turnActions: string[] = [];
  for (const payload of socket.sent) {
    try {
      const parsed = JSON.parse(payload) as { type?: string };
      if (parsed.type === "turn_action") turnActions.push(payload);
    } catch {
      // ignore — classifyTurnAction will surface JSON errors below
    }
  }

  if (turnActions.length === 0) {
    return {
      severity: "fail",
      name,
      message:
        "bot completed the exchange but never sent a turn_action — decide() may have " +
        "returned an unexpected value or the SDK's runner hit a fallback path",
    };
  }

  const first = turnActions[0];
  if (!first) {
    // Defensive — turnActions.length > 0 above guarantees a value, but TS
    // doesn't know that without the explicit check.
    return { severity: "fail", name, message: "internal: missing first turn_action" };
  }
  const verdict = classifyTurnAction(first);
  if (!verdict.ok) {
    return { severity: "fail", name, message: verdict.message };
  }
  return {
    severity: "pass",
    name,
    message: `completed handshake + 1 hand + match_end; ${verdict.message}`,
  };
}

// ---------------------------------------------------------------------------
// Public entry point
// ---------------------------------------------------------------------------

export interface RunConformanceOptions {
  /** Per-scenario timeout in milliseconds. Default 10s. */
  timeoutMs?: number;
}

/**
 * Run every conformance scenario against `bot` and return per-check
 * results. The same bot instance is reused across scenarios — match
 * the user's production usage shape.
 */
export async function runConformanceChecks(
  bot: Bot,
  options: RunConformanceOptions = {},
): Promise<ConformanceCheck[]> {
  const timeoutMs = options.timeoutMs ?? 10_000;
  const scenarios = [runFullMatchScenario];
  const results: ConformanceCheck[] = [];
  for (const scenario of scenarios) {
    results.push(await scenario(bot, timeoutMs));
  }
  return results;
}
