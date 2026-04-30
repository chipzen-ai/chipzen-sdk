/**
 * Core data models — Action, GameState, Card.
 *
 * Field naming uses idiomatic camelCase for the SDK's user-facing
 * surface. The on-the-wire JSON the protocol uses is snake_case
 * (Layer 1 / Layer 2 spec); the parsers in this module translate.
 */

// ---------------------------------------------------------------------------
// Card
// ---------------------------------------------------------------------------

/**
 * A standard playing card. `rank` is one of `2`-`9`, `T`, `J`, `Q`,
 * `K`, `A`. `suit` is one of `h` (hearts), `d` (diamonds), `c`
 * (clubs), `s` (spades).
 */
export interface Card {
  readonly rank: string;
  readonly suit: string;
}

const VALID_RANKS = new Set([
  "2", "3", "4", "5", "6", "7", "8", "9", "T", "J", "Q", "K", "A",
]);
const VALID_SUITS = new Set(["h", "d", "c", "s"]);

/**
 * Parse a card from its 2-character wire representation, e.g. `"Ah"`.
 *
 * Throws `Error` on malformed input; the wire format is
 * always exactly 2 characters with a valid rank + suit.
 */
export function cardFromString(s: string): Card {
  if (typeof s !== "string" || s.length !== 2) {
    throw new Error(`Invalid card string: ${JSON.stringify(s)} (expected 2 chars)`);
  }
  const rank = s[0]!;
  const suit = s[1]!;
  if (!VALID_RANKS.has(rank)) {
    throw new Error(`Invalid card rank: ${JSON.stringify(rank)} in ${JSON.stringify(s)}`);
  }
  if (!VALID_SUITS.has(suit)) {
    throw new Error(`Invalid card suit: ${JSON.stringify(suit)} in ${JSON.stringify(s)}`);
  }
  return { rank, suit };
}

/** Render a card back to its 2-character wire form. */
export function cardToString(c: Card): string {
  return `${c.rank}${c.suit}`;
}

// ---------------------------------------------------------------------------
// Action
// ---------------------------------------------------------------------------

export type ActionKind = "fold" | "check" | "call" | "raise" | "all_in";

/**
 * The action a bot returns from `decide()`.
 *
 * Construct via the static factories (`Action.fold()` etc.) — they
 * validate the action vs. amount invariants for you.
 */
export class Action {
  private constructor(
    public readonly action: ActionKind,
    public readonly amount?: number,
  ) {
    if (action === "raise") {
      if (amount === undefined || !Number.isFinite(amount) || amount < 0) {
        throw new Error(
          `Action.raiseTo requires a non-negative finite amount; got ${amount}`,
        );
      }
    } else if (amount !== undefined) {
      throw new Error(
        `Only raise actions take an amount; ${action} got amount=${amount}`,
      );
    }
  }

  static fold(): Action {
    return new Action("fold");
  }

  static check(): Action {
    return new Action("check");
  }

  static call(): Action {
    return new Action("call");
  }

  static raiseTo(amount: number): Action {
    return new Action("raise", amount);
  }

  static allIn(): Action {
    return new Action("all_in");
  }

  /**
   * Serialize to the two-layer `turn_action` payload shape the server expects.
   *
   * Returns `{action, params}` where `params` carries the raise amount
   * for `raise` and is empty for everything else.
   */
  toWire(): { action: ActionKind; params: Record<string, unknown> } {
    const params: Record<string, unknown> = {};
    if (this.action === "raise" && this.amount !== undefined) {
      params.amount = this.amount;
    }
    return { action: this.action, params };
  }
}

// ---------------------------------------------------------------------------
// GameState
// ---------------------------------------------------------------------------

/**
 * A single entry from `state.actionHistory`. Synthetic blind/ante
 * entries (`post_small_blind`, `post_big_blind`, `post_ante`) appear
 * here too — the server generates them; bots do not submit them.
 */
export interface ActionHistoryEntry {
  readonly seat: number;
  readonly action: string;
  readonly amount?: number;
  readonly isTimeout?: boolean;
}

/**
 * Built from the server's `turn_request` message. The parser in
 * `parseGameState` converts the wire-format snake_case to the
 * camelCase fields below.
 */
export interface GameState {
  readonly handNumber: number;
  readonly phase: "preflop" | "flop" | "turn" | "river";
  readonly holeCards: readonly Card[];
  readonly board: readonly Card[];
  readonly pot: number;
  readonly yourStack: number;
  readonly opponentStacks: readonly number[];
  readonly yourSeat: number;
  readonly dealerSeat: number;
  readonly toCall: number;
  readonly minRaise: number;
  readonly maxRaise: number;
  readonly validActions: readonly string[];
  readonly actionHistory: readonly ActionHistoryEntry[];
  readonly roundId: string;
  readonly requestId: string;
}

interface RawTurnRequest {
  request_id?: string;
  round_id?: string;
  valid_actions?: string[];
  state?: Record<string, unknown> | null;
}

/**
 * Parse a `turn_request` envelope into a `GameState`.
 *
 * The wire shape is documented in
 * `docs/protocol/POKER-GAME-STATE-PROTOCOL.md`. All fields default
 * to safe values when absent — but a real server always sends them.
 */
export function parseGameState(message: RawTurnRequest): GameState {
  const state = (message?.state ?? {}) as Record<string, unknown>;
  const holeStrs = (state["your_hole_cards"] as string[] | undefined) ?? [];
  const boardStrs = (state["board"] as string[] | undefined) ?? [];
  const validActions =
    (message?.valid_actions as string[] | undefined) ??
    (state["valid_actions"] as string[] | undefined) ??
    [];
  const actionHistoryRaw = (state["action_history"] as RawHistoryEntry[] | undefined) ?? [];

  return {
    handNumber: numberOrZero(state["hand_number"]),
    phase: (state["phase"] as GameState["phase"] | undefined) ?? "preflop",
    holeCards: holeStrs.map(cardFromString),
    board: boardStrs.map(cardFromString),
    pot: numberOrZero(state["pot"]),
    yourStack: numberOrZero(state["your_stack"]),
    opponentStacks: ((state["opponent_stacks"] as number[] | undefined) ?? []).map(Number),
    yourSeat: numberOrZero(state["your_seat"]),
    dealerSeat: numberOrZero(state["dealer_seat"]),
    toCall: numberOrZero(state["to_call"]),
    minRaise: numberOrZero(state["min_raise"]),
    maxRaise: numberOrZero(state["max_raise"]),
    validActions,
    actionHistory: actionHistoryRaw.map(parseHistoryEntry),
    roundId: (message?.round_id as string | undefined) ?? "",
    requestId: (message?.request_id as string | undefined) ?? "",
  };
}

interface RawHistoryEntry {
  seat?: number;
  action?: string;
  amount?: number;
  is_timeout?: boolean;
}

function parseHistoryEntry(raw: RawHistoryEntry): ActionHistoryEntry {
  const entry: { seat: number; action: string; amount?: number; isTimeout?: boolean } = {
    seat: numberOrZero(raw.seat),
    action: raw.action ?? "",
  };
  if (typeof raw.amount === "number") entry.amount = raw.amount;
  if (typeof raw.is_timeout === "boolean") entry.isTimeout = raw.is_timeout;
  return entry;
}

function numberOrZero(v: unknown): number {
  return typeof v === "number" && Number.isFinite(v) ? v : 0;
}
