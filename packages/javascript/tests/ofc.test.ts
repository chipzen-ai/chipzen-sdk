/**
 * Pineapple OFC in the JavaScript SDK (chipzen-sdk#175).
 *
 * Ports the OFC half of the Python SDK's `tests/test_variant_backcompat.py`
 * against the same frames: the `_ofc*` fixtures in `src/conformance.ts` are
 * the Python `_ofc_*` fixtures value for value. What is pinned:
 *
 * - `parseGameState` reads an OFC `turn_request`, the new fields default at
 *   a hold'em table, and a mistyped optional key degrades to its default.
 * - OFC carries `your_seat` / `dealer_seat` in the turn state, and the
 *   state's values win over the seat the SDK re-learns on a reconnect.
 * - No hold'em-era field is displaced or retyped, and the OFC state is a
 *   superset of the hold'em one.
 * - A bad card in `board` / `your_hole_cards` still throws inside the parser,
 *   before `decide()` runs (LAYER2-COMMON section 3, Rule 1).
 * - `Action.place()` puts the placements and the discard under `params`.
 * - `decidePlacement` is defaulted (legal, not good) and never called by
 *   the SDK.
 * - The safe fallback at an OFC turn (`["place"]`, no check, no fold) is the
 *   protocol's default placement (OFC section 5.9).
 * - The fixtures stay out of the conformance grader.
 * - End to end: a bot that declares `ofc` plays an OFC match through the
 *   real session loop, with the `game` descriptor a live table sends.
 *
 * Authority: docs/protocol/OFC-GAME-STATE-PROTOCOL.md.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Bot } from "../src/bot.js";
import {
  _ofcDefaultPlacement,
  _runSession,
  _safeFallbackAction,
  type AsyncMessageReader,
  type SessionWebSocket,
} from "../src/client.js";
import {
  _classifyTurnAction,
  _driveSession,
  _extractTurnActions,
  _ofcGameDescriptor,
  _ofcMatchStart,
  _ofcRoundResult,
  _ofcRoundStart,
  _ofcScript,
  _ofcServerHello,
  _ofcTurnRequest,
  _ofcTurnResult,
  _turnRequest,
  OFC_ACTIONS,
  runConformanceChecks,
  SCENARIOS,
} from "../src/conformance.js";
import * as sdk from "../src/index.js";
import { Action, parseGameState, parseTurnRequest, POKER_STATE_SHAPES, type GameState } from "../src/models.js";

type Json = Record<string, unknown>;
type Placement = { card: string; row: string };

/** Every `GameState` field that existed before the variants, written out longhand. */
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

const OFC_FIELDS = [
  "yourRows",
  "opponentRows",
  "cardsToPlace",
  "place",
  "mustDiscard",
  "rowCapacity",
  "royalties",
  "opponentRoyalties",
  "pointValue",
  "inFantasyLand",
  "phaseSequence",
] as const;

const PHASE_SEQUENCE = ["deal1", "street2", "street3", "street4", "street5", "complete"];

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

/** An OFC bot: dispatches placement turns to `decidePlacement` (the SDK default). */
class OfcBot extends Bot {
  readonly seen: GameState[] = [];
  decide(state: GameState): Action {
    this.seen.push(state);
    if (state.validActions.includes("place")) return this.decidePlacement(state);
    return state.validActions.includes("check") ? Action.check() : Action.fold();
  }
}

function parsed(): GameState {
  return parseGameState(_ofcTurnRequest());
}

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function poison(message: Json, key: string, value: unknown): Json {
  const copy = clone(message);
  (copy.state as Json)[key] = value;
  return copy;
}

/**
 * The opening-set `turn_request` for the same hand as `_ofcTurnRequest`
 * (OFC section 3.2 / 4.2): five cards dealt, place 5, discard 0, every row
 * empty.
 */
function deal1TurnRequest(seq: number, requestId: string): Json {
  const msg = clone(_ofcTurnRequest(seq, requestId));
  const state = msg.state as Json;
  const opening = (_ofcRoundStart().state as Json).cards_to_place as string[];
  Object.assign(state, {
    phase: "deal1",
    your_hole_cards: [...opening],
    cards_to_place: [...opening],
    your_rows: { top: [], middle: [], bottom: [] },
    opponent_rows: { "0": { top: [], middle: [], bottom: [] } },
    place: 5,
    must_discard: 0,
    row_capacity: { top: 3, middle: 5, bottom: 5 },
    action_history: [],
  });
  return msg;
}

/** A Fantasy Land turn (OFC section 5.6): 14 cards, place 13, discard 1. */
function fantasyLandTurnRequest(): Json {
  const cards = ["2c", "3d", "4h", "5s", "6s", "7c", "8d", "9h", "Ts", "Jc", "Qd", "Kh", "As", "Ac"];
  const msg = clone(deal1TurnRequest(4, "req_1"));
  Object.assign(msg.state as Json, {
    your_hole_cards: [...cards],
    cards_to_place: [...cards],
    place: 13,
    must_discard: 1,
    in_fantasy_land: true,
    phase_sequence: ["deal1", "complete"],
  });
  return msg;
}

function rowCounts(placements: readonly Placement[]): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const p of placements) counts[p.row] = (counts[p.row] ?? 0) + 1;
  return counts;
}

function discardList(discard: unknown): string[] {
  return typeof discard === "string" ? [discard] : (discard as string[]);
}

/** OFC section 3.5: exactly `place` placements and `must_discard` discards, each card once, within capacity. */
function expectLegalPlacement(params: Json, state: GameState): void {
  const placements = params.placements as Placement[];
  const discard = discardList(params.discard);
  expect(placements).toHaveLength(state.place!);
  expect(discard).toHaveLength(state.mustDiscard!);
  const used = [...placements.map((p) => p.card), ...discard];
  expect([...used].sort()).toEqual([...state.cardsToPlace!].sort());
  for (const [row, count] of Object.entries(rowCounts(placements))) {
    expect(row in state.rowCapacity!, row).toBe(true);
    expect(count, row).toBeLessThanOrEqual(state.rowCapacity![row]!);
  }
}

const typeOf = (v: unknown): string => (Array.isArray(v) ? "array" : v === null ? "null" : typeof v);

// ---------------------------------------------------------------------------
// 1. An OFC turn_request parses, and its new keys are readable
// ---------------------------------------------------------------------------

describe("an OFC turn_request parses", () => {
  it("parses without throwing, with board carried as []", () => {
    expect(parsed().board).toEqual([]);
  });

  it("a hold'em-only bot decides on it without throwing", () => {
    // Wrong for the table, but WRONG is survivable and CRASHING is not.
    const bot = new NlheOnlyBot();
    const action = bot.decide(parsed());
    expect(["fold", "check", "call", "raise", "all_in"]).toContain(action.action);
    expect(bot.seen).toHaveLength(1);
  });

  it("the OFC keys are readable", () => {
    const state = parsed();
    expect(state.validActions).toEqual(["place"]);
    expect(state.cardsToPlace).toEqual(["9s", "Js", "Ks"]);
    expect(state.place).toBe(2);
    expect(state.mustDiscard).toBe(1);
    // place + mustDiscard == cardsToPlace.length, always.
    expect(state.place! + state.mustDiscard!).toBe(state.cardsToPlace!.length);
    expect(state.rowCapacity).toEqual({ top: 2, middle: 3, bottom: 3 });
    expect(state.yourRows).toEqual({ top: ["2c"], middle: ["3d", "4h"], bottom: ["5s", "6s"] });
    // Seat index is a DECIMAL STRING key, because JSON object keys are.
    expect(state.opponentRows!["0"]!.middle).toEqual(["8d", "9h"]);
    expect(state.royalties).toEqual({ top: 0, middle: 0, bottom: 0 });
    expect(state.opponentRoyalties!["0"]!.top).toBe(0);
    expect(state.pointValue).toBe(50);
    expect(state.inFantasyLand).toBe(false);
    expect(state.phaseSequence).toEqual(PHASE_SEQUENCE);
    expect(state.phase).toBe("street2");
    // Your pending cards are ALSO the Card-parsed holeCards.
    expect(state.holeCards.map((c) => `${c.rank}${c.suit}`)).toEqual(state.cardsToPlace);
  });

  it("OFC carries its own seats in the turn state", () => {
    const state = parsed();
    expect(state.yourSeat).toBe(1);
    expect(state.dealerSeat).toBe(0);
  });

  it("a Fantasy Land turn reads as 14 cards, place 13, discard 1", () => {
    const state = parseGameState(fantasyLandTurnRequest());
    expect(state.cardsToPlace).toHaveLength(14);
    expect(state.place).toBe(13);
    expect(state.mustDiscard).toBe(1);
    expect(state.inFantasyLand).toBe(true);
    expect(state.phaseSequence).toEqual(["deal1", "complete"]);
  });

  it("a hidden Fantasy Land board reads as empty rows, never a placeholder", () => {
    const msg = _ofcTurnRequest();
    const s = msg.state as Json;
    s.opponent_rows = { "0": { top: [], middle: [], bottom: [] } };
    s.opponent_royalties = { "0": { top: 0, middle: 0, bottom: 0 } };
    const state = parseGameState(msg);
    expect(state.opponentRows).toEqual({ "0": { top: [], middle: [], bottom: [] } });
    expect(state.opponentRoyalties).toEqual({ "0": { top: 0, middle: 0, bottom: 0 } });
  });

  it("a hold'em payload leaves every OFC field at its default", () => {
    const state = parseGameState(_turnRequest());
    expect(state.yourRows).toEqual({});
    expect(state.opponentRows).toEqual({});
    expect(state.cardsToPlace).toEqual([]);
    expect(state.place).toBe(0);
    expect(state.mustDiscard).toBe(0);
    expect(state.rowCapacity).toEqual({});
    expect(state.royalties).toEqual({});
    expect(state.opponentRoyalties).toEqual({});
    expect(state.pointValue).toBe(0);
    expect(state.inFantasyLand).toBe(false);
    expect(state.phaseSequence).toEqual([]);
  });

  it.each([
    ["your_rows", ["2c"], "yourRows", {}],
    ["your_rows", { top: "2c" }, "yourRows", { top: [] }],
    ["opponent_rows", [1], "opponentRows", {}],
    ["opponent_rows", { "0": "x" }, "opponentRows", { "0": {} }],
    ["cards_to_place", "9s", "cardsToPlace", []],
    ["place", "2", "place", 0],
    ["place", true, "place", 0],
    ["must_discard", null, "mustDiscard", 0],
    ["must_discard", 1.9, "mustDiscard", 1],
    ["row_capacity", [2, 3, 3], "rowCapacity", {}],
    ["row_capacity", { top: "2", middle: 3 }, "rowCapacity", { top: 0, middle: 3 }],
    ["royalties", "none", "royalties", {}],
    ["opponent_royalties", { "0": [1] }, "opponentRoyalties", { "0": {} }],
    ["point_value", "50", "pointValue", 0],
    ["in_fantasy_land", "yes", "inFantasyLand", false],
    ["phase_sequence", "deal1", "phaseSequence", []],
  ])("a mistyped optional key %s=%j degrades to its default", (key, value, field, expected) => {
    // The variant keys are lenient on purpose (as in Python): a mistyped
    // optional key must not become a second session-killing parse failure.
    const state = parseGameState(poison(_ofcTurnRequest(), key, value)) as unknown as Json;
    expect(state[field]).toEqual(expected);
  });

  it("the OFC shape is a poker shape", () => {
    expect(POKER_STATE_SHAPES.has("pineapple")).toBe(true);
    const state = parseTurnRequest(_ofcTurnRequest(), "pineapple") as GameState;
    expect(state.cardsToPlace).toEqual(["9s", "Js", "Ks"]);
  });
});

// ---------------------------------------------------------------------------
// 2. No existing field is displaced or retyped
// ---------------------------------------------------------------------------

describe("hold'em-era fields survive an OFC payload", () => {
  it("every hold'em-era field is present with its hold'em type", () => {
    const nlhe = parseGameState(_turnRequest()) as unknown as Json;
    const state = parsed() as unknown as Json;
    for (const field of NLHE_ERA_FIELDS) {
      expect(field in state, field).toBe(true);
      expect(typeOf(state[field]), field).toBe(typeOf(nlhe[field]));
    }
  });

  it("OFC zeroes the betting fields rather than dropping them (Rule 2)", () => {
    // A bot that computes a bet size from these computes zero, which is
    // correct. A bot that hits a MISSING key crashes, which is not.
    const raw = _ofcTurnRequest().state as Json;
    for (const key of ["pot", "to_call", "min_raise", "max_raise", "your_stack"]) {
      expect(Number.isInteger(raw[key]), key).toBe(true);
    }
    expect(Array.isArray(raw.opponent_stacks)).toBe(true);
    const state = parsed();
    expect([state.toCall, state.minRaise, state.maxRaise, state.pot]).toEqual([0, 0, 0, 0]);
  });

  it("the OFC fields are optional in the type: a hold'em-era literal still compiles", () => {
    // If any OFC field were required, this literal would not typecheck.
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

  it("the parser always fills the OFC fields", () => {
    const state = parseGameState({ state: {} }) as unknown as Json;
    for (const field of OFC_FIELDS) expect(state[field], field).toBeDefined();
  });
});

// ---------------------------------------------------------------------------
// 3. The OFC envelope is an additive superset of the hold'em one
// ---------------------------------------------------------------------------

describe("the OFC envelope is an additive superset", () => {
  it("the state carries every hold'em key with the same type", () => {
    const nlhe = _turnRequest().state as Json;
    const variant = _ofcTurnRequest().state as Json;
    for (const key of Object.keys(nlhe)) {
      expect(key in variant, key).toBe(true);
      expect(typeOf(variant[key]), key).toBe(typeOf(nlhe[key]));
    }
  });

  it("the envelope fields are unchanged", () => {
    const nlhe = _turnRequest();
    const variant = _ofcTurnRequest();
    for (const key of ["type", "match_id", "seq", "seat", "request_id", "timeout_ms"]) {
      expect(typeOf(variant[key]), key).toBe(typeOf(nlhe[key]));
    }
    // valid_actions is exactly ["place"]; the placement's shape lives in state.
    expect(variant.valid_actions).toEqual(["place"]);
  });

  it("the variant is announced by the game_config key that already existed", () => {
    expect((_ofcMatchStart().game_config as Json).variant).toBe("pineapple");
  });

  it("the config says scoring: points and carries no betting keys", () => {
    const cfg = _ofcMatchStart().game_config as Json;
    expect(cfg.scoring).toBe("points");
    for (const absent of ["betting_structure", "small_blind", "big_blind", "bet_cap"]) {
      expect(absent in cfg, absent).toBe(false);
    }
    // point_value is the ruled quantity; bank_points is its derivation.
    expect(cfg.point_value).toBe(50);
    expect(cfg.bank_points).toBe(Math.floor((cfg.starting_stack as number) / (cfg.point_value as number)));
    // rows is an OBJECT of row name -> capacity; its keys are the `place` row names.
    const rows = cfg.rows as Record<string, number>;
    expect(rows).toEqual({ top: 3, middle: 5, bottom: 5 });
    expect(cfg.cards_placed).toBe(Object.values(rows).reduce((a, b) => a + b, 0));
    expect("points_per_chip" in cfg).toBe(false);
  });

  it("the game descriptor matches OFC section 1.1", () => {
    expect(_ofcGameDescriptor()).toEqual({
      game_type: "ofc",
      variant: "pineapple",
      actions: ["place"],
      phases: PHASE_SEQUENCE,
      state_shape: "pineapple",
    });
  });
});

// ---------------------------------------------------------------------------
// 4. The hard constraint: a bad card throws BEFORE decide()
// ---------------------------------------------------------------------------

const BAD_CARD_ARRAYS = [["??"], ["XX"], ["Ah", "??"], ["10h"], [null]];

describe("an invalid card is a hard parse error", () => {
  it.each(BAD_CARD_ARRAYS)("bad hole cards %j throw in parseGameState", (...bad) => {
    expect(() => parseGameState(poison(_ofcTurnRequest(), "your_hole_cards", bad))).toThrow();
  });

  it.each(BAD_CARD_ARRAYS)("a bad board %j throws in parseGameState", (...bad) => {
    expect(() => parseGameState(poison(_ofcTurnRequest(), "board", bad))).toThrow();
  });

  it("the session dies before decide() on a poisoned board", async () => {
    const bot = new NlheOnlyBot();
    const script = _ofcScript();
    script[3] = poison(script[3]!, "board", ["??"]);
    const { sent, error } = await _driveSession(bot, script);
    expect(error).toBeInstanceOf(Error);
    expect(bot.seen).toEqual([]);
    expect(_extractTurnActions(sent)).toEqual([]);
  });

  it("control: the same script, unpoisoned, reaches decide()", async () => {
    const bot = new NlheOnlyBot();
    const { error } = await _driveSession(bot, _ofcScript());
    expect(error).toBeNull();
    expect(bot.seen).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// 5. Action.place puts the placements and the discard under `params`
// ---------------------------------------------------------------------------

describe("Action.place", () => {
  it("emits the placements and the discard under params", () => {
    expect(Action.place([["Ks", "bottom"], ["Js", "middle"]], "9s").toWire()).toEqual({
      action: "place",
      params: {
        placements: [
          { card: "Ks", row: "bottom" },
          { card: "Js", row: "middle" },
        ],
        discard: "9s",
      },
    });
  });

  it("accepts { card, row } entries and a list discard", () => {
    const wire = Action.place(
      [
        { card: "2c", row: "top" },
        { card: "3d", row: "middle" },
      ],
      ["9s"],
    ).toWire();
    expect((wire.params.placements as Placement[])[0]).toEqual({ card: "2c", row: "top" });
    expect(wire.params.discard).toEqual(["9s"]);
  });

  it.each([
    ["omitted", undefined],
    ["null", null],
    ["[]", []],
  ])("the opening set discards nothing (discard %s)", (_label, discard) => {
    expect(Action.place([["2c", "top"]], discard).toWire().params.discard).toEqual([]);
  });

  it("copies the caller's entries rather than aliasing them", () => {
    const entry = { card: "Ks", row: "bottom" };
    const discard = ["9s"];
    const action = Action.place([entry], discard);
    entry.row = "top";
    discard.push("Qs");
    const wire = action.toWire();
    expect(wire.params).toEqual({ placements: [{ card: "Ks", row: "bottom" }], discard: ["9s"] });
    (wire.params.placements as Placement[])[0]!.row = "middle";
    expect((action.toWire().params.placements as Placement[])[0]!.row).toBe("bottom");
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
    expect(sdk.Action.place([["Ks", "bottom"]]).toWire().action).toBe("place");
  });
});

// ---------------------------------------------------------------------------
// 6. decide() stays the single required method; decidePlacement is defaulted
// ---------------------------------------------------------------------------

class LegacyBot extends Bot {
  decide(): Action {
    return Action.fold();
  }
}

describe("the decidePlacement hook", () => {
  it("a decide()-only bot still instantiates and decides", () => {
    const bot = new LegacyBot();
    expect(bot).toBeInstanceOf(Bot);
    expect(bot.decide().action).toBe("fold");
  });

  it("defaults to a legal placement", () => {
    const state = parsed();
    const action = new LegacyBot().decidePlacement(state);
    expect(action.action).toBe("place");
    expectLegalPlacement(action.toWire().params, state);
    // Bottom, then middle, then top; the leftovers are the discard.
    expect(action.toWire().params).toEqual({
      placements: [
        { card: "9s", row: "bottom" },
        { card: "Js", row: "bottom" },
      ],
      discard: ["Ks"],
    });
  });

  it("respects a full row", () => {
    const msg = _ofcTurnRequest();
    (msg.state as Json).row_capacity = { top: 0, middle: 0, bottom: 2 };
    const params = new LegacyBot().decidePlacement(parseGameState(msg)).toWire().params;
    expect(rowCounts(params.placements as Placement[])).toEqual({ bottom: 2 });
  });

  it.each([
    ["the opening set", () => deal1TurnRequest(4, "req_1")],
    ["a Fantasy Land turn", fantasyLandTurnRequest],
  ])("is legal on %s", (_label, make) => {
    const state = parseGameState(make());
    expectLegalPlacement(new LegacyBot().decidePlacement(state).toWire().params, state);
  });

  it("is not called by the session loop", async () => {
    class HookSpy extends NlheOnlyBot {
      hookCalls = 0;
      override decidePlacement(state: GameState): Action {
        this.hookCalls += 1;
        return super.decidePlacement(state);
      }
    }
    const bot = new HookSpy();
    const { error } = await _driveSession(bot, _ofcScript());
    expect(error).toBeNull();
    expect(bot.seen).toHaveLength(1);
    expect(bot.hookCalls).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// 7. The safe fallback at an OFC turn is the protocol's default placement
// ---------------------------------------------------------------------------

describe("the OFC safe fallback (OFC section 5.9)", () => {
  it("fills the row with the most capacity, ties toward the back, and discards the rest", () => {
    // Capacity top 2, middle 3, bottom 3: 9s ties middle/bottom -> bottom;
    // then middle (3) beats bottom (2) -> Js to middle; Ks is the discard.
    expect(_ofcDefaultPlacement(parsed()).toWire().params).toEqual({
      placements: [
        { card: "9s", row: "bottom" },
        { card: "Js", row: "middle" },
      ],
      discard: ["Ks"],
    });
  });

  it.each([
    ["a pineapple street", () => _ofcTurnRequest()],
    ["the opening set", () => deal1TurnRequest(4, "req_1")],
    ["a Fantasy Land turn", fantasyLandTurnRequest],
  ])("is legal on %s", (_label, make) => {
    const state = parseGameState(make());
    expectLegalPlacement(_safeFallbackAction(["place"], state).toWire().params, state);
  });

  it("without a state it is an empty, well-formed placement", () => {
    expect(_safeFallbackAction(["place"]).toWire()).toEqual({
      action: "place",
      params: { placements: [], discard: [] },
    });
  });

  it("leaves check / fold / stand pat wherever they are offered", () => {
    const state = parsed();
    expect(_safeFallbackAction(["check", "raise"], state).action).toBe("check");
    expect(_safeFallbackAction(["fold", "call", "raise"], state).action).toBe("fold");
    expect(_safeFallbackAction(["draw"], state).toWire()).toEqual({ action: "draw", params: { discard: [] } });
    expect(_safeFallbackAction([], state).action).toBe("fold");
  });
});

// ---------------------------------------------------------------------------
// 8. The fixtures match the spec and stay out of the grader
// ---------------------------------------------------------------------------

describe("the OFC conformance fixtures", () => {
  it("are not wired into the grader", async () => {
    expect(SCENARIOS.map((s) => s.name)).toEqual([
      "connectivity_full_match",
      "multi_turn_request_id_echo",
      "action_rejected_recovery",
      "retry_storm_bounded",
    ]);
    expect(await runConformanceChecks(new LegacyBot())).toHaveLength(4);
  });

  it("classify defaults to the hold'em vocabulary; place is legal only under OFC_ACTIONS", () => {
    // A bot that answers a hold'em table with `place` is broken; widening
    // the default would hide that.
    const payload = JSON.stringify({ type: "turn_action", request_id: "req_1", action: "place" });
    expect(_classifyTurnAction(payload).ok).toBe(false);
    expect(_classifyTurnAction(payload, "req_1", OFC_ACTIONS).ok).toBe(true);
    const fold = JSON.stringify({ type: "turn_action", request_id: "req_1", action: "fold" });
    expect(_classifyTurnAction(fold, "req_1", OFC_ACTIONS).ok).toBe(false);
  });

  it("an action-history amount is a placement mask that decodes to your_rows", () => {
    const entry = ((_ofcTurnRequest().state as Json).action_history as Json[])[0]!;
    expect(entry.action).toBe("place");
    const codes = [0, 1, 2, 3, 4].map((i) => ((entry.amount as number) >> (2 * i)) & 0b11);
    expect(codes).toEqual([1, 2, 2, 3, 3]); // top, middle, middle, bottom, bottom

    const rows: Record<number, string> = { 1: "top", 2: "middle", 3: "bottom" };
    const dealt = (_ofcRoundStart().state as Json).cards_to_place as string[];
    const rebuilt: Record<string, string[]> = { top: [], middle: [], bottom: [] };
    dealt.forEach((card, i) => {
      expect(codes[i], "the opening set discards nothing").not.toBe(0);
      rebuilt[rows[codes[i]!]!]!.push(card);
    });
    expect(rebuilt).toEqual((_ofcTurnRequest().state as Json).your_rows);

    // A pineapple street DOES carry one code 0: place 2, discard 1.
    const details = _ofcTurnResult().details as Json;
    expect(details.action).toBe("place");
    expect([0, 1, 2].map((i) => ((details.amount as number) >> (2 * i)) & 0b11)).toEqual([2, 3, 0]);
  });

  it("the showdown points and chips sum to zero, and every seat appears", () => {
    const showdown = (_ofcRoundResult().result as Json).showdown as Json[];
    expect(showdown.reduce((a, e) => a + (e.points as number), 0)).toBe(0);
    expect(showdown.reduce((a, e) => a + (e.net_chips as number), 0)).toBe(0);
    expect(showdown.map((e) => e.seat).sort()).toEqual([0, 1]);
    // A discard never appears, not even in its owner's own entry.
    for (const e of showdown) expect(e.hole_cards).toHaveLength(13);
  });
});

// ---------------------------------------------------------------------------
// 9. End to end: a bot that declares ofc plays an OFC match
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

function withSeq(frame: Json, seq: number): Json {
  return { ...frame, seq };
}

/** Handshake + a hand with two placement turns (deal1, then street2) + match_end. */
function twoStreetScript(withDescriptor = true): Json[] {
  const [hello, matchStart, roundStart, , , , matchEnd] = _ofcScript({ withDescriptor });
  return [
    hello!,
    matchStart!,
    roundStart!,
    deal1TurnRequest(4, "req_1"),
    withSeq(_ofcTurnResult(), 5),
    _ofcTurnRequest(6, "req_2"),
    withSeq(_ofcTurnResult(), 7),
    _ofcRoundResult(8),
    withSeq(matchEnd!, 9),
  ];
}

function rejection(seq: number, requestId: string): Json {
  return {
    type: "action_rejected",
    match_id: "m_conformance_test",
    seq,
    request_id: requestId,
    reason: "invalid_action",
    valid_actions: ["place"],
  };
}

describe("a JavaScript bot that declares ofc", () => {
  let errorSpy: ReturnType<typeof vi.spyOn>;
  beforeEach(() => {
    errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
  });
  afterEach(() => errorSpy.mockRestore());

  it.each([
    ["with the live game descriptor", true],
    ["with the Python fixture's hello (no descriptor)", false],
  ])("plays the fixture hand end to end %s", async (_label, withDescriptor) => {
    const bot = new OfcBot();
    const { ws, end } = await play(bot, _ofcScript({ withDescriptor }), {
      supportedGames: ["poker", "ofc"],
    });

    expect(end?.type).toBe("match_end");
    const hello = ws.parsed.find((m) => m.type === "hello")!;
    expect(hello.supported_games).toEqual(["poker", "ofc"]);

    const actions = _extractTurnActions(ws.sent);
    expect(actions).toHaveLength(1);
    expect(actions[0]).toMatchObject({ type: "turn_action", request_id: "req_1", action: "place" });
    expect(_classifyTurnAction(JSON.stringify(actions[0]), "req_1", OFC_ACTIONS).ok).toBe(true);
    expectLegalPlacement(actions[0]!.params as Json, bot.seen[0]!);
    expect(bot.seen[0]!.yourSeat).toBe(1);
    expect(errorSpy).not.toHaveBeenCalled();
  });

  it("plays the opening set and a pineapple street, each answer legal", async () => {
    const bot = new OfcBot();
    const { ws, end } = await play(bot, twoStreetScript(), { supportedGames: ["ofc"] });

    expect(end?.type).toBe("match_end");
    const actions = _extractTurnActions(ws.sent);
    expect(actions.map((a) => [a.request_id, a.action])).toEqual([
      ["req_1", "place"],
      ["req_2", "place"],
    ]);
    expect(bot.seen.map((s) => [s.phase, s.place, s.mustDiscard])).toEqual([
      ["deal1", 5, 0],
      ["street2", 2, 1],
    ]);
    for (const [i, a] of actions.entries()) {
      expect(_classifyTurnAction(JSON.stringify(a), `req_${i + 1}`, OFC_ACTIONS).ok).toBe(true);
      expectLegalPlacement(a.params as Json, bot.seen[i]!);
    }
    expect(errorSpy).not.toHaveBeenCalled();
  });

  it("a hold'em bot at an OFC table survives the session (its fold is simply wrong)", async () => {
    const bot = new NlheOnlyBot();
    const { ws, end } = await play(bot, twoStreetScript());
    expect(end?.type).toBe("match_end");
    expect(_extractTurnActions(ws.sent).map((a) => a.action)).toEqual(["fold", "fold"]);
  });

  it("a throw on an OFC turn sends the protocol's default placement, not a fold", async () => {
    class Thrower extends Bot {
      decide(): Action {
        throw new Error("boom");
      }
    }
    const { ws } = await play(new Thrower(), _ofcScript());
    const actions = _extractTurnActions(ws.sent);
    expect(actions[0]).toMatchObject({
      request_id: "req_1",
      action: "place",
      params: {
        placements: [
          { card: "9s", row: "bottom" },
          { card: "Js", row: "middle" },
        ],
        discard: ["Ks"],
      },
    });
  });

  it("a non-Action return on an OFC turn sends the default placement", async () => {
    class WrongType extends Bot {
      decide(): Action {
        return "place" as unknown as Action;
      }
    }
    const { ws } = await play(new WrongType(), _ofcScript());
    const action = _extractTurnActions(ws.sent)[0]!;
    expect(action.action).toBe("place");
    expectLegalPlacement(action.params as Json, parsed());
  });

  it("an action_rejected on an OFC turn retries with the default placement", async () => {
    const { ws } = await play(new NlheOnlyBot(), [
      _ofcServerHello(),
      _ofcMatchStart(),
      _ofcTurnRequest(3, "req_1"),
      rejection(4, "req_1"),
    ]);
    const actions = _extractTurnActions(ws.sent);
    // The hold'em bot's own answer was a fold; the retry is a legal placement.
    expect(actions.map((a) => [a.request_id, a.action])).toEqual([
      ["req_1", "fold"],
      ["req_1", "place"],
    ]);
    expectLegalPlacement(actions[1]!.params as Json, parsed());
  });

  it("an action_rejected for a turn this session never saw sends an empty placement", async () => {
    const { ws } = await play(new NlheOnlyBot(), [
      _ofcServerHello(),
      _ofcMatchStart(),
      _ofcTurnRequest(3, "req_1"),
      rejection(4, "req_other"),
    ]);
    const retry = _extractTurnActions(ws.sent)[1]!;
    expect(retry).toMatchObject({ request_id: "req_other", action: "place", params: { placements: [], discard: [] } });
  });

  it("on a reconnect, the OFC state's own your_seat wins over the re-learned seat", async () => {
    const bot = new OfcBot();
    const { ws } = await play(bot, [
      _ofcServerHello(),
      {
        type: "reconnected",
        match_id: "m_conformance_test",
        seq: 2,
        round_number: 1,
        seats: [{ seat: 0, participant_id: "p1", is_self: true }],
        pending_request: _ofcTurnRequest(3, "req_1"),
      },
      rejection(4, "req_1"),
    ]);
    expect(bot.seen.map((s) => [s.yourSeat, s.dealerSeat])).toEqual([[1, 0]]);
    // The pending turn is remembered for an action_rejected retry, too.
    const actions = _extractTurnActions(ws.sent);
    expect(actions.map((a) => [a.request_id, a.action])).toEqual([
      ["req_1", "place"],
      ["req_1", "place"],
    ]);
    expectLegalPlacement(actions[1]!.params as Json, parsed());
  });

  it("on a reconnect, a state without your_seat still takes the re-learned seat", async () => {
    const bot = new OfcBot();
    const pending = _ofcTurnRequest(3, "req_1");
    delete (pending.state as Json).your_seat;
    await play(bot, [
      _ofcServerHello(),
      {
        type: "reconnected",
        match_id: "m_conformance_test",
        seq: 2,
        round_number: 1,
        seats: [{ seat: 1, participant_id: "p1", is_self: true }],
        pending_request: pending,
      },
    ]);
    expect(bot.seen.map((s) => s.yourSeat)).toEqual([1]);
  });
});
