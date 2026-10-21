/**
 * 2-7 Triple Draw in the JavaScript SDK (chipzen-sdk#173).
 *
 * Ports the 27TD half of the Python SDK's `tests/test_variant_backcompat.py`
 * against the same frames: the `_draw27*` fixtures in `src/conformance.ts`
 * are the Python `_draw27_*` fixtures value for value. What is pinned:
 *
 * - `parseGameState` reads a 27TD `turn_request` (draw turn and betting
 *   turn), and the new fields default at a hold'em table.
 * - No hold'em-era field is displaced or retyped, and the 27TD state is a
 *   superset of the hold'em one.
 * - A bad card in `board` / `your_hole_cards` still throws inside the parser,
 *   before `decide()` runs (LAYER2-COMMON section 3, Rule 1).
 * - `Action.discard()` / `Action.standPat()` put the discard under `params`.
 * - `decideDraw` is defaulted (stand pat) and never called by the SDK.
 * - The fixtures stay out of the conformance grader.
 * - End to end: a bot that declares `draw27` plays a 27TD match through the
 *   real session loop, with the `game` descriptor a live table sends.
 *
 * Authority: docs/protocol/DRAW27-GAME-STATE-PROTOCOL.md.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Bot } from "../src/bot.js";
import { _runSession, _safeFallbackAction, type AsyncMessageReader, type SessionWebSocket } from "../src/client.js";
import {
  _classifyTurnAction,
  _draw27GameDescriptor,
  _draw27MatchStart,
  _draw27RoundResult,
  _draw27Script,
  _draw27ServerHello,
  _draw27TurnRequest,
  _draw27TurnResult,
  _driveSession,
  _extractTurnActions,
  _turnRequest,
  DRAW27_ACTIONS,
  runConformanceChecks,
  SCENARIOS,
} from "../src/conformance.js";
import * as sdk from "../src/index.js";
import { Action, parseGameState, parseTurnRequest, POKER_STATE_SHAPES, type GameState } from "../src/models.js";

type Json = Record<string, unknown>;

/** Every `GameState` field that existed before 27TD, written out longhand. */
const NLHE_ERA_FIELDS = [
  "handNumber",
  "phase",
  "holeCards",
  "board",
  "pot",
  "yourStack",
  "opponentStacks",
  "yourSeat",
  "dealerSeat",
  "toCall",
  "minRaise",
  "maxRaise",
  "validActions",
  "actionHistory",
  "roundId",
  "requestId",
] as const;

const DRAW27_FIELDS = [
  "isDrawPhase",
  "drawNumber",
  "drawsRemaining",
  "maxDiscard",
  "yourDrawCounts",
  "opponentDrawCounts",
] as const;

/** A bot written against hold'em and nothing else; records what it was handed. */
class NlheOnlyBot extends Bot {
  readonly seen: GameState[] = [];
  decide(state: GameState): Action {
    this.seen.push(state);
    if (state.validActions.includes("check")) return Action.check();
    if (state.toCall && state.toCall * 3 < state.pot) return Action.call();
    return Action.fold();
  }
}

/** A 27TD bot: draws two on a draw turn, raises when it can on a betting turn. */
class Draw27Bot extends Bot {
  readonly seen: GameState[] = [];
  decide(state: GameState): Action {
    this.seen.push(state);
    if (state.isDrawPhase) return this.decideDraw(state);
    if (state.validActions.includes("raise")) return Action.raiseTo(state.minRaise);
    return state.validActions.includes("check") ? Action.check() : Action.call();
  }
  override decideDraw(state: GameState): Action {
    // Throw the two highest cards in the holding (lowball keeps low cards).
    return Action.discard(["Ah", "Kd"].slice(0, state.maxDiscard));
  }
}

const VARIANT_TURN_REQUESTS: Record<string, () => Json> = {
  draw27_draw_phase: () => _draw27TurnRequest(4, "req_1", { drawPhase: true }),
  draw27_betting_phase: () => _draw27TurnRequest(4, "req_1", { drawPhase: false }),
};
const NAMES = Object.keys(VARIANT_TURN_REQUESTS).sort();

function parsed(name: string): GameState {
  return parseGameState(VARIANT_TURN_REQUESTS[name]!());
}

function poison(message: Json, key: string, value: unknown): Json {
  const copy = JSON.parse(JSON.stringify(message)) as Json;
  (copy.state as Json)[key] = value;
  return copy;
}

const typeOf = (v: unknown): string => (Array.isArray(v) ? "array" : v === null ? "null" : typeof v);

// ---------------------------------------------------------------------------
// 1. A 27TD turn_request parses, and its new keys are readable
// ---------------------------------------------------------------------------

describe("a 27TD turn_request parses", () => {
  it.each(NAMES)("%s parses without throwing, with board carried as []", (name) => {
    expect(parsed(name).board).toEqual([]);
  });

  it.each(NAMES)("a hold'em-only bot decides on %s without throwing", (name) => {
    // Wrong for the table, but WRONG is survivable and CRASHING is not.
    const bot = new NlheOnlyBot();
    const action = bot.decide(parsed(name));
    expect(["fold", "check", "call", "raise", "all_in"]).toContain(action.action);
    expect(bot.seen).toHaveLength(1);
  });

  it("the draw-turn keys are readable", () => {
    const state = parsed("draw27_draw_phase");
    expect(state.isDrawPhase).toBe(true);
    expect(state.drawNumber).toBe(1);
    expect(state.drawsRemaining).toBe(3);
    // The BOUND on the discard lives in state, not in validActions.
    expect(state.maxDiscard).toBe(5);
    expect(state.validActions).toEqual(["draw"]);
    expect(state.yourDrawCounts).toEqual([]);
    // Seat index is a DECIMAL STRING key, because JSON object keys are.
    expect(state.opponentDrawCounts).toEqual({ "1": [] });
    expect(state.phase).toBe("draw1");
    expect(state.holeCards).toHaveLength(5);
  });

  it("a betting turn is fixed limit: minRaise == maxRaise", () => {
    const state = parsed("draw27_betting_phase");
    expect(state.isDrawPhase).toBe(false);
    expect(state.minRaise).toBe(200);
    expect(state.maxRaise).toBe(200);
    expect(state.maxDiscard).toBe(0);
    expect(state.validActions).toContain("raise");
    expect(state.phase).toBe("bet1");
  });

  it("draw counts are parsed per seat", () => {
    const msg = _draw27TurnRequest();
    const s = msg.state as Json;
    s.your_draw_counts = [2, 0];
    s.opponent_draw_counts = { "1": [3, 1], "2": [0] };
    const state = parseGameState(msg);
    expect(state.yourDrawCounts).toEqual([2, 0]);
    expect(state.opponentDrawCounts).toEqual({ "1": [3, 1], "2": [0] });
  });

  it("27TD state carries no your_seat or dealer_seat (the button comes from round_start)", () => {
    const state = _draw27TurnRequest().state as Json;
    expect("your_seat" in state).toBe(false);
    expect("dealer_seat" in state).toBe(false);
  });

  it("a hold'em payload leaves every 27TD field at its default", () => {
    const state = parseGameState(_turnRequest());
    expect(state.isDrawPhase).toBe(false);
    expect(state.drawNumber).toBe(0);
    expect(state.drawsRemaining).toBe(0);
    expect(state.maxDiscard).toBe(0);
    expect(state.yourDrawCounts).toEqual([]);
    expect(state.opponentDrawCounts).toEqual({});
  });

  it.each([
    ["is_draw_phase", "yes", "isDrawPhase", false],
    ["draw_number", "1", "drawNumber", 0],
    ["max_discard", null, "maxDiscard", 0],
    ["max_discard", 4.7, "maxDiscard", 4],
    ["your_draw_counts", "2", "yourDrawCounts", []],
    ["your_draw_counts", [1, "x", true], "yourDrawCounts", [1, 0, 0]],
    ["opponent_draw_counts", [1], "opponentDrawCounts", {}],
    ["opponent_draw_counts", { "1": "x" }, "opponentDrawCounts", { "1": [] }],
  ])("a mistyped optional key %s=%j degrades to its default", (key, value, field, expected) => {
    // The variant keys are lenient on purpose (as in Python): a mistyped
    // optional key must not become a second session-killing parse failure.
    const state = parseGameState(poison(_draw27TurnRequest(), key, value)) as unknown as Json;
    expect(state[field]).toEqual(expected);
  });

  it("the 27TD shape is a poker shape", () => {
    expect(POKER_STATE_SHAPES.has("27tripledraw")).toBe(true);
    const state = parseTurnRequest(_draw27TurnRequest(), "27tripledraw") as GameState;
    expect(state.maxDiscard).toBe(5);
  });
});

// ---------------------------------------------------------------------------
// 2. No existing field is displaced or retyped
// ---------------------------------------------------------------------------

describe("hold'em-era fields survive a 27TD payload", () => {
  it.each(NAMES)("every hold'em-era field is present with its hold'em type (%s)", (name) => {
    const nlhe = parseGameState(_turnRequest()) as unknown as Json;
    const state = parsed(name) as unknown as Json;
    for (const field of NLHE_ERA_FIELDS) {
      expect(field in state, field).toBe(true);
      expect(typeOf(state[field]), field).toBe(typeOf(nlhe[field]));
    }
  });

  it("a draw phase zeroes the raise bounds rather than dropping them (Rule 2)", () => {
    const raw = _draw27TurnRequest(4, "req_1", { drawPhase: true }).state as Json;
    for (const key of ["pot", "to_call", "min_raise", "max_raise", "your_stack"]) {
      expect(typeof raw[key], key).toBe("number");
    }
    const state = parsed("draw27_draw_phase");
    expect(state.minRaise).toBe(0);
    expect(state.maxRaise).toBe(0);
  });

  it("the 27TD fields are optional in the type: a hold'em-era literal still compiles", () => {
    // If any 27TD field were required, this literal would not typecheck.
    const literal: GameState = {
      handNumber: 1,
      phase: "preflop",
      holeCards: [],
      board: [],
      pot: 0,
      yourStack: 0,
      opponentStacks: [],
      yourSeat: 0,
      dealerSeat: 0,
      toCall: 0,
      minRaise: 0,
      maxRaise: 0,
      validActions: ["fold"],
      actionHistory: [],
      roundId: "",
      requestId: "",
    };
    expect(new NlheOnlyBot().decide(literal).action).toBe("fold");
  });

  it("the parser always fills the 27TD fields", () => {
    const state = parseGameState({ state: {} }) as unknown as Json;
    for (const field of DRAW27_FIELDS) expect(state[field], field).toBeDefined();
  });
});

// ---------------------------------------------------------------------------
// 3. The 27TD envelope is an additive superset of the hold'em one
// ---------------------------------------------------------------------------

describe("the 27TD envelope is an additive superset", () => {
  it.each(NAMES)("%s state carries every hold'em key with the same type", (name) => {
    const nlhe = _turnRequest().state as Json;
    const variant = VARIANT_TURN_REQUESTS[name]!().state as Json;
    for (const key of Object.keys(nlhe)) {
      expect(key in variant, key).toBe(true);
      expect(typeOf(variant[key]), key).toBe(typeOf(nlhe[key]));
    }
  });

  it.each(NAMES)("%s envelope fields are unchanged", (name) => {
    const nlhe = _turnRequest();
    const variant = VARIANT_TURN_REQUESTS[name]!();
    for (const key of ["type", "match_id", "seq", "seat", "request_id", "timeout_ms"]) {
      expect(typeOf(variant[key]), key).toBe(typeOf(nlhe[key]));
    }
    // valid_actions stays a flat array of strings; the bounds live in state.
    const valid = variant.valid_actions as unknown[];
    expect(valid.every((a) => typeof a === "string")).toBe(true);
  });

  it("the variant is announced by the game_config key that already existed", () => {
    expect((_draw27MatchStart().game_config as Json).variant).toBe("27tripledraw");
  });

  it("the config is fixed limit with four independent sizes", () => {
    const cfg = _draw27MatchStart().game_config as Json;
    expect(cfg.betting_structure).toBe("fixed_limit");
    for (const key of ["small_blind", "big_blind", "small_bet", "big_bet"]) {
      expect(Number.isInteger(cfg[key]), key).toBe(true);
    }
    expect(cfg.bet_cap).toBe(5);
    expect(cfg.num_draws).toBe(3);
    expect(cfg.cards_per_player).toBe(5);
    expect("total_hands" in cfg).toBe(false);
  });

  it("the game descriptor matches DRAW27 section 1.1", () => {
    expect(_draw27GameDescriptor()).toEqual({
      game_type: "draw27",
      variant: "27tripledraw",
      actions: ["call", "check", "draw", "fold", "raise"],
      phases: ["predraw", "draw1", "bet1", "draw2", "bet2", "draw3", "bet3", "showdown", "complete"],
      state_shape: "27tripledraw",
    });
  });
});

// ---------------------------------------------------------------------------
// 4. The hard constraint: a bad card throws BEFORE decide()
// ---------------------------------------------------------------------------

const BAD_CARD_ARRAYS = [["??"], ["XX"], ["Ah", "??"], ["10h"], [null]];

describe("an invalid card is a hard parse error", () => {
  it.each(BAD_CARD_ARRAYS)("bad hole cards %j throw in parseGameState", (...bad) => {
    expect(() => parseGameState(poison(_draw27TurnRequest(), "your_hole_cards", bad))).toThrow();
  });

  it.each(BAD_CARD_ARRAYS)("a bad board %j throws in parseGameState", (...bad) => {
    expect(() => parseGameState(poison(_draw27TurnRequest(), "board", bad))).toThrow();
  });

  it("the session dies before decide() on a poisoned board", async () => {
    const bot = new NlheOnlyBot();
    const script = _draw27Script();
    script[3] = poison(script[3]!, "board", ["??"]);
    const { sent, error } = await _driveSession(bot, script);
    expect(error).toBeInstanceOf(Error);
    expect(bot.seen).toEqual([]);
    expect(_extractTurnActions(sent)).toEqual([]);
  });

  it("control: the same script, unpoisoned, reaches decide() on both turns", async () => {
    const bot = new NlheOnlyBot();
    const { error } = await _driveSession(bot, _draw27Script());
    expect(error).toBeNull();
    expect(bot.seen).toHaveLength(2);
  });
});

// ---------------------------------------------------------------------------
// 5. Action factories put the discard under `params`
// ---------------------------------------------------------------------------

describe("Action.discard / Action.standPat", () => {
  it("emits the discard under params", () => {
    expect(Action.discard(["Ah", "Kd"]).toWire()).toEqual({
      action: "draw",
      params: { discard: ["Ah", "Kd"] },
    });
  });

  it("accepts 0-based hand positions", () => {
    expect(Action.discard([0, 3]).toWire()).toEqual({ action: "draw", params: { discard: [0, 3] } });
  });

  it.each([
    ["standPat()", () => Action.standPat()],
    ["discard([])", () => Action.discard([])],
    ["discard()", () => Action.discard()],
    ["discard(null)", () => Action.discard(null)],
  ])("%s is an empty discard", (_label, make) => {
    expect(make().toWire()).toEqual({ action: "draw", params: { discard: [] } });
  });

  it("a stand pat is a real action, not a no-op", () => {
    expect(Action.standPat().action).toBe("draw");
    expect(Action.standPat().toWire()).toEqual(Action.discard([]).toWire());
    expect(Action.standPat().toWire()).not.toEqual(Action.discard(["Ah"]).toWire());
  });

  it("copies the caller's list rather than aliasing it", () => {
    const cards = ["Ah"];
    const action = Action.discard(cards);
    cards.push("Kd");
    const wire = action.toWire();
    expect(wire.params.discard).toEqual(["Ah"]);
    (wire.params.discard as string[]).push("Qs");
    expect(action.toWire().params.discard).toEqual(["Ah"]);
  });

  it.each([
    [Action.fold(), {}],
    [Action.check(), {}],
    [Action.call(), {}],
    [Action.allIn(), {}],
    [Action.raiseTo(200), { amount: 200 }],
  ])("the hold'em factories are byte-unchanged (%#)", (action, params) => {
    expect(Object.keys(action.toWire())).toEqual(["action", "params"]);
    expect(action.toWire().params).toEqual(params);
  });

  it("is exported from the package", () => {
    expect(sdk.Action.standPat().toWire().action).toBe("draw");
  });
});

// ---------------------------------------------------------------------------
// 6. decide() stays the single required method; decideDraw is defaulted
// ---------------------------------------------------------------------------

class LegacyBot extends Bot {
  decide(): Action {
    return Action.fold();
  }
}

describe("the decideDraw hook", () => {
  it("defaults to a stand pat", () => {
    const state = parsed("draw27_draw_phase");
    expect(new LegacyBot().decideDraw(state).toWire()).toEqual({ action: "draw", params: { discard: [] } });
  });

  it("is not called by the session loop", async () => {
    class HookSpy extends NlheOnlyBot {
      hookCalls = 0;
      override decideDraw(): Action {
        this.hookCalls += 1;
        return Action.standPat();
      }
    }
    const bot = new HookSpy();
    const { error } = await _driveSession(bot, _draw27Script());
    expect(error).toBeNull();
    expect(bot.seen).toHaveLength(2);
    expect(bot.hookCalls).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// 7. The fixtures match the spec and stay out of the grader
// ---------------------------------------------------------------------------

describe("the 27TD conformance fixtures", () => {
  it("are not wired into the grader", async () => {
    expect(SCENARIOS.map((s) => s.name)).toEqual([
      "connectivity_full_match",
      "multi_turn_request_id_echo",
      "action_rejected_recovery",
      "retry_storm_bounded",
    ]);
    expect(await runConformanceChecks(new LegacyBot())).toHaveLength(4);
  });

  it("classify defaults to the hold'em vocabulary; draw is legal only under DRAW27_ACTIONS", () => {
    const payload = JSON.stringify({ type: "turn_action", request_id: "req_1", action: "draw" });
    expect(_classifyTurnAction(payload).ok).toBe(false);
    expect(_classifyTurnAction(payload, "req_1", DRAW27_ACTIONS).ok).toBe(true);
  });

  it("a draw's turn_result amount is the discard mask (DRAW27 section 3.6)", () => {
    const details = _draw27TurnResult().details as Json;
    expect(details.action).toBe("draw");
    // Bit i set = hand position i replaced: 2 is position 1 alone.
    const positions = [0, 1, 2, 3, 4].filter((i) => ((details.amount as number) >> i) & 1);
    expect(positions).toEqual([1]);
  });

  it("the showdown rank is a dash-joined string, and the low hand wins", () => {
    const result = _draw27RoundResult().result as Json;
    const showdown = result.showdown as Json[];
    expect(showdown[0]!.hand_rank).toBe("8-6-4-3-2");
    expect(showdown.every((e) => String(e.hand_rank).includes("-"))).toBe(true);
    expect(result.winner_seats).toEqual([0]);
  });
});

// ---------------------------------------------------------------------------
// 8. End to end: a bot that declares draw27 plays a 27TD match
// ---------------------------------------------------------------------------

class ScriptedReader implements AsyncMessageReader {
  private idx = 0;
  constructor(private readonly frames: Json[]) {}
  async next(): Promise<string | null> {
    if (this.idx >= this.frames.length) return null;
    return JSON.stringify(this.frames[this.idx++]);
  }
}

class CapturingSocket implements SessionWebSocket {
  readonly sent: string[] = [];
  send(data: string): void {
    this.sent.push(data);
  }
  close(): void {}
  get parsed(): Json[] {
    return this.sent.map((s) => JSON.parse(s) as Json);
  }
}

async function play(
  bot: Bot,
  frames: Json[],
  extra: { supportedGames?: readonly string[]; safeMode?: boolean } = {},
): Promise<{ ws: CapturingSocket; end: Json | null }> {
  const ws = new CapturingSocket();
  const end = await _runSession(
    ws,
    bot,
    { matchId: "m_conformance_test", token: "t", ticket: null, clientName: "test", clientVersion: "0.0.0", ...extra },
    new ScriptedReader(frames),
  );
  return { ws, end };
}

describe("a JavaScript bot that declares draw27", () => {
  let errorSpy: ReturnType<typeof vi.spyOn>;
  beforeEach(() => {
    errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
  });
  afterEach(() => errorSpy.mockRestore());

  it.each([
    ["with the live game descriptor", true],
    ["with the Python fixture's hello (no descriptor)", false],
  ])("plays a 27TD hand end to end %s", async (_label, withDescriptor) => {
    const bot = new Draw27Bot();
    const { ws, end } = await play(bot, _draw27Script({ withDescriptor }), {
      supportedGames: ["poker", "draw27"],
    });

    expect(end?.type).toBe("match_end");
    const hello = ws.parsed.find((m) => m.type === "hello")!;
    expect(hello.supported_games).toEqual(["poker", "draw27"]);

    const actions = _extractTurnActions(ws.sent);
    expect(actions.map((a) => [a.request_id, a.action, a.params])).toEqual([
      ["req_1", "draw", { discard: ["Ah", "Kd"] }],
      ["req_2", "raise", { amount: 200 }],
    ]);
    for (const [i, a] of actions.entries()) {
      expect(_classifyTurnAction(JSON.stringify(a), `req_${i + 1}`, DRAW27_ACTIONS).ok).toBe(true);
    }
    // The bot saw a draw turn and then a betting turn.
    expect(bot.seen.map((s) => s.isDrawPhase)).toEqual([true, false]);
    expect(errorSpy).not.toHaveBeenCalled();
  });

  it("a throw on a draw turn sends a stand pat, not a fold", async () => {
    class Thrower extends Bot {
      decide(): Action {
        throw new Error("boom");
      }
    }
    const { ws } = await play(new Thrower(), _draw27Script());
    const actions = _extractTurnActions(ws.sent);
    expect(actions[0]).toMatchObject({ request_id: "req_1", action: "draw", params: { discard: [] } });
    // The betting turn (fold, call, raise) still falls back to fold.
    expect(actions[1]).toMatchObject({ request_id: "req_2", action: "fold" });
  });

  it("an action_rejected on a draw turn retries with a stand pat", async () => {
    const { ws } = await play(new NlheOnlyBot(), [
      _draw27ServerHello(),
      _draw27MatchStart(),
      _draw27TurnRequest(3, "req_1", { drawPhase: true }),
      {
        type: "action_rejected",
        match_id: "m_conformance_test",
        seq: 4,
        request_id: "req_1",
        reason: "invalid_action",
        valid_actions: ["draw"],
      },
    ]);
    const actions = _extractTurnActions(ws.sent);
    // The hold'em bot's own answer was a fold; the retry is the stand pat.
    expect(actions.map((a) => a.action)).toEqual(["fold", "draw"]);
    expect(actions[1]).toMatchObject({ request_id: "req_1", params: { discard: [] } });
  });

  it("_safeFallbackAction keeps check/fold wherever they are offered", () => {
    expect(_safeFallbackAction(["draw"]).toWire()).toEqual({ action: "draw", params: { discard: [] } });
    expect(_safeFallbackAction(["fold", "call", "raise"]).action).toBe("fold");
    expect(_safeFallbackAction(["check", "raise"]).action).toBe("check");
  });
});
