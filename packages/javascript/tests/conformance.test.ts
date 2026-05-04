import { describe, expect, it } from "vitest";

import { Bot } from "../src/bot.js";
import { Action, type GameState } from "../src/models.js";
import { runConformanceChecks, SCENARIOS } from "../src/conformance.js";

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

describe("runConformanceChecks — backward compatibility", () => {
  it("passes when the bot completes the canned full-match exchange", async () => {
    const results = await runConformanceChecks(new GoodBot());
    // 4 scenarios now: full_match, multi_turn, action_rejected, retry_storm.
    expect(results).toHaveLength(4);
    const fullMatch = results.find((r) => r.name === "connectivity_full_match")!;
    expect(fullMatch.severity).toBe("pass");
    expect(fullMatch.message).toMatch(/handshake \+ 1 hand \+ match_end/);
    expect(fullMatch.message).toMatch(/turn_action: action="call"/);
  });

  it("passes every scenario even when decide() throws — SDK safe-fallback applies", async () => {
    // `_runSession` catches user-code exceptions and substitutes a
    // safe fallback action. Conformance verifies the SDK protocol
    // plumbing, not user-code happiness, so all four scenarios should
    // still complete green.
    const results = await runConformanceChecks(new ThrowingBot());
    expect(results).toHaveLength(4);
    const failures = results.filter((r) => r.severity === "fail");
    expect(failures).toEqual([]);
  });

  it("passes when decide() returns the wrong type — fallback recovers", async () => {
    const results = await runConformanceChecks(new WeirdReturnBot());
    expect(results).toHaveLength(4);
    const failures = results.filter((r) => r.severity === "fail");
    expect(failures).toEqual([]);
    // Fallback picks check (in valid_actions) or fold; the canned
    // valid_actions are ["fold", "call", "raise"] so it lands on fold.
    const fullMatch = results.find((r) => r.name === "connectivity_full_match")!;
    expect(fullMatch.message).toMatch(/turn_action: action="fold"/);
  });
});

describe("SCENARIOS registry", () => {
  it("registers all four scenarios in the documented order", () => {
    expect(SCENARIOS.map((s) => s.name)).toEqual([
      "connectivity_full_match",
      "multi_turn_request_id_echo",
      "action_rejected_recovery",
      "retry_storm_bounded",
    ]);
  });
});

describe("multi_turn_request_id_echo scenario", () => {
  it("verifies request_id echoes correctly across 3 turn_requests", async () => {
    const results = await runConformanceChecks(new GoodBot());
    const multiTurn = results.find((r) => r.name === "multi_turn_request_id_echo")!;
    expect(multiTurn.severity).toBe("pass");
    expect(multiTurn.message).toMatch(/3 turn_actions/);
    expect(multiTurn.message).toMatch(/preflop\/flop\/turn/);
  });
});

describe("action_rejected_recovery scenario", () => {
  it("passes when the SDK retries with a safe-fallback action and the original request_id", async () => {
    const results = await runConformanceChecks(new GoodBot());
    const recovery = results.find((r) => r.name === "action_rejected_recovery")!;
    expect(recovery.severity).toBe("pass");
    // Pass message names which safe action was used (check or fold).
    expect(recovery.message).toMatch(/(check|fold)/);
    expect(recovery.message).toMatch(/original request_id/);
  });
});

describe("retry_storm_bounded scenario", () => {
  it("passes when the SDK responds to 3 back-to-back rejections with 4 total turn_actions", async () => {
    const results = await runConformanceChecks(new GoodBot());
    const storm = results.find((r) => r.name === "retry_storm_bounded")!;
    expect(storm.severity).toBe("pass");
    // Pass message confirms the 4-turn-action total (1 initial + 3 retries).
    expect(storm.message).toMatch(/4 turn_actions/);
    expect(storm.message).toMatch(/exited cleanly on match_end/);
  });
});

// NOTE: a "slow decide()" detection test (parallel to the Python SDK's
// test_busy_loop_bot_is_caught_by_inner_or_outer_timeout) is intentionally
// omitted here. JavaScript's Promise.race + setTimeout-based timeout cannot
// fire while the SDK's inner microtask queue is processing scripted
// messages back-to-back — once a sync-blocking decide() returns, microtasks
// drain to the end of the scenario before the macrotask timer callback
// gets a chance to run. The Python harness gets around this with a daemon
// thread; the JS equivalent would be a Worker thread, which is heavier
// and deferred to a follow-up. See the docstring on `runConformanceChecks`
// in `conformance.ts` for the documented limitation.
