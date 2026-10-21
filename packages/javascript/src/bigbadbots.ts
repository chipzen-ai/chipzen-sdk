/**
 * Typed models for the Big Bad Bots Layer 2 dialect (`state_shape: "bigbadbots"`).
 *
 * A Big Bad Bots match is two stages, gear-up (body shop, auction, fitting)
 * and the fight, and every step asks both seats at once. Each step's
 * `turn_request.state` has its own shape, so each gets its own model, picked
 * by `state.phase`:
 *
 * - `choose_body` (phase `body_shop`): {@link BodyShopState}
 * - `bid` (phase `auction`, once per lot): {@link AuctionState}
 * - `fit` (phase `fitting`): {@link FittingState}
 * - `act` (phase `fight`, once per tick): {@link FightState}
 *
 * The answers are built with `Action.chooseBody`, `Action.bid`, `Action.fit`
 * and `Action.act`.
 *
 * Mirrors the Python SDK's `chipzen.bigbadbots`: the same model names, with
 * the JS SDK's camelCase field names (`lot_index` is `lotIndex`). Maps keyed
 * by game data (`stats`, `bonus`, `effects`, `attrs`, `buffs`) keep their
 * wire keys.
 *
 * **These parsers are strict, on purpose.** A client that declares
 * `bigbadbots` in `supported_games` asserts it implements this dialect, and
 * every field below is marked required by the spec, so a missing or mistyped
 * field throws {@link StateParseError} instead of reaching the bot as a
 * silent default. Keys this release does not know are ignored: the spec only
 * ever adds keys (LAYER2-COMMON section 3, rule 5). One difference from
 * Python: `JSON.parse` reads `12.0` as `12`, so JS cannot tell that a
 * whole-number float was sent where the spec wants an integer.
 *
 * See `docs/protocol/BIGBADBOTS-GAME-STATE-PROTOCOL.md` for the
 * authoritative spec. Section numbers below refer to it.
 */

/** The `game.state_shape` (and `game_type` and `variant`) of a Big Bad Bots table (section 1.1). */
export const BIGBADBOTS_STATE_SHAPE = "bigbadbots";

/** The gear-up calls; each is also its answer's `action` string (section 3.1). */
export const GEARUP_ACTIONS = ["choose_body", "bid", "fit"] as const;

/** The ten fight actions, every one of them in every `act` request's `valid_actions` (section 4.3.4). */
export const FIGHT_ACTIONS = [
  "idle",
  "move_in",
  "move_out",
  "block",
  "punch_high",
  "uppercut",
  "kick_high",
  "kick_low",
  "throw",
  "special",
] as const;

/** The four `state.phase` strings, in match order. */
export const PHASES = ["body_shop", "auction", "fitting", "fight"] as const;

export type GearUpActionKind = (typeof GEARUP_ACTIONS)[number];
export type FightActionKind = (typeof FIGHT_ACTIONS)[number];
export type BigBadBotsActionKind = GearUpActionKind | FightActionKind;
export type BigBadBotsPhase = (typeof PHASES)[number];

/** A Big Bad Bots payload is missing a required field, or one is mistyped. */
export class StateParseError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "StateParseError";
  }
}

// ---------------------------------------------------------------------------
// Strict field readers
// ---------------------------------------------------------------------------
//
// Section 4: every integer is a JSON integer and every float is finite.

type Json = Record<string, unknown>;

function isObject(value: unknown): value is Json {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function isInt(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value);
}

function isNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function show(value: unknown): string {
  try {
    return JSON.stringify(value) ?? String(value);
  } catch {
    return String(value);
  }
}

function need(obj: Json, key: string, where: string): unknown {
  if (!Object.prototype.hasOwnProperty.call(obj, key)) {
    throw new StateParseError(`${where}: missing required field ${JSON.stringify(key)}`);
  }
  return obj[key];
}

function fail(where: string, key: string, expected: string, value: unknown): StateParseError {
  return new StateParseError(`${where}.${key}: expected ${expected}, got ${show(value)}`);
}

function readInt(obj: Json, key: string, where: string): number {
  const value = need(obj, key, where);
  if (!isInt(value)) throw fail(where, key, "an integer", value);
  return value;
}

function readOptInt(obj: Json, key: string, where: string): number | null {
  const value = need(obj, key, where);
  if (value !== null && !isInt(value)) throw fail(where, key, "an integer or null", value);
  return value;
}

function readNumber(obj: Json, key: string, where: string): number {
  const value = need(obj, key, where);
  if (!isNumber(value)) throw fail(where, key, "a finite number", value);
  return value;
}

function readStr(obj: Json, key: string, where: string): string {
  const value = need(obj, key, where);
  if (typeof value !== "string") throw fail(where, key, "a string", value);
  return value;
}

function readOptStr(obj: Json, key: string, where: string): string | null {
  const value = need(obj, key, where);
  if (value !== null && typeof value !== "string") {
    throw fail(where, key, "a string or null", value);
  }
  return value;
}

function readBool(obj: Json, key: string, where: string): boolean {
  const value = need(obj, key, where);
  if (typeof value !== "boolean") throw fail(where, key, "a boolean", value);
  return value;
}

function readList(obj: Json, key: string, where: string): unknown[] {
  const value = need(obj, key, where);
  if (!Array.isArray(value)) throw fail(where, key, "an array", value);
  return value;
}

function readObject(obj: Json, key: string, where: string): Json {
  const value = need(obj, key, where);
  if (!isObject(value)) throw fail(where, key, "an object", value);
  return value;
}

function readStrList(obj: Json, key: string, where: string): readonly string[] {
  const items = readList(obj, key, where);
  if (!items.every((item) => typeof item === "string")) {
    throw fail(where, key, "an array of strings", items);
  }
  return Object.freeze([...(items as string[])]);
}

function readIntList(obj: Json, key: string, where: string): readonly number[] {
  const items = readList(obj, key, where);
  if (!items.every(isInt)) throw fail(where, key, "an array of integers", items);
  return Object.freeze([...(items as number[])]);
}

function readNumberList(obj: Json, key: string, where: string): readonly number[] {
  const items = readList(obj, key, where);
  if (!items.every(isNumber)) throw fail(where, key, "an array of finite numbers", items);
  return Object.freeze([...(items as number[])]);
}

function readNumberMap(obj: Json, key: string, where: string): Readonly<Record<string, number>> {
  const mapping = readObject(obj, key, where);
  if (!Object.values(mapping).every(isNumber)) {
    throw fail(where, key, "an object of finite numbers", mapping);
  }
  return Object.freeze({ ...(mapping as Record<string, number>) });
}

function readObjects(obj: Json, key: string, where: string): Json[] {
  const items = readList(obj, key, where);
  if (!items.every(isObject)) throw fail(where, key, "an array of objects", items);
  return items as Json[];
}

/** A read-only deep copy of a JSON value. */
function deepFreeze<T>(value: T): T {
  if (Array.isArray(value)) {
    return Object.freeze(value.map(deepFreeze)) as unknown as T;
  }
  if (isObject(value)) {
    const copy: Json = {};
    for (const [k, v] of Object.entries(value)) copy[k] = deepFreeze(v);
    return Object.freeze(copy) as T;
  }
  return value;
}

/** The JSON form of a {@link deepFreeze} result (a mutable deep copy). */
function thaw(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(thaw);
  if (isObject(value)) {
    const copy: Json = {};
    for (const [k, v] of Object.entries(value)) copy[k] = thaw(v);
    return copy;
  }
  return value;
}

// ---------------------------------------------------------------------------
// Payload objects (section 4.3.5)
// ---------------------------------------------------------------------------

/** A body on this seat's offer, in the body shop. */
export interface Body {
  /** The body's id. */
  readonly id: string;
  /** Credits it costs. */
  readonly cost: number;
  /** Attribute name to base value. */
  readonly stats: Readonly<Record<string, number>>;
  /** The body's part slots, by slot type. A type can repeat. */
  readonly slots: readonly string[];
  /** The id of the body's special move. */
  readonly special: string;
}

/** A body-mod part: the lot on sale, or one this seat owns. */
export interface Part {
  /** The part's id. */
  readonly id: string;
  /** The slot type it fits. */
  readonly slot: string;
  /** Attribute name to the amount it adds (negative subtracts). */
  readonly bonus: Readonly<Record<string, number>>;
  /** Effect name to a number, for parts that change a rule rather than an attribute. Empty for most parts. */
  readonly effects: Readonly<Record<string, number>>;
}

/** One seat's gear-up state: `me` in full, or `opp` masked (section 5). */
export interface Side {
  /** Credits left. Public for both seats. */
  readonly credits: number;
  /** This seat's offer, by body id. Always `[]` for `opp`. */
  readonly offer: readonly string[];
  /** The chosen body, or `null` in the body shop. */
  readonly body: string | null;
  /** Parts won, by id. Public for both seats. */
  readonly owned: readonly string[];
  /** One `null` per slot of the chosen body (`[]` before one is chosen). The opponent's fitting is never shown. */
  readonly fitted: readonly null[];
}

/** One entry of `history`: a lot already sold, seat-relative. */
export interface SoldLot {
  /** The part that was sold. */
  readonly part: string;
  /** This seat's bid, as applied (after clamping). */
  readonly myBid: number;
  /** The opponent's bid, as applied. */
  readonly oppBid: number;
  /** `"me"`, `"opp"`, or `null` when both bid 0 and the lot went unsold. */
  readonly winner: "me" | "opp" | null;
}

/**
 * One fighter in the fight: `me` in full, or `opp` masked (section 5).
 *
 * While the opponent winds up a move, it shows as `action === "attack"` in
 * `phase === "startup"` with `framesLeft` `null`, and its `stamina`,
 * `specialUsed`, `lastAttack`, `repeats` and `buffs` keep their values from
 * before the move started.
 */
export interface Fighter {
  readonly body: string;
  readonly special: string;
  /** Position on the arena. */
  readonly x: number;
  readonly hp: number;
  /** The round's maximum hit points. */
  readonly maxHp: number;
  readonly stamina: number;
  /** The round's maximum stamina. */
  readonly maxStamina: number;
  /** A move or special id, `"hitstun"`, `"dazed"`, `"attack"` (the opponent winding up), or `null` when free. */
  readonly action: string | null;
  /** `"startup"`, `"active"`, `"recovery"`, `"hitstun"`, `"dazed"`, or `null` when free. */
  readonly phase: string | null;
  /** Ticks left in the current phase; `null` for the opponent during a wind-up. */
  readonly framesLeft: number | null;
  /** Whether the special has been used this round. */
  readonly specialUsed: boolean;
  /** The last attack started, for the repeated-move penalty. */
  readonly lastAttack: string | null;
  /** How many times in a row `lastAttack` has been used. */
  readonly repeats: number;
  /** The fighter's strike range, from its Reach. */
  readonly strikeRange: number;
  /** Distance per tick when moving, from its Speed. */
  readonly moveRate: number;
  /** The six attributes after fitting and live buffs. */
  readonly attrs: Readonly<Record<string, number>>;
  /** Active effect name to what is left of it: ticks for a timed effect, strikes for a per-strike one. */
  readonly buffs: Readonly<Record<string, number>>;
}

function parseBody(data: Json, where: string): Body {
  return Object.freeze({
    id: readStr(data, "id", where),
    cost: readInt(data, "cost", where),
    stats: readNumberMap(data, "stats", where),
    slots: readStrList(data, "slots", where),
    special: readStr(data, "special", where),
  });
}

function parsePart(data: Json, where: string): Part {
  return Object.freeze({
    id: readStr(data, "id", where),
    slot: readStr(data, "slot", where),
    bonus: readNumberMap(data, "bonus", where),
    effects: readNumberMap(data, "effects", where),
  });
}

function parseSide(data: Json, where: string): Side {
  const fitted = readList(data, "fitted", where);
  if (!fitted.every((slot) => slot === null)) {
    throw fail(where, "fitted", "an array of null", fitted);
  }
  return Object.freeze({
    credits: readInt(data, "credits", where),
    offer: readStrList(data, "offer", where),
    body: readOptStr(data, "body", where),
    owned: readStrList(data, "owned", where),
    fitted: Object.freeze([...(fitted as null[])]),
  });
}

function parseSoldLot(data: Json, where: string): SoldLot {
  const winner = readOptStr(data, "winner", where);
  if (winner !== "me" && winner !== "opp" && winner !== null) {
    throw fail(where, "winner", '"me", "opp" or null', winner);
  }
  return Object.freeze({
    part: readStr(data, "part", where),
    myBid: readInt(data, "my_bid", where),
    oppBid: readInt(data, "opp_bid", where),
    winner,
  });
}

function parseFighter(data: Json, where: string): Fighter {
  return Object.freeze({
    body: readStr(data, "body", where),
    special: readStr(data, "special", where),
    x: readNumber(data, "x", where),
    hp: readNumber(data, "hp", where),
    maxHp: readNumber(data, "max_hp", where),
    stamina: readNumber(data, "stamina", where),
    maxStamina: readNumber(data, "max_stamina", where),
    action: readOptStr(data, "action", where),
    phase: readOptStr(data, "phase", where),
    framesLeft: readOptInt(data, "frames_left", where),
    specialUsed: readBool(data, "special_used", where),
    lastAttack: readOptStr(data, "last_attack", where),
    repeats: readInt(data, "repeats", where),
    strikeRange: readNumber(data, "strike_range", where),
    moveRate: readNumber(data, "move_rate", where),
    attrs: readNumberMap(data, "attrs", where),
    buffs: readNumberMap(data, "buffs", where),
  });
}

// ---------------------------------------------------------------------------
// Game config (section 4.1) and the rule set (docs/RULESETS.md)
// ---------------------------------------------------------------------------

/** Where the rule-set spec lives: the id scheme, the lifecycle and the changelog. */
export const RULESETS_URL = "https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/RULESETS.md";

/**
 * `rounds.timeout_tiebreak` when a rule set leaves it out (`bigbadbots-1.0`):
 * at the tick limit the higher share of max HP wins (protocol section 6.2).
 */
export const DEFAULT_TIMEOUT_TIEBREAK = "hp_pct";

/** `match_start.game_config` at a Big Bad Bots table. */
export interface BigBadBotsGameConfig {
  /** Always `"bigbadbots"`. */
  readonly variant: string;
  /** The rule set this match is pinned to, e.g. `"bigbadbots-1.2"`. Fixed for the whole match. */
  readonly ruleSetId: string;
  /** Always `2`. */
  readonly numPlayers: number;
  /**
   * The rule set's full parameter table, by section, in its wire form
   * (read-only). Every call also gets it typed as `state.rules`; read the
   * numbers from there and never hard-code them. See {@link RULESETS_URL}.
   */
  readonly ruleSet: Readonly<Record<string, unknown>>;
}

/**
 * Parse a Big Bad Bots `match_start.game_config` object.
 *
 * @throws StateParseError when a field is missing or mistyped, or the variant is not `bigbadbots`.
 */
export function parseBigBadBotsGameConfig(
  data: Record<string, unknown>,
  where = "game_config",
): BigBadBotsGameConfig {
  const variant = readStr(data, "variant", where);
  if (variant !== BIGBADBOTS_STATE_SHAPE) {
    throw fail(where, "variant", JSON.stringify(BIGBADBOTS_STATE_SHAPE), variant);
  }
  return Object.freeze({
    variant,
    ruleSetId: readStr(data, "rule_set_id", where),
    numPlayers: readInt(data, "num_players", where),
    ruleSet: deepFreeze({ ...readObject(data, "rule_set", where) }),
  });
}

/** Parse the `game_config` of a `match_start` (or `reconnected`) message. */
export function gameConfigFromMatchStart(message: Record<string, unknown>): BigBadBotsGameConfig {
  if (!isObject(message)) throw new StateParseError("match_start: expected an object");
  return parseBigBadBotsGameConfig(readObject(message, "game_config", "match_start"));
}

/** `rule_set.gearup`: the budget and the size of the body shop and the auction. */
export interface GearUpRules {
  /** Credits each seat starts with, for the body and the auction. */
  readonly budget: number;
  /** How many bodies each seat is offered. */
  readonly bodyOfferSize: number;
  /** How many parts the auction sells. */
  readonly auctionLots: number;
}

/** `rule_set.arena`: the one-dimensional arena. */
export interface ArenaRules {
  /** Distance between the walls, in arena units. */
  readonly width: number;
  /** Each seat's start position, by seat. */
  readonly startX: readonly number[];
  /** The most the start positions shift each round (both by the same seeded amount). */
  readonly startJitter: number;
  /** The fighters never stand closer than this. */
  readonly minSeparation: number;
}

/** `rule_set.rounds`: ticks, round length and how a match is won. */
export interface RoundRules {
  readonly ticksPerSecond: number;
  /** The most ticks a round lasts. */
  readonly roundTicks: number;
  /** Round wins that take the match. */
  readonly roundsToWin: number;
  /** The most rounds a match plays, drawn rounds included. */
  readonly maxRounds: number;
  /** `"hp_pct"` (also what a rule set that leaves the key out plays) or `"damage"`. */
  readonly timeoutTiebreak: string;
  /** Closing walls: ticks with no damage before the walls start to close; `null` when the rule set has none. */
  readonly stallCloseAfter: number | null;
  /** How far each wall moves in per tick while closing. */
  readonly stallCloseRate: number | null;
  /** The walls stop closing this far apart. */
  readonly stallMinWidth: number | null;
  /** Whether the walls close in on a stalled round (protocol section 6.2). */
  readonly closingWalls: boolean;
}

/** `rule_set.forfeit`: disconnects and forfeits (protocol section 3.9). */
export interface ForfeitRules {
  /** Seconds of missed ticks before a seat forfeits the round. */
  readonly graceSeconds: number;
  /** Forfeited rounds that forfeit the match. */
  readonly roundsForMatch: number;
}

/** `rule_set.deadline`: the per-tick deadline and the time bank (sections 3.4, 3.5). */
export interface DeadlineRules {
  /** The per-tick deadline. Answer inside it and the time bank is never touched. */
  readonly deadlineMs: number;
  /** Each seat's time bank per round, reset every round. */
  readonly timeBankMs: number;
  /** What the server plays on a missed tick. */
  readonly defaultActionPolicy: string;
  /** What happens to an answer that arrives too late. */
  readonly lateAnswer: string;
  /** Whether missed ticks count toward the disconnect forfeit. */
  readonly missCountsTowardForfeit: boolean;
}

/**
 * The rule set this match is played under, as `match_start` sent it. Read-only.
 *
 * The sections every bot needs are typed (`gearup`, `arena`, `rounds`,
 * `forfeit`, `deadline`). Every section, including the bodies, parts, moves
 * and specials, is in `params` in its wire form.
 *
 * Read every number from here and never hard-code one: a new rule set
 * changes numbers, not code. See {@link RULESETS_URL}.
 */
export interface RuleSet {
  /** The rule set's id, `game_config.rule_set_id` (e.g. `"bigbadbots-1.2"`). */
  readonly id: string;
  /** The full parameter table, `game_config.rule_set`, deep read-only. */
  readonly params: Readonly<Record<string, unknown>>;
  readonly gearup: GearUpRules;
  readonly arena: ArenaRules;
  readonly rounds: RoundRules;
  readonly forfeit: ForfeitRules;
  readonly deadline: DeadlineRules;
}

function optional<T>(
  obj: Json,
  key: string,
  where: string,
  reader: (o: Json, k: string, w: string) => T,
): T | null {
  return Object.prototype.hasOwnProperty.call(obj, key) ? reader(obj, key, where) : null;
}

/**
 * Build the typed {@link RuleSet} from a `rule_set_id` and its parameter table.
 *
 * @throws StateParseError when a typed section is missing, or one of its keys is missing or mistyped.
 */
export function parseRuleSet(ruleSetId: string, ruleSet: Readonly<Record<string, unknown>>): RuleSet {
  const where = "rule_set";
  const table = ruleSet as Json;
  const gearup = readObject(table, "gearup", where);
  const arena = readObject(table, "arena", where);
  const rounds = readObject(table, "rounds", where);
  const forfeit = readObject(table, "forfeit", where);
  const deadline = readObject(table, "deadline", where);
  const g = "rule_set.gearup";
  const a = "rule_set.arena";
  const r = "rule_set.rounds";
  const f = "rule_set.forfeit";
  const d = "rule_set.deadline";
  const tiebreak = optional(rounds, "timeout_tiebreak", r, readStr);
  const stallCloseAfter = optional(rounds, "stall_close_after", r, readInt);
  return Object.freeze({
    id: ruleSetId,
    params: deepFreeze({ ...table }),
    gearup: Object.freeze({
      budget: readInt(gearup, "budget", g),
      bodyOfferSize: readInt(gearup, "body_offer_size", g),
      auctionLots: readInt(gearup, "auction_lots", g),
    }),
    arena: Object.freeze({
      width: readNumber(arena, "width", a),
      startX: readNumberList(arena, "start_x", a),
      startJitter: readNumber(arena, "start_jitter", a),
      minSeparation: readNumber(arena, "min_separation", a),
    }),
    rounds: Object.freeze({
      ticksPerSecond: readInt(rounds, "ticks_per_second", r),
      roundTicks: readInt(rounds, "round_ticks", r),
      roundsToWin: readInt(rounds, "rounds_to_win", r),
      maxRounds: readInt(rounds, "max_rounds", r),
      timeoutTiebreak: tiebreak ?? DEFAULT_TIMEOUT_TIEBREAK,
      stallCloseAfter,
      stallCloseRate: optional(rounds, "stall_close_rate", r, readNumber),
      stallMinWidth: optional(rounds, "stall_min_width", r, readNumber),
      closingWalls: stallCloseAfter !== null,
    }),
    forfeit: Object.freeze({
      graceSeconds: readNumber(forfeit, "grace_seconds", f),
      roundsForMatch: readInt(forfeit, "rounds_for_match", f),
    }),
    deadline: Object.freeze({
      deadlineMs: readInt(deadline, "deadline_ms", d),
      timeBankMs: readInt(deadline, "time_bank_ms", d),
      defaultActionPolicy: readStr(deadline, "default_action_policy", d),
      lateAnswer: readStr(deadline, "late_answer", d),
      missCountsTowardForfeit: readBool(deadline, "miss_counts_toward_forfeit", d),
    }),
  });
}

/** Build the typed {@link RuleSet} from a parsed `match_start.game_config`. */
export function ruleSetFromGameConfig(config: BigBadBotsGameConfig): RuleSet {
  return parseRuleSet(config.ruleSetId, config.ruleSet);
}

/** The parameter table of a {@link RuleSet} in its wire form (a mutable copy of `game_config.rule_set`). */
export function ruleSetToWire(rules: RuleSet): Record<string, unknown> {
  return thaw(rules.params) as Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// Turn request states (section 4.3)
// ---------------------------------------------------------------------------

/** What every Big Bad Bots `turn_request` carries, whatever the call. */
export interface BigBadBotsState {
  // Envelope (Layer 1)
  /** Echo it in the `turn_action` (the SDK does). */
  readonly requestId: string;
  /** The receiving seat. At a simultaneous step both seats are the seat whose turn it is. */
  readonly seat: number;
  /**
   * The whole time the server waits for this answer. In the fight that is
   * the per-tick deadline plus what is left of this seat's time bank;
   * `timeoutMs - rules.deadline.deadlineMs` is the bank.
   */
  readonly timeoutMs: number;
  /** The call's legal `action` strings (section 3.1). */
  readonly validActions: readonly string[];
  // State (Layer 2)
  readonly phase: BigBadBotsPhase;
  /** Always `[]`; Big Bad Bots has no cards. */
  readonly board: readonly unknown[];
  /** Always `[]`. */
  readonly yourHoleCards: readonly unknown[];
  /** Always `0`; Big Bad Bots has no chips. Credits are on `me.credits` / `opp.credits`. */
  readonly pot: number;
  readonly toCall: number;
  readonly minRaise: number;
  readonly maxRaise: number;
  readonly yourStack: number;
  /** Always `[0]`. */
  readonly opponentStacks: readonly number[];
  /**
   * Match context, from `match_start` (not from the `turn_request`): the
   * rule set this match is played under, typed and read-only. A
   * `BigBadBotsBot` fills it in on every call; it is `null` only when no
   * `match_start` was seen (or an `onMatchStart` override did not call
   * `super.onMatchStart`).
   */
  readonly rules: RuleSet | null;
}

/** What the three gear-up calls share (sections 4.3.1 to 4.3.3). */
export interface GearUpState extends BigBadBotsState {
  /** This seat's side, in full. */
  readonly me: Side;
  /** The opponent's side, masked: `offer` is always `[]` and `fitted` never shows a part. */
  readonly opp: Side;
  /** 0-based index of the lot being sold; `0` in the body shop and `lotsTotal` once the auction is over. */
  readonly lotIndex: number;
  /** How many lots the auction sells. */
  readonly lotsTotal: number;
  /** Every lot already sold, in order, with both bids. */
  readonly history: readonly SoldLot[];
}

/** The `choose_body` call (phase `body_shop`, section 4.3.1). Answer with `Action.chooseBody`. */
export interface BodyShopState extends GearUpState {
  readonly phase: "body_shop";
  /** This seat's own offer, in full. The opponent's is never shown. */
  readonly offer: readonly Body[];
}

/**
 * One `bid` call (phase `auction`, section 4.3.2). Answer with `Action.bid`.
 * Future lots are never shown, and neither is the opponent's bid on this one.
 */
export interface AuctionState extends GearUpState {
  readonly phase: "auction";
  /** The part being sold now. */
  readonly lot: Part;
}

/**
 * The `fit` call (phase `fitting`, section 4.3.3). Answer with `Action.fit`,
 * naming slots by their index into `slots`.
 */
export interface FittingState extends GearUpState {
  readonly phase: "fitting";
  /** Every part this seat won, in full, including more than its slots hold. */
  readonly owned: readonly Part[];
  /** This seat's body's slot types, by slot index. */
  readonly slots: readonly string[];
}

/**
 * One `act` call: one fight tick (phase `fight`, section 4.3.4). Answer with
 * `Action.act`. `validActions` is always all ten fight actions; whether one
 * can be performed now is decided when the tick resolves.
 */
export interface FightState extends BigBadBotsState {
  readonly phase: "fight";
  /** The tick within the round, 0-based. */
  readonly tick: number;
  /** The fight round, 1-based. */
  readonly round: number;
  /** `[mine, opponent's]`. */
  readonly roundsWon: readonly number[];
  /** This seat's fighter, in full. */
  readonly me: Fighter;
  /** The opponent's fighter, masked (section 5). */
  readonly opp: Fighter;
  /** The distance between the two fighters, in arena units. */
  readonly distance: number;
  /** `1` if the opponent is to this seat's right, `-1` if to its left. */
  readonly facing: number;
  /** `[left, right]`: where the arena's walls stand this tick. */
  readonly walls: readonly number[];
}

/** Any parsed Big Bad Bots `turn_request`. Narrow on `phase`. */
export type BigBadBotsTurnState = BodyShopState | AuctionState | FittingState | FightState;

/**
 * Parse a Big Bad Bots `turn_request` into the model for its call.
 *
 * Routes on `state.phase` (section 3.1).
 *
 * @throws StateParseError when `state` is missing, its `phase` is not one of
 * the four Big Bad Bots phases, or a required field is missing or mistyped.
 */
export function parseBigBadBotsTurnRequest(message: Record<string, unknown>): BigBadBotsTurnState {
  if (!isObject(message)) throw new StateParseError("turn_request: expected an object");
  const state = readObject(message, "state", "turn_request");
  const where = "turn_request.state";
  const phase = readStr(state, "phase", where);
  const common = {
    requestId: readStr(message, "request_id", "turn_request"),
    seat: readInt(message, "seat", "turn_request"),
    timeoutMs: readInt(message, "timeout_ms", "turn_request"),
    validActions: readStrList(message, "valid_actions", "turn_request"),
    board: Object.freeze([...readList(state, "board", where)]),
    yourHoleCards: Object.freeze([...readList(state, "your_hole_cards", where)]),
    pot: readInt(state, "pot", where),
    toCall: readInt(state, "to_call", where),
    minRaise: readInt(state, "min_raise", where),
    maxRaise: readInt(state, "max_raise", where),
    yourStack: readInt(state, "your_stack", where),
    opponentStacks: readIntList(state, "opponent_stacks", where),
    rules: null,
  };

  if (phase === "fight") {
    return Object.freeze({
      ...common,
      phase,
      tick: readInt(state, "tick", where),
      round: readInt(state, "round", where),
      roundsWon: readIntList(state, "rounds_won", where),
      me: parseFighter(readObject(state, "me", where), `${where}.me`),
      opp: parseFighter(readObject(state, "opp", where), `${where}.opp`),
      distance: readNumber(state, "distance", where),
      facing: readInt(state, "facing", where),
      walls: readNumberList(state, "walls", where),
    });
  }
  if (phase === "body_shop" || phase === "auction" || phase === "fitting") {
    const gearup = {
      me: parseSide(readObject(state, "me", where), `${where}.me`),
      opp: parseSide(readObject(state, "opp", where), `${where}.opp`),
      lotIndex: readInt(state, "lot_index", where),
      lotsTotal: readInt(state, "lots_total", where),
      history: Object.freeze(
        readObjects(state, "history", where).map((lot, i) =>
          parseSoldLot(lot, `${where}.history[${i}]`),
        ),
      ),
    };
    if (phase === "body_shop") {
      return Object.freeze({
        ...common,
        ...gearup,
        phase,
        offer: Object.freeze(
          readObjects(state, "offer", where).map((body, i) =>
            parseBody(body, `${where}.offer[${i}]`),
          ),
        ),
      });
    }
    if (phase === "auction") {
      return Object.freeze({
        ...common,
        ...gearup,
        phase,
        lot: parsePart(readObject(state, "lot", where), `${where}.lot`),
      });
    }
    return Object.freeze({
      ...common,
      ...gearup,
      phase,
      owned: Object.freeze(
        readObjects(state, "owned", where).map((part, i) =>
          parsePart(part, `${where}.owned[${i}]`),
        ),
      ),
      slots: readStrList(state, "slots", where),
    });
  }
  throw new StateParseError(
    `${where}.phase: ${JSON.stringify(phase)} is not a Big Bad Bots phase ` +
      `(expected one of ${PHASES.join(", ")})`,
  );
}

/** Whether `state` is a parsed Big Bad Bots state (as opposed to a poker `GameState`). */
export function isBigBadBotsState(state: unknown): state is BigBadBotsTurnState {
  return (
    isObject(state) &&
    typeof state.requestId === "string" &&
    typeof state.timeoutMs === "number" &&
    (PHASES as readonly string[]).includes(state.phase as string)
  );
}

/** Return a copy of `state` with `rules` set. States are read-only, so this never mutates. */
export function withRules<S extends BigBadBotsTurnState>(state: S, rules: RuleSet | null): S {
  return Object.freeze({ ...state, rules } as S);
}

// ---------------------------------------------------------------------------
// Back to the wire
// ---------------------------------------------------------------------------

function sideToWire(side: Side): Json {
  return {
    credits: side.credits,
    offer: [...side.offer],
    body: side.body,
    owned: [...side.owned],
    fitted: [...side.fitted],
  };
}

function partToWire(part: Part): Json {
  return { id: part.id, slot: part.slot, bonus: { ...part.bonus }, effects: { ...part.effects } };
}

function fighterToWire(f: Fighter): Json {
  return {
    body: f.body,
    special: f.special,
    x: f.x,
    hp: f.hp,
    max_hp: f.maxHp,
    stamina: f.stamina,
    max_stamina: f.maxStamina,
    action: f.action,
    phase: f.phase,
    frames_left: f.framesLeft,
    special_used: f.specialUsed,
    last_attack: f.lastAttack,
    repeats: f.repeats,
    strike_range: f.strikeRange,
    move_rate: f.moveRate,
    attrs: { ...f.attrs },
    buffs: { ...f.buffs },
  };
}

/**
 * Return the `turn_request.state` payload a parsed state was built from
 * (keys this release does not know are not carried). The Python SDK's
 * `to_state()`.
 */
export function toWireState(state: BigBadBotsTurnState): Record<string, unknown> {
  const out: Json = {
    phase: state.phase,
    board: [...state.board],
    your_hole_cards: [...state.yourHoleCards],
    pot: state.pot,
    to_call: state.toCall,
    min_raise: state.minRaise,
    max_raise: state.maxRaise,
    your_stack: state.yourStack,
    opponent_stacks: [...state.opponentStacks],
  };
  if (state.phase === "fight") {
    return {
      ...out,
      tick: state.tick,
      round: state.round,
      rounds_won: [...state.roundsWon],
      me: fighterToWire(state.me),
      opp: fighterToWire(state.opp),
      distance: state.distance,
      facing: state.facing,
      walls: [...state.walls],
    };
  }
  Object.assign(out, {
    me: sideToWire(state.me),
    opp: sideToWire(state.opp),
    lot_index: state.lotIndex,
    lots_total: state.lotsTotal,
    history: state.history.map((lot) => ({
      part: lot.part,
      my_bid: lot.myBid,
      opp_bid: lot.oppBid,
      winner: lot.winner,
    })),
  });
  if (state.phase === "body_shop") {
    out.offer = state.offer.map((body) => ({
      id: body.id,
      cost: body.cost,
      stats: { ...body.stats },
      slots: [...body.slots],
      special: body.special,
    }));
  } else if (state.phase === "auction") {
    out.lot = partToWire(state.lot);
  } else {
    out.owned = state.owned.map(partToWire);
    out.slots = [...state.slots];
  }
  return out;
}
