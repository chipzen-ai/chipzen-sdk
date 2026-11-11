// Seat and button after a real reconnect (chipzen-ai/chipzen-sdk#199).
//
// The match server sends no `reconnected` frame. A reconnecting bot runs the
// handshake again and the new session receives the next game frames, so it
// never sees `match_start` (the seat) or the current hand's `round_start`
// (the button). The SDK must then take the seat from `turn_request.seat` and
// work out the button from the hand's blinds. Mirrors the Python SDK's
// tests/test_reconnect_seat.py.

import { describe, expect, it } from "vitest";

import { Bot } from "../src/bot.js";
import { _runSession, type AsyncMessageReader, type SessionWebSocket } from "../src/client.js";
import { _deriveDealerSeat, _turnSeatContext, Action, type GameState } from "../src/models.js";

const MATCH_ID = "m_reconnect_199";

class ScriptedReader implements AsyncMessageReader {
  private idx = 0;
  constructor(private readonly messages: Array<Record<string, unknown>>) {}
  async next(): Promise<string | null> {
    if (this.idx >= this.messages.length) return null;
    return JSON.stringify(this.messages[this.idx++]);
  }
}

class CapturingSocket implements SessionWebSocket {
  readonly sent: Array<Record<string, unknown>> = [];
  send(data: string): void {
    this.sent.push(JSON.parse(data) as Record<string, unknown>);
  }
  close(): void {
    /* no-op */
  }
}

class SeatRecordingBot extends Bot {
  states: GameState[] = [];
  decide(state: GameState): Action {
    this.states.push(state);
    return state.validActions.includes("check") ? Action.check() : Action.fold();
  }
}

const SESSION_CTX = {
  matchId: MATCH_ID,
  token: "t",
  ticket: null,
  clientName: "test",
  clientVersion: "0",
};

const hello = { type: "hello", match_id: MATCH_ID, seq: 1, capabilities: ["reconnect"] };

function entry(seat: number, action: string, amount: number): Record<string, unknown> {
  return { seat, action, amount, phase: "preflop" };
}

function turn(
  seq: number,
  seat: number,
  hand: number,
  stacks: number[],
  history: Array<Record<string, unknown>>,
): Record<string, unknown> {
  return {
    type: "turn_request",
    match_id: MATCH_ID,
    seq,
    seat,
    request_id: `req_${seq}`,
    timeout_ms: 5000,
    valid_actions: ["fold", "call", "raise"],
    state: {
      hand_number: hand,
      phase: "preflop",
      board: [],
      your_hole_cards: ["Ah", "Kd"],
      pot: 25,
      your_stack: 995,
      opponent_stacks: stacks,
      to_call: 5,
      min_raise: 20,
      max_raise: 995,
      action_history: history,
    },
  };
}

function roundStart(seq: number, hand: number, dealer: number): Record<string, unknown> {
  return {
    type: "round_start",
    match_id: MATCH_ID,
    seq,
    round_id: `r_${hand}`,
    round_number: hand,
    state: { hand_number: hand, dealer_seat: dealer, your_hole_cards: ["2c", "7d"] },
  };
}

async function run(messages: Array<Record<string, unknown>>) {
  const ws = new CapturingSocket();
  const bot = new SeatRecordingBot();
  await _runSession(ws, bot, SESSION_CTX, new ScriptedReader(messages));
  return { bot, ws };
}

describe("seat and button after a reconnect (#199)", () => {
  it("reads seat and button without match_start or round_start", async () => {
    // A 3-seat table: the bot is seat 2 and seat 1 has the button. The
    // reconnected session used to read seat 0 and button 0.
    const history = [
      entry(2, "post_small_blind", 5),
      entry(0, "post_big_blind", 10),
      entry(1, "call", 10),
    ];
    const { bot, ws } = await run([
      hello,
      // seq continues from the dropped connection
      turn(41, 2, 7, [990, 990], history),
      roundStart(45, 8, 2),
      turn(46, 2, 8, [995, 990], [entry(0, "post_small_blind", 5), entry(1, "post_big_blind", 10)]),
    ]);
    expect(bot.states.map((s) => [s.yourSeat, s.dealerSeat])).toEqual([
      [2, 1],
      [2, 2],
    ]);
    expect(ws.sent.filter((m) => m.type === "turn_action").map((m) => m.request_id)).toEqual([
      "req_41",
      "req_46",
    ]);
  });

  it("heads-up: the button posts the small blind", async () => {
    const history = [entry(1, "post_small_blind", 5), entry(0, "post_big_blind", 10)];
    const { bot } = await run([hello, turn(9, 1, 3, [990], history)]);
    expect([bot.states[0]?.yourSeat, bot.states[0]?.dealerSeat]).toEqual([1, 1]);
  });

  it("leaves a session that saw match_start and round_start unchanged", async () => {
    const matchStart = {
      type: "match_start",
      match_id: MATCH_ID,
      seq: 2,
      seats: [
        { seat: 0, is_self: false },
        { seat: 1, is_self: true },
      ],
      game_config: {},
    };
    const history = [entry(0, "post_small_blind", 5), entry(1, "post_big_blind", 10)];
    const { bot } = await run([hello, matchStart, roundStart(3, 1, 1), turn(4, 1, 1, [995], history)]);
    expect([bot.states[0]?.yourSeat, bot.states[0]?.dealerSeat]).toEqual([1, 1]);
  });
});

describe("_deriveDealerSeat", () => {
  it("skips busted seats (protocol section 5.9 example)", () => {
    const state = {
      opponent_stacks: [1500, 0, 0, 1200, 0], // seats 0, 1, 2, 4, 5
      action_history: [entry(3, "post_small_blind", 5), entry(4, "post_big_blind", 10)],
    };
    expect(_deriveDealerSeat(state, 3)).toBe(0);
  });

  it("wraps, and counts an all-in seat as live", () => {
    const state = {
      opponent_stacks: [990, 0, 0, 0], // seats 1, 2, 3, 4
      action_history: [
        entry(0, "post_small_blind", 5),
        entry(1, "post_big_blind", 10),
        entry(4, "raise", 300),
      ],
    };
    expect(_deriveDealerSeat(state, 0)).toBe(4);
  });

  it("heads-up at a six-seat table", () => {
    const state = {
      opponent_stacks: [0, 0, 1990, 0, 0], // seats 0, 1, 3, 4, 5
      action_history: [entry(3, "post_small_blind", 5), entry(2, "post_big_blind", 10)],
    };
    expect(_deriveDealerSeat(state, 2)).toBe(3);
  });

  it("returns null without a small blind", () => {
    expect(_deriveDealerSeat({ opponent_stacks: [100], action_history: [] }, 0)).toBeNull();
    expect(_deriveDealerSeat({ your_rows: {} }, 1)).toBeNull();
  });
});

describe("_turnSeatContext", () => {
  const message = turn(5, 2, 1, [990, 990], [
    entry(2, "post_small_blind", 5),
    entry(0, "post_big_blind", 10),
  ]);
  const base = { yourSeat: 0, dealerSeat: 0 };

  it("fills seat and button the session never saw", () => {
    expect(_turnSeatContext(message, { ...base, seatKnown: false, dealerKnown: false })).toEqual({
      yourSeat: 2,
      dealerSeat: 1,
    });
  });

  it("keeps values the session saw", () => {
    expect(_turnSeatContext(message, { ...base, seatKnown: true, dealerKnown: true })).toEqual(base);
  });

  it("falls back to today's values with no Layer 1 seat and no blinds", () => {
    expect(
      _turnSeatContext({ state: {} }, { ...base, seatKnown: false, dealerKnown: false }),
    ).toEqual(base);
  });
});
