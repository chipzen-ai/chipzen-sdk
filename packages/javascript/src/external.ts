/**
 * External-API remote-play entry point: {@link runExternalBot}.
 *
 * Where `runBot` connects a bot to a *known* match URL (the
 * containerized/upload path — the platform's executor hands the container
 * its `/ws/match/{matchId}/{participantId}` URL), this module implements
 * the **external-API remote-play** path: a developer runs their bot on
 * their own machine, authenticates with a long-lived `cz_extbot_` token,
 * and the platform matches and dispatches them like any other competitor.
 *
 * The flow:
 *
 *     lobby WS  /ws/external/bot/{botId}      (token in authenticate frame)
 *         -> "matched" notify (carries matchId + gatewayWsUrl)
 *         -> per-match gateway WS /ws/external/match/{mid}/{pid}
 *                                  (token in Sec-WebSocket-Protocol header)
 *         -> two-layer bot handshake + game loop to match_end
 *
 * The **match data plane is identical** to the containerized path, so the
 * game loop here reuses `_runSession` from `client.ts` verbatim — the only
 * external-API-specific code is the lobby connection and the per-match
 * gateway handshake. A developer writes ONE `Bot` subclass and it works on
 * both paths.
 *
 * The lobby is held open for the bot's whole session and each `matched`
 * plays in its own task (Promise), so the 15-second lobby heartbeat is
 * answered even while a multi-minute match is in flight. This is what lets
 * a single connection serve a whole tournament (the bot is "checked in"
 * via lobby presence and matched once per round).
 *
 * Mirrors the Python SDK's `chipzen.external`, including the reconnect
 * fix: a dropped gateway socket RECONNECTS and resumes; match-task
 * ownership is hoisted to the top level so a lobby reconnect doesn't kill
 * in-flight matches; teardown drains-then-cancels, never orphaning a task.
 */

import WebSocket from "ws";

import type { Bot } from "./bot.js";
import {
  _NodeWebSocketReader,
  _runSession,
  type AsyncMessageReader,
  BotDecisionError,
} from "./client.js";
import {
  type ChipzenConfig,
  loadChipzenConfig,
  resolveToken,
} from "./config.js";
import { connectToChipzen, type EnvName } from "./connect.js";
import { DEFAULT_RETRY_POLICY, RetryPolicy } from "./retry.js";
import { VERSION } from "./version.js";

/**
 * Sentinel subprotocol that marks the `cz_extbot_` token in the
 * `Sec-WebSocket-Protocol` header (CZ issue 2932 moved the token off the
 * query string, where it leaked into proxy access logs). Must match the
 * value the platform's api gateway expects.
 */
export const BOT_TOKEN_SUBPROTOCOL = "chipzen-bot-token";

/**
 * How long the lobby loop blocks on a single `reader.next()` before waking
 * to re-check the stop signal. Short enough that a stop is honored
 * promptly; long enough that the loop isn't a busy-wait. Mutable so tests
 * can shrink it.
 */
export let LOBBY_RECV_TIMEOUT_MS = 2000;

/** Test hook: override {@link LOBBY_RECV_TIMEOUT_MS}. */
export function _setLobbyRecvTimeoutMs(ms: number): void {
  LOBBY_RECV_TIMEOUT_MS = ms;
}

/**
 * On teardown, how long to let still-in-flight matches finish before
 * cancelling them, so nothing is left orphaned.
 */
export const MATCH_DRAIN_GRACE_MS = 5000;

/**
 * Build the `Sec-WebSocket-Protocol` offer that carries the bot token.
 *
 * Returns `[sentinel, token]` — the sentinel marks "the next value is my
 * bot token". The api gateway extracts the token from this header (so it
 * never appears in any access log / URL) and echoes the sentinel back on
 * accept.
 */
export function botTokenSubprotocols(token: string): [string, string] {
  return [BOT_TOKEN_SUBPROTOCOL, token];
}

function normaliseBase(url: string): string {
  // Strip path/query/fragment, keeping scheme + authority only. Tolerate a
  // bare `host:port` without a scheme (defaults to wss).
  try {
    const u = new URL(url);
    return `${u.protocol}//${u.host}`;
  } catch {
    // Not a parseable absolute URL; treat the whole thing as host[:port].
    const host = url.replace(/\/+$/, "");
    return `wss://${host}`;
  }
}

/**
 * Resolve the `matched.gateway_ws_url` path against the lobby origin.
 *
 * The `matched` notification carries `gateway_ws_url` as a *path*
 * (`/ws/external/match/{mid}/{pid}`). The `cz_extbot_` token is NOT on the
 * query string — it travels in the `Sec-WebSocket-Protocol` header. A
 * future server that returns a full URL is passed through unchanged.
 */
export function resolveGatewayUrl(lobbyUrl: string, gatewayWsPath: string): string {
  if (gatewayWsPath.startsWith("ws://") || gatewayWsPath.startsWith("wss://")) {
    // Absolute URL from the server: honor it only if it stays on the same
    // origin as the lobby and doesn't downgrade wss -> ws. Otherwise the bot
    // token could be sent to an attacker-/misconfig-supplied host (or in
    // cleartext). A relative path is re-anchored to the lobby origin below, so
    // it's inherently same-origin.
    const lobby = new URL(normaliseBase(lobbyUrl));
    const gateway = new URL(gatewayWsPath);
    const downgrade = lobby.protocol === "wss:" && gateway.protocol !== "wss:";
    if (gateway.host !== lobby.host || downgrade) {
      throw new Error(
        `refusing gateway URL ${gatewayWsPath}: cross-origin or insecure relative to ` +
          `lobby ${lobby.protocol}//${lobby.host} (the bot token must not be sent to a ` +
          `different host or in cleartext)`,
      );
    }
    return gatewayWsPath;
  }
  return `${normaliseBase(lobbyUrl)}${gatewayWsPath}`;
}

/** Parse a WS frame into an object (`{}` on non-object / bad JSON). */
function loads(raw: string): Record<string, unknown> {
  try {
    const msg = JSON.parse(raw) as unknown;
    return msg !== null && typeof msg === "object" && !Array.isArray(msg)
      ? (msg as Record<string, unknown>)
      : {};
  } catch {
    return {};
  }
}

/** A factory that produces a fresh {@link Bot} per match (or returns the same one). */
export type BotFactory = () => Bot;

/**
 * Normalize the `bot` argument into a per-match instance factory.
 *
 * Accepts either:
 *
 * - a `Bot` **instance** — reused for every match (correct for the common
 *   case of sequential tournament matches), or
 * - a **callable** returning a fresh `Bot` (a factory function) — a new
 *   instance per match, the right choice for overlapping matches with
 *   per-match mutable state.
 *
 * (TS class constructors are also callable; passing `MyBot` works, but the
 * idiomatic JS form is `() => new MyBot()`.)
 */
export function asFactory(bot: Bot | BotFactory): BotFactory {
  // A Bot instance has a `decide` method; a factory function does not (it
  // returns one when called). Disambiguate on `decide`.
  if (bot && typeof (bot as Bot).decide === "function") {
    const instance = bot as Bot;
    return () => instance;
  }
  if (typeof bot === "function") {
    return bot as BotFactory;
  }
  throw new TypeError(
    "runExternalBot(bot=...) must be a Bot instance or a callable returning one, " +
      `got ${typeof bot}.`,
  );
}

// ---------------------------------------------------------------------------
// Transport — injectable so tests can drive the lobby/gateway without a
// real network (mirrors the Python tests' monkeypatch of websockets.connect).
// ---------------------------------------------------------------------------

/** A connected WS-shaped object the lobby loop / `_runSession` drive. */
export interface ExternalConnection {
  /** Send a string frame. */
  send(data: string): void | Promise<void>;
  /** Pull-based message reader; resolves `null` when the socket closes. */
  reader: AsyncMessageReader;
  /** Close the underlying socket. */
  close(): void;
}

/** Options handed to a {@link Transport} on connect. */
export interface TransportConnectOptions {
  /** WS `User-Agent` header value. */
  userAgent: string;
  /**
   * Sec-WebSocket-Protocol offer (gateway leg carries `[sentinel, token]`).
   * Omitted for the lobby leg.
   */
  subprotocols?: string[];
}

/** Opens WS connections for the external path. Injectable for tests. */
export type Transport = (
  url: string,
  options: TransportConnectOptions,
) => Promise<ExternalConnection>;

const MAX_WS_PAYLOAD = 2 ** 24; // 16 MiB — large round_result / deck_reveal frames.

/** Default transport: a real `ws.WebSocket`. */
const defaultTransport: Transport = async (url, options) => {
  const ws = new WebSocket(url, options.subprotocols ?? [], {
    headers: { "User-Agent": options.userAgent },
    maxPayload: MAX_WS_PAYLOAD,
  });
  await new Promise<void>((resolve, reject) => {
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
  return {
    send: (data: string) => ws.send(data),
    reader: new _NodeWebSocketReader(ws),
    close: () => {
      if (ws.readyState !== WebSocket.CLOSED && ws.readyState !== WebSocket.CLOSING) {
        ws.close();
      }
    },
  };
};

/**
 * Backoff sleep. Indirected through a module-level binding so tests can
 * replace it with an instant, recording stub (mirrors the Python tests'
 * monkeypatch of `asyncio.sleep`).
 */
let sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));

/** Test hook: replace the backoff sleep (e.g. to record delays instantly). */
export function _setSleep(fn: (ms: number) => Promise<void>): void {
  sleep = fn;
}

/** Test hook: restore the default real backoff sleep. */
export function _resetSleep(): void {
  sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));
}

/** Read the next lobby frame, or `undefined` if the timeout elapsed first. */
async function recvWithTimeout(
  reader: AsyncMessageReader,
  timeoutMs: number,
): Promise<string | null | undefined> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<undefined>((resolve) => {
    timer = setTimeout(() => resolve(undefined), timeoutMs);
  });
  try {
    return await Promise.race([reader.next(), timeout]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

/** Per-match result recorded by the session. */
export interface MatchResult {
  matchId: string | null;
  end: Record<string, unknown> | null;
}

/**
 * A match-play Promise with a `done` flag so the lobby loop can prune
 * settled tasks from the shared list across reconnects (mirrors Python's
 * `match_tasks[:] = [t for t in match_tasks if not t.done()]`).
 */
interface MatchTask {
  promise: Promise<void>;
  done: boolean;
}

interface PlayMatchParams {
  gatewayUrl: string;
  matchId: string;
  token: string;
  bot: Bot;
  policy: RetryPolicy;
  clientName: string;
  clientVersion: string;
  safeMode: boolean;
  userAgent: string;
  transport: Transport;
}

/**
 * Play one match end-to-end over the per-match gateway WS, reconnecting
 * across a mid-match drop.
 *
 * Opens the gateway WS (token in the `Sec-WebSocket-Protocol` header) and
 * hands off to `_runSession` for the two-layer handshake + game loop —
 * the exact data-plane code the containerized path runs.
 *
 * If the socket drops before `match_end`, reconnect (bounded by `policy`)
 * and let the platform's reconnect-resume re-deliver the pending turn:
 * `_runSession` already consumes the server `reconnected` frame and
 * replays its `pending_request`, and the same `bot` instance carries its
 * state across the gap.
 *
 * Returns the `match_end` payload, or `null` if the match could not be
 * completed within the reconnect budget.
 *
 * Throws {@link BotDecisionError} (safeMode=false) without retrying — a
 * deterministic bot bug is terminal.
 */
async function playOneMatch(params: PlayMatchParams): Promise<Record<string, unknown> | null> {
  const { gatewayUrl, matchId, token, bot, policy, transport } = params;
  let attempt = 0;
  for (;;) {
    let conn: ExternalConnection | null = null;
    let reason: string;
    try {
      conn = await transport(gatewayUrl, {
        userAgent: params.userAgent,
        subprotocols: botTokenSubprotocols(token),
      });
      // The inner leg's token is the gateway's internal JWT (authoritative);
      // the executor ignores the value we send, but the authenticate frame
      // MUST be first. _runSession sends an empty token when token+ticket
      // are both null.
      const end = await _runSession(
        conn,
        bot,
        {
          matchId,
          token: null,
          ticket: null,
          clientName: params.clientName,
          clientVersion: params.clientVersion,
          safeMode: params.safeMode,
        },
        conn.reader,
      );
      if (end !== null) {
        return end; // clean match_end — done
      }
      // _runSession returned null: the socket closed without a match_end
      // (a drop). Try to resume.
      reason = "closed without match_end";
    } catch (err) {
      if (err instanceof BotDecisionError) {
        throw err; // deterministic bot bug — terminal, never reconnect-retry
      }
      reason = err instanceof Error ? err.message || err.constructor.name : String(err);
    } finally {
      if (conn) conn.close();
    }

    attempt += 1;
    if (attempt > policy.maxReconnectAttempts) {
      // Reconnect budget exhausted; abandon the match rather than hang.
      return null;
    }
    await sleep(policy.backoffMs(attempt));
  }
}

interface LobbyOnceParams {
  lobbyUrl: string;
  token: string;
  factory: BotFactory;
  results: MatchResult[];
  matchTasks: MatchTask[];
  completed: { value: number };
  policy: RetryPolicy;
  clientName: string;
  clientVersion: string;
  safeMode: boolean;
  userAgent: string;
  maxMatches: number | null;
  stop: { value: boolean };
  fatal: Error[];
  transport: Transport;
}

/**
 * Hold ONE lobby connection and dispatch every `matched` it delivers.
 *
 * Returns a status string:
 *
 * - `"stopped"`  — the stop signal fired, or `maxMatches` was reached.
 * - `"evicted"`  — the lobby evicted us (a newer connection replaced this).
 * - `"closed"`   — the lobby connection closed unexpectedly (may reconnect).
 *
 * Each `matched` is played in its own task, appended to `matchTasks`
 * (which the CALLER owns) so the lobby heartbeat is answered during
 * matches AND in-flight matches survive a lobby reconnect — a match runs
 * on its own gateway socket, independent of the lobby.
 */
async function runLobbyOnce(p: LobbyOnceParams): Promise<"stopped" | "evicted" | "closed"> {
  const onMatchSettled = (
    end: Record<string, unknown> | null | undefined,
    err: unknown,
  ): void => {
    if (err !== undefined && err !== null) {
      if (err instanceof BotDecisionError) {
        // safeMode=false: a deterministic bot bug. Surface it (stop the
        // session and re-raise from runExternalBot).
        p.fatal.push(err);
        p.stop.value = true;
        return;
      }
      // Record, never crash the lobby.
      p.results.push({ matchId: null, end: null });
    } else {
      p.completed.value += 1;
      p.results.push({
        matchId: (end?.match_id as string | undefined) ?? null,
        end: end ?? null,
      });
    }
    if (p.maxMatches !== null && p.completed.value >= p.maxMatches) {
      p.stop.value = true;
    }
  };

  const conn = await p.transport(p.lobbyUrl, { userAgent: p.userAgent });
  try {
    await conn.send(JSON.stringify({ type: "authenticate", token: p.token }));
    while (!p.stop.value) {
      const raw = await recvWithTimeout(conn.reader, LOBBY_RECV_TIMEOUT_MS);
      if (raw === undefined) {
        continue; // periodic wake to re-check the stop signal
      }
      if (raw === null) {
        // Socket closed.
        return "closed";
      }

      const msg = loads(raw);
      const mtype = msg.type;
      if (mtype === "ping") {
        await conn.send(JSON.stringify({ type: "pong" }));
      } else if (mtype === "hello") {
        // Lobby server hello — informational; the client does NOT reply.
      } else if (mtype === "matched") {
        let gatewayUrl: string;
        try {
          gatewayUrl = resolveGatewayUrl(p.lobbyUrl, msg.gateway_ws_url as string);
        } catch {
          // Untrusted gateway URL (cross-origin / downgrade); skip this match
          // rather than send the token there.
          continue;
        }
        const matchId = msg.match_id as string;
        const task: MatchTask = { promise: Promise.resolve(), done: false };
        task.promise = playOneMatch({
          gatewayUrl,
          matchId,
          token: p.token,
          bot: p.factory(),
          policy: p.policy,
          clientName: p.clientName,
          clientVersion: p.clientVersion,
          safeMode: p.safeMode,
          userAgent: p.userAgent,
          transport: p.transport,
        }).then(
          (end) => {
            task.done = true;
            onMatchSettled(end, undefined);
          },
          (err) => {
            task.done = true;
            onMatchSettled(undefined, err);
          },
        );
        p.matchTasks.push(task);
      } else if (mtype === "evict") {
        return "evicted";
      }
      // else: ignore unknown frame types (forward-compat).
    }
    return "stopped";
  } finally {
    conn.close();
  }
}

/**
 * Teardown: let still-in-flight matches finish for a short grace window,
 * then await everything so no task is orphaned.
 *
 * (JS Promises can't be cancelled the way asyncio tasks can; the match
 * tasks are already bounded by the reconnect budget, so the grace window
 * is the meaningful knob — after it, we simply await the rest to
 * completion rather than leaving them dangling.)
 */
async function drainMatches(
  matchTasks: MatchTask[],
  grace: number = MATCH_DRAIN_GRACE_MS,
): Promise<void> {
  if (matchTasks.length === 0) return;
  const all = Promise.allSettled(matchTasks.map((t) => t.promise));
  let timer: ReturnType<typeof setTimeout> | undefined;
  const graceTimeout = new Promise<void>((resolve) => {
    timer = setTimeout(resolve, grace);
  });
  await Promise.race([all.then(() => undefined), graceTimeout]);
  if (timer) clearTimeout(timer);
  // Await the rest so nothing is orphaned / unhandled-rejection-leaks.
  await all;
}

/** Options for {@link runExternalBot}. */
export interface RunExternalBotOptions {
  /** External-API bot UUID. Used to build the lobby URL when `url` is absent. */
  botId?: string;
  /** Target environment (`prod` / `staging` / `local`). `undefined` consults `$CHIPZEN_ENV`. */
  env?: EnvName | null;
  /** Explicit full lobby URL. Overrides `botId` / `env` derivation. */
  url?: string;
  /** Long-lived `cz_extbot_` API token. Falls back to `[external_api].token`. Required. */
  token?: string | null;
  /** Pre-loaded config, to avoid a second filesystem stat. `undefined` triggers discovery. */
  config?: ChipzenConfig | null;
  /** Reconnect-pacing policy. Defaults to {@link DEFAULT_RETRY_POLICY}. */
  retryPolicy?: RetryPolicy;
  /** Client software name sent in the per-match `hello`. */
  clientName?: string;
  /** Client software version. Defaults to the SDK version. */
  clientVersion?: string;
  /** When `true` (default), a `decide()` throw is folded; `false` propagates a {@link BotDecisionError}. */
  safeMode?: boolean;
  /** Stop after this many matches complete. `undefined` runs until the lobby closes / evict. */
  maxMatches?: number | null;
  /** Override the WS `User-Agent`. Defaults to `chipzen-sdk-js/<version>`. */
  userAgent?: string;
  /** Injectable transport (tests). Defaults to a real `ws.WebSocket`. */
  transport?: Transport;
}

/**
 * Run a bot on the Chipzen external-API remote-play path.
 *
 * Connects to the lobby, then plays every match the platform dispatches to
 * this bot (a single challenge, or every round of a tournament) until the
 * lobby closes, the bot is evicted, or `maxMatches` matches complete.
 *
 * @returns A list of per-match result objects (`{matchId, end}`), one per
 *   match played this session.
 * @throws Error if no token can be resolved, or neither `url` nor a
 *   `botId` is available to build the lobby URL.
 * @throws BotDecisionError if `safeMode` is `false` and `bot.decide()` throws.
 */
export async function runExternalBot(
  bot: Bot | BotFactory,
  options: RunExternalBotOptions = {},
): Promise<MatchResult[]> {
  const config = options.config !== undefined ? options.config : loadChipzenConfig();
  const transport = options.transport ?? defaultTransport;
  const clientName = options.clientName ?? "chipzen-sdk-js";
  const clientVersion = options.clientVersion ?? VERSION;
  const safeMode = options.safeMode ?? true;
  const maxMatches = options.maxMatches ?? null;

  // --- Resolve lobby URL + token + retry policy --------------------------
  let lobbyUrl: string;
  let policy: RetryPolicy;
  let resolvedToken: string | null;
  if (options.url !== undefined && options.url !== null) {
    lobbyUrl = options.url;
    policy = options.retryPolicy ?? DEFAULT_RETRY_POLICY;
    resolvedToken = resolveToken({ explicitToken: options.token, config });
  } else {
    const resolvedBotId = options.botId ?? (config ? config.botId : null);
    if (!resolvedBotId) {
      throw new Error(
        "runExternalBot() needs a lobby URL. Pass url=..., or botId=... " +
          "(or set [external_api].bot_id / url in chipzen.toml).",
      );
    }
    const conn = connectToChipzen(resolvedBotId, options.env, {
      retryPolicy: options.retryPolicy,
      config,
    });
    lobbyUrl = conn.url;
    policy = conn.retryPolicy;
    // An explicit token kwarg still wins over the config-file token.
    resolvedToken =
      options.token !== undefined && options.token !== null ? options.token : conn.token;
  }

  if (!resolvedToken) {
    throw new Error(
      "runExternalBot() requires an external-API token (cz_extbot_...). " +
        "Pass token=..., or set [external_api].token in chipzen.toml.",
    );
  }

  const userAgent = options.userAgent ?? `chipzen-sdk-js/${clientVersion}`;
  const factory = asFactory(bot);
  const results: MatchResult[] = [];
  const stop = { value: false };
  const fatal: Error[] = [];
  // Owned HERE, not by a single lobby session, so in-flight matches survive
  // a lobby reconnect (a match plays on its own gateway socket).
  const matchTasks: MatchTask[] = [];
  const completed = { value: 0 };

  // --- Lobby session loop with reconnect/backoff -------------------------
  let consecutiveFailures = 0;
  let everConnected = false;
  let giveupExc: Error | null = null;

  const dropDoneTasks = (): void => {
    // Prune settled tasks so the list doesn't grow unbounded across
    // reconnects (mirrors Python's match_tasks[:] = [...not done]).
    for (let i = matchTasks.length - 1; i >= 0; i--) {
      if (matchTasks[i]!.done) matchTasks.splice(i, 1);
    }
  };

  while (!stop.value) {
    let status: "stopped" | "evicted" | "closed" | null = null;
    try {
      status = await runLobbyOnce({
        lobbyUrl,
        token: resolvedToken,
        factory,
        results,
        matchTasks,
        completed,
        policy,
        clientName,
        clientVersion,
        safeMode,
        userAgent,
        maxMatches,
        stop,
        fatal,
        transport,
      });
      everConnected = true;
    } catch (err) {
      // connect() itself failed — count it as a reconnect attempt.
      consecutiveFailures += 1;
      if (consecutiveFailures > policy.maxReconnectAttempts) {
        // Only a hard error if we NEVER reached the lobby (bad URL / token /
        // network). If we connected and played, give up quietly.
        if (!everConnected) {
          giveupExc = err instanceof Error ? err : new Error(String(err));
        }
        break;
      }
      await sleep(policy.backoffMs(consecutiveFailures));
      dropDoneTasks();
      continue;
    }

    // A live lobby session ran; reset the backoff counter.
    consecutiveFailures = 0;
    if (status === "stopped" || status === "evicted" || fatal.length > 0) {
      break;
    }
    // status === "closed": the lobby dropped. In-flight matches keep playing
    // on their own sockets; reconnect the lobby per the policy.
    consecutiveFailures += 1;
    if (consecutiveFailures > policy.maxReconnectAttempts) {
      break;
    }
    await sleep(policy.backoffMs(consecutiveFailures));
    dropDoneTasks();
  }

  // --- Teardown: never orphan an in-flight match task --------------------
  await drainMatches(matchTasks);
  if (fatal.length > 0) {
    // A bot.decide() error under safeMode=false — re-raise so the process
    // exits non-zero (matches runBot's behavior).
    throw fatal[0];
  }
  if (giveupExc !== null) {
    throw giveupExc;
  }
  return results;
}
