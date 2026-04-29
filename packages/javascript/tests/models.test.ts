import { describe, expect, it } from "vitest";

import {
  Action,
  cardFromString,
  cardToString,
  parseGameState,
} from "../src/models.js";

// ---------------------------------------------------------------------------
// Card
// ---------------------------------------------------------------------------

describe("cardFromString", () => {
  it("parses standard 2-char card strings", () => {
    expect(cardFromString("Ah")).toEqual({ rank: "A", suit: "h" });
    expect(cardFromString("Td")).toEqual({ rank: "T", suit: "d" });
    expect(cardFromString("2c")).toEqual({ rank: "2", suit: "c" });
    expect(cardFromString("Ks")).toEqual({ rank: "K", suit: "s" });
  });

  it("rejects wrong length", () => {
    expect(() => cardFromString("A")).toThrow(/expected 2 chars/);
    expect(() => cardFromString("Ahd")).toThrow(/expected 2 chars/);
    expect(() => cardFromString("")).toThrow(/expected 2 chars/);
  });

  it("rejects invalid rank", () => {
    expect(() => cardFromString("1h")).toThrow(/Invalid card rank/);
    expect(() => cardFromString("Xh")).toThrow(/Invalid card rank/);
  });

  it("rejects invalid suit", () => {
    expect(() => cardFromString("Ax")).toThrow(/Invalid card suit/);
    expect(() => cardFromString("AH")).toThrow(/Invalid card suit/); // case-sensitive
  });

  it("rejects non-string input", () => {
    // @ts-expect-error — testing runtime guard, not the type system
    expect(() => cardFromString(null)).toThrow();
    // @ts-expect-error
    expect(() => cardFromString(123)).toThrow();
  });
});

describe("cardToString", () => {
  it("round-trips with cardFromString", () => {
    for (const s of ["Ah", "2c", "Td", "Ks", "9h"]) {
      expect(cardToString(cardFromString(s))).toBe(s);
    }
  });
});

// ---------------------------------------------------------------------------
// Action
// ---------------------------------------------------------------------------

describe("Action factories", () => {
  it("fold/check/call/allIn produce the right kind with no amount", () => {
    expect(Action.fold().action).toBe("fold");
    expect(Action.check().action).toBe("check");
    expect(Action.call().action).toBe("call");
    expect(Action.allIn().action).toBe("all_in");

    expect(Action.fold().amount).toBeUndefined();
    expect(Action.check().amount).toBeUndefined();
    expect(Action.call().amount).toBeUndefined();
    expect(Action.allIn().amount).toBeUndefined();
  });

  it("raiseTo carries the amount", () => {
    const a = Action.raiseTo(60);
    expect(a.action).toBe("raise");
    expect(a.amount).toBe(60);
  });

  it("raiseTo rejects invalid amounts", () => {
    expect(() => Action.raiseTo(-1)).toThrow(/non-negative/);
    expect(() => Action.raiseTo(NaN)).toThrow(/finite/);
    expect(() => Action.raiseTo(Infinity)).toThrow(/finite/);
  });
});

describe("Action.toWire", () => {
  it("omits amount for non-raise actions", () => {
    expect(Action.fold().toWire()).toEqual({ action: "fold", params: {} });
    expect(Action.check().toWire()).toEqual({ action: "check", params: {} });
    expect(Action.call().toWire()).toEqual({ action: "call", params: {} });
    expect(Action.allIn().toWire()).toEqual({ action: "all_in", params: {} });
  });

  it("includes amount for raise", () => {
    expect(Action.raiseTo(60).toWire()).toEqual({
      action: "raise",
      params: { amount: 60 },
    });
  });
});

// ---------------------------------------------------------------------------
// parseGameState
// ---------------------------------------------------------------------------

describe("parseGameState", () => {
  it("converts a full turn_request message", () => {
    const message = {
      type: "turn_request",
      match_id: "m1",
      seq: 4,
      request_id: "req_1",
      round_id: "r_1",
      valid_actions: ["fold", "call", "raise"],
      state: {
        hand_number: 7,
        phase: "flop",
        board: ["Ts", "7h", "2c"],
        your_hole_cards: ["Ah", "Kd"],
        pot: 60,
        your_stack: 940,
        opponent_stacks: [990],
        your_seat: 0,
        dealer_seat: 1,
        to_call: 20,
        min_raise: 40,
        max_raise: 940,
        action_history: [
          { seat: 0, action: "post_small_blind", amount: 5 },
          { seat: 1, action: "post_big_blind", amount: 10 },
          { seat: 0, action: "call" },
          { seat: 1, action: "raise", amount: 20 },
        ],
      },
    };

    const state = parseGameState(message);

    expect(state.handNumber).toBe(7);
    expect(state.phase).toBe("flop");
    expect(state.board).toEqual([
      { rank: "T", suit: "s" },
      { rank: "7", suit: "h" },
      { rank: "2", suit: "c" },
    ]);
    expect(state.holeCards).toEqual([
      { rank: "A", suit: "h" },
      { rank: "K", suit: "d" },
    ]);
    expect(state.pot).toBe(60);
    expect(state.yourStack).toBe(940);
    expect(state.opponentStacks).toEqual([990]);
    expect(state.yourSeat).toBe(0);
    expect(state.dealerSeat).toBe(1);
    expect(state.toCall).toBe(20);
    expect(state.minRaise).toBe(40);
    expect(state.maxRaise).toBe(940);
    expect(state.validActions).toEqual(["fold", "call", "raise"]);
    expect(state.actionHistory).toHaveLength(4);
    expect(state.actionHistory[0]).toEqual({
      seat: 0,
      action: "post_small_blind",
      amount: 5,
    });
    expect(state.actionHistory[2]).toEqual({ seat: 0, action: "call" });
    expect(state.requestId).toBe("req_1");
    expect(state.roundId).toBe("r_1");
  });

  it("defaults missing fields safely", () => {
    const state = parseGameState({});
    expect(state.handNumber).toBe(0);
    expect(state.phase).toBe("preflop");
    expect(state.holeCards).toEqual([]);
    expect(state.board).toEqual([]);
    expect(state.pot).toBe(0);
    expect(state.validActions).toEqual([]);
    expect(state.actionHistory).toEqual([]);
    expect(state.requestId).toBe("");
    expect(state.roundId).toBe("");
  });

  it("falls back to state.valid_actions when not in the envelope", () => {
    const state = parseGameState({
      state: { valid_actions: ["check", "raise"] },
    });
    expect(state.validActions).toEqual(["check", "raise"]);
  });

  it("preserves is_timeout in action_history when present", () => {
    const state = parseGameState({
      state: {
        action_history: [
          { seat: 1, action: "fold", is_timeout: true },
        ],
      },
    });
    expect(state.actionHistory[0]?.isTimeout).toBe(true);
  });
});
