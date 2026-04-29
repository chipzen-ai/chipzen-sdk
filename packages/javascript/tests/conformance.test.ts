import { describe, expect, it } from "vitest";

import { Bot } from "../src/bot.js";
import { Action, type GameState } from "../src/models.js";
import { runConformanceChecks } from "../src/conformance.js";

class GoodBot extends Bot {
  decide(state: GameState): Action {
    if (state.validActions.includes("check")) return Action.check();
    if (state.validActions.includes("call")) return Action.call();
    return Action.fold();
  }
}

class ThrowingBot extends Bot {
  decide(): Action {
    throw new Error("boom");
  }
}

class WeirdReturnBot extends Bot {
  decide(): Action {
    // Returns a string instead of an Action. `_runSession`'s instanceof
    // check should reject this and substitute the safe fallback so the
    // protocol exchange still completes cleanly.
    return "not-an-action" as unknown as Action;
  }
}

describe("runConformanceChecks", () => {
  it("passes when the bot completes the canned full-match exchange", async () => {
    const results = await runConformanceChecks(new GoodBot());
    expect(results).toHaveLength(1);
    const check = results[0]!;
    expect(check.severity).toBe("pass");
    expect(check.name).toBe("connectivity_full_match");
    expect(check.message).toMatch(/handshake \+ 1 hand \+ match_end/);
    expect(check.message).toMatch(/turn_action: action="call"/);
  });

  it("passes even when decide() throws — the SDK's safe-fallback applies", async () => {
    // `_runSession` catches user-code exceptions and substitutes a
    // safe fallback action. Conformance verifies the SDK protocol
    // plumbing, not user-code happiness, so this should still pass.
    const results = await runConformanceChecks(new ThrowingBot());
    expect(results).toHaveLength(1);
    const check = results[0]!;
    expect(check.severity).toBe("pass");
    expect(check.message).toMatch(/turn_action: action=/);
  });

  it("passes when decide() returns the wrong type — fallback recovers", async () => {
    const results = await runConformanceChecks(new WeirdReturnBot());
    expect(results).toHaveLength(1);
    const check = results[0]!;
    expect(check.severity).toBe("pass");
    // Fallback picks check (in valid_actions) or fold; the canned
    // valid_actions are ["fold", "call", "raise"] so it lands on fold.
    expect(check.message).toMatch(/turn_action: action="fold"/);
  });
});
