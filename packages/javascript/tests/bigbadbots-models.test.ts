/**
 * Big Bad Bots typed models and the `state_shape` router (chipzen-sdk#151).
 *
 * Mirrors the Python SDK's `tests/test_bigbadbots_models.py`, on the same
 * frames, so both SDKs agree:
 *
 * - every JSON example published in
 *   `docs/protocol/BIGBADBOTS-GAME-STATE-PROTOCOL.md`, read straight out of
 *   the document;
 * - every request in the Python SDK's conformance fixtures
 *   (`packages/python/src/chipzen/fixtures/bigbadbots/*.json`), replayed from
 *   recorded matches of the reference engine;
 * - the published rule sets the Python SDK bundles.
 */

import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import {
  FIGHT_ACTIONS,
  StateParseError,
  gameConfigFromMatchStart,
  parseBigBadBotsGameConfig,
  parseBigBadBotsTurnRequest,
  parseRuleSet,
  ruleSetFromGameConfig,
  ruleSetToWire,
  toWireState,
  withRules,
  type AuctionState,
  type BigBadBotsTurnState,
  type BodyShopState,
  type FightState,
  type FittingState,
} from "../src/bigbadbots.js";
import * as sdk from "../src/index.js";
import {
  Action,
  parseTurnRequest,
  stateShapeOf,
  UnsupportedStateShapeError,
} from "../src/models.js";

type Json = Record<string, unknown>;

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, "..", "..", "..");
const DOC = path.join(REPO, "docs", "protocol", "BIGBADBOTS-GAME-STATE-PROTOCOL.md");
const PY_PKG = path.join(REPO, "packages", "python", "src", "chipzen");
const FIXTURES = path.join(PY_PKG, "fixtures", "bigbadbots");
const RULESETS = path.join(PY_PKG, "rulesets", "bigbadbots");

const PHASE_CALL: Record<string, string> = {
  body_shop: "choose_body",
  auction: "bid",
  fitting: "fit",
  fight: "act",
};
const CALLS = ["choose_body", "bid", "fit", "act"] as const;
type Call = (typeof CALLS)[number];

function blocks(): Json[] {
  const text = readFileSync(DOC, "utf-8").replace(/\r\n/g, "\n");
  const fence = /^```json\n([\s\S]*?)\n```$/gm;
  return [...text.matchAll(fence)].map((m) => JSON.parse(m[1]!) as Json);
}

/** A fresh deep copy of the published `turn_request` for `call`. */
function request(call: Call): Json {
  const found = blocks().filter(
    (b) => b.type === "turn_request" && PHASE_CALL[(b.state as Json).phase as string] === call,
  );
  expect(found).toHaveLength(1);
  return structuredClone(found[0]!);
}

function answers(): Json[] {
  return blocks().filter((b) => b.type === "turn_action");
}

function publishedGameConfig(): Json {
  const found = blocks().filter((b) => b.variant === "bigbadbots" && "rule_set" in b);
  expect(found).toHaveLength(1);
  return structuredClone(found[0]!);
}

interface FixturePair {
  request: Json;
  answer: string;
  expect: string;
}

function fixtureRequests(): Array<{ name: string; request: Json }> {
  const out: Array<{ name: string; request: Json }> = [];
  for (const file of readdirSync(FIXTURES).filter((f) => f.endsWith(".json")).sort()) {
    const data = JSON.parse(readFileSync(path.join(FIXTURES, file), "utf-8")) as {
      pairs: FixturePair[];
    };
    data.pairs.forEach((pair, i) => out.push({ name: `${file}#${i}`, request: pair.request }));
  }
  return out;
}

const parse = (msg: Json): BigBadBotsTurnState => parseBigBadBotsTurnRequest(msg);

/** Walk `path` (dot/bracket separated) into `obj`, returning the parent and the last key. */
function locate(obj: Json, keyPath: string): [Json | unknown[], string | number] {
  const parts = keyPath.split(".").map((p) => (/^\d+$/.test(p) ? Number(p) : p));
  let cur: unknown = obj;
  for (const part of parts.slice(0, -1)) cur = (cur as Json)[part as string];
  return [cur as Json, parts[parts.length - 1]!];
}

// ---------------------------------------------------------------------------
// The published examples
// ---------------------------------------------------------------------------

describe("published turn_requests", () => {
  it.each(CALLS)("%s parses into its call's model", (call) => {
    const state = parse(request(call));
    expect(PHASE_CALL[state.phase]).toBe(call);
    expect(state.requestId).toBe(request(call).request_id);
    expect(state.rules).toBeNull();
  });

  it.each(CALLS)("%s round-trips to the exact state it came from", (call) => {
    const msg = request(call);
    expect(toWireState(parse(msg))).toEqual(msg.state);
  });

  it("body shop offer is typed", () => {
    const state = parse(request("choose_body")) as BodyShopState;
    expect(state.offer.map((b) => b.id)).toEqual(["warden", "golem", "brawler", "scrapper"]);
    expect(state.offer[1]!.cost).toBe(550);
    expect(state.offer[1]!.stats.health).toBe(80);
    expect(state.offer[3]!.slots).toEqual(["legs", "legs", "arms", "arms", "core", "head"]);
    expect(state.me.body).toBeNull();
    expect(state.opp.offer).toEqual([]);
    expect(state.lotsTotal).toBe(6);
    expect(state.timeoutMs).toBe(1000);
    expect(state.opponentStacks).toEqual([0]);
  });

  it("auction lot and sides are typed", () => {
    const state = parse(request("bid")) as AuctionState;
    expect(state.lot.id).toBe("spring");
    expect(state.lot.bonus).toEqual({ speed: 25, stamina: -10 });
    expect(state.me.credits).toBe(450);
    expect(state.me.fitted).toEqual([null]);
    expect(state.opp.body).toBe("wisp");
    expect(state.history).toEqual([]);
  });

  it("fitting owned, slots and history are typed", () => {
    const state = parse(request("fit")) as FittingState;
    expect(state.owned[0]!.id).toBe("capacitor");
    expect(state.slots).toEqual(["core"]);
    expect(state.lotIndex).toBe(state.lotsTotal);
    expect(state.history[0]).toEqual({ part: "spring", myBid: 0, oppBid: 92, winner: "opp" });
    expect(state.history[1]!.winner).toBeNull();
    expect(state.history[5]!.winner).toBe("me");
  });

  it("fight fighters are typed and the wind-up mask survives", () => {
    const state = parse(request("act")) as FightState;
    expect(state.tick).toBe(21);
    expect(state.round).toBe(1);
    expect(state.roundsWon).toEqual([0, 0]);
    expect(state.me.maxHp).toBe(226);
    expect(state.me.strikeRange).toBe(1.615);
    expect(state.me.lastAttack).toBe("kick_high");
    expect(state.opp.action).toBe("attack");
    expect(state.opp.phase).toBe("startup");
    expect(state.opp.framesLeft).toBeNull();
    expect(state.distance).toBe(1.798);
    expect(state.facing).toBe(-1);
    expect(state.walls).toEqual([0, 12]);
    expect(state.validActions).toEqual([...FIGHT_ACTIONS]);
  });

  it("the models are frozen", () => {
    const state = parse(request("act")) as FightState;
    expect(Object.isFrozen(state)).toBe(true);
    expect(Object.isFrozen(state.me)).toBe(true);
    expect(Object.isFrozen(state.me.attrs)).toBe(true);
    expect(Object.isFrozen(state.walls)).toBe(true);
    expect(() => {
      (state as { tick: number }).tick = 3;
    }).toThrow(TypeError);
  });

  it("withRules returns a frozen copy and leaves the original alone", () => {
    const state = parse(request("bid"));
    const rules = parseRuleSet("x", publishedGameConfig().rule_set as Json);
    const copy = withRules(state, rules);
    expect(copy.rules).toBe(rules);
    expect(state.rules).toBeNull();
    expect(Object.isFrozen(copy)).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// The Python SDK's conformance fixtures: recorded engine frames
// ---------------------------------------------------------------------------

describe("recorded engine frames (shared with the Python SDK)", () => {
  const frames = fixtureRequests();

  it("covers every call", () => {
    const phases = new Set(frames.map((f) => (f.request.state as Json).phase));
    expect([...phases].sort()).toEqual(["auction", "body_shop", "fight", "fitting"]);
    expect(frames.length).toBeGreaterThan(100);
  });

  it.each(frames.map((f) => [f.name, f.request] as const))(
    "%s parses and round-trips",
    (_name, req) => {
      const state = parse(req);
      expect(state.phase).toBe((req.state as Json).phase);
      expect(toWireState(state)).toEqual(req.state);
    },
  );
});

// ---------------------------------------------------------------------------
// Game config and rule sets
// ---------------------------------------------------------------------------

describe("game config and rule set", () => {
  it("game config parses and keeps the wire table", () => {
    const raw = publishedGameConfig();
    const config = parseBigBadBotsGameConfig(raw);
    expect(config.ruleSetId).toBe("bigbadbots-1.0");
    expect(config.numPlayers).toBe(2);
    expect(config.ruleSet).toEqual(raw.rule_set);
    expect(gameConfigFromMatchStart({ type: "match_start", game_config: raw })).toEqual(config);
  });

  it("a game config for another variant fails loudly", () => {
    expect(() => parseBigBadBotsGameConfig({ ...publishedGameConfig(), variant: "nlhe" })).toThrow(
      /game_config\.variant/,
    );
  });

  it("a game config missing its rule set fails loudly", () => {
    const raw = publishedGameConfig();
    delete raw.rule_set;
    expect(() => parseBigBadBotsGameConfig(raw)).toThrow(/rule_set/);
    expect(() => gameConfigFromMatchStart({ type: "match_start" })).toThrow(StateParseError);
  });

  it("the published game config builds the typed rule set", () => {
    const rules = ruleSetFromGameConfig(parseBigBadBotsGameConfig(publishedGameConfig()));
    expect(rules.id).toBe("bigbadbots-1.0");
    expect(rules.gearup).toEqual({ budget: 1000, bodyOfferSize: 4, auctionLots: 6 });
    expect(rules.arena.startX).toEqual([4, 8]);
    expect(rules.rounds.timeoutTiebreak).toBe("hp_pct");
    expect(rules.rounds.closingWalls).toBe(false);
    expect(rules.rounds.stallCloseAfter).toBeNull();
    expect(rules.forfeit.graceSeconds).toBe(5);
    expect(rules.deadline.deadlineMs).toBe(100);
    expect(rules.deadline.missCountsTowardForfeit).toBe(true);
    expect(Object.isFrozen(rules.params)).toBe(true);
    expect(ruleSetToWire(rules)).toEqual(publishedGameConfig().rule_set);
  });

  const published = readdirSync(RULESETS)
    .filter((f) => f.endsWith(".json"))
    .sort();

  it("the Python SDK bundles the rule sets this test reads", () => {
    expect(published).toEqual(["bigbadbots-1.0.json", "bigbadbots-1.1.json", "bigbadbots-1.2.json"]);
  });

  it.each(published)("published %s builds the typed rule set", (file) => {
    const entry = JSON.parse(readFileSync(path.join(RULESETS, file), "utf-8")) as Json;
    const rules = parseRuleSet(entry.rule_set_id as string, entry.rule_set as Json);
    expect(rules.id).toBe(entry.rule_set_id);
    const rounds = (entry.rule_set as Json).rounds as Json;
    expect(rules.rounds.closingWalls).toBe("stall_close_after" in rounds);
    expect(rules.rounds.timeoutTiebreak).toBe(rounds.timeout_tiebreak ?? "hp_pct");
    expect(ruleSetToWire(rules)).toEqual(entry.rule_set);
  });

  it("a rule set missing a typed section fails naming it", () => {
    const table = publishedGameConfig().rule_set as Json;
    delete table.deadline;
    expect(() => parseRuleSet("x", table)).toThrow(/rule_set: missing required field "deadline"/);
  });
});

// ---------------------------------------------------------------------------
// Answers
// ---------------------------------------------------------------------------

describe("Big Bad Bots actions", () => {
  const factory: Record<string, (a: Json) => Action> = {
    choose_body: (a) => Action.chooseBody((a.params as Json).body as string),
    bid: (a) => Action.bid((a.params as Json).amount as number),
    fit: (a) => Action.fit((a.params as Json).fit as Record<string, string>),
  };

  it.each(answers().map((a) => [a.action as string, a] as const))(
    "published %s answer is what the factory emits",
    (action, answer) => {
      const built = (factory[action] ?? (() => Action.act(action as never)))(answer);
      expect(built.toWire()).toEqual({ action, params: answer.params ?? {} });
    },
  );

  it("a bid of zero and an empty fitting still carry their params", () => {
    expect(Action.bid(0).toWire()).toEqual({ action: "bid", params: { amount: 0 } });
    expect(Action.fit().toWire()).toEqual({ action: "fit", params: { fit: {} } });
    expect(Action.fit(null).toWire()).toEqual({ action: "fit", params: { fit: {} } });
  });

  it("fit slot indexes go out as decimal strings", () => {
    expect(Action.fit({ 0: "a", 2: "b" }).toWire().params).toEqual({ fit: { "0": "a", "2": "b" } });
    expect(Action.fit(new Map([[1, "c"]])).toWire().params).toEqual({ fit: { "1": "c" } });
  });

  it("toWire hands back a copy", () => {
    const action = Action.fit({ 0: "a" });
    ((action.toWire().params as Json).fit as Json)["1"] = "x";
    expect(action.toWire().params).toEqual({ fit: { "0": "a" } });
  });

  it.each([12.5, Number.NaN, Infinity, "12", null, true])("bid refuses %s", (amount) => {
    expect(() => Action.bid(amount as number)).toThrow(TypeError);
  });

  it.each(["-1", "01", "x", "1.5", ""])("fit refuses slot %j", (slot) => {
    expect(() => Action.fit({ [slot]: "p" })).toThrow(RangeError);
  });

  it("fit refuses a fractional or negative numeric slot", () => {
    expect(() => Action.fit(new Map([[1.5, "p"]]))).toThrow(RangeError);
    expect(() => Action.fit(new Map([[-1, "p"]]))).toThrow(RangeError);
  });

  it.each([...FIGHT_ACTIONS])("act takes %s", (move) => {
    expect(Action.act(move).toWire()).toEqual({ action: move, params: {} });
  });

  it.each(["fold", "check", "choose_body", "attack", ""])("act refuses %j", (move) => {
    expect(() => Action.act(move as never)).toThrow(RangeError);
  });

  it("poker actions are unchanged", () => {
    expect(Action.raiseTo(40).toWire()).toEqual({ action: "raise", params: { amount: 40 } });
    expect(Action.check().toWire()).toEqual({ action: "check", params: {} });
  });
});

// ---------------------------------------------------------------------------
// Strictness
// ---------------------------------------------------------------------------

const STATE_KEYS: Record<Call, string[]> = {
  choose_body: Object.keys(request("choose_body").state as Json),
  bid: Object.keys(request("bid").state as Json),
  fit: Object.keys(request("fit").state as Json),
  act: Object.keys(request("act").state as Json),
};

describe("strict parsing", () => {
  const missingState = CALLS.flatMap((call) => STATE_KEYS[call].map((key) => [call, key] as const));

  it.each(missingState)("%s: a missing state.%s fails naming it", (call, key) => {
    const msg = request(call);
    delete (msg.state as Json)[key];
    expect(() => parse(msg)).toThrow(new RegExp(`missing required field "${key}"`));
  });

  it.each(["request_id", "seat", "timeout_ms", "valid_actions", "state"])(
    "a missing envelope %s fails naming it",
    (key) => {
      const msg = request("act");
      delete msg[key];
      expect(() => parse(msg)).toThrow(new RegExp(`turn_request: missing required field "${key}"`));
    },
  );

  it.each([
    ["choose_body", "offer.0.cost"],
    ["choose_body", "me.credits"],
    ["bid", "lot.effects"],
    ["bid", "opp.owned"],
    ["fit", "history.0.winner"],
    ["fit", "owned.0.slot"],
    ["act", "me.frames_left"],
    ["act", "opp.attrs"],
  ] as const)("%s: a missing nested %s fails naming it", (call, keyPath) => {
    const msg = request(call);
    const [parent, key] = locate(msg.state as Json, keyPath);
    delete (parent as Json)[key as string];
    expect(() => parse(msg)).toThrow(new RegExp(`missing required field "${key}"`));
  });

  it.each([
    ["choose_body", "offer.0.cost", 1.5],
    ["choose_body", "lot_index", "0"],
    ["bid", "me.credits", null],
    ["bid", "me.fitted", ["capacitor"]],
    ["bid", "lot.bonus", { speed: "fast" }],
    ["fit", "history.0.winner", "both"],
    ["fit", "slots", [0]],
    ["act", "distance", "near"],
    ["act", "me.special_used", 0],
    ["act", "opp.frames_left", 1.5],
    ["act", "walls", [0, null]],
    ["act", "me", []],
    ["act", "pot", null],
  ] as const)("%s: a mistyped %s fails loudly", (call, keyPath, value) => {
    const msg = request(call);
    const [parent, key] = locate(msg.state as Json, keyPath);
    (parent as Json)[key as string] = value;
    expect(() => parse(msg)).toThrow(StateParseError);
  });

  it.each(["preflop", "draw1", "", "FIGHT"])("phase %j is not Big Bad Bots and fails loudly", (phase) => {
    const msg = request("act");
    (msg.state as Json).phase = phase;
    expect(() => parse(msg)).toThrow(/is not a Big Bad Bots phase/);
  });

  it.each(CALLS)("%s: a new key is ignored", (call) => {
    const msg = request(call);
    (msg.state as Json).brand_new_key = { anything: [1, 2, 3] };
    msg.brand_new_envelope_key = true;
    if (call === "act") ((msg.state as Json).me as Json).new_fighter_key = 7;
    const state = parse(msg);
    expect(state.phase).toBe((msg.state as Json).phase);
    expect("brand_new_key" in state).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// The state_shape router (LAYER2-COMMON section 2)
// ---------------------------------------------------------------------------

describe("state_shape router", () => {
  const descriptor = (): Json => {
    const found = blocks().filter((b) => b.state_shape === "bigbadbots" && "phases" in b);
    expect(found).toHaveLength(1);
    return found[0]!;
  };

  it("a hello with no game descriptor is poker", () => {
    expect(stateShapeOf({ type: "hello" })).toBeNull();
  });

  it("the published descriptor is bigbadbots", () => {
    expect(stateShapeOf({ type: "hello", game: descriptor() })).toBe("bigbadbots");
  });

  it.each([{ game_type: "bigbadbots" }, { state_shape: 3 }, "bigbadbots"])(
    "a game descriptor without a string state_shape fails loudly (%j)",
    (game) => {
      expect(() => stateShapeOf({ type: "hello", game })).toThrow(UnsupportedStateShapeError);
    },
  );

  it.each(CALLS)("bigbadbots routes %s to the Big Bad Bots models", (call) => {
    const state = parseTurnRequest(request(call), "bigbadbots");
    expect(PHASE_CALL[(state as BigBadBotsTurnState).phase]).toBe(call);
  });

  it("no state_shape still routes to the poker parser", () => {
    const state = parseTurnRequest({
      request_id: "r1",
      valid_actions: ["fold", "check"],
      state: { phase: "flop", board: ["Ah", "Kd", "2c"], your_hole_cards: ["Qs", "Qh"], pot: 10 },
    });
    expect("holeCards" in state && state.holeCards).toHaveLength(2);
  });

  it("the 2-7 Triple Draw shape routes to the poker parser", () => {
    const state = parseTurnRequest(
      {
        request_id: "r1",
        valid_actions: ["draw"],
        state: { phase: "draw1", board: [], your_hole_cards: ["Ah", "Kd", "9s", "7c", "3h"], max_discard: 5 },
      },
      "27tripledraw",
    );
    expect("maxDiscard" in state && state.maxDiscard).toBe(5);
  });

  // Pineapple OFC is not parsed by this release yet (chipzen-sdk#175).
  it.each(["fighter", "chess", "nlhe-v2", "", "BIGBADBOTS", "pineapple"])(
    "unknown state_shape %j fails loudly with no fallback",
    (shape) => {
      expect(() => parseTurnRequest(request("act"), shape)).toThrow(UnsupportedStateShapeError);
    },
  );

  it("a Big Bad Bots payload with a missing field fails through the router", () => {
    const msg = request("act");
    delete (msg.state as Json).distance;
    expect(() => parseTurnRequest(msg, "bigbadbots")).toThrow(StateParseError);
  });

  it("the Big Bad Bots surface is exported from the package", () => {
    expect(sdk.parseBigBadBotsTurnRequest).toBe(parseBigBadBotsTurnRequest);
    expect(sdk.parseTurnRequest).toBe(parseTurnRequest);
    expect(typeof sdk.BigBadBotsBot).toBe("function");
    expect(typeof sdk.fallbackAction).toBe("function");
    expect(sdk.BIGBADBOTS_STATE_SHAPE).toBe("bigbadbots");
    expect(sdk.FIGHT_ACTIONS).toHaveLength(10);
  });
});
