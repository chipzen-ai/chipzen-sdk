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

// ---------------------------------------------------------------------------
// Public surface
// ---------------------------------------------------------------------------

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
  /** Client software version sent in the `hello` handshake. */
  clientVersion?: string;
  /** Number of reconnect attempts on unexpected disconnect. */
  maxRetries?: number;
}

/** Protocol versions this client claims to support in the handshake. */
export const SUPPORTED_PROTOCOL_VERSIONS = ["1.0"] as const;

/**
 * Connect a bot to the Chipzen server and play until the match ends.
 *
 * Resolves cleanly on `match_end`. Rejects if the connection cannot
 * be established (after `maxRetries` reconnect attempts) or if a
 * fatal protocol error occurs.
 */
export async function runBot(
  url: string,
  bot: Bot,
  options: RunBotOptions = {},
): Promise<void> {
  const maxRetries = options.maxRetries ?? 3;
  const matchId = options.matchId ?? _extractMatchId(url);
  const token = options.token ?? null;
  const ticket = options.ticket ?? null;
  const clientName = options.clientName ?? "chipzen-sdk";
  const clientVersion = options.clientVersion ?? "0.2.0";

  let retries = 0;
  for (;;) {
    let ws: WebSocket | null = null;
    try {
      ws = new WebSocket(url);
      await _waitForOpen(ws);
      retries = 0; // reset on successful connect
      await _runSession(ws, bot, {
        matchId,
        token,
        ticket,
        clientName,
        clientVersion,
      });
      return; // _runSession returns cleanly on match_end
    } catch (err) {
      retries++;
      if (retries > maxRetries) {
        throw err;
      }
      const waitMs = Math.min(2 ** retries, 8) * 1000;
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
}

/**
 * Drive a single connected session: handshake + message loop until
 * `match_end`. Exported for the conformance harness in `conformance.ts`.
 */
export async function _runSession(
  ws: WebSocket | SessionWebSocket,
  bot: Bot,
  ctx: SessionContext,
  /** Optional pre-built reader; the real client builds one from the ws. */
  readerOverride?: AsyncMessageReader,
): Promise<void> {
  const reader = readerOverride ?? new _NodeWebSocketReader(ws as WebSocket);

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
      // Connection closed (peer or transport). Caller decides whether
      // to retry — we just exit the loop here.
      return;
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
        let action: Action;
        try {
          const state = parseGameState(msg);
          action = bot.decide(state);
          if (!(action instanceof Action)) {
            // User code returned the wrong type — treat as a safe-fallback case.
            action = _safeFallbackAction(msg.valid_actions as string[] | undefined);
          }
        } catch {
          // User code raised — apply the safe fallback so the protocol
          // exchange completes. The smoke_test in `validate` is what's
          // supposed to catch these in development.
          action = _safeFallbackAction(msg.valid_actions as string[] | undefined);
        }
        await sendJson(ws, {
          type: "turn_action",
          match_id: ctx.matchId,
          request_id: requestId,
          ...action.toWire(),
        });
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
          // Re-enter the turn_request handler logically — easier to just
          // call ourselves with a synthesized iterator for one message.
          let action: Action;
          const requestId = (pending.request_id as string | undefined) ?? "";
          try {
            action = bot.decide(parseGameState(pending));
            if (!(action instanceof Action)) {
              action = _safeFallbackAction(pending.valid_actions as string[] | undefined);
            }
          } catch {
            action = _safeFallbackAction(pending.valid_actions as string[] | undefined);
          }
          await sendJson(ws, {
            type: "turn_action",
            match_id: ctx.matchId,
            request_id: requestId,
            ...action.toWire(),
          });
        }
        break;
      }

      case "match_end":
        bot.onMatchEnd((msg.results as Record<string, unknown>) ?? msg);
        return; // clean exit

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
