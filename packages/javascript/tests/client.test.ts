import { describe, expect, it } from "vitest";

import { Bot } from "../src/bot.js";
import {
  _extractMatchId,
  _runSession,
  _safeFallbackAction,
  type AsyncMessageReader,
  type SessionWebSocket,
} from "../src/client.js";
import { Action, type GameState } from "../src/models.js";

// ---------------------------------------------------------------------------
// _extractMatchId
// ---------------------------------------------------------------------------

describe("_extractMatchId", () => {
  it("pulls match_id from a UUID-shaped path", () => {
    expect(
      _extractMatchId("ws://localhost:8001/ws/match/abc123-def-456/bot"),
    ).toBe("abc123-def-456");
  });

  it("pulls match_id from a participant-id path", () => {
    expect(
      _extractMatchId("ws://localhost:8001/ws/match/m_abc/p_xyz"),
    ).toBe("m_abc");
  });

  it("returns empty string for URLs without /ws/match/", () => {
    expect(_extractMatchId("ws://localhost:8001/something/else")).toBe("");
    expect(_extractMatchId("not even a url")).toBe("");
  });
});

// ---------------------------------------------------------------------------
// _safeFallbackAction
// ---------------------------------------------------------------------------

describe("_safeFallbackAction", () => {
  it("returns check when check is legal", () => {
    expect(_safeFallbackAction(["fold", "check", "call"]).action).toBe("check");
  });

  it("returns fold when check is not legal", () => {
    expect(_safeFallbackAction(["fold", "call"]).action).toBe("fold");
    expect(_safeFallbackAction(["fold"]).action).toBe("fold");
  });

  it("returns fold for empty / undefined valid_actions", () => {
    expect(_safeFallbackAction([]).action).toBe("fold");
    expect(_safeFallbackAction(undefined).action).toBe("fold");
  });
});

// ---------------------------------------------------------------------------
// Mock infrastructure for session tests
// ---------------------------------------------------------------------------

class ScriptedReader implements AsyncMessageReader {
  private idx = 0;
  constructor(private readonly messages: Array<Record<string, unknown>>) {}
  async next(): Promise<string | null> {
    if (this.idx >= this.messages.length) return null;
    const msg = this.messages[this.idx++];
    return JSON.stringify(msg);
  }
}

class CapturingSocket implements SessionWebSocket {
  readonly sent: string[] = [];
  send(data: string): void {
    this.sent.push(data);
  }
  close(): void {
    /* no-op */
  }
  /** Convenience: parsed sent messages. */
  get sentParsed(): Array<Record<string, unknown>> {
    return this.sent.map((s) => JSON.parse(s) as Record<string, unknown>);
  }
}

class RecordingBot extends Bot {
  events: string[] = [];
  decide(state: GameState): Action {
    this.events.push("decide");
    if (state.validActions.includes("check")) return Action.check();
    if (state.validActions.includes("call")) return Action.call();
    return Action.fold();
  }
  override onMatchStart(_m: Record<string, unknown>): void {
    this.events.push("match_start");
  }
  override onRoundStart(_m: Record<string, unknown>): void {
    this.events.push("round_start");
  }
  override onPhaseChange(_m: Record<string, unknown>): void {
    this.events.push("phase_change");
  }
  override onTurnResult(_m: Record<string, unknown>): void {
    this.events.push("turn_result");
  }
  override onRoundResult(_m: Record<string, unknown>): void {
    this.events.push("round_result");
  }
  override onMatchEnd(_r: Record<string, unknown>): void {
    this.events.push("match_end");
  }
}

class FailingBot extends Bot {
  decide(_state: GameState): Action {
    throw new Error("intentional failure for testing");
  }
}

const SESSION_CTX = {
  matchId: "m_test",
  token: "tok",
  ticket: null,
  clientName: "test",
  clientVersion: "0.0.0",
};

// ---------------------------------------------------------------------------
// _runSession — handshake + happy path
// ---------------------------------------------------------------------------

describe("_runSession handshake", () => {
  it("sends authenticate, then client hello, after server hello", async () => {
    const reader = new ScriptedReader([
      { type: "hello", match_id: "m_test", seq: 1 },
      { type: "match_end", match_id: "m_test", seq: 2 },
    ]);
    const ws = new CapturingSocket();
    const bot = new RecordingBot();

    await _runSession(ws, bot, SESSION_CTX, reader);

    const sent = ws.sentParsed;
    expect(sent[0]?.type).toBe("authenticate");
    expect(sent[0]?.token).toBe("tok");
    expect(sent[1]?.type).toBe("hello");
    expect(sent[1]?.supported_versions).toEqual(["1.0"]);
  });

  it("throws when the connection closes before server hello", async () => {
    const reader = new ScriptedReader([]);
    const ws = new CapturingSocket();
    await expect(
      _runSession(ws, new RecordingBot(), SESSION_CTX, reader),
    ).rejects.toThrow(/before server hello/);
  });

  it("throws on a non-hello first message", async () => {
    const reader = new ScriptedReader([
      { type: "error", match_id: "m_test", code: "auth_failed" },
    ]);
    const ws = new CapturingSocket();
    await expect(
      _runSession(ws, new RecordingBot(), SESSION_CTX, reader),
    ).rejects.toThrow(/expected server hello/);
  });
});

// ---------------------------------------------------------------------------
// _runSession — full match lifecycle
// ---------------------------------------------------------------------------

describe("_runSession full match lifecycle", () => {
  it("walks through the canonical message sequence", async () => {
    const reader = new ScriptedReader([
      { type: "hello", match_id: "m_test", seq: 1 },
      { type: "match_start", match_id: "m_test", seq: 2 },
      { type: "round_start", match_id: "m_test", seq: 3 },
      {
        type: "turn_request",
        match_id: "m_test",
        seq: 4,
        request_id: "req_1",
        valid_actions: ["fold", "call", "raise"],
        state: {
          hand_number: 1,
          phase: "preflop",
          your_hole_cards: ["Ah", "Kd"],
          to_call: 5,
        },
      },
      { type: "turn_result", match_id: "m_test", seq: 5 },
      { type: "phase_change", match_id: "m_test", seq: 6 },
      { type: "round_result", match_id: "m_test", seq: 7 },
      { type: "match_end", match_id: "m_test", seq: 8 },
    ]);
    const ws = new CapturingSocket();
    const bot = new RecordingBot();

    await _runSession(ws, bot, SESSION_CTX, reader);

    // All lifecycle hooks fired in order
    expect(bot.events).toEqual([
      "match_start",
      "round_start",
      "decide",
      "turn_result",
      "phase_change",
      "round_result",
      "match_end",
    ]);

    // Bot sent authenticate, hello, and one turn_action
    const types = ws.sentParsed.map((m) => m.type);
    expect(types).toContain("authenticate");
    expect(types).toContain("hello");
    expect(types).toContain("turn_action");

    const turnAction = ws.sentParsed.find((m) => m.type === "turn_action");
    expect(turnAction?.request_id).toBe("req_1");
    expect(turnAction?.action).toBe("call");
  });
});

// ---------------------------------------------------------------------------
// _runSession — adversarial / robustness
// ---------------------------------------------------------------------------

describe("_runSession robustness", () => {
  it("responds to ping with pong", async () => {
    const reader = new ScriptedReader([
      { type: "hello", match_id: "m_test", seq: 1 },
      { type: "ping", match_id: "m_test", seq: 2 },
      { type: "match_end", match_id: "m_test", seq: 3 },
    ]);
    const ws = new CapturingSocket();
    await _runSession(ws, new RecordingBot(), SESSION_CTX, reader);
    expect(ws.sentParsed.some((m) => m.type === "pong")).toBe(true);
  });

  it("ignores malformed JSON envelopes mid-loop", async () => {
    // Reader that inserts a malformed payload between two valid messages.
    let idx = 0;
    const messages = [
      JSON.stringify({ type: "hello", match_id: "m_test", seq: 1 }),
      "this is not json",
      JSON.stringify({ type: "match_end", match_id: "m_test", seq: 2 }),
    ];
    const reader: AsyncMessageReader = {
      async next() {
        if (idx >= messages.length) return null;
        return messages[idx++] ?? null;
      },
    };
    const ws = new CapturingSocket();
    const bot = new RecordingBot();
    // Should resolve cleanly on match_end despite the bad payload.
    await expect(_runSession(ws, bot, SESSION_CTX, reader)).resolves.toBeUndefined();
    expect(bot.events).toContain("match_end");
  });

  it("safe-fallbacks when bot.decide throws", async () => {
    const reader = new ScriptedReader([
      { type: "hello", match_id: "m_test", seq: 1 },
      {
        type: "turn_request",
        match_id: "m_test",
        seq: 2,
        request_id: "req_x",
        valid_actions: ["fold", "check"],
        state: { hand_number: 1, phase: "preflop" },
      },
      { type: "match_end", match_id: "m_test", seq: 3 },
    ]);
    const ws = new CapturingSocket();
    await _runSession(ws, new FailingBot(), SESSION_CTX, reader);
    const turnAction = ws.sentParsed.find((m) => m.type === "turn_action");
    // check is legal in valid_actions, so fallback picks check
    expect(turnAction?.action).toBe("check");
    expect(turnAction?.request_id).toBe("req_x");
  });

  it("retries with safe fallback on action_rejected", async () => {
    const reader = new ScriptedReader([
      { type: "hello", match_id: "m_test", seq: 1 },
      {
        type: "action_rejected",
        match_id: "m_test",
        seq: 2,
        request_id: "req_y",
        valid_actions: ["fold", "check"],
      },
      { type: "match_end", match_id: "m_test", seq: 3 },
    ]);
    const ws = new CapturingSocket();
    await _runSession(ws, new RecordingBot(), SESSION_CTX, reader);
    const turnAction = ws.sentParsed.find((m) => m.type === "turn_action");
    expect(turnAction?.action).toBe("check");
    expect(turnAction?.request_id).toBe("req_y");
  });

  it("ignores unknown message types (forward-compat)", async () => {
    const reader = new ScriptedReader([
      { type: "hello", match_id: "m_test", seq: 1 },
      { type: "future_event_we_dont_know_about", match_id: "m_test", seq: 2 },
      { type: "match_end", match_id: "m_test", seq: 3 },
    ]);
    const ws = new CapturingSocket();
    await expect(
      _runSession(ws, new RecordingBot(), SESSION_CTX, reader),
    ).resolves.toBeUndefined();
  });
});
