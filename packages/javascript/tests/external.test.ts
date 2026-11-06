import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Bot } from "../src/bot.js";
import { BotDecisionError } from "../src/client.js";
import { type ChipzenConfig } from "../src/config.js";
import { Action, type GameState } from "../src/models.js";
import { RetryPolicy } from "../src/retry.js";
import {
  BOT_TOKEN_SUBPROTOCOL,
  asFactory,
  botTokenSubprotocols,
  resolveGatewayUrl,
  runExternalBot,
  type ExternalConnection,
  type Transport,
  _resetSleep,
  _setLobbyRecvTimeoutMs,
  _setSleep,
} from "../src/external.js";

const LOBBY_URL = "wss://staging.chipzen.ai/ws/external/bot/test-bot-uuid";

// ---------------------------------------------------------------------------
// Bots
// ---------------------------------------------------------------------------

class CollectBot extends Bot {
  events: string[] = [];
  latencies: number[] = [];
  decide(state: GameState): Action {
    this.events.push("decide");
    if (state.validActions.includes("check")) return Action.check();
    return Action.fold();
  }
  override onMatchStart(): void {
    this.events.push("match_start");
  }
  override onMatchEnd(): void {
    this.events.push("match_end");
  }
  override onDecisionLatency(latencyMs: number): void {
    this.latencies.push(latencyMs);
  }
}

class CrashBot extends Bot {
  decide(): Action {
    throw new Error("boom");
  }
}

// ---------------------------------------------------------------------------
// Canned protocol frames
// ---------------------------------------------------------------------------

const serverHelloLobby = (): Record<string, unknown> => ({ type: "hello", endpoint: "lobby" });
const serverHello = (): Record<string, unknown> => ({
  type: "hello",
  selected_version: "1.0",
  game_type: "nlhe_6max",
});
const matchStart = (): Record<string, unknown> => ({
  type: "match_start",
  match_id: "m1",
  seats: [
    { seat: 0, is_self: true },
    { seat: 1, is_self: false },
  ],
  turn_timeout_ms: 5000,
});
const turnRequest = (): Record<string, unknown> => ({
  type: "turn_request",
  match_id: "m1",
  request_id: "req_1",
  valid_actions: ["fold", "call", "raise"],
  state: {
    phase: "preflop",
    board: [],
    your_hole_cards: ["Ah", "Kd"],
    pot: 15,
    to_call: 5,
    min_raise: 20,
    max_raise: 995,
  },
});
const matchEnd = (reason = "complete"): Record<string, unknown> => ({
  type: "match_end",
  match_id: "m1",
  reason,
});
const fullMatch = (): Record<string, unknown>[] => [
  serverHello(),
  matchStart(),
  turnRequest(),
  matchEnd(),
];
const matched = (gatewayPath = "/ws/external/match/m1/p1"): Record<string, unknown> => ({
  type: "matched",
  match_id: "m1",
  participant_id: "p1",
  gateway_ws_url: gatewayPath,
  rated: false,
});

// ---------------------------------------------------------------------------
// Mock transport
// ---------------------------------------------------------------------------

/** Sentinel placed in a lobby script to make the next read return null (drop). */
const CLOSE = Symbol("close");

/** A test step placed in a script: it runs between frames and may hold the reader. */
type Step = () => Promise<void>;

/** A gateway script entry: the server closes the socket with this close code. */
class CloseWith {
  constructor(readonly code: number) {}
}

type ScriptEntry = Record<string, unknown> | typeof CLOSE | Step | CloseWith;

/**
 * A reader over a fixed frame list. Gateway readers yield frames then
 * signal close (`null`). Lobby readers, when `blockAfterExhausted`, block
 * forever once frames run out so the loop's recv-timeout re-checks stop.
 */
class ScriptReader {
  private idx = 0;
  private code: number | undefined;
  constructor(
    private readonly frames: ScriptEntry[],
    private readonly blockAfterExhausted: boolean,
  ) {}
  closeCode(): number | undefined {
    return this.code;
  }
  async next(): Promise<string | null> {
    if (this.idx < this.frames.length) {
      const frame = this.frames[this.idx++]!;
      if (frame === CLOSE) return null;
      if (frame instanceof CloseWith) {
        this.code = frame.code;
        this.idx = this.frames.length; // nothing arrives after a close
        return null;
      }
      if (typeof frame === "function") {
        await frame();
        return this.next();
      }
      return JSON.stringify(frame);
    }
    if (!this.blockAfterExhausted) return null;
    // Block forever — the lobby loop's recv-timeout wakes it to re-check stop.
    return new Promise<string | null>(() => {
      /* never resolves */
    });
  }
}

interface MockCalls {
  lobby: string[];
  gateway: string[];
  subprotocols: Array<string[] | undefined>;
  ua: Array<string | undefined>;
  sleeps: number[];
  lobbySent: Array<Record<string, unknown>>;
}

/**
 * Build a transport that routes lobby vs gateway by URL and replays
 * scripted frames. `gatewayScripts` / `lobbyScripts` are arrays of
 * frame-lists; one is consumed per connect (so successive connects can
 * differ — reconnect tests). Single-list shorthand reuses one script.
 */
function installTransport(opts: {
  lobbyScripts: ScriptEntry[][];
  gatewayScripts: ScriptEntry[][];
}): { transport: Transport; calls: MockCalls } {
  const calls: MockCalls = {
    lobby: [],
    gateway: [],
    subprotocols: [],
    ua: [],
    sleeps: [],
    lobbySent: [],
  };
  let lobbyIdx = 0;
  let gwIdx = 0;

  const transport: Transport = async (url, options) => {
    calls.ua.push(options.userAgent);
    if (url.includes("/ws/external/match/")) {
      calls.gateway.push(url);
      calls.subprotocols.push(options.subprotocols);
      const frames = opts.gatewayScripts[gwIdx++] ?? fullMatch();
      const reader = new ScriptReader([...frames], false);
      const conn: ExternalConnection = {
        send: () => {},
        reader,
        close: () => {},
      };
      return conn;
    }
    calls.lobby.push(url);
    const frames = opts.lobbyScripts[lobbyIdx++] ?? [];
    const reader = new ScriptReader([...frames], true);
    const conn: ExternalConnection = {
      send: (data: string) => {
        calls.lobbySent.push(JSON.parse(data) as Record<string, unknown>);
      },
      reader,
      close: () => {},
    };
    return conn;
  };

  return { transport, calls };
}

beforeEach(() => {
  // Keep the lobby idle re-check fast so tests don't wait.
  _setLobbyRecvTimeoutMs(5);
  // Make backoff instant + recorded.
});
afterEach(() => {
  _setLobbyRecvTimeoutMs(2000);
  _resetSleep();
});

// ---------------------------------------------------------------------------
// URL helpers (pure)
// ---------------------------------------------------------------------------

describe("url + token helpers", () => {
  it("botTokenSubprotocols builds [sentinel, token]", () => {
    expect(botTokenSubprotocols("cz_extbot_x")).toEqual([BOT_TOKEN_SUBPROTOCOL, "cz_extbot_x"]);
  });

  it("resolveGatewayUrl joins a path to the lobby origin", () => {
    expect(
      resolveGatewayUrl("wss://staging.chipzen.ai/ws/external/bot/abc", "/ws/external/match/m1/p1"),
    ).toBe("wss://staging.chipzen.ai/ws/external/match/m1/p1");
  });

  it("resolveGatewayUrl passes a same-origin absolute url through", () => {
    const full = "wss://staging.chipzen.ai/ws/external/match/m1/p1";
    expect(resolveGatewayUrl("wss://staging.chipzen.ai/x", full)).toBe(full);
  });

  it("resolveGatewayUrl rejects a cross-origin absolute url", () => {
    // The bot token must not follow a redirect to another host.
    expect(() =>
      resolveGatewayUrl(
        "wss://staging.chipzen.ai/x",
        "wss://attacker.example/ws/external/match/m1/p1",
      ),
    ).toThrow(/cross-origin/);
  });

  it("resolveGatewayUrl rejects a wss->ws downgrade", () => {
    expect(() =>
      resolveGatewayUrl(
        "wss://staging.chipzen.ai/x",
        "ws://staging.chipzen.ai/ws/external/match/m1/p1",
      ),
    ).toThrow(/cross-origin or insecure/);
  });
});

// ---------------------------------------------------------------------------
// asFactory
// ---------------------------------------------------------------------------

describe("asFactory", () => {
  it("reuses an instance", () => {
    const bot = new CollectBot();
    const factory = asFactory(bot);
    expect(factory()).toBe(bot);
    expect(factory()).toBe(bot);
  });

  it("a factory function makes fresh instances", () => {
    const factory = asFactory(() => new CollectBot());
    const a = factory();
    const b = factory();
    expect(a).toBeInstanceOf(CollectBot);
    expect(a).not.toBe(b);
  });

  it("rejects a non-bot", () => {
    expect(() => asFactory({} as never)).toThrow(TypeError);
  });
});

// ---------------------------------------------------------------------------
// Connection resolution + validation
// ---------------------------------------------------------------------------

function cfg(fields: Partial<ChipzenConfig>): ChipzenConfig {
  return { path: "/x", token: null, url: null, botId: null, ...fields };
}

describe("connection resolution", () => {
  it("requires a token", async () => {
    await expect(
      runExternalBot(new CollectBot(), { url: LOBBY_URL, config: cfg({ token: null }) }),
    ).rejects.toThrow(/requires an external-API token/);
  });

  it("requires url or botId", async () => {
    await expect(
      runExternalBot(new CollectBot(), { config: cfg({ token: "cz_extbot_x", botId: null }) }),
    ).rejects.toThrow(/needs a lobby URL/);
  });

  it("uses the config token when no kwarg", async () => {
    const { transport, calls } = installTransport({
      lobbyScripts: [[serverHelloLobby(), matched()]],
      gatewayScripts: [fullMatch()],
    });
    await runExternalBot(new CollectBot(), {
      url: LOBBY_URL,
      config: cfg({ token: "cz_extbot_from_config" }),
      maxMatches: 1,
      transport,
    });
    const auth = calls.lobbySent[0]!;
    expect(auth.type).toBe("authenticate");
    expect(auth.token).toBe("cz_extbot_from_config");
  });

  it("explicit token overrides config", async () => {
    const { transport, calls } = installTransport({
      lobbyScripts: [[serverHelloLobby(), matched()]],
      gatewayScripts: [fullMatch()],
    });
    await runExternalBot(new CollectBot(), {
      url: LOBBY_URL,
      token: "cz_extbot_explicit",
      config: cfg({ token: "cz_extbot_config" }),
      maxMatches: 1,
      transport,
    });
    expect(calls.lobbySent[0]!.token).toBe("cz_extbot_explicit");
  });

  it("botId + env builds the lobby URL", async () => {
    const { transport, calls } = installTransport({
      lobbyScripts: [[serverHelloLobby(), matched()]],
      gatewayScripts: [fullMatch()],
    });
    await runExternalBot(new CollectBot(), {
      botId: "abc",
      env: "staging",
      config: cfg({ token: "cz_extbot_x" }),
      maxMatches: 1,
      transport,
    });
    expect(calls.lobby[0]).toBe("wss://staging.chipzen.ai/ws/external/bot/abc");
  });
});

// ---------------------------------------------------------------------------
// Lobby -> gateway happy path
// ---------------------------------------------------------------------------

describe("lobby -> gateway happy path", () => {
  it("plays one match end-to-end", async () => {
    const { transport, calls } = installTransport({
      lobbyScripts: [[serverHelloLobby(), matched()]],
      gatewayScripts: [fullMatch()],
    });
    const bot = new CollectBot();
    const results = await runExternalBot(bot, {
      url: LOBBY_URL,
      token: "cz_extbot_x",
      maxMatches: 1,
      transport,
    });

    expect(results).toHaveLength(1);
    expect((results[0]!.end as Record<string, unknown>).reason).toBe("complete");
    expect(results[0]!.matchId).toBe("m1");

    expect(bot.events).toEqual(["match_start", "decide", "match_end"]);
    expect(bot.latencies).toHaveLength(1);

    // Gateway leg carried the token in the Sec-WebSocket-Protocol offer.
    expect(calls.subprotocols[0]).toEqual([BOT_TOKEN_SUBPROTOCOL, "cz_extbot_x"]);
    // Default non-default UA.
    expect(calls.ua[0]?.startsWith("chipzen-sdk-js/")).toBe(true);
  });

  it("answers a lobby ping with pong", async () => {
    const { transport, calls } = installTransport({
      lobbyScripts: [[serverHelloLobby(), { type: "ping" }, matched()]],
      gatewayScripts: [fullMatch()],
    });
    await runExternalBot(new CollectBot(), {
      url: LOBBY_URL,
      token: "cz_extbot_x",
      maxMatches: 1,
      transport,
    });
    const sentTypes = calls.lobbySent.map((m) => m.type);
    expect(sentTypes).toContain("pong");
  });

  it("evict ends the session with no match", async () => {
    const { transport } = installTransport({
      lobbyScripts: [[serverHelloLobby(), { type: "evict" }]],
      gatewayScripts: [],
    });
    const results = await runExternalBot(new CollectBot(), {
      url: LOBBY_URL,
      token: "cz_extbot_x",
      transport,
    });
    expect(results).toEqual([]);
  });

  it("maxMatches stops after one", async () => {
    const { transport } = installTransport({
      lobbyScripts: [[serverHelloLobby(), matched()]],
      gatewayScripts: [fullMatch()],
    });
    const results = await runExternalBot(new CollectBot(), {
      url: LOBBY_URL,
      token: "cz_extbot_x",
      maxMatches: 1,
      transport,
    });
    expect(results).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// safeMode
// ---------------------------------------------------------------------------

describe("safeMode", () => {
  it("safeMode=false propagates the bot error", async () => {
    const { transport } = installTransport({
      lobbyScripts: [[serverHelloLobby(), matched()]],
      gatewayScripts: [fullMatch()],
    });
    await expect(
      runExternalBot(new CrashBot(), {
        url: LOBBY_URL,
        token: "cz_extbot_x",
        safeMode: false,
        maxMatches: 1,
        transport,
      }),
    ).rejects.toThrow(BotDecisionError);
  });

  it("safeMode=true folds the bot error and still records the match", async () => {
    const { transport } = installTransport({
      lobbyScripts: [[serverHelloLobby(), matched()]],
      gatewayScripts: [fullMatch()],
    });
    const results = await runExternalBot(new CrashBot(), {
      url: LOBBY_URL,
      token: "cz_extbot_x",
      safeMode: true,
      maxMatches: 1,
      transport,
    });
    expect(results).toHaveLength(1);
    expect((results[0]!.end as Record<string, unknown>).reason).toBe("complete");
  });
});

// ---------------------------------------------------------------------------
// Reconnect behavior (gateway mid-match drop + lobby drop)
// ---------------------------------------------------------------------------

describe("reconnect behavior", () => {
  it("gateway reconnects and resumes", async () => {
    _setSleep(async () => {}); // instant backoff
    const { transport, calls } = installTransport({
      lobbyScripts: [[serverHelloLobby(), matched()]],
      gatewayScripts: [
        [serverHello(), matchStart(), turnRequest()], // drops, no match_end
        [serverHello(), matchEnd()], // reconnect -> completes
      ],
    });
    const results = await runExternalBot(new CollectBot(), {
      url: LOBBY_URL,
      token: "cz_extbot_x",
      maxMatches: 1,
      transport,
    });
    expect(calls.gateway).toHaveLength(2); // reconnected once
    expect(results).toHaveLength(1);
    expect((results[0]!.end as Record<string, unknown>).reason).toBe("complete");
  });

  it("gateway reconnect budget exhausted abandons the match", async () => {
    const sleeps: number[] = [];
    _setSleep(async (ms: number) => {
      sleeps.push(ms);
    });
    const policy = new RetryPolicy({ maxReconnectAttempts: 2, initialBackoffMs: 1, maxBackoffMs: 1 });
    const { transport, calls } = installTransport({
      lobbyScripts: [[serverHelloLobby(), matched()]],
      gatewayScripts: [
        [serverHello(), matchStart()], // initial
        [serverHello(), matchStart()], // retry 1
        [serverHello(), matchStart()], // retry 2
      ],
    });
    const results = await runExternalBot(new CollectBot(), {
      url: LOBBY_URL,
      token: "cz_extbot_x",
      retryPolicy: policy,
      maxMatches: 1,
      transport,
    });
    expect(calls.gateway).toHaveLength(3); // initial + 2 retries, then give up
    expect(results[0]!.end).toBeNull();
    // Backoff used the policy (capped at 1ms) for each reconnect.
    expect(sleeps).toEqual([1, 1]);
  });

  it("lobby reconnects after a close", async () => {
    _setSleep(async () => {}); // instant backoff
    const { transport, calls } = installTransport({
      lobbyScripts: [
        [serverHelloLobby(), CLOSE], // connects, then drops
        [serverHelloLobby(), matched()], // reconnect -> a match arrives
      ],
      gatewayScripts: [fullMatch()],
    });
    const results = await runExternalBot(new CollectBot(), {
      url: LOBBY_URL,
      token: "cz_extbot_x",
      maxMatches: 1,
      transport,
    });
    expect(calls.lobby).toHaveLength(2); // lobby reconnected
    expect(results).toHaveLength(1);
    expect((results[0]!.end as Record<string, unknown>).reason).toBe("complete");
  });
});

// ---------------------------------------------------------------------------
// A repeat `matched` for a match still being played (chipzen-ai/Chipzen#6629)
//
// On lobby connect the platform re-sends `matched` (`resume: true`) for every
// match the bot holds a seat in, so that a RESTARTED process can pick its match
// back up. A process that only lost its lobby socket is still playing that match
// on its own gateway socket; opening a second one gets `duplicate_participant`.
// ---------------------------------------------------------------------------

describe("repeat matched for a live match", () => {
  const matchedFor = (matchId: string, extra: Record<string, unknown> = {}) => ({
    ...matched(`/ws/external/match/${matchId}/p1`),
    match_id: matchId,
    ...extra,
  });

  /** A healthy mid-match gateway: holds before `match_end` until released. */
  const heldMatch = (): { script: ScriptEntry[]; release: Step } => {
    let open!: () => void;
    const released = new Promise<void>((resolve) => {
      open = resolve;
    });
    return {
      script: [serverHello(), matchStart(), turnRequest(), () => released, matchEnd()],
      release: async () => open(),
    };
  };

  /** Microtasks only, so the lobby's recv-timeout cannot fire during a step. */
  const letTasksRun: Step = async () => {
    for (let i = 0; i < 500; i++) await Promise.resolve();
  };

  const countingFactory = (): { made: CollectBot[]; factory: () => CollectBot } => {
    const made: CollectBot[] = [];
    return {
      made,
      factory: () => {
        const bot = new CollectBot();
        made.push(bot);
        return bot;
      },
    };
  };

  it.each([
    ["one-match", 1],
    ["long-running", null],
  ])("a lobby reconnect mid-match keeps the one gateway (%s)", async (_name, maxMatches) => {
    // The #6629 sequence: the lobby drops mid-match, the reconnected lobby
    // re-dispatches the match, and the bot must stay on the gateway it has.
    _setSleep(async () => {}); // instant backoff
    const held = heldMatch();
    const { transport, calls } = installTransport({
      lobbyScripts: [
        [serverHelloLobby(), matched(), CLOSE],
        [
          serverHelloLobby(),
          matchedFor("m1", { resume: true }),
          held.release,
          letTasksRun,
          { type: "evict" },
        ],
      ],
      gatewayScripts: [held.script],
    });
    const { made, factory } = countingFactory();
    const results = await runExternalBot(factory, {
      url: LOBBY_URL,
      token: "cz_extbot_x",
      maxMatches,
      transport,
    });
    expect(calls.lobby).toHaveLength(2); // the lobby did reconnect
    expect(calls.gateway).toHaveLength(1); // ...and no second gateway opened
    expect(made).toHaveLength(1); // no bot instance built for the repeat frame
    expect(results).toHaveLength(1); // the ignored frame is not a played match
    expect((results[0]!.end as Record<string, unknown>).reason).toBe("complete");
    expect(made[0]!.events.at(-1)).toBe("match_end"); // the original match played out
  });

  it("a repeat on the same lobby socket is ignored", async () => {
    const held = heldMatch();
    const { transport, calls } = installTransport({
      lobbyScripts: [[serverHelloLobby(), matched(), matched(), held.release]],
      gatewayScripts: [held.script],
    });
    const results = await runExternalBot(new CollectBot(), {
      url: LOBBY_URL,
      token: "cz_extbot_x",
      maxMatches: 1,
      transport,
    });
    expect(calls.gateway).toHaveLength(1);
    expect(results).toHaveLength(1);
  });

  it("a new match id during a live match starts normally", async () => {
    // Overlapping matches are supported: only the SAME id is a repeat.
    const held = heldMatch();
    const { transport, calls } = installTransport({
      lobbyScripts: [
        [
          serverHelloLobby(),
          matchedFor("m1"),
          matchedFor("m2"),
          letTasksRun,
          held.release,
          letTasksRun,
          { type: "evict" }, // ends the session even if m2 was ignored
        ],
      ],
      gatewayScripts: [held.script, fullMatch()],
    });
    const { made, factory } = countingFactory();
    const results = await runExternalBot(factory, {
      url: LOBBY_URL,
      token: "cz_extbot_x",
      maxMatches: 2,
      transport,
    });
    expect(calls.gateway.map((url) => url.split("/match/")[1])).toEqual(["m1/p1", "m2/p1"]);
    expect(made).toHaveLength(2);
    expect(results).toHaveLength(2);
  });

  it("a matched after the task gave up plays again", async () => {
    // "Still playing" means a live task. If this session abandoned the match
    // (reconnect budget spent) and the platform still holds the seat, a later
    // re-dispatch must get a fresh attempt, not be ignored.
    _setSleep(async () => {}); // instant backoff
    const policy = new RetryPolicy({ maxReconnectAttempts: 1, initialBackoffMs: 1, maxBackoffMs: 1 });
    const { transport, calls } = installTransport({
      lobbyScripts: [
        [
          serverHelloLobby(),
          matched(),
          letTasksRun,
          matchedFor("m1", { resume: true }),
          letTasksRun,
          { type: "evict" }, // ends the session even if the repeat was ignored
        ],
      ],
      gatewayScripts: [
        [serverHello(), matchStart()], // drops
        [serverHello(), matchStart()], // retry drops: abandoned
        fullMatch(), // the re-dispatch plays to match_end
      ],
    });
    const results = await runExternalBot(new CollectBot(), {
      url: LOBBY_URL,
      token: "cz_extbot_x",
      retryPolicy: policy,
      maxMatches: 2,
      transport,
    });
    expect(calls.gateway).toHaveLength(3);
    expect(results.map((r) => (r.end as Record<string, unknown> | null)?.reason ?? null)).toEqual([
      null,
      "complete",
    ]);
  });
});

// ---------------------------------------------------------------------------
// A match seat taken over by another connection of the same bot (#242)
//
// A newer socket for the same seat takes the match over (chipzen-ai/Chipzen#6734).
// The old socket gets an `error` frame (`replaced_by_new_connection`) and then a
// 4000 close. Reconnecting would take the seat back, and two live copies of one
// bot would trade it forever, so the match's task ends instead.
// ---------------------------------------------------------------------------

describe("a match seat taken over by another connection", () => {
  const SEAT_LINE = "seat taken over by another connection of this bot";
  const matchedFor = (matchId: string, extra: Record<string, unknown> = {}) => ({
    ...matched(`/ws/external/match/${matchId}/p1`),
    match_id: matchId,
    ...extra,
  });
  const replacedErrorFrame = (): Record<string, unknown> => ({
    type: "error",
    code: "replaced_by_new_connection",
    message: "a newer connection for this seat replaced this one",
  });
  const letTasksRun: Step = async () => {
    for (let i = 0; i < 500; i++) await Promise.resolve();
  };

  let warn: ReturnType<typeof vi.spyOn>;
  const seatLines = (): string[] =>
    warn.mock.calls.map((c) => String(c[0])).filter((line) => line.includes(SEAT_LINE));
  let sleeps: number[];

  beforeEach(() => {
    warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    sleeps = [];
    _setSleep(async (ms: number) => {
      sleeps.push(ms);
    });
  });
  afterEach(() => {
    warn.mockRestore();
  });

  it.each([
    ["4000 close", [serverHello(), matchStart(), turnRequest(), new CloseWith(4000)]],
    [
      "error frame then close",
      [serverHello(), matchStart(), replacedErrorFrame(), new CloseWith(4000)],
    ],
  ] as Array<[string, ScriptEntry[]]>)(
    "%s ends the match with no reconnect and one log line",
    async (_name, script) => {
      const { transport, calls } = installTransport({
        lobbyScripts: [[serverHelloLobby(), matched(), letTasksRun, { type: "evict" }]],
        // A reconnect would get the default full match and record a result.
        gatewayScripts: [script],
      });
      const results = await runExternalBot(new CollectBot(), {
        url: LOBBY_URL,
        token: "cz_extbot_x",
        maxMatches: 1,
        transport,
      });
      expect(calls.gateway).toHaveLength(1); // no reconnect
      expect(sleeps).toEqual([]); // no backoff either
      expect(results).toEqual([]); // not a finished match, and no bogus result entry
      expect(seatLines()).toEqual([
        `chipzen-sdk: match m1: ${SEAT_LINE}; not reconnecting`,
      ]);
    },
  );

  it.each([
    ["no close frame (1006)", [serverHello(), matchStart(), turnRequest()]],
    ["1011", [serverHello(), matchStart(), turnRequest(), new CloseWith(1011)]],
    ["4001", [serverHello(), matchStart(), turnRequest(), new CloseWith(4001)]],
  ] as Array<[string, ScriptEntry[]]>)("a %s drop still reconnects", async (_name, script) => {
    const { transport, calls } = installTransport({
      lobbyScripts: [[serverHelloLobby(), matched()]],
      gatewayScripts: [script, [serverHello(), matchEnd()]],
    });
    const results = await runExternalBot(new CollectBot(), {
      url: LOBBY_URL,
      token: "cz_extbot_x",
      maxMatches: 1,
      transport,
    });
    expect(calls.gateway).toHaveLength(2); // reconnected once
    expect((results[0]!.end as Record<string, unknown>).reason).toBe("complete");
    expect(seatLines()).toEqual([]);
  });

  it("a later matched for another match plays, and the taken-over one is not retaken", async () => {
    const { transport, calls } = installTransport({
      lobbyScripts: [
        [
          serverHelloLobby(),
          matchedFor("m1"),
          letTasksRun,
          // The lobby re-sends matched for m1 (the bot still holds that seat,
          // on the other connection): taking it would steal it back.
          matchedFor("m1", { resume: true }),
          matchedFor("m2"),
          letTasksRun,
          { type: "evict" },
        ],
      ],
      gatewayScripts: [[serverHello(), matchStart(), new CloseWith(4000)], fullMatch()],
    });
    const made: CollectBot[] = [];
    const results = await runExternalBot(
      () => {
        made.push(new CollectBot());
        return made.at(-1)!;
      },
      { url: LOBBY_URL, token: "cz_extbot_x", maxMatches: 1, transport },
    );
    expect(calls.gateway.map((url) => url.split("/match/")[1])).toEqual(["m1/p1", "m2/p1"]);
    expect(made).toHaveLength(2); // no bot built for the ignored re-dispatch
    expect(results).toHaveLength(1);
    expect((results[0]!.end as Record<string, unknown>).reason).toBe("complete");
    expect(seatLines()).toHaveLength(1);
  });
});
