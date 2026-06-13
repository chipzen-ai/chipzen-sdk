/**
 * WebSocket client for the Chipzen two-layer protocol.
 *
 * The user-facing surface is `runBot(url, bot, options)`. Internals
 * (session loop, mock-friendly helpers) are exported with `_` prefix
 * for the conformance harness and tests; they are not part of the
 * supported API.
 */

import WebSocket from "ws";

import type { Bot } from "./bot.js";
import { Action, parseGameState } from "./models.js";
import { DEFAULT_RETRY_POLICY, RetryPolicy } from "./retry.js";
import { VERSION } from "./version.js";

// ---------------------------------------------------------------------------
// Public surface
// ---------------------------------------------------------------------------

/**
 * Raised when `bot.decide()` errors and `safeMode` is `false`.
 *
 * Distinguished from transport/connection errors so the caller treats it
 * as terminal (a deterministic bot bug, not a transient disconnect) and
 * does NOT reconnect-retry it. See chipzen-ai/chipzen-sdk#52.
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
}

/** Protocol versions this client claims to support in the handshake. */
export const SUPPORTED_PROTOCOL_VERSIONS = ["1.0"] as const;

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
      action = _safeFallbackAction(validActions);
    }
  } catch (err) {
    if (err instanceof BotDecisionError) throw err;
    if (!safeMode) {
      throw new BotDecisionError(err instanceof Error ? err.message : String(err));
    }
    action = _safeFallbackAction(validActions);
  }
  return { action, latencyMs: Date.now() - start };
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

  await sendJson(ws, {
    type: "hello",
    match_id: ctx.matchId,
    supported_versions: [...SUPPORTED_PROTOCOL_VERSIONS],
  });

  // --- Message loop -----------------------------------------------------
  let lastSeq = 0;
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

      case "match_start":
        bot.onMatchStart(msg);
        break;

      case "round_start":
        bot.onRoundStart(msg);
        break;

      case "phase_change":
        bot.onPhaseChange(msg);
        break;

      case "turn_result":
        bot.onTurnResult(msg);
        break;

      case "round_result":
        bot.onRoundResult(msg);
        break;

      case "turn_request": {
        const requestId = (msg.request_id as string | undefined) ?? "";
        const state = parseGameState(msg);
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
        bot.onDecisionLatency(latencyMs);
        break;
      }

      case "action_rejected": {
        // Server rejected our action. Retry with a safe fallback,
        // echoing the original request_id.
        const requestId = (msg.request_id as string | undefined) ?? "";
        const validActions = (msg.valid_actions as string[] | undefined) ?? ["fold"];
        const fallback = _safeFallbackAction(validActions);
        await sendJson(ws, {
          type: "turn_action",
          match_id: ctx.matchId,
          request_id: requestId,
          ...fallback.toWire(),
        });
        break;
      }

      case "reconnected": {
        // The server is replaying state after we reconnected. If a
        // pending_request is included, dispatch it as if it were a
        // fresh turn_request.
        const pending = msg.pending_request as Record<string, unknown> | undefined;
        if (pending && pending.type === "turn_request") {
          // Replay the pending turn exactly like a fresh turn_request.
          const requestId = (pending.request_id as string | undefined) ?? "";
          const { action, latencyMs } = _decide(
            bot,
            parseGameState(pending),
            pending.valid_actions as string[] | undefined,
            safeMode,
          );
          await sendJson(ws, {
            type: "turn_action",
            match_id: ctx.matchId,
            request_id: requestId,
            ...action.toWire(),
          });
          bot.onDecisionLatency(latencyMs);
        }
        break;
      }

      case "match_end":
        bot.onMatchEnd((msg.results as Record<string, unknown>) ?? msg);
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
// Helpers
// ---------------------------------------------------------------------------

export interface AsyncMessageReader {
  next(): Promise<string | null>;
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
  private readonly queue: string[] = [];
  private readonly waiters: Array<(msg: string | null) => void> = [];
  private closed = false;

  constructor(ws: WebSocket) {
    ws.on("message", (data) => {
      const msg = data.toString();
      const w = this.waiters.shift();
      if (w) w(msg);
      else this.queue.push(msg);
    });
    ws.on("close", () => this._close());
    ws.on("error", () => this._close());
  }

  async next(): Promise<string | null> {
    const queued = this.queue.shift();
    if (queued !== undefined) return queued;
    if (this.closed) return null;
    return new Promise((resolve) => this.waiters.push(resolve));
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

export function _safeFallbackAction(validActions: string[] | undefined): Action {
  const valid = new Set(validActions ?? []);
  if (valid.has("check")) return Action.check();
  return Action.fold();
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
