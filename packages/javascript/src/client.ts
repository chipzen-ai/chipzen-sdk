/**
 * WebSocket client for the Chipzen two-layer protocol.
 *
 * The user-facing surface is `runBot(url, bot, options)`. Internals
 * (session loop, mock-friendly helpers) are exported with `_` prefix
 * for the conformance harness and tests; they are not part of the
 * supported API.
 */

import WebSocket from "ws";

import {
  BIGBADBOTS_STATE_SHAPE,
  parseBigBadBotsTurnRequest,
  StateParseError,
} from "./bigbadbots.js";
import { BigBadBotsBot, fallbackAction, isAnswerShaped } from "./bigbadbots_bot.js";
import type { Bot } from "./bot.js";
import {
  Action,
  type GameState,
  parseGameState,
  POKER_STATE_SHAPES,
  stateShapeOf,
  UnsupportedStateShapeError,
} from "./models.js";
import { DEFAULT_RETRY_POLICY, RetryPolicy } from "./retry.js";
import { VERSION } from "./version.js";

// ---------------------------------------------------------------------------
// Public surface
// ---------------------------------------------------------------------------

/**
 * Raised when `bot.decide()` (or a lifecycle hook) errors and `safeMode`
 * is `false`.
 *
 * Distinguished from transport/connection errors so the caller treats it
 * as terminal (a deterministic bot bug, not a transient disconnect) and
 * does NOT reconnect-retry it. See chipzen-ai/chipzen-sdk#52 (decide)
 * and #80 (lifecycle hooks).
 */
export class BotDecisionError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "BotDecisionError";
  }
}

/**
 * Optional knobs for `runBot`. All fields default to sensible values
 * matching the platform's expectations.
 */
export interface RunBotOptions {
  /** Bot API token. Required for the `/bot` endpoint; empty is fine for local dev. */
  token?: string | null;
  /** Single-use ticket alternative to `token` (competitive endpoints). */
  ticket?: string | null;
  /** Match UUID. Auto-extracted from the URL if not supplied. */
  matchId?: string;
  /** Client software name sent in the `hello` handshake. */
  clientName?: string;
  /** Client software version sent in the `hello` handshake. Defaults to the SDK version. */
  clientVersion?: string;
  /**
   * Reconnect attempt **cap**. When given, overrides the attempt count
   * from `retryPolicy` (the policy's backoff knobs still apply). When
   * omitted, the policy's `maxReconnectAttempts` is used.
   */
  maxRetries?: number;
  /**
   * {@link RetryPolicy} controlling reconnect attempts + exponential
   * backoff. Defaults to {@link DEFAULT_RETRY_POLICY}.
   */
  retryPolicy?: RetryPolicy;
  /**
   * When `true` (default), an exception thrown by `bot.decide()` is
   * folded (a safe fallback action is sent). Set `false` for dev/eval so
   * the first exception propagates as a {@link BotDecisionError} and the
   * process exits non-zero (chipzen-ai/chipzen-sdk#52).
   */
  safeMode?: boolean;
  /**
   * Override the WS `User-Agent` header. Defaults to
   * `chipzen-sdk-js/<version>` (a non-default UA also clears the
   * platform's Cloudflare bot-fight rule; chipzen-ai/chipzen-sdk#46).
   */
  userAgent?: string;
  /**
   * The games this client can actually play, declared in the client
   * `hello` as `supported_games`: a list of `game_type` strings (`"poker"`,
   * `"draw27"`, `"ofc"`). See `docs/protocol/LAYER2-COMMON.md` section 2.
   *
   * `undefined` / `null` (the default) **omits the field entirely**, and the
   * platform reads an absent declaration as "poker only", so the default
   * `hello` is byte-identical to earlier releases. An empty list is sent as
   * is and means "supports nothing". At a non-poker table a client that has
   * not declared the seat's game is rejected before it is seated (close
   * `4002`). Declaring a `game_type` asserts you implement everything in
   * that game's Layer 2 spec, so do not declare a game your `decide()`
   * cannot play. A `BigBadBotsBot` declares `["bigbadbots"]` when this is
   * omitted.
   */
  supportedGames?: readonly string[] | null;
}

/** Protocol versions this client claims to support in the handshake. */
export const SUPPORTED_PROTOCOL_VERSIONS = ["1.0"] as const;

/**
 * How long before a Big Bad Bots request's `timeout_ms` runs out the SDK
 * stops waiting and sends the default instead, so the answer still reaches
 * the server in time.
 */
export const BIGBADBOTS_SEND_MARGIN_MS = 20;

/**
 * Connect a bot to the Chipzen server and play until the match ends.
 *
 * Resolves on `match_end` (with the `match_end` payload, ignored by most
 * callers). Rejects if the connection cannot be established (after the
 * reconnect budget is exhausted) or if `safeMode` is `false` and
 * `bot.decide()` throws (a terminal {@link BotDecisionError}).
 */
export async function runBot(
  url: string,
  bot: Bot,
  options: RunBotOptions = {},
): Promise<Record<string, unknown> | null> {
  const matchId = options.matchId ?? _extractMatchId(url);
  const token = options.token ?? null;
  const ticket = options.ticket ?? null;
  const clientName = options.clientName ?? "chipzen-sdk";
  const clientVersion = options.clientVersion ?? VERSION;
  const safeMode = options.safeMode ?? true;
  const userAgent = options.userAgent ?? `chipzen-sdk-js/${clientVersion}`;
  const policy = options.retryPolicy ?? DEFAULT_RETRY_POLICY;
  // An explicit maxRetries caps the attempt count; the policy still
  // supplies the backoff progression.
  const maxAttempts = options.maxRetries ?? policy.maxReconnectAttempts;

  let retries = 0;
  for (;;) {
    let ws: WebSocket | null = null;
    try {
      ws = new WebSocket(url, { headers: { "User-Agent": userAgent } });
      await _waitForOpen(ws);
      retries = 0; // reset on successful connect
      return await _runSession(ws, bot, {
        matchId,
        token,
        ticket,
        clientName,
        clientVersion,
        safeMode,
        supportedGames: options.supportedGames ?? null,
      });
    } catch (err) {
      if (err instanceof BotDecisionError) {
        // A deterministic bot bug (safeMode=false) — terminal, not a
        // transient disconnect. Do not reconnect-retry.
        throw err;
      }
      retries++;
      if (retries > maxAttempts) {
        throw err;
      }
      const waitMs = policy.backoffMs(retries);
      await new Promise((r) => setTimeout(r, waitMs));
    } finally {
      if (ws && ws.readyState !== WebSocket.CLOSED && ws.readyState !== WebSocket.CLOSING) {
        ws.close();
      }
    }
  }
}

// ---------------------------------------------------------------------------
// _runSession — internal session loop, exported for the conformance harness
// ---------------------------------------------------------------------------

/**
 * Minimal WebSocket-shaped interface the session loop needs. Real
 * `ws.WebSocket` instances satisfy it; the conformance mock also
 * implements it.
 */
export interface SessionWebSocket {
  send(data: string): void | Promise<void>;
  close(): void;
}

interface SessionContext {
  matchId: string;
  token: string | null;
  ticket: string | null;
  clientName: string;
  clientVersion: string;
  /**
   * When `true` (default), a throw from `bot.decide()` is folded to a
   * safe fallback action. When `false`, it surfaces as a
   * {@link BotDecisionError} (terminal — never reconnect-retried).
   */
  safeMode?: boolean;
  /** See {@link RunBotOptions.supportedGames}. */
  supportedGames?: readonly string[] | null;
}

/**
 * Run `bot.decide()` with timing + safeMode handling. Returns the action
 * to send plus the wall-clock decision time in ms.
 *
 * Under safeMode a thrown error (or a non-Action return) is folded to a
 * safe fallback; otherwise a thrown error is re-raised as a
 * {@link BotDecisionError} so the caller treats it as terminal.
 */
function _decide(
  bot: Bot,
  state: ReturnType<typeof parseGameState>,
  validActions: string[] | undefined,
  safeMode: boolean,
): { action: Action; latencyMs: number } {
  const start = Date.now();
  let action: Action;
  try {
    action = bot.decide(state);
    if (!(action instanceof Action)) {
      // User code returned the wrong type. Under safeMode, fold; otherwise
      // this is a deterministic bug — surface it.
      if (!safeMode) {
        throw new BotDecisionError(
          `bot.decide() returned a non-Action value (${typeof action})`,
        );
      }
      action = _safeFallbackAction(validActions, state);
    }
  } catch (err) {
    if (err instanceof BotDecisionError) throw err;
    if (!safeMode) {
      throw new BotDecisionError(err instanceof Error ? err.message : String(err));
    }
    action = _safeFallbackAction(validActions, state);
  }
  return { action, latencyMs: Date.now() - start };
}

/**
 * The seat of the `is_self` entry in a `match_start` / `reconnected` frame's
 * `seats` roster (TRANSPORT-PROTOCOL section 8.3), or `null` when the frame
 * has none. Mirrors the Python loop: an `is_self` entry without a numeric
 * `seat` reads as seat 0.
 */
function _selfSeat(msg: Record<string, unknown>): number | null {
  const roster = Array.isArray(msg.seats) ? (msg.seats as unknown[]) : [];
  for (const seatInfo of roster) {
    if (seatInfo && typeof seatInfo === "object" && (seatInfo as Record<string, unknown>).is_self) {
      const seat = (seatInfo as Record<string, unknown>).seat;
      return typeof seat === "number" && Number.isFinite(seat) ? seat : 0;
    }
  }
  return null;
}

/**
 * Invoke a bot lifecycle hook with the same safe-mode guard as `_decide`.
 *
 * A user exception in a lifecycle/stats callback must never tear down the
 * WS session (chipzen-ai/chipzen-sdk#80): historically the throw killed
 * the connection, the reconnect path hid the real error, and the bot
 * zombied into an auto-substitute forfeit. The SDK otherwise stays quiet,
 * but a swallowed user bug must be LOUD — log the full stack to stderr
 * and keep the session alive.
 *
 * Under `safeMode = false` (dev/eval) the error propagates as a
 * {@link BotDecisionError} so the bug surfaces immediately — mirroring
 * the `_decide` semantics.
 */
function _callHook(hookName: string, invoke: () => void, safeMode: boolean): void {
  try {
    invoke();
  } catch (err) {
    console.error(
      `chipzen-sdk: bot ${hookName}() threw; ignoring it and continuing the session (chipzen-ai/chipzen-sdk#80)`,
      err instanceof Error ? (err.stack ?? err.message) : err,
    );
    if (!safeMode) {
      throw new BotDecisionError(err instanceof Error ? err.message : String(err));
    }
  }
}

/**
 * Drive a single connected session: handshake + message loop until
 * `match_end`. Returns the `match_end` payload on a clean finish, or
 * `null` if the handshake failed / the socket closed without a
 * `match_end` (the caller decides whether to reconnect). Exported for
 * the conformance harness in `conformance.ts`.
 */
export async function _runSession(
  ws: WebSocket | SessionWebSocket,
  bot: Bot,
  ctx: SessionContext,
  /** Optional pre-built reader; the real client builds one from the ws. */
  readerOverride?: AsyncMessageReader,
): Promise<Record<string, unknown> | null> {
  const reader = readerOverride ?? new _NodeWebSocketReader(ws as WebSocket);
  const safeMode = ctx.safeMode ?? true;

  // --- Layer 1 handshake ------------------------------------------------
  const auth: Record<string, unknown> = {
    type: "authenticate",
    match_id: ctx.matchId,
    client_name: ctx.clientName,
    client_version: ctx.clientVersion,
  };
  if (ctx.token) auth.token = ctx.token;
  if (ctx.ticket) auth.ticket = ctx.ticket;
  await sendJson(ws, auth);

  const helloRaw = await reader.next();
  if (!helloRaw) {
    throw new Error("connection closed before server hello");
  }
  const hello = parseJson(helloRaw);
  if (hello.type !== "hello") {
    throw new Error(`expected server hello, got ${hello.type ?? "<no type>"}`);
  }

  // LAYER2-COMMON section 2: branch on `state_shape` before parsing any
  // state. An NLHE hello carries no `game` descriptor (shape `null`), so
  // every poker table keeps the path it has always taken. A shape this
  // release cannot parse ends the session rather than being read as NLHE.
  let stateShape: string | null;
  try {
    stateShape = stateShapeOf(hello);
  } catch (err) {
    console.error(
      `chipzen-sdk: server hello has a malformed game descriptor: ${(err as Error).message}`,
    );
    return null;
  }
  const isBigBadBots = stateShape === BIGBADBOTS_STATE_SHAPE;
  if (!isBigBadBots && !POKER_STATE_SHAPES.has(stateShape)) {
    console.error(`chipzen-sdk: ${new UnsupportedStateShapeError(stateShape).message}`);
    return null;
  }
  if (bot instanceof BigBadBotsBot && !isBigBadBots) {
    console.error(
      `chipzen-sdk: ${bot.constructor.name} is a BigBadBotsBot but the table's state_shape ` +
        `is ${JSON.stringify(stateShape)}; it cannot play here`,
    );
    return null;
  }
  let supportedGames = ctx.supportedGames ?? null;
  if (supportedGames === null && bot instanceof BigBadBotsBot) {
    supportedGames = [BIGBADBOTS_STATE_SHAPE];
  }

  // `supported_games` is appended LAST and only when the caller declared
  // something, so a bot that declares nothing sends the exact hello it has
  // always sent; the platform reads an absent list as "poker only".
  const clientHello: Record<string, unknown> = {
    type: "hello",
    match_id: ctx.matchId,
    supported_versions: [...SUPPORTED_PROTOCOL_VERSIONS],
  };
  if (supportedGames !== null) clientHello.supported_games = [...supportedGames];
  await sendJson(ws, clientHello);

  const bbb = isBigBadBots ? new _BigBadBotsTurns(ws, bot, reader, ctx.matchId, safeMode) : null;

  // --- Message loop -----------------------------------------------------
  let lastSeq = 0;
  // The last turn we answered, kept so an `action_rejected` for it can be
  // retried with a fallback built from that turn's state (an OFC default
  // placement needs the cards and the row capacity).
  let lastTurn: { requestId: string; state: GameState } | null = null;
  // Seat context, tracked as the Python loop does (#194): a hold'em or 27TD
  // turn state carries neither `your_seat` nor `dealer_seat`, so the seat is
  // taken from `match_start` (or `reconnected`) and the button from each
  // `round_start`. An OFC turn state carries both, and its own values win
  // (parseGameState).
  const seats: { yourSeat: number; dealerSeat: number } = { yourSeat: 0, dealerSeat: 0 };
  for (;;) {
    const raw = await reader.next();
    if (raw === null) {
      // Connection closed (peer or transport) without a match_end. Caller
      // decides whether to retry — we just exit the loop here.
      return null;
    }

    let msg: Record<string, unknown>;
    try {
      msg = parseJson(raw);
    } catch {
      // Malformed envelope — log + continue. Real production deployments
      // never emit invalid JSON; this is for adversarial-input robustness.
      continue;
    }

    if (typeof msg.seq === "number" && msg.seq <= lastSeq) {
      // Sequence regression — likely a retransmit. Skip silently.
      continue;
    }
    if (typeof msg.seq === "number") lastSeq = msg.seq;

    const type = msg.type as string | undefined;
    switch (type) {
      case "ping":
        await sendJson(ws, { type: "pong", match_id: ctx.matchId });
        break;

      case "match_start": {
        const selfSeat = _selfSeat(msg);
        if (selfSeat !== null) seats.yourSeat = selfSeat;
        _callHook("onMatchStart", () => bot.onMatchStart(msg), safeMode);
        break;
      }

      case "round_start": {
        const roundState = msg.state as Record<string, unknown> | undefined;
        const dealer = roundState && typeof roundState === "object" ? roundState.dealer_seat : undefined;
        if (typeof dealer === "number" && Number.isFinite(dealer)) seats.dealerSeat = dealer;
        _callHook("onRoundStart", () => bot.onRoundStart(msg), safeMode);
        break;
      }

      case "phase_change":
        _callHook("onPhaseChange", () => bot.onPhaseChange(msg), safeMode);
        break;

      case "turn_result":
        bbb?.noteTurnResult(msg);
        _callHook("onTurnResult", () => bot.onTurnResult(msg), safeMode);
        break;

      case "round_result":
        _callHook("onRoundResult", () => bot.onRoundResult(msg), safeMode);
        break;

      case "turn_request": {
        if (bbb) {
          await bbb.answer(msg);
          break;
        }
        const requestId = (msg.request_id as string | undefined) ?? "";
        const state = parseGameState(msg, seats);
        lastTurn = { requestId, state };
        // _decide handles timing + safeMode; under safeMode=false it
        // throws BotDecisionError, which propagates out of _runSession.
        const { action, latencyMs } = _decide(
          bot,
          state,
          msg.valid_actions as string[] | undefined,
          safeMode,
        );
        await sendJson(ws, {
          type: "turn_action",
          match_id: ctx.matchId,
          request_id: requestId,
          ...action.toWire(),
        });
        _callHook("onDecisionLatency", () => bot.onDecisionLatency(latencyMs), safeMode);
        break;
      }

      case "action_rejected": {
        if (bbb) {
          await bbb.retryRejected(msg);
          break;
        }
        // Server rejected our action. Retry with a safe fallback,
        // echoing the original request_id.
        const requestId = (msg.request_id as string | undefined) ?? "";
        const validActions = (msg.valid_actions as string[] | undefined) ?? ["fold"];
        const rejectedState = lastTurn?.requestId === requestId ? lastTurn.state : undefined;
        const fallback = _safeFallbackAction(validActions, rejectedState);
        await sendJson(ws, {
          type: "turn_action",
          match_id: ctx.matchId,
          request_id: requestId,
          ...fallback.toWire(),
        });
        break;
      }

      case "reconnected": {
        // The server is replaying state after we reconnected. Re-learn
        // seat context from `reconnected.seats` (same schema as
        // `match_start.seats`, TRANSPORT-PROTOCOL §8.15) exactly as a
        // `match_start` would deliver it: on a re-attach this session
        // never saw `match_start`, so without this the replayed pending
        // turn below would be decided with yourSeat=0
        // (chipzen-ai/chipzen-sdk#121, the JS analog of #119).
        // The button is kept from the last `round_start` this session saw.
        const relearnedSeat = _selfSeat(msg);
        if (relearnedSeat !== null) seats.yourSeat = relearnedSeat;
        _callHook("onReconnected", () => bot.onReconnected(msg), safeMode);
        // If a pending_request is included, dispatch it as if it were a
        // fresh turn_request.
        const pending = msg.pending_request as Record<string, unknown> | undefined;
        if (bbb && pending && typeof pending === "object") {
          await bbb.answer(pending);
        } else if (pending && pending.type === "turn_request") {
          // Replay the pending turn exactly like a fresh turn_request,
          // with the seat re-learned from the reconnected frame. A hold'em
          // or 27TD state does not carry `your_seat`; an OFC state does, and
          // the state's own value wins (as in the Python SDK).
          const requestId = (pending.request_id as string | undefined) ?? "";
          const state = parseGameState(pending, seats);
          lastTurn = { requestId, state };
          const { action, latencyMs } = _decide(
            bot,
            state,
            pending.valid_actions as string[] | undefined,
            safeMode,
          );
          await sendJson(ws, {
            type: "turn_action",
            match_id: ctx.matchId,
            request_id: requestId,
            ...action.toWire(),
          });
          _callHook("onDecisionLatency", () => bot.onDecisionLatency(latencyMs), safeMode);
        }
        break;
      }

      case "match_end":
        _callHook(
          "onMatchEnd",
          () => bot.onMatchEnd((msg.results as Record<string, unknown>) ?? msg),
          safeMode,
        );
        return msg; // clean exit — hand the match_end payload to the caller

      case "error":
        // Non-fatal — log + continue (the real logging plumbing belongs
        // in the bot author's code; the SDK stays quiet).
        break;

      default:
        // Unknown message type — forward-compat: just ignore.
        break;
    }
  }
}

// ---------------------------------------------------------------------------
// Big Bad Bots turn handling
// ---------------------------------------------------------------------------

/**
 * The Big Bad Bots side of one session: only used when the server hello
 * declares `state_shape: "bigbadbots"`. Exported for tests; not part of the
 * supported API.
 */
export class _BigBadBotsTurns {
  /**
   * The previous fight action, as applied, keyed by round: the server's
   * default for a missed tick repeats it if it was block/move_in/move_out
   * and resets every round (protocol section 3.5).
   */
  private lastFight: { round: unknown; action: string | null } = { round: null, action: null };
  /** The last request answered, kept so an `action_rejected` for it can be retried with its default. */
  private lastRequest: Record<string, unknown> | null = null;

  constructor(
    private readonly ws: WebSocket | SessionWebSocket,
    private readonly bot: Bot,
    private readonly reader: AsyncMessageReader,
    private readonly matchId: string,
    private readonly safeMode: boolean,
  ) {}

  private previousFightAction(request: Record<string, unknown>): string | null {
    const state = request.state as Record<string, unknown> | undefined;
    const round = state && typeof state === "object" ? state.round : undefined;
    if (this.lastFight.round !== null && this.lastFight.round === round) {
      return this.lastFight.action;
    }
    return null;
  }

  private fallback(request: Record<string, unknown>, why: string): Action | null {
    const action = fallbackAction(request, this.previousFightAction(request));
    const sent = action ? JSON.stringify(action.toWire()) : "(none: the server applies its own)";
    console.warn(
      `chipzen-sdk: Big Bad Bots request ${String(request.request_id)}: ${why}; ` +
        `sending the protocol default ${sent}`,
    );
    return action;
  }

  /**
   * Decide one request: the bot's action or the protocol default.
   *
   * Never sends a silent wrong action. The default (section 3.5) replaces
   * the bot's answer, with a warning, when the payload fails to parse,
   * `decide` throws (`safeMode`), `decide` returns something that is not
   * one of `valid_actions`, or the request already waited out its budget
   * (`timeout_ms` less {@link BIGBADBOTS_SEND_MARGIN_MS}) behind earlier
   * work, in which case the bot is not called. `decide` is synchronous and
   * cannot be interrupted, so an answer that comes back past its budget is
   * still sent, with a warning: if it missed `timeout_ms` the server drops
   * it and plays the same default. Under `safeMode = false` a parse
   * failure, a throw or a malformed answer is a {@link BotDecisionError}; a
   * slow answer is not.
   */
  decide(request: Record<string, unknown>): { action: Action | null; latencyMs: number } {
    const arrived = this.reader.receivedAt?.();
    const start = Date.now();
    const waited = arrived === undefined ? 0 : Math.max(start - arrived, 0);
    const elapsed = (): number => Date.now() - start;

    let state;
    try {
      state = parseBigBadBotsTurnRequest(request);
    } catch (err) {
      if (!(err instanceof StateParseError)) throw err;
      console.error(`chipzen-sdk: Big Bad Bots turn_request failed to parse: ${err.message}`);
      if (!this.safeMode) throw new BotDecisionError(err.message);
      return { action: this.fallback(request, "the payload failed to parse"), latencyMs: elapsed() };
    }

    const budgetMs = Math.max(state.timeoutMs - BIGBADBOTS_SEND_MARGIN_MS, 1);
    if (waited >= budgetMs) {
      const why = `it waited ${waited}ms behind earlier work, past its ${budgetMs}ms budget`;
      return { action: this.fallback(request, why), latencyMs: elapsed() };
    }

    let action: unknown;
    try {
      // `Bot.decide` is typed for poker; at this table it takes the Big Bad
      // Bots model (`BigBadBotsBot.decide` accepts both).
      action = (this.bot.decide as (s: unknown) => unknown).call(this.bot, state);
    } catch (err) {
      if (err instanceof BotDecisionError) throw err;
      console.error(
        "chipzen-sdk: bot decide() threw",
        err instanceof Error ? (err.stack ?? err.message) : err,
      );
      if (!this.safeMode) {
        throw new BotDecisionError(err instanceof Error ? err.message : String(err));
      }
      return { action: this.fallback(request, "decide() threw"), latencyMs: elapsed() };
    }

    if (!isAnswerShaped(action, state.validActions)) {
      const what =
        action !== null &&
        typeof action === "object" &&
        typeof (action as { then?: unknown }).then === "function"
          ? "a Promise (the Big Bad Bots methods must be synchronous)"
          : action instanceof Action
            ? JSON.stringify(action.toWire())
            : String(action);
      const message = `decide() returned ${what}, not one of ${JSON.stringify(state.validActions)}`;
      if (!this.safeMode) throw new BotDecisionError(message);
      console.error(`chipzen-sdk: Big Bad Bots request ${state.requestId}: ${message}`);
      return {
        action: this.fallback(request, "the answer was not a valid action"),
        latencyMs: elapsed(),
      };
    }

    const latencyMs = elapsed();
    if (waited + latencyMs > budgetMs) {
      console.warn(
        `chipzen-sdk: Big Bad Bots request ${state.requestId}: answered ${waited + latencyMs}ms ` +
          `after it arrived, past its ${budgetMs}ms budget; sending it anyway (the server drops ` +
          "an answer later than timeout_ms and plays the protocol default)",
      );
    }
    return { action, latencyMs };
  }

  /** Answer one `turn_request` (or a `reconnected.pending_request`). */
  async answer(request: Record<string, unknown>): Promise<void> {
    const { action, latencyMs } = this.decide(request);
    this.lastRequest = request;
    if (action === null) return;
    const state = request.state as Record<string, unknown> | undefined;
    if (state && typeof state === "object" && state.phase === "fight") {
      this.lastFight = { round: state.round, action: action.action };
    }
    await sendJson(this.ws, {
      type: "turn_action",
      match_id: this.matchId,
      request_id: request.request_id, // MUST echo
      ...action.toWire(),
    });
    _callHook("onDecisionLatency", () => this.bot.onDecisionLatency(latencyMs), this.safeMode);
  }

  /**
   * Retry an `action_rejected` once with the request's protocol default,
   * which is always well-shaped (section 3.7). A second rejection gets no
   * retry; the server applies the default itself when the time runs out.
   */
  async retryRejected(msg: Record<string, unknown>): Promise<void> {
    const requestId = msg.request_id;
    console.warn(
      `chipzen-sdk: action rejected (${String(msg.reason)}): ${String(msg.message ?? "")} -- ` +
        `${String(msg.remaining_ms ?? 0)}ms remaining`,
    );
    const rejected = this.lastRequest;
    if (rejected === null || rejected.request_id !== requestId) {
      console.warn(`chipzen-sdk: no retry for rejected request ${String(requestId)}`);
      return;
    }
    this.lastRequest = null;
    const retry = this.fallback(rejected, "the answer was rejected");
    if (retry === null) return;
    await sendJson(this.ws, {
      type: "turn_action",
      match_id: this.matchId,
      request_id: requestId,
      ...retry.toWire(),
    });
  }

  /**
   * Track the fight action as applied (after defaults and corrections):
   * what the server repeats on a missed tick.
   */
  noteTurnResult(msg: Record<string, unknown>): void {
    const details = msg.details as Record<string, unknown> | undefined;
    if (!details || typeof details !== "object" || details.phase !== "fight") return;
    this.lastFight = {
      round: "round" in details ? details.round : this.lastFight.round,
      action: typeof details.action === "string" ? details.action : this.lastFight.action,
    };
  }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

export interface AsyncMessageReader {
  next(): Promise<string | null>;
  /**
   * Optional: when (`Date.now()`) the message the last `next()` returned
   * arrived. The Big Bad Bots loop uses it to see how long a request
   * already waited behind earlier work; readers without it count as no wait.
   */
  receivedAt?(): number | undefined;
}

/**
 * Adapts a `ws.WebSocket` to a pull-based async iterator the session
 * loop can drive.
 *
 * Resolves with `null` when the underlying socket closes. Errors on
 * the socket are not surfaced to consumers — they cause `next()` to
 * return `null` and the caller's outer reconnect logic decides what to do.
 */
export class _NodeWebSocketReader implements AsyncMessageReader {
  private readonly queue: Array<{ msg: string; at: number }> = [];
  private readonly waiters: Array<(msg: string | null) => void> = [];
  private closed = false;
  private lastAt: number | undefined;

  constructor(ws: WebSocket) {
    ws.on("message", (data) => {
      const msg = data.toString();
      const at = Date.now();
      const w = this.waiters.shift();
      if (w) {
        this.lastAt = at;
        w(msg);
      } else {
        this.queue.push({ msg, at });
      }
    });
    ws.on("close", () => this._close());
    ws.on("error", () => this._close());
  }

  async next(): Promise<string | null> {
    const queued = this.queue.shift();
    if (queued !== undefined) {
      this.lastAt = queued.at;
      return queued.msg;
    }
    if (this.closed) return null;
    return new Promise((resolve) => this.waiters.push(resolve));
  }

  receivedAt(): number | undefined {
    return this.lastAt;
  }

  private _close(): void {
    if (this.closed) return;
    this.closed = true;
    while (this.waiters.length) {
      const w = this.waiters.shift();
      if (w) w(null);
    }
  }
}

export function _waitForOpen(ws: WebSocket): Promise<void> {
  if (ws.readyState === WebSocket.OPEN) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const onOpen = (): void => {
      ws.removeListener("error", onError);
      resolve();
    };
    const onError = (err: Error): void => {
      ws.removeListener("open", onOpen);
      reject(err);
    };
    ws.once("open", onOpen);
    ws.once("error", onError);
  });
}

/**
 * The action sent in place of the bot's when `decide()` throws or returns a
 * non-`Action` (under safeMode), and on an `action_rejected` retry.
 *
 * `check` when legal, else `fold`, mirroring the server's own auto-action
 * (TRANSPORT-PROTOCOL section 8.12). The variant turns that offer neither
 * get that variant's protocol default instead:
 *
 * - a 2-7 Triple Draw draw turn (`["draw"]`): a stand pat;
 * - a Pineapple OFC turn (`["place"]`): the default placement of
 *   OFC-GAME-STATE-PROTOCOL.md section 5.9, built from `state` (see
 *   {@link _ofcDefaultPlacement}).
 */
export function _safeFallbackAction(
  validActions: readonly string[] | undefined,
  state?: GameState,
): Action {
  const valid = new Set(validActions ?? []);
  if (valid.has("check")) return Action.check();
  if (!valid.has("fold")) {
    // A 2-7 Triple Draw draw turn offers exactly `["draw"]`, so `fold` would
    // be rejected; a stand pat is the server's own default there.
    if (valid.has("draw")) return Action.standPat();
    // An OFC turn offers exactly `["place"]`: no fold, no check.
    if (valid.has("place")) return _ofcDefaultPlacement(state);
  }
  return Action.fold();
}

/**
 * Pineapple OFC: the protocol's default placement (OFC-GAME-STATE-PROTOCOL.md
 * section 5.9), which is also what the server substitutes for a seat that
 * times out: each card goes to the row with the most remaining capacity,
 * ties toward the back (`bottom`, then `middle`, then `top`). The first
 * `state.place` of `state.cardsToPlace` are placed and the next
 * `state.mustDiscard` are discarded, so the result is legal for any state
 * the server sends.
 *
 * Without a state (an `action_rejected` for a turn this session never saw)
 * it is an empty placement, which the server rejects and then applies its
 * own default for, as it would for no answer at all.
 */
export function _ofcDefaultPlacement(state?: GameState): Action {
  const capacity: Record<string, number> = { ...(state?.rowCapacity ?? {}) };
  // Back to front, then any row name this release has not heard of.
  const order = ["bottom", "middle", "top"].filter((name) => name in capacity);
  for (const name of Object.keys(capacity)) if (!order.includes(name)) order.push(name);

  const cards = [...(state?.cardsToPlace ?? [])];
  const place = state?.place ?? 0;
  const placements: Array<[string, string]> = [];
  for (const card of cards.slice(0, place)) {
    let best: string | undefined;
    for (const row of order) {
      // Strictly greater, so a tie keeps the row nearer the back.
      if ((capacity[row] ?? 0) > (best === undefined ? 0 : (capacity[best] ?? 0))) best = row;
    }
    if (best === undefined) break;
    placements.push([card, best]);
    capacity[best] = (capacity[best] ?? 0) - 1;
  }
  return Action.place(placements, cards.slice(place, place + (state?.mustDiscard ?? 0)));
}

/**
 * Pull `match_id` out of a Chipzen WebSocket URL. Path shape is
 * `.../ws/match/<match_id>/...`. Returns `""` if the URL doesn't
 * match the expected pattern.
 */
export function _extractMatchId(url: string): string {
  // Match the segment between "/ws/match/" and the next "/" (or end of URL).
  // Permissive on the inner shape — server-side IDs may be UUIDs,
  // shortened hashes, or namespaced strings like `m_abc_123`.
  const m = /\/ws\/match\/([^/?#]+)/.exec(url);
  return m ? (m[1] ?? "") : "";
}

async function sendJson(
  ws: WebSocket | SessionWebSocket,
  msg: Record<string, unknown>,
): Promise<void> {
  const payload = JSON.stringify(msg);
  const result = (ws as { send: (data: string) => unknown }).send(payload);
  if (result && typeof (result as Promise<unknown>).then === "function") {
    await (result as Promise<unknown>);
  }
}

function parseJson(raw: string): Record<string, unknown> {
  return JSON.parse(raw) as Record<string, unknown>;
}
