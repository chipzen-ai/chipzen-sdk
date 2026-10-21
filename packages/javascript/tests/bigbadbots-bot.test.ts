/**
 * The Big Bad Bots base class and the session loop's Big Bad Bots path
 * (chipzen-sdk#151). Mirrors the Python SDK's `tests/test_bigbadbots_bot.py`.
 *
 * The frames are the published examples in
 * `docs/protocol/BIGBADBOTS-GAME-STATE-PROTOCOL.md`, played through
 * `_runSession` over a scripted reader and a capturing socket, and through
 * `runExternalBot` over a fake transport.
 */

import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  parseBigBadBotsTurnRequest,
  type AuctionState,
  type BigBadBotsTurnState,
  type BodyShopState,
  type FightState,
  type FittingState,
} from "../src/bigbadbots.js";
import { BigBadBotsBot, fallbackAction, isAnswerShaped } from "../src/bigbadbots_bot.js";
import { Bot } from "../src/bot.js";
import {
  _runSession,
  BIGBADBOTS_SEND_MARGIN_MS,
  BotDecisionError,
  type AsyncMessageReader,
  type SessionWebSocket,
} from "../src/client.js";
import {
  _resetSleep,
  _setLobbyRecvTimeoutMs,
  runExternalBot,
  type ExternalConnection,
  type Transport,
} from "../src/external.js";
import { Action, type GameState } from "../src/models.js";
import { findBotSubclasses } from "../src/run_external.js";

type Json = Record<string, unknown>;

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DOC = path.resolve(HERE, "..", "..", "..", "docs", "protocol", "BIGBADBOTS-GAME-STATE-PROTOCOL.md");
const PHASE_CALL: Record<string, string> = {
  body_shop: "choose_body",
  auction: "bid",
  fitting: "fit",
  fight: "act",
};

function blocks(): Json[] {
  const text = readFileSync(DOC, "utf-8").replace(/\r\n/g, "\n");
  return [...text.matchAll(/^```json\n([\s\S]*?)\n```$/gm)].map((m) => JSON.parse(m[1]!) as Json);
}

function request(call: string, overrides: { requestId?: string; round?: number } = {}): Json {
  const found = blocks().find(
    (b) => b.type === "turn_request" && PHASE_CALL[(b.state as Json).phase as string] === call,
  );
  const msg = structuredClone(found!);
  delete msg.seq; // the loop skips a seq that does not advance; tests replay frames
  if (overrides.requestId) msg.request_id = overrides.requestId;
  if (overrides.round !== undefined) (msg.state as Json).round = overrides.round;
  return msg;
}

const descriptor = (): Json => blocks().find((b) => b.state_shape === "bigbadbots" && "phases" in b)!;
const gameConfig = (): Json => structuredClone(blocks().find((b) => b.variant === "bigbadbots" && "rule_set" in b)!);

const b3Hello = (): Json => ({ type: "hello", selected_version: "1.0", game: descriptor() });
const pokerHello = (): Json => ({ type: "hello", selected_version: "1.0" });
const matchStart = (): Json => ({
  type: "match_start",
  match_id: "m1",
  seats: [
    { seat: 0, is_self: false },
    { seat: 1, is_self: true },
  ],
  turn_timeout_ms: 1000,
  game_config: gameConfig(),
});
const matchEnd = (): Json => ({ type: "match_end", match_id: "m1", reason: "complete", results: [] });
const fightResult = (action: string, round = 1): Json => ({
  type: "turn_result",
  match_id: "m1",
  seat: 1,
  is_timeout: false,
  details: { phase: "fight", action, params: {}, round, tick: 21, opponent_action: "attack" },
});

class ScriptedReader implements AsyncMessageReader {
  private idx = 0;
  constructor(
    private readonly messages: Json[],
    private readonly arrivedAt?: () => number,
  ) {}
  async next(): Promise<string | null> {
    if (this.idx >= this.messages.length) return null;
    return JSON.stringify(this.messages[this.idx++]);
  }
  receivedAt(): number | undefined {
    return this.arrivedAt?.();
  }
}

class CapturingSocket implements SessionWebSocket {
  readonly sent: Json[] = [];
  send(data: string): void {
    this.sent.push(JSON.parse(data) as Json);
  }
  close(): void {}
  get actions(): Json[] {
    return this.sent.filter((m) => m.type === "turn_action");
  }
  get hello(): Json | undefined {
    return this.sent.find((m) => m.type === "hello");
  }
}

const CTX = { matchId: "m1", token: "tok", ticket: null, clientName: "t", clientVersion: "0" };

async function play(
  bot: Bot,
  frames: Json[],
  opts: { safeMode?: boolean; supportedGames?: string[] | null; arrivedAt?: () => number } = {},
): Promise<{ ws: CapturingSocket; end: Json | null }> {
  const ws = new CapturingSocket();
  const end = await _runSession(
    ws,
    bot,
    { ...CTX, safeMode: opts.safeMode ?? true, supportedGames: opts.supportedGames ?? null },
    new ScriptedReader(frames, opts.arrivedAt),
  );
  return { ws, end };
}

/** Records every call it gets and answers with a fixed fight move. */
class RecordingFighter extends BigBadBotsBot {
  calls: string[] = [];
  states: BigBadBotsTurnState[] = [];
  latencies: number[] = [];
  constructor(private readonly move: Parameters<typeof Action.act>[0] = "kick_low") {
    super();
  }
  override chooseBody(state: BodyShopState): Action {
    this.calls.push("choose_body");
    this.states.push(state);
    return Action.chooseBody("golem");
  }
  override bid(state: AuctionState): Action {
    this.calls.push("bid");
    this.states.push(state);
    return Action.bid(0);
  }
  override fit(state: FittingState): Action {
    this.calls.push("fit");
    this.states.push(state);
    return Action.fit({ 0: "capacitor" });
  }
  act(state: FightState): Action {
    this.calls.push("act");
    this.states.push(state);
    return Action.act(this.move);
  }
  override onDecisionLatency(latencyMs: number): void {
    this.latencies.push(latencyMs);
  }
}

class Thrower extends BigBadBotsBot {
  act(_state: FightState): Action {
    throw new Error("boom");
  }
}

class MinimalFighter extends BigBadBotsBot {
  act(_state: FightState): Action {
    return Action.act("block");
  }
}

let warn: ReturnType<typeof vi.spyOn>;
let error: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  warn = vi.spyOn(console, "warn").mockImplementation(() => {});
  error = vi.spyOn(console, "error").mockImplementation(() => {});
});
afterEach(() => {
  vi.restoreAllMocks();
});

const warnings = (): string => warn.mock.calls.map((c) => c.join(" ")).join("\n");
const errors = (): string => error.mock.calls.map((c) => c.join(" ")).join("\n");

// ---------------------------------------------------------------------------
// The base class
// ---------------------------------------------------------------------------

describe("BigBadBotsBot", () => {
  it.each([
    ["choose_body", "body_shop"],
    ["bid", "auction"],
    ["fit", "fitting"],
    ["act", "fight"],
  ])("decide routes %s to its method", (call, phase) => {
    const bot = new RecordingFighter();
    bot.decide(parseBigBadBotsTurnRequest(request(call)));
    expect(bot.calls).toEqual([call]);
    expect(bot.states[0]!.phase).toBe(phase);
  });

  it("decide fills in state.rules from match_start", () => {
    const bot = new RecordingFighter();
    bot.onMatchStart(matchStart());
    expect(bot.gameConfig?.ruleSetId).toBe("bigbadbots-1.0");
    bot.decide(parseBigBadBotsTurnRequest(request("act")));
    expect(bot.states[0]!.rules).toBe(bot.rules);
    expect(bot.states[0]!.rules?.deadline.deadlineMs).toBe(100);
  });

  it("a rule set the typed view cannot read leaves rules null and warns", () => {
    const bot = new MinimalFighter();
    const start = matchStart();
    delete ((start.game_config as Json).rule_set as Json).arena;
    bot.onMatchStart(start);
    expect(bot.gameConfig).not.toBeNull();
    expect(bot.rules).toBeNull();
    expect(warnings()).toMatch(/could not be read into state\.rules/);
  });

  it("onReconnected re-learns the game config", () => {
    const bot = new MinimalFighter();
    bot.onReconnected({ type: "reconnected", game_config: gameConfig() });
    expect(bot.rules?.id).toBe("bigbadbots-1.0");
  });

  it("the default gear-up methods are legal", () => {
    const bot = new MinimalFighter();
    const shop = parseBigBadBotsTurnRequest(request("choose_body"));
    expect(bot.decide(shop).toWire()).toEqual({ action: "choose_body", params: { body: "warden" } });
    const auction = parseBigBadBotsTurnRequest(request("bid"));
    expect(bot.decide(auction).toWire()).toEqual({ action: "bid", params: { amount: 0 } });
    const fitting = parseBigBadBotsTurnRequest(request("fit"));
    expect(bot.decide(fitting).toWire()).toEqual({ action: "fit", params: { fit: { "0": "capacitor" } } });
  });

  it("the default fit uses each part once and matches slot types", () => {
    const msg = request("fit");
    const state = msg.state as Json;
    state.slots = ["arms", "core", "arms", "legs"];
    const part = (id: string, slot: string): Json => ({ id, slot, bonus: {}, effects: {} });
    state.owned = [part("a1", "arms"), part("c1", "core"), part("h1", "head"), part("a2", "arms")];
    const action = new MinimalFighter().decide(parseBigBadBotsTurnRequest(msg));
    expect(action.toWire().params).toEqual({ fit: { "0": "a1", "1": "c1", "2": "a2" } });
  });

  it("decide refuses a poker state", () => {
    const poker = { handNumber: 1, phase: "preflop", holeCards: [], validActions: ["fold"] } as unknown as GameState;
    expect(() => new MinimalFighter().decide(poker)).toThrow(TypeError);
  });

  it("is a Bot, so run-external discovers subclasses but not the base", () => {
    expect(new MinimalFighter()).toBeInstanceOf(Bot);
    const found = findBotSubclasses({ MinimalFighter, BigBadBotsBot, Bot, other: 3 });
    expect(found).toEqual([MinimalFighter]);
  });
});

// ---------------------------------------------------------------------------
// The fallback
// ---------------------------------------------------------------------------

describe("fallbackAction", () => {
  it.each([
    ["choose_body", { action: "choose_body", params: { body: "warden" } }],
    ["bid", { action: "bid", params: { amount: 0 } }],
    ["fit", { action: "fit", params: { fit: {} } }],
    ["act", { action: "idle", params: {} }],
  ])("is the server's default for %s", (call, expected) => {
    expect(fallbackAction(request(call), null)?.toWire()).toEqual(expected);
  });

  it.each([
    ["block", "block"],
    ["move_in", "move_in"],
    ["move_out", "move_out"],
    ["kick_low", "idle"],
    ["special", "idle"],
    [null, "idle"],
  ])("in the fight repeats only block and moves (%s -> %s)", (previous, expected) => {
    expect(fallbackAction(request("act"), previous)?.action).toBe(expected);
  });

  it("has no answer for an unknown phase or a missing offer", () => {
    expect(fallbackAction({ state: { phase: "preflop" } }, null)).toBeNull();
    expect(fallbackAction({ state: { phase: "body_shop", offer: [] } }, null)).toBeNull();
    expect(fallbackAction({ state: { phase: "body_shop", offer: [{ cost: 1 }] } }, null)).toBeNull();
    expect(fallbackAction({}, null)).toBeNull();
  });

  it("isAnswerShaped checks the class and valid_actions", () => {
    expect(isAnswerShaped(Action.act("idle"), ["idle"])).toBe(true);
    expect(isAnswerShaped(Action.fold(), ["idle"])).toBe(false);
    expect(isAnswerShaped({ action: "idle" }, ["idle"])).toBe(false);
    expect(isAnswerShaped(undefined, ["idle"])).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// The session loop
// ---------------------------------------------------------------------------

describe("Big Bad Bots session", () => {
  it("plays the published frames end to end", async () => {
    const bot = new RecordingFighter();
    const { ws, end } = await play(bot, [
      b3Hello(),
      matchStart(),
      request("choose_body"),
      { type: "phase_change", state: { phase: "auction", board: [] } },
      request("bid"),
      request("fit"),
      { type: "round_start", state: { hand_number: 1, round: 1, phase: "fight", rounds_won: [0, 0] } },
      request("act"),
      fightResult("kick_low"),
      matchEnd(),
    ]);
    expect(end?.type).toBe("match_end");
    expect(bot.calls).toEqual(["choose_body", "bid", "fit", "act"]);
    expect(bot.states.every((s) => s.rules?.id === "bigbadbots-1.0")).toBe(true);
    // The published answers, field for field.
    const published = blocks().filter((b) => b.type === "turn_action");
    expect(ws.actions).toEqual(
      published.map((a) => ({ ...a, match_id: "m1", params: a.params ?? {} })),
    );
    expect(ws.hello?.supported_games).toEqual(["bigbadbots"]);
    expect(bot.latencies).toHaveLength(4);
  });

  it("an explicit supportedGames wins over the default", async () => {
    const { ws } = await play(new MinimalFighter(), [b3Hello(), matchEnd()], {
      supportedGames: ["bigbadbots", "nlhe"],
    });
    expect(ws.hello?.supported_games).toEqual(["bigbadbots", "nlhe"]);
  });

  it("a plain Bot declaring bigbadbots gets typed states", async () => {
    const seen: unknown[] = [];
    class Plain extends Bot {
      decide(state: GameState): Action {
        seen.push(state);
        return Action.act("block");
      }
    }
    const { ws } = await play(new Plain(), [b3Hello(), request("act"), matchEnd()], {
      supportedGames: ["bigbadbots"],
    });
    expect((seen[0] as FightState).me.body).toBe("golem");
    expect(ws.actions[0]!.action).toBe("block");
  });

  it("a pending request on reconnect takes the Big Bad Bots path", async () => {
    const bot = new RecordingFighter();
    const { ws } = await play(bot, [
      b3Hello(),
      { type: "reconnected", round_number: 1, game_config: gameConfig(), pending_request: request("act") },
      matchEnd(),
    ]);
    expect(bot.calls).toEqual(["act"]);
    expect(bot.rules?.id).toBe("bigbadbots-1.0");
    expect(ws.actions[0]).toMatchObject({ request_id: "req_00029", action: "kick_low" });
  });

  it("a throw sends the protocol default, not a fold", async () => {
    const { ws } = await play(new Thrower(), [b3Hello(), request("act"), matchEnd()]);
    expect(ws.actions).toEqual([
      { type: "turn_action", match_id: "m1", request_id: "req_00029", action: "idle", params: {} },
    ]);
    expect(warnings()).toMatch(/decide\(\) threw; sending the protocol default/);
  });

  it("a throw repeats the applied block from turn_result within the round", async () => {
    const { ws } = await play(new Thrower(), [
      b3Hello(),
      fightResult("block", 1),
      request("act", { requestId: "r2" }),
      matchEnd(),
    ]);
    expect(ws.actions[0]!.action).toBe("block");
  });

  it("the repeated action resets every round", async () => {
    const { ws } = await play(new Thrower(), [
      b3Hello(),
      fightResult("block", 1),
      request("act", { requestId: "r2", round: 2 }),
      matchEnd(),
    ]);
    expect(ws.actions[0]!.action).toBe("idle");
  });

  it("a throw in the same round repeats what was sent", async () => {
    let n = 0;
    class Flaky extends BigBadBotsBot {
      act(_state: FightState): Action {
        n += 1;
        if (n === 1) return Action.act("move_in");
        throw new Error("boom");
      }
    }
    const { ws } = await play(new Flaky(), [
      b3Hello(),
      request("act", { requestId: "r1" }),
      request("act", { requestId: "r2" }),
      matchEnd(),
    ]);
    expect(ws.actions.map((a) => a.action)).toEqual(["move_in", "move_in"]);
  });

  it("a throw is terminal without safeMode", async () => {
    await expect(play(new Thrower(), [b3Hello(), request("act")], { safeMode: false })).rejects.toThrow(
      BotDecisionError,
    );
  });

  it.each([
    ["a poker action", (): unknown => Action.fold()],
    ["a gear-up action in the fight", (): unknown => Action.bid(3)],
    ["undefined", (): unknown => undefined],
    ["a Promise", (): unknown => Promise.resolve(Action.act("idle"))],
  ])("%s is replaced by the default", async (_what, answer) => {
    class Bad extends BigBadBotsBot {
      act(_state: FightState): Action {
        return answer() as Action;
      }
    }
    const { ws } = await play(new Bad(), [b3Hello(), request("act"), matchEnd()]);
    expect(ws.actions[0]!.action).toBe("idle");
    expect(errors()).toMatch(/not one of/);
  });

  it("an invalid answer is terminal without safeMode", async () => {
    class Bad extends BigBadBotsBot {
      act(_state: FightState): Action {
        return Action.fold();
      }
    }
    await expect(play(new Bad(), [b3Hello(), request("act")], { safeMode: false })).rejects.toThrow(
      /not one of/,
    );
  });

  it("a request that already waited out its budget gets the default and the bot is not called", async () => {
    const bot = new RecordingFighter();
    const timeout = (request("act").timeout_ms as number) - BIGBADBOTS_SEND_MARGIN_MS;
    const { ws } = await play(bot, [b3Hello(), request("act"), matchEnd()], {
      arrivedAt: () => Date.now() - timeout - 5,
    });
    expect(bot.calls).toEqual([]);
    expect(ws.actions[0]!.action).toBe("idle");
    expect(warnings()).toMatch(/waited \d+ms behind earlier work/);
  });

  it("a slow answer is still sent, with a warning", async () => {
    const msg = request("act");
    msg.timeout_ms = BIGBADBOTS_SEND_MARGIN_MS + 5;
    class Slow extends BigBadBotsBot {
      act(_state: FightState): Action {
        const until = Date.now() + 15;
        while (Date.now() < until) {
          /* spin */
        }
        return Action.act("uppercut");
      }
    }
    const { ws } = await play(new Slow(), [b3Hello(), msg, matchEnd()]);
    expect(ws.actions[0]!.action).toBe("uppercut");
    expect(warnings()).toMatch(/past its 5ms budget; sending it anyway/);
  });

  it("an answer within budget is sent without a warning", async () => {
    const { ws } = await play(new RecordingFighter("throw"), [b3Hello(), request("act"), matchEnd()]);
    expect(ws.actions[0]!.action).toBe("throw");
    expect(warn).not.toHaveBeenCalled();
  });

  it("an unparseable payload sends the default and never calls the bot", async () => {
    const bot = new RecordingFighter();
    const msg = request("act");
    delete (msg.state as Json).distance;
    const { ws } = await play(bot, [b3Hello(), msg, matchEnd()]);
    expect(bot.calls).toEqual([]);
    expect(ws.actions[0]!.action).toBe("idle");
    expect(errors()).toMatch(/failed to parse: .*"distance"/);
  });

  it("an unparseable payload is terminal without safeMode", async () => {
    const msg = request("act");
    delete (msg.state as Json).distance;
    await expect(play(new RecordingFighter(), [b3Hello(), msg], { safeMode: false })).rejects.toThrow(
      BotDecisionError,
    );
  });

  it("a rejected answer is retried once with the default", async () => {
    const rejected = (id: string): Json => ({
      type: "action_rejected",
      request_id: id,
      reason: "invalid_params",
      remaining_ms: 50,
      valid_actions: [],
    });
    const { ws } = await play(new RecordingFighter("kick_low"), [
      b3Hello(),
      request("act"),
      rejected("req_00029"),
      rejected("req_00029"),
      rejected("unknown"),
      matchEnd(),
    ]);
    expect(ws.actions.map((a) => [a.request_id, a.action])).toEqual([
      ["req_00029", "kick_low"],
      ["req_00029", "idle"],
    ]);
  });

  it("a rejected body choice retries the first body on the offer", async () => {
    const { ws } = await play(new RecordingFighter(), [
      b3Hello(),
      request("choose_body"),
      { type: "action_rejected", request_id: "req_00000", reason: "invalid_params", remaining_ms: 900 },
      matchEnd(),
    ]);
    expect(ws.actions.map((a) => a.params)).toEqual([{ body: "golem" }, { body: "warden" }]);
  });

  it("an unknown state_shape ends the session before the client hello", async () => {
    const { ws, end } = await play(new RecordingFighter(), [
      { type: "hello", game: { ...descriptor(), state_shape: "chess" } },
      request("act"),
    ]);
    expect(end).toBeNull();
    expect(ws.hello).toBeUndefined();
    expect(errors()).toMatch(/cannot parse state_shape "chess"/);
  });

  it("a malformed game descriptor ends the session", async () => {
    const { ws, end } = await play(new RecordingFighter(), [{ type: "hello", game: { game_type: "x" } }]);
    expect(end).toBeNull();
    expect(ws.hello).toBeUndefined();
    expect(errors()).toMatch(/malformed game descriptor/);
  });

  it("a BigBadBotsBot refuses a poker table", async () => {
    const { ws, end } = await play(new RecordingFighter(), [pokerHello(), matchEnd()]);
    expect(end).toBeNull();
    expect(ws.hello).toBeUndefined();
    expect(errors()).toMatch(/is a BigBadBotsBot but the table's state_shape is null/);
  });

  it("a poker table keeps the poker path and the exact poker hello", async () => {
    class Poker extends Bot {
      decide(state: GameState): Action {
        return state.validActions.includes("check") ? Action.check() : Action.fold();
      }
    }
    const { ws, end } = await play(new Poker(), [
      pokerHello(),
      {
        type: "turn_request",
        request_id: "p1",
        valid_actions: ["fold", "check"],
        state: { phase: "flop", board: ["Ah", "Kd", "2c"], your_hole_cards: ["Qs", "Qh"] },
      },
      { type: "action_rejected", request_id: "p1", valid_actions: ["fold"] },
      matchEnd(),
    ]);
    expect(end?.type).toBe("match_end");
    expect(ws.hello).toEqual({ type: "hello", match_id: "m1", supported_versions: ["1.0"] });
    expect(ws.actions.map((a) => a.action)).toEqual(["check", "fold"]);
  });
});

// ---------------------------------------------------------------------------
// The external runner: the same base URL mechanism as the poker SDK
// ---------------------------------------------------------------------------

describe("runExternalBot with a BigBadBotsBot", () => {
  beforeEach(() => _setLobbyRecvTimeoutMs(5));
  afterEach(() => {
    _setLobbyRecvTimeoutMs(2000);
    _resetSleep();
  });

  function transport(gatewayFrames: Json[]): { transport: Transport; urls: string[]; sent: Json[] } {
    const urls: string[] = [];
    const sent: Json[] = [];
    const reader = (frames: Json[], block: boolean): AsyncMessageReader => {
      let i = 0;
      return {
        next: async () => {
          if (i < frames.length) return JSON.stringify(frames[i++]);
          return block ? new Promise<string | null>(() => {}) : null;
        },
      };
    };
    const t: Transport = async (url) => {
      urls.push(url);
      const gateway = url.includes("/ws/external/match/");
      const conn: ExternalConnection = {
        send: (data: string) => {
          if (gateway) sent.push(JSON.parse(data) as Json);
        },
        reader: gateway
          ? reader(gatewayFrames, false)
          : reader(
              [
                { type: "hello", endpoint: "lobby" },
                { type: "matched", match_id: "m1", participant_id: "p1", gateway_ws_url: "/ws/external/match/m1/p1" },
              ],
              true,
            ),
        close: () => {},
      };
      return conn;
    };
    return { transport: t, urls, sent };
  }

  it.each([
    ["staging", "wss://staging.chipzen.ai/ws/external/bot/b3-bot"],
    ["prod", "wss://chipzen.ai/ws/external/bot/b3-bot"],
  ] as const)("--env %s picks the lobby and the match plays", async (env, lobby) => {
    const { transport: t, urls, sent } = transport([
      b3Hello(),
      matchStart(),
      request("choose_body"),
      request("act"),
      matchEnd(),
    ]);
    const results = await runExternalBot(new RecordingFighter(), {
      botId: "b3-bot",
      env,
      token: "cz_extbot_x",
      config: null,
      maxMatches: 1,
      transport: t,
    });
    expect(urls[0]).toBe(lobby);
    expect(urls[1]).toBe(`${new URL(lobby).origin}/ws/external/match/m1/p1`);
    expect(results).toHaveLength(1);
    expect(sent.find((m) => m.type === "hello")?.supported_games).toEqual(["bigbadbots"]);
    expect(sent.filter((m) => m.type === "turn_action").map((m) => m.action)).toEqual([
      "choose_body",
      "kick_low",
    ]);
  });

  it("CHIPZEN_ENV picks the lobby when no env is passed, and prod is the default", async () => {
    const saved = process.env.CHIPZEN_ENV;
    try {
      process.env.CHIPZEN_ENV = "staging";
      const a = transport([b3Hello(), matchEnd()]);
      await runExternalBot(new MinimalFighter(), {
        botId: "b3-bot",
        token: "cz_extbot_x",
        config: null,
        maxMatches: 1,
        transport: a.transport,
      });
      expect(a.urls[0]).toBe("wss://staging.chipzen.ai/ws/external/bot/b3-bot");

      delete process.env.CHIPZEN_ENV;
      const b = transport([b3Hello(), matchEnd()]);
      await runExternalBot(new MinimalFighter(), {
        botId: "b3-bot",
        token: "cz_extbot_x",
        config: null,
        maxMatches: 1,
        transport: b.transport,
      });
      expect(b.urls[0]).toBe("wss://chipzen.ai/ws/external/bot/b3-bot");
    } finally {
      if (saved === undefined) delete process.env.CHIPZEN_ENV;
      else process.env.CHIPZEN_ENV = saved;
    }
  });
});
