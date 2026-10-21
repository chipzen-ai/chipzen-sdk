/**
 * Core data models — Action, GameState, Card.
 *
 * Field naming uses idiomatic camelCase for the SDK's user-facing
 * surface. The on-the-wire JSON the protocol uses is snake_case
 * (Layer 1 / Layer 2 spec); the parsers in this module translate.
 */

import {
  BIGBADBOTS_STATE_SHAPE,
  FIGHT_ACTIONS,
  parseBigBadBotsTurnRequest,
  type BigBadBotsActionKind,
  type BigBadBotsTurnState,
  type FightActionKind,
} from "./bigbadbots.js";

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
 * validate the action vs. amount invariants for you. Big Bad Bots answers
 * are built with `Action.chooseBody`, `Action.bid`, `Action.fit` and
 * `Action.act`.
 */
export class Action {
  private constructor(
    public readonly action: ActionKind | BigBadBotsActionKind,
    public readonly amount?: number,
    private readonly extraParams?: Readonly<Record<string, unknown>>,
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

  // ------------------------------------------------------------------
  // Big Bad Bots actions (docs/protocol/BIGBADBOTS-GAME-STATE-PROTOCOL.md
  // section 4.4). Every one emits a `params` object, never a bespoke
  // top-level key, and `params` admits no keys beyond the ones below.
  // ------------------------------------------------------------------

  /**
   * Big Bad Bots body shop: buy one body from `BodyShopState.offer`.
   *
   * Emits `{"action": "choose_body", "params": {"body": <body id>}}`. A body
   * that is not on the offer is corrected to the cheapest one on it, not
   * rejected.
   */
  static chooseBody(body: string): Action {
    return new Action("choose_body", undefined, { body: String(body) });
  }

  /**
   * Big Bad Bots auction: a sealed bid on `AuctionState.lot`.
   *
   * `amount` is clamped by the server to `0..me.credits`; `0` passes. Emits
   * `{"action": "bid", "params": {"amount": <integer>}}`.
   *
   * @throws TypeError if `amount` is not an integer (the wire wants a JSON
   * integer; a bid of `12.5` is malformed and rejected).
   */
  static bid(amount: number): Action {
    if (typeof amount !== "number" || !Number.isSafeInteger(amount)) {
      throw new TypeError(`bid amount must be an integer, got ${String(amount)}`);
    }
    return new Action("bid", undefined, { amount });
  }

  /**
   * Big Bad Bots fitting: put owned parts into the body's slots.
   *
   * `fitting` maps a slot index (into `FittingState.slots`) to a part id. At
   * most one part per slot, of the slot's type. `null` / `{}` fits nothing.
   * A `Map<number, string>` works too. Emits
   * `{"action": "fit", "params": {"fit": {"0": "capacitor"}}}`: slot indexes
   * go out as decimal strings, because JSON object keys are strings. An
   * entry naming a part you do not own, the wrong slot type or a missing
   * slot is dropped by the server; the rest are fitted.
   *
   * @throws RangeError if a slot index is not a non-negative integer.
   */
  static fit(
    fitting?: Readonly<Record<string | number, string>> | ReadonlyMap<string | number, string> | null,
  ): Action {
    const entries: Array<[string | number, string]> =
      fitting instanceof Map
        ? [...fitting.entries()]
        : Object.entries((fitting ?? {}) as Record<string, string>);
    const wire: Record<string, string> = {};
    for (const [slot, part] of entries) {
      const index =
        typeof slot === "number" ? (Number.isInteger(slot) ? String(slot) : "") : slot;
      if (!/^(0|[1-9][0-9]*)$/.test(index)) {
        throw new RangeError(
          `fit slot index must be a non-negative integer, got ${JSON.stringify(slot)}`,
        );
      }
      wire[index] = String(part);
    }
    return new Action("fit", undefined, { fit: wire });
  }

  /**
   * Big Bad Bots fight: this tick's action, one of the ten fight actions
   * (`"idle"`, `"move_in"`, `"move_out"`, `"block"`, `"punch_high"`,
   * `"uppercut"`, `"kick_high"`, `"kick_low"`, `"throw"`, `"special"`).
   * Emits `{"action": <move>, "params": {}}`. A move the fighter cannot
   * perform now plays as `idle`, or changes nothing while it is busy.
   *
   * @throws RangeError if `move` is not a fight action.
   */
  static act(move: FightActionKind): Action {
    if (!(FIGHT_ACTIONS as readonly string[]).includes(move)) {
      throw new RangeError(
        `${JSON.stringify(move)} is not a Big Bad Bots fight action (${FIGHT_ACTIONS.join(", ")})`,
      );
    }
    return new Action(move);
  }

  /**
   * Serialize to the two-layer `turn_action` payload shape the server expects.
   *
   * Returns `{action, params}` where `params` carries the raise amount
   * for `raise`, the Big Bad Bots parameters for `choose_body` / `bid` /
   * `fit`, and is empty for everything else.
   */
  toWire(): { action: ActionKind | BigBadBotsActionKind; params: Record<string, unknown> } {
    const params: Record<string, unknown> = {};
    if (this.action === "raise" && this.amount !== undefined) {
      params.amount = this.amount;
    }
    if (this.extraParams) {
      for (const [key, value] of Object.entries(this.extraParams)) {
        params[key] = isPlainObject(value) ? { ...value } : value;
      }
    }
    return { action: this.action, params };
  }
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
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

// ---------------------------------------------------------------------------
// state_shape routing (LAYER2-COMMON section 2)
// ---------------------------------------------------------------------------

/**
 * The `state_shape`s this SDK parses with the poker {@link parseGameState}.
 * `null` is a server `hello` with no `game` descriptor, which means NLHE.
 * (The JS SDK does not implement the draw or OFC dialects.)
 */
export const POKER_STATE_SHAPES: ReadonlySet<string | null> = new Set([null]);

/**
 * The table's `state_shape` is one this SDK release cannot parse.
 *
 * LAYER2-COMMON section 2: a client MUST treat an unfamiliar `state_shape`
 * as "I cannot parse this table", never as "drop the keys I do not
 * recognise". This is that refusal.
 */
export class UnsupportedStateShapeError extends Error {
  constructor(public readonly stateShape: unknown) {
    super(
      `cannot parse state_shape ${JSON.stringify(stateShape) ?? String(stateShape)}: ` +
        "this SDK release does not know it",
    );
    this.name = "UnsupportedStateShapeError";
  }
}

/**
 * Return the `state_shape` a server `hello` advertises.
 *
 * `null` when the `hello` has no `game` descriptor, which means poker
 * (NLHE). Pass the result to {@link parseTurnRequest}.
 *
 * @throws UnsupportedStateShapeError when the `game` descriptor is present
 * but does not carry a string `state_shape`.
 */
export function stateShapeOf(serverHello: Record<string, unknown>): string | null {
  const game = serverHello.game;
  if (game === undefined || game === null) return null;
  const shape = isPlainObject(game) ? game.state_shape : undefined;
  if (typeof shape !== "string") throw new UnsupportedStateShapeError(shape ?? null);
  return shape;
}

/**
 * Parse a `turn_request` into the model for the table's `state_shape`.
 *
 * - `null` (NLHE): a {@link GameState}, exactly as {@link parseGameState} builds it.
 * - `"bigbadbots"`: the Big Bad Bots model for the call
 *   (`parseBigBadBotsTurnRequest`).
 *
 * @throws UnsupportedStateShapeError for any other shape: there is no
 * fallback to the poker parser.
 * @throws StateParseError when a Big Bad Bots payload is malformed.
 */
export function parseTurnRequest(
  message: Record<string, unknown>,
  stateShape: string | null = null,
): GameState | BigBadBotsTurnState {
  if (POKER_STATE_SHAPES.has(stateShape)) return parseGameState(message as RawTurnRequest);
  if (stateShape === BIGBADBOTS_STATE_SHAPE) return parseBigBadBotsTurnRequest(message);
  throw new UnsupportedStateShapeError(stateShape);
}
