/**
 * chipzen-ai/chipzen-sdk#194: `yourSeat` and `dealerSeat` through a whole
 * session, for every poker-family game.
 *
 * A hold'em or 27TD `turn_request.state` carries neither `your_seat` nor
 * `dealer_seat` (POKER-GAME-STATE-PROTOCOL sections 3.3 and 5.9,
 * DRAW27-GAME-STATE-PROTOCOL section 3.3): the seat arrives once in
 * `match_start` (the `is_self` entry of `seats`) and the button on every
 * `round_start.state`. The session loop has to carry both into each turn, as
 * the Python loop does. An OFC turn state carries both itself, and that value
 * wins.
 *
 * Every session here seats the bot in seat 1 and moves the button each round,
 * so a loop that never tracks them (both stuck at 0) fails.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Bot } from "../src/bot.js";
import { _runSession, type AsyncMessageReader, type SessionWebSocket } from "../src/client.js";
import {
  _draw27MatchStart,
  _draw27RoundStart,
  _draw27ServerHello,
  _draw27TurnRequest,
  _ofcMatchStart,
  _ofcRoundStart,
  _ofcServerHello,
  _ofcTurnRequest,
} from "../src/conformance.js";
import { Action, parseGameState, parseTurnRequest, type GameState } from "../src/models.js";

type Json = Record<string, unknown>;

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
}

/** Records the seat context of every state it is asked to decide. */
class SeatBot extends Bot {
  readonly seen: GameState[] = [];
  decide(state: GameState): Action {
    this.seen.push(state);
    if (state.validActions.includes("check")) return Action.check();
    if (state.validActions.includes("call")) return Action.call();
    return Action.fold();
  }
}

const SEATS_BOT_IN_1 = [
  { seat: 0, participant_id: "p0", display_name: "Opp", is_self: false },
  { seat: 1, participant_id: "p1", display_name: "You", is_self: true },
];

/** The button moves every round: 0, 1, 0. */
const BUTTONS = [0, 1, 0];

async function play(frames: Json[], supportedGames?: readonly string[]): Promise<SeatBot> {
  const bot = new SeatBot();
  await _runSession(
    new CapturingSocket(),
    bot,
    {
      matchId: "m_conformance_test",
      token: "t",
      ticket: null,
      clientName: "test",
      clientVersion: "0.0.0",
      ...(supportedGames ? { supportedGames } : {}),
    },
    new ScriptedReader(frames),
  );
  return bot;
}

function seatsSeen(bot: SeatBot): Array<[number, number]> {
  return bot.seen.map((s) => [s.yourSeat, s.dealerSeat]);
}

// ---------------------------------------------------------------------------
// Hold'em frames
// ---------------------------------------------------------------------------

function holdemHello(): Json {
  return { type: "hello", match_id: "m_conformance_test", seq: 1, selected_version: "1.0" };
}

function holdemMatchStart(seq: number): Json {
  return {
    type: "match_start",
    match_id: "m_conformance_test",
    seq,
    seats: SEATS_BOT_IN_1,
    game_config: {
      variant: "nlhe",
      starting_stack: 1000,
      small_blind: 5,
      big_blind: 10,
      ante: 0,
      num_players: 2,
    },
    turn_timeout_ms: 5000,
    your_seat: 1,
  };
}

function holdemRoundStart(seq: number, hand: number, dealer: number): Json {
  return {
    type: "round_start",
    match_id: "m_conformance_test",
    seq,
    round_id: `r_${hand}`,
    round_number: hand,
    state: {
      hand_number: hand,
      dealer_seat: dealer,
      your_hole_cards: ["Ah", "Kd"],
      pot: 15,
      post_blind_stacks: [995, 990],
      stacks: [1000, 1000],
      deck_commitment: "",
    },
  };
}

/** A hold'em turn state, which by protocol has no your_seat / dealer_seat. */
function holdemTurnRequest(seq: number, hand: number): Json {
  return {
    type: "turn_request",
    match_id: "m_conformance_test",
    seq,
    seat: 1,
    request_id: `req_${hand}`,
    timeout_ms: 5000,
    valid_actions: ["fold", "call", "raise"],
    state: {
      hand_number: hand,
      phase: "preflop",
      board: [],
      your_hole_cards: ["Ah", "Kd"],
      pot: 15,
      your_stack: 990,
      opponent_stacks: [995],
      to_call: 5,
      min_raise: 20,
      max_raise: 1000,
      action_history: [],
    },
  };
}

function holdemMatch(): Json[] {
  const frames: Json[] = [holdemHello(), holdemMatchStart(2)];
  let seq = 3;
  BUTTONS.forEach((dealer, i) => {
    frames.push(holdemRoundStart(seq++, i + 1, dealer));
    frames.push(holdemTurnRequest(seq++, i + 1));
  });
  frames.push({ type: "match_end", match_id: "m_conformance_test", seq });
  return frames;
}

describe("seat context through a session (#194)", () => {
  let errorSpy: ReturnType<typeof vi.spyOn>;
  beforeEach(() => {
    errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
  });
  afterEach(() => errorSpy.mockRestore());

  it("hold'em: yourSeat comes from match_start and dealerSeat from each round_start", async () => {
    const bot = await play(holdemMatch());
    expect(seatsSeen(bot)).toEqual([
      [1, 0],
      [1, 1],
      [1, 0],
    ]);
    expect(errorSpy).not.toHaveBeenCalled();
  });

  it("2-7 Triple Draw: the same, since its turn state carries neither seat", async () => {
    const frames: Json[] = [
      _draw27ServerHello(),
      { ..._draw27MatchStart(2), seats: SEATS_BOT_IN_1 },
    ];
    let seq = 3;
    BUTTONS.forEach((dealer, i) => {
      const roundStart = _draw27RoundStart(seq++);
      frames.push({
        ...roundStart,
        state: { ...(roundStart.state as Json), hand_number: i + 1, dealer_seat: dealer },
      });
      const turn = _draw27TurnRequest(seq++, `req_${i + 1}`);
      // The fixture's turn state carries neither seat, as the protocol says.
      expect(turn.state).not.toHaveProperty("your_seat");
      expect(turn.state).not.toHaveProperty("dealer_seat");
      frames.push({ ...turn, seat: 1 });
    });
    frames.push({ type: "match_end", match_id: "m_conformance_test", seq });

    const bot = await play(frames, ["draw27"]);
    expect(seatsSeen(bot)).toEqual([
      [1, 0],
      [1, 1],
      [1, 0],
    ]);
    expect(bot.seen.every((s) => s.isDrawPhase)).toBe(true);
    expect(errorSpy).not.toHaveBeenCalled();
  });

  it("OFC: the turn state's own your_seat / dealer_seat are used, and move with the button", async () => {
    const frames: Json[] = [_ofcServerHello(), _ofcMatchStart(2)];
    let seq = 3;
    BUTTONS.forEach((dealer, i) => {
      const roundStart = _ofcRoundStart(seq++);
      frames.push({
        ...roundStart,
        state: { ...(roundStart.state as Json), hand_number: i + 1, dealer_seat: dealer },
      });
      const turn = _ofcTurnRequest(seq++, `req_${i + 1}`);
      frames.push({
        ...turn,
        state: { ...(turn.state as Json), hand_number: i + 1, dealer_seat: dealer },
      });
    });
    frames.push({ type: "match_end", match_id: "m_conformance_test", seq });

    const bot = await play(frames, ["ofc"]);
    expect(seatsSeen(bot)).toEqual([
      [1, 0],
      [1, 1],
      [1, 0],
    ]);
  });

  it("OFC: the turn state's own values win over match_start / round_start", async () => {
    // The OFC button can freeze into a Fantasy Land hand (OFC protocol
    // section 5.6); whatever the turn state says is the truth for that turn.
    const roundStart = _ofcRoundStart(3);
    const turn = _ofcTurnRequest(4, "req_1");
    const bot = await play(
      [
        _ofcServerHello(),
        {
          ..._ofcMatchStart(2),
          seats: [{ seat: 0, participant_id: "p1", display_name: "You", is_self: true }],
        },
        { ...roundStart, state: { ...(roundStart.state as Json), dealer_seat: 1 } },
        { ...turn, state: { ...(turn.state as Json), your_seat: 1, dealer_seat: 0 } },
        { type: "match_end", match_id: "m_conformance_test", seq: 5 },
      ],
      ["ofc"],
    );
    expect(seatsSeen(bot)).toEqual([[1, 0]]);
  });

  it("a reconnect mid-match keeps the button from round_start, and the seat for later turns", async () => {
    const bot = await play([
      holdemHello(),
      holdemMatchStart(2),
      holdemRoundStart(3, 1, 0),
      holdemTurnRequest(4, 1),
      holdemRoundStart(5, 2, 1),
      {
        type: "reconnected",
        match_id: "m_conformance_test",
        seq: 6,
        round_number: 2,
        seats: SEATS_BOT_IN_1,
        pending_request: holdemTurnRequest(0, 2),
      },
      holdemRoundStart(7, 3, 0),
      holdemTurnRequest(8, 3),
      { type: "match_end", match_id: "m_conformance_test", seq: 9 },
    ]);
    expect(seatsSeen(bot)).toEqual([
      [1, 0],
      [1, 1],
      [1, 0],
    ]);
  });

  it("a session that re-attaches with reconnected (no match_start) keeps the re-learned seat", async () => {
    const bot = await play([
      holdemHello(),
      {
        type: "reconnected",
        match_id: "m_conformance_test",
        seq: 2,
        round_number: 1,
        seats: SEATS_BOT_IN_1,
      },
      holdemRoundStart(3, 2, 1),
      holdemTurnRequest(4, 2),
      holdemRoundStart(5, 3, 0),
      holdemTurnRequest(6, 3),
      { type: "match_end", match_id: "m_conformance_test", seq: 7 },
    ]);
    expect(seatsSeen(bot)).toEqual([
      [1, 1],
      [1, 0],
    ]);
  });
});

describe("parseGameState seat context (#194)", () => {
  it("takes the seat context for a state without your_seat / dealer_seat", () => {
    const state = parseGameState(holdemTurnRequest(1, 1), { yourSeat: 1, dealerSeat: 1 });
    expect([state.yourSeat, state.dealerSeat]).toEqual([1, 1]);
  });

  it("defaults to 0 / 0 with no seat context, as before", () => {
    const state = parseGameState(holdemTurnRequest(1, 1));
    expect([state.yourSeat, state.dealerSeat]).toEqual([0, 0]);
  });

  it("lets the state's own values win, including a 0", () => {
    const turn = _ofcTurnRequest(1, "req_1");
    const state = parseGameState(
      { ...turn, state: { ...(turn.state as Json), your_seat: 0, dealer_seat: 0 } },
      { yourSeat: 1, dealerSeat: 1 },
    );
    expect([state.yourSeat, state.dealerSeat]).toEqual([0, 0]);
  });

  it("parseTurnRequest passes the seat context through for poker shapes", () => {
    const state = parseTurnRequest(holdemTurnRequest(1, 1), "27tripledraw", {
      yourSeat: 1,
      dealerSeat: 0,
    }) as GameState;
    expect([state.yourSeat, state.dealerSeat]).toEqual([1, 0]);
  });
});
