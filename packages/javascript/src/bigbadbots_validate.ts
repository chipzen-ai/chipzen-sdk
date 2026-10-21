/**
 * Big Bad Bots checks for `chipzen-sdk validate`: answer shapes and the
 * conformance fixtures.
 *
 * `chipzen-sdk validate` runs these against a bot whose game is
 * `bigbadbots` (a `BigBadBotsBot` subclass, or any bot with `--game
 * bigbadbots`). It hands the bot every request in the conformance fixtures,
 * real `turn_request` messages from recorded matches covering all four
 * calls (`choose_body`, `bid`, `fit` and `act`), and judges each answer the
 * way the server does (protocol section 3.7):
 *
 * - `ok`: applied as sent;
 * - `corrected`: well-shaped but not legal (a body that is not on the
 *   offer, a bid outside `0..me.credits`, a fitting entry that does not
 *   fit). The server applies the nearest legal answer and logs a violation;
 * - `malformed`: not the call's shape. The server rejects it and, unless a
 *   retry lands in time, applies the call's default.
 *
 * It also reports a handler that throws (or calls `process.exit()`), one
 * that mutates the state it is handed, and one that is slow for the
 * per-tick deadline.
 *
 * The fixtures are the Python SDK's (`chipzen/fixtures/bigbadbots/`),
 * bundled byte for byte, so both SDKs are held to the same recorded frames
 * and the same verdicts. Mirrors the Python SDK's
 * `chipzen.bigbadbots_validate`.
 *
 * See `docs/protocol/BIGBADBOTS-GAME-STATE-PROTOCOL.md`; section numbers
 * below refer to it.
 */

import {
  BIGBADBOTS_STATE_SHAPE,
  FIGHT_ACTIONS,
  StateParseError,
  parseBigBadBotsTurnRequest,
  toWireState,
  type BigBadBotsTurnState,
} from "./bigbadbots.js";

export type BigBadBotsSeverity = "pass" | "warn" | "fail";

/** One check result, the same shape as `ValidationResult`. */
export interface BigBadBotsCheck {
  severity: BigBadBotsSeverity;
  name: string;
  message: string;
}

/** What the server does with one answer. */
export interface BigBadBotsAnswerCheck {
  /** `"ok"`, `"corrected"` or `"malformed"`. */
  readonly verdict: "ok" | "corrected" | "malformed";
  /** Why, for `corrected` and `malformed`; empty for `ok`. */
  readonly reason: string;
}

/** One conformance fixture: a recorded request, an answer as sent, and the server's verdict on it. */
export interface BigBadBotsFixture {
  /** The recorded match the pair comes from. */
  source: string;
  /** A `turn_request` exactly as the server sends it. */
  request: Record<string, unknown>;
  /** A `turn_action` for that request, as the Python client sent it: JSON text, which may not be valid JSON. */
  answer: string;
  /** What the server does with `answer`. */
  expect: "ok" | "corrected" | "malformed";
}

/** The four calls, in match order, by `state.phase`. */
export const BIGBADBOTS_CALL_OF_PHASE: Readonly<Record<string, BigBadBotsCall>> = Object.freeze({
  body_shop: "choose_body",
  auction: "bid",
  fitting: "fit",
  fight: "act",
});
export const BIGBADBOTS_CALLS = ["choose_body", "bid", "fit", "act"] as const;
export type BigBadBotsCall = (typeof BIGBADBOTS_CALLS)[number];

/** Layer 1 cap on a bot message, as sent (TRANSPORT-PROTOCOL section 3.5). */
export const MAX_BOT_MESSAGE_BYTES = 4096;

/** Every integer on the wire is signed 64-bit (section 4). */
const INT64_MIN = -(2n ** 63n);
const INT64_MAX = 2n ** 63n - 1n;

/** A rule-set id (a body or a part): lower case, digits and `_`, at most 32 characters. */
const ID = /^[a-z][a-z0-9_]{0,31}$/;

/** A fitting's slot index: a decimal string without leading zeros, at most two digits. */
const SLOT = /^(?:0|[1-9][0-9]?)$/;
const MAX_FIT_ENTRIES = 8;

/** The fields a `turn_action` may carry (Layer 1 section 9.2). */
const ENVELOPE = new Set(["type", "match_id", "request_id", "action", "params"]);
const PARAM_KEY: Record<string, string> = { choose_body: "body", bid: "amount", fit: "fit" };

/**
 * The `match_start.game_config` the checks open with: the protocol doc's
 * published, abridged example (section 4.1). A bot must answer legally with
 * only these sections; a real match sends the full table.
 */
export const BIGBADBOTS_VALIDATE_GAME_CONFIG: Readonly<Record<string, unknown>> = deepFreeze({
  variant: BIGBADBOTS_STATE_SHAPE,
  rule_set_id: "bigbadbots-1.0",
  num_players: 2,
  rule_set: {
    gearup: { budget: 1000, body_offer_size: 4, auction_lots: 6 },
    arena: { width: 12.0, start_x: [4.0, 8.0], start_jitter: 1.0, min_separation: 1.15 },
    rounds: { ticks_per_second: 10, round_ticks: 600, rounds_to_win: 2, max_rounds: 5 },
    forfeit: { grace_seconds: 5, rounds_for_match: 2 },
    deadline: {
      deadline_ms: 100,
      time_bank_ms: 3000,
      default_action_policy: "repeat_block_or_move",
      late_answer: "drop",
      miss_counts_toward_forfeit: true,
    },
  },
});
const DEADLINE_MS = 100;

/** What the server applies when a call gets no acceptable answer (section 3.5). */
const DEFAULT_OF_CALL: Record<BigBadBotsCall, string> = {
  choose_body: "the first body on the offer",
  bid: "a bid of 0",
  fit: "the empty fitting",
  act: "idle (or the previous block/move)",
};

function deepFreeze<T>(value: T): T {
  if (value !== null && typeof value === "object") {
    for (const child of Object.values(value as Record<string, unknown>)) deepFreeze(child);
    Object.freeze(value);
  }
  return value;
}

function isObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

// ---------------------------------------------------------------------------
// Judging one answer
// ---------------------------------------------------------------------------

const OK: BigBadBotsAnswerCheck = Object.freeze({ verdict: "ok", reason: "" });

function malformed(reason: string): BigBadBotsAnswerCheck {
  return { verdict: "malformed", reason };
}

function corrected(reason: string): BigBadBotsAnswerCheck {
  return { verdict: "corrected", reason };
}

function short(value: unknown, limit = 60): string {
  let text: string;
  try {
    text =
      typeof value === "string"
        ? JSON.stringify(value)
        : value === undefined
          ? "undefined"
          : typeof value === "bigint"
            ? `${value}n`
            : (JSON.stringify(value) ?? String(value));
  } catch {
    text = `<unprintable ${typeof value}>`;
  }
  return text.length <= limit ? text : `${text.slice(0, limit - 3)}...`;
}

/**
 * The call a `turn_request` asks for, from its `state.phase`.
 *
 * @throws RangeError if the request has no Big Bad Bots phase.
 */
export function bigBadBotsCallOf(request: Record<string, unknown>): BigBadBotsCall {
  const state = request.state;
  const phase = isObject(state) ? state.phase : undefined;
  const call = typeof phase === "string" ? BIGBADBOTS_CALL_OF_PHASE[phase] : undefined;
  if (call === undefined) {
    throw new RangeError(`not a Big Bad Bots turn_request (phase ${JSON.stringify(phase)})`);
  }
  return call;
}

/** Something with the SDK `Action`'s `toWire()`; checked by shape because a bot may import its own copy of the SDK. */
interface WireAction {
  toWire(): { action: unknown; params?: unknown };
}

function isWireAction(value: unknown): value is WireAction {
  return (
    value !== null &&
    typeof value === "object" &&
    typeof (value as { toWire?: unknown }).toWire === "function"
  );
}

/**
 * The `turn_action` the JavaScript client sends for `action`, as text.
 * Built exactly as the session loop builds it.
 *
 * @throws if `action.toWire()` throws or its result cannot be serialised.
 */
export function bigBadBotsAnswerText(request: Record<string, unknown>, action: WireAction): string {
  return JSON.stringify({
    type: "turn_action",
    match_id: request.match_id,
    request_id: request.request_id,
    ...action.toWire(),
  });
}

/**
 * Judge what a bot's handler returned for `request`.
 *
 * `answer` is the handler's return value. Anything but an `Action` is
 * malformed: the client cannot send it, and sends the call's default
 * instead. (A Promise is not an Action: the handlers are synchronous.)
 */
export function checkBigBadBotsAnswer(
  request: Record<string, unknown>,
  answer: unknown,
): BigBadBotsAnswerCheck {
  if (answer instanceof Promise) {
    return malformed("returned a Promise, not an Action (the handlers are synchronous)");
  }
  if (!isWireAction(answer)) return malformed(`returned ${short(answer)}, not an Action`);
  let text: string;
  try {
    text = bigBadBotsAnswerText(request, answer);
  } catch (err) {
    return malformed(`the answer cannot be sent as JSON (${(err as Error).message})`);
  }
  return checkBigBadBotsTurnAction(request, text);
}

/**
 * The literal a JSON number was written as, for `"amount"`: JSON.parse turns
 * `5.0` into `5` and rounds integers past 2^53, but the server reads the text.
 * The last occurrence wins, as with JSON.parse.
 */
function amountLexeme(text: string): string | null {
  const pattern = /"amount"\s*:\s*(-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)/g;
  let last: string | null = null;
  for (const match of text.matchAll(pattern)) last = match[1] ?? null;
  return last;
}

/** Judge a `turn_action` exactly as sent (its JSON text) against `request`. */
export function checkBigBadBotsTurnAction(
  request: Record<string, unknown>,
  text: string,
): BigBadBotsAnswerCheck {
  const size = Buffer.byteLength(text, "utf-8");
  if (size > MAX_BOT_MESSAGE_BYTES) {
    return malformed(
      `the message is ${size} bytes; a bot message is at most ${MAX_BOT_MESSAGE_BYTES} ` +
        "and a longer one closes the connection (4008)",
    );
  }
  let message: unknown;
  try {
    message = JSON.parse(text);
  } catch (err) {
    return malformed(
      `the message is not valid JSON (${(err as Error).message}); NaN and Infinity are not JSON`,
    );
  }
  if (!isObject(message)) return malformed("the message is not a JSON object");
  const extra = Object.keys(message)
    .filter((key) => !ENVELOPE.has(key))
    .sort();
  if (extra.length > 0) return malformed(`unexpected top-level keys ${JSON.stringify(extra)}`);
  if (message.type !== "turn_action") {
    return malformed(`type is ${short(message.type)}, not "turn_action"`);
  }
  if (message.request_id !== request.request_id) {
    return malformed("request_id does not echo the request's");
  }

  const call = bigBadBotsCallOf(request);
  const state = request.state as Record<string, unknown>;
  const action = message.action;
  const requested = request.valid_actions;
  const valid: readonly unknown[] =
    Array.isArray(requested) && requested.length > 0
      ? requested
      : call !== "act"
        ? [call]
        : FIGHT_ACTIONS;
  if (typeof action !== "string" || !valid.includes(action)) {
    return malformed(`action ${short(action)} is not one of ${JSON.stringify(valid)}`);
  }

  if (!("params" in message)) {
    if (call === "act") return OK;
    return malformed(`${call} needs params {"${PARAM_KEY[call]}": ...}`);
  }
  const params = message.params;
  if (!isObject(params)) return malformed(`params is ${short(params)}, not an object`);
  if (call === "act") {
    return Object.keys(params).length > 0
      ? malformed(`act takes no params, got ${short(params)}`)
      : OK;
  }

  const key = PARAM_KEY[call]!;
  const keys = Object.keys(params).sort();
  if (keys.length !== 1 || keys[0] !== key) {
    return malformed(
      `${call} params must be exactly {"${key}": ...}, got keys ${JSON.stringify(keys)}`,
    );
  }
  const value = params[key];
  if (call === "choose_body") return checkBody(state, value);
  if (call === "bid") return checkBid(state, value, amountLexeme(text));
  return checkFit(state, value);
}

function isId(value: unknown): value is string {
  return typeof value === "string" && ID.test(value);
}

function checkBody(state: Record<string, unknown>, body: unknown): BigBadBotsAnswerCheck {
  if (!isId(body)) return malformed(`body ${short(body)} is not a body id`);
  const offer = (Array.isArray(state.offer) ? state.offer : [])
    .filter(isObject)
    .map((b) => b.id);
  if (!offer.includes(body)) {
    return corrected(
      `body ${JSON.stringify(body)} is not on your offer ${JSON.stringify(offer)}; ` +
        "the cheapest one is bought",
    );
  }
  return OK;
}

function checkBid(
  state: Record<string, unknown>,
  amount: unknown,
  lexeme: string | null,
): BigBadBotsAnswerCheck {
  if (typeof amount !== "number") return malformed(`bid ${short(amount)} is not a JSON integer`);
  // JSON.parse has already rejected NaN and Infinity; what is left is a
  // number literal, which must be written as an integer.
  const literal = lexeme ?? String(amount);
  if (!/^-?(?:0|[1-9]\d*)$/.test(literal)) {
    return malformed(`bid ${literal} is not a JSON integer`);
  }
  const value = BigInt(literal);
  if (value < INT64_MIN || value > INT64_MAX) {
    return malformed(`bid ${short(literal)} is outside the signed 64-bit range`);
  }
  const me = state.me;
  const credits = isObject(me) ? me.credits : undefined;
  if (typeof credits === "number" && Number.isInteger(credits)) {
    const max = BigInt(credits);
    if (value < 0n || value > max) {
      const clamped = value < 0n ? 0n : value > max ? max : value;
      return corrected(`bid ${value} is outside 0..${credits}; it is clamped to ${clamped}`);
    }
  }
  return OK;
}

function checkFit(state: Record<string, unknown>, fit: unknown): BigBadBotsAnswerCheck {
  if (!isObject(fit)) {
    return malformed(`fit is ${short(fit)}, not an object of slot index to part id`);
  }
  const entries = Object.entries(fit);
  if (entries.length > MAX_FIT_ENTRIES) {
    return malformed(`fit has ${entries.length} entries; at most ${MAX_FIT_ENTRIES}`);
  }
  for (const [slot, part] of entries) {
    if (!SLOT.test(slot)) {
      return malformed(`fit slot ${JSON.stringify(slot)} is not a slot index ("0", "1", ...)`);
    }
    if (!isId(part)) return malformed(`fit slot ${slot} holds ${short(part)}, not a part id`);
  }

  const slots = Array.isArray(state.slots) ? (state.slots as unknown[]) : [];
  const owned = (Array.isArray(state.owned) ? state.owned : []).filter(isObject);
  const slotOfPart = new Map<unknown, unknown>(owned.map((p) => [p.id, p.slot]));
  const left = new Map<unknown, number>();
  for (const p of owned) left.set(p.id, (left.get(p.id) ?? 0) + 1);
  const dropped: string[] = [];
  for (const [slot, part] of entries as Array<[string, string]>) {
    const index = Number(slot);
    if (index >= slots.length) {
      dropped.push(`slot ${slot} does not exist`);
    } else if ((left.get(part) ?? 0) <= 0) {
      dropped.push(`${JSON.stringify(part)} is not a part you own (or is already fitted)`);
    } else if (slotOfPart.get(part) !== slots[index]) {
      dropped.push(
        `${JSON.stringify(part)} is a ${String(slotOfPart.get(part))} part, ` +
          `slot ${slot} is ${String(slots[index])}`,
      );
    } else {
      left.set(part, (left.get(part) ?? 0) - 1);
    }
  }
  if (dropped.length > 0) return corrected(`fit entries dropped: ${dropped.join("; ")}`);
  return OK;
}

// ---------------------------------------------------------------------------
// The fixtures
// ---------------------------------------------------------------------------

type FixtureFile = { call: string; pairs: BigBadBotsFixture[] };

const fixtureCache = new Map<BigBadBotsCall, readonly BigBadBotsFixture[]>();

async function importFixtureFile(call: BigBadBotsCall): Promise<FixtureFile> {
  // One static import per file, loaded only when validate runs, so the
  // fixtures stay out of the code path every bot loads.
  switch (call) {
    case "choose_body":
      return (await import("./data/bigbadbots/fixtures/choose_body.json", { with: { type: "json" } }))
        .default as FixtureFile;
    case "bid":
      return (await import("./data/bigbadbots/fixtures/bid.json", { with: { type: "json" } }))
        .default as FixtureFile;
    case "fit":
      return (await import("./data/bigbadbots/fixtures/fit.json", { with: { type: "json" } }))
        .default as FixtureFile;
    case "act":
      return (await import("./data/bigbadbots/fixtures/act.json", { with: { type: "json" } }))
        .default as FixtureFile;
  }
}

/**
 * The conformance fixtures: `{source, request, answer, expect}` pairs.
 *
 * `request` is a `turn_request`; `answer` is a `turn_action` as sent (its
 * JSON text, which may not be valid JSON); `expect` is `"ok"`,
 * `"corrected"` or `"malformed"`. `call` picks one call's fixtures; the
 * default is all four, in match order. Returns fresh copies.
 */
export async function loadBigBadBotsFixtures(call?: BigBadBotsCall): Promise<BigBadBotsFixture[]> {
  const calls = call === undefined ? BIGBADBOTS_CALLS : [call];
  const out: BigBadBotsFixture[] = [];
  for (const c of calls) {
    let pairs = fixtureCache.get(c);
    if (pairs === undefined) {
      pairs = (await importFixtureFile(c)).pairs;
      fixtureCache.set(c, pairs);
    }
    out.push(...pairs.map((pair) => structuredClone(pair)));
  }
  return out;
}

// ---------------------------------------------------------------------------
// Running a bot through the fixtures
// ---------------------------------------------------------------------------

/** The two methods the checks call on a bot. */
export interface BigBadBotsCheckable {
  decide(state: BigBadBotsTurnState): unknown;
  onMatchStart?(matchInfo: Record<string, unknown>): void;
}

/** Options for {@link runBigBadBotsChecks}. */
export interface RunBigBadBotsChecksOptions {
  /** The pairs to run; default every bundled fixture. */
  fixtures?: BigBadBotsFixture[];
}

interface CallReport {
  asked: number;
  crashes: string[];
  malformed: string[];
  corrected: string[];
  slowestMs: number;
  overDeadline: number;
  overTimeout: number;
}

function snapshot(state: BigBadBotsTurnState): string {
  return JSON.stringify([state.validActions, toWireState(state)]);
}

function examples(items: string[]): string {
  const shown = items.slice(0, 2).join("; ");
  return shown + (items.length > 2 ? `; and ${items.length - 2} more` : "");
}

/** Thrown in place of exiting when a handler calls `process.exit()` during the checks. */
class ExitCalled extends Error {
  constructor(code: unknown) {
    super(`process.exit(${code === undefined ? "" : String(code)})`);
    this.name = "ExitCalled";
  }
}

function errorText(err: unknown): string {
  if (err instanceof Error) return `${err.name}: ${short(err.message, 80).replace(/^"|"$/g, "")}`;
  return `threw ${short(err, 80)}`;
}

/**
 * Answer every fixture request with `bot` and judge each answer.
 *
 * The bot first gets a `match_start` carrying
 * {@link BIGBADBOTS_VALIDATE_GAME_CONFIG}, then each request in match order,
 * through `bot.decide` (which a `BigBadBotsBot` routes to `chooseBody`,
 * `bid`, `fit` or `act`). Nothing a handler does escapes: a throw is
 * reported, and a `process.exit()` call is caught and reported instead of
 * ending the process.
 *
 * Returns one `bigbadbots_<call>` result per call, plus
 * `bigbadbots_state_mutation`, `bigbadbots_timing` and, when they apply,
 * `bigbadbots_match_start`, `bigbadbots_crash_every_tick` and
 * `bigbadbots_exit`.
 */
export async function runBigBadBotsChecks(
  bot: BigBadBotsCheckable,
  options: RunBigBadBotsChecksOptions = {},
): Promise<BigBadBotsCheck[]> {
  const pairs = options.fixtures ?? (await loadBigBadBotsFixtures());
  const results: BigBadBotsCheck[] = [];
  const exits: string[] = [];

  const realExit = process.exit;
  process.exit = ((code?: unknown) => {
    throw new ExitCalled(code);
  }) as typeof process.exit;
  try {
    const matchStart = {
      type: "match_start",
      match_id: "fixture-validate",
      turn_timeout_ms: 1000,
      game_config: structuredClone(BIGBADBOTS_VALIDATE_GAME_CONFIG),
    };
    try {
      bot.onMatchStart?.(matchStart);
    } catch (err) {
      results.push({
        severity: "fail",
        name: "bigbadbots_match_start",
        message: `onMatchStart() threw ${errorText(err)}`,
      });
      if (err instanceof ExitCalled) exits.push(`onMatchStart() called ${err.message}`);
    }

    const reports = Object.fromEntries(
      BIGBADBOTS_CALLS.map((call) => [
        call,
        {
          asked: 0,
          crashes: [],
          malformed: [],
          corrected: [],
          slowestMs: 0,
          overDeadline: 0,
          overTimeout: 0,
        } as CallReport,
      ]),
    ) as Record<BigBadBotsCall, CallReport>;
    const mutated: string[] = [];

    for (const pair of pairs) {
      const request = pair.request;
      const call = bigBadBotsCallOf(request);
      const report = reports[call];
      let state: BigBadBotsTurnState;
      try {
        state = parseBigBadBotsTurnRequest(request);
      } catch (err) {
        /* c8 ignore next 4 -- the bundled fixtures all parse (tested) */
        if (!(err instanceof StateParseError)) throw err;
        results.push({
          severity: "fail",
          name: `bigbadbots_${call}`,
          message: `fixture failed to parse: ${err.message}`,
        });
        continue;
      }
      report.asked += 1;
      const before = snapshot(state);
      const where = String(request.request_id);
      const timeoutMs = typeof request.timeout_ms === "number" ? request.timeout_ms : Infinity;
      const start = performance.now();
      let answer: unknown;
      let threw = false;
      try {
        answer = bot.decide(state);
      } catch (err) {
        threw = true;
        report.crashes.push(`${where}: ${errorText(err)}`);
        if (err instanceof ExitCalled) {
          exits.push(`${call}() called ${err.message}`);
        }
      }
      const elapsedMs = performance.now() - start;
      report.slowestMs = Math.max(report.slowestMs, elapsedMs);
      if (elapsedMs > timeoutMs) report.overTimeout += 1;
      else if (call === "act" && elapsedMs > DEADLINE_MS) report.overDeadline += 1;
      if (threw) continue;

      if (snapshot(state) !== before) mutated.push(`${call}() at ${where}`);
      const verdict = checkBigBadBotsAnswer(request, answer);
      if (verdict.verdict === "malformed") report.malformed.push(`${where}: ${verdict.reason}`);
      else if (verdict.verdict === "corrected") report.corrected.push(`${where}: ${verdict.reason}`);
    }

    for (const call of BIGBADBOTS_CALLS) {
      const report = reports[call];
      const name = `bigbadbots_${call}`;
      if (report.asked === 0) continue;
      if (report.crashes.length > 0) {
        results.push({
          severity: "fail",
          name,
          message:
            `${call}() threw on ${report.crashes.length} of ${report.asked} requests ` +
            `(${examples(report.crashes)}). The SDK sends the protocol default ` +
            `(${DEFAULT_OF_CALL[call]}) instead.`,
        });
      }
      if (report.malformed.length > 0) {
        results.push({
          severity: "fail",
          name,
          message:
            `${report.malformed.length} of ${report.asked} answers are malformed ` +
            `(${examples(report.malformed)}). The server rejects them and applies ` +
            `${DEFAULT_OF_CALL[call]}.`,
        });
      }
      if (report.corrected.length > 0) {
        results.push({
          severity: "warn",
          name,
          message:
            `${report.corrected.length} of ${report.asked} answers are not legal and get ` +
            `corrected, with a violation logged (${examples(report.corrected)}).`,
        });
      }
      if (!report.crashes.length && !report.malformed.length && !report.corrected.length) {
        results.push({
          severity: "pass",
          name,
          message: `${call}() answered all ${report.asked} requests legally`,
        });
      }
    }

    const act = reports.act;
    if (act.asked > 0 && act.crashes.length === act.asked) {
      results.push({
        severity: "fail",
        name: "bigbadbots_crash_every_tick",
        message:
          `act() threw on every one of ${act.asked} fight ticks, so every tick plays the ` +
          "default and the bot never fights. A bot that stops answering altogether " +
          "forfeits a round after rule_set.forfeit.grace_seconds of missed ticks and " +
          "the match after rule_set.forfeit.rounds_for_match rounds (protocol 3.9).",
      });
    }
    if (exits.length > 0) {
      results.push({
        severity: "fail",
        name: "bigbadbots_exit",
        message:
          `${examples([...new Set(exits)].sort())}. Outside validate that is not caught: it ` +
          "ends the bot's process, and a seat that stops answering forfeits (protocol 3.9).",
      });
    }

    if (mutated.length > 0) {
      results.push({
        severity: "warn",
        name: "bigbadbots_state_mutation",
        message:
          `the bot changed the state it was handed (${examples(mutated)}). The server ` +
          "never sees it, but your own later reads do: copy what you need to change.",
      });
    } else {
      results.push({
        severity: "pass",
        name: "bigbadbots_state_mutation",
        message: "no handler changed its state",
      });
    }

    const slow = BIGBADBOTS_CALLS.filter((call) => reports[call].overTimeout > 0);
    if (slow.length > 0) {
      results.push({
        severity: "fail",
        name: "bigbadbots_timing",
        message:
          slow
            .map(
              (call) =>
                `${call}() ran past the request's timeout_ms ${reports[call].overTimeout} times ` +
                `(slowest ${reports[call].slowestMs.toFixed(0)}ms)`,
            )
            .join("; ") + ". A late answer is dropped and the call's default applies.",
      });
    } else if (act.overDeadline > 0) {
      results.push({
        severity: "warn",
        name: "bigbadbots_timing",
        message:
          `act() took over the ${DEADLINE_MS}ms per-tick deadline ${act.overDeadline} times ` +
          `(slowest ${act.slowestMs.toFixed(0)}ms). Each one spends the round's time bank.`,
      });
    } else {
      const slowest = Math.max(...BIGBADBOTS_CALLS.map((call) => reports[call].slowestMs));
      results.push({
        severity: "pass",
        name: "bigbadbots_timing",
        message: `every call answered in time (slowest ${slowest.toFixed(1)}ms)`,
      });
    }
  } finally {
    process.exit = realExit;
  }
  return results;
}
