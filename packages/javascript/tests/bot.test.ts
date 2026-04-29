import { describe, expect, it } from "vitest";

import { Bot } from "../src/bot.js";
import { Action, type GameState } from "../src/models.js";

class TestBot extends Bot {
  decide(state: GameState): Action {
    if (state.validActions.includes("check")) return Action.check();
    return Action.fold();
  }
}

describe("Bot subclass", () => {
  it("can be instantiated and invoked", () => {
    const bot = new TestBot();
    const fakeState = {
      handNumber: 1,
      phase: "preflop" as const,
      holeCards: [],
      board: [],
      pot: 0,
      yourStack: 1000,
      opponentStacks: [],
      yourSeat: 0,
      dealerSeat: 0,
      toCall: 0,
      minRaise: 0,
      maxRaise: 0,
      validActions: ["check", "fold"],
      actionHistory: [],
      roundId: "",
      requestId: "",
    };
    const action = bot.decide(fakeState);
    expect(action.action).toBe("check");
  });

  it("exposes lifecycle hook defaults that are no-ops", () => {
    const bot = new TestBot();
    // None should throw on a default implementation.
    expect(() => bot.onMatchStart({})).not.toThrow();
    expect(() => bot.onRoundStart({})).not.toThrow();
    expect(() => bot.onPhaseChange({})).not.toThrow();
    expect(() => bot.onTurnResult({})).not.toThrow();
    expect(() => bot.onRoundResult({})).not.toThrow();
    expect(() => bot.onMatchEnd({})).not.toThrow();
  });
});
