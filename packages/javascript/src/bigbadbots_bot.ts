/**
 * Base class for Big Bad Bots bots, and the safe fallback answers.
 *
 * Extend {@link BigBadBotsBot} and implement `act()` (one fight tick). The
 * three gear-up calls have working defaults you can override:
 * `chooseBody()`, `bid()` and `fit()`.
 *
 * The session loop is the same one every Chipzen bot runs (`runBot` /
 * `runExternalBot`): at a table whose server `hello` declares
 * `state_shape: "bigbadbots"` it parses each `turn_request` into the model
 * for its call and hands it to `decide()`, which routes on `state.phase`. A
 * `BigBadBotsBot` declares `supported_games: ["bigbadbots"]` in its `hello`
 * unless the caller passes its own list.
 *
 * Mirrors the Python SDK's `chipzen.BigBadBotsBot`. See
 * `docs/protocol/BIGBADBOTS-GAME-STATE-PROTOCOL.md`; section numbers below
 * refer to it.
 */

import {
  gameConfigFromMatchStart,
  isBigBadBotsState,
  ruleSetFromGameConfig,
  StateParseError,
  withRules,
  type AuctionState,
  type BigBadBotsGameConfig,
  type BigBadBotsTurnState,
  type BodyShopState,
  type FightActionKind,
  type FightState,
  type FittingState,
  type RuleSet,
} from "./bigbadbots.js";
import { Bot } from "./bot.js";
import { Action, type GameState } from "./models.js";

/** The fight actions the server repeats on a missed tick (section 3.5). Any other previous action defaults to `idle`. */
export const REPEATED_ON_MISS: readonly FightActionKind[] = ["block", "move_in", "move_out"];

/**
 * Base class for a Big Bad Bots bot: one method per phase.
 *
 * | `state.phase`        | Method         | State model      |
 * |----------------------|----------------|------------------|
 * | `body_shop`          | `chooseBody()` | `BodyShopState`  |
 * | `auction` (per lot)  | `bid()`        | `AuctionState`   |
 * | `fitting`            | `fit()`        | `FittingState`   |
 * | `fight` (per tick)   | `act()`        | `FightState`     |
 *
 * Only `act()` is required. Every step is simultaneous: both seats are asked
 * at the same moment, and nothing about the opponent's answer reaches you
 * before you answer.
 *
 * **Rule sets.** Every state carries `state.rules`, the rule set this match
 * is played under: its id and every number it plays by, as `match_start`
 * sent them. Read the numbers from there and the bot runs unchanged when a
 * new rule set changes them.
 *
 * **Timing.** A fight tick's `state.timeoutMs` is the per-tick deadline plus
 * what is left of your time bank (section 3.4). The methods are synchronous
 * and JavaScript cannot interrupt them, so keep `act()` well inside
 * `state.rules.deadline.deadlineMs`. The SDK sends the protocol's default
 * for the call (see {@link fallbackAction}) and logs a warning when a method
 * throws (under `safeMode`), returns something that is not one of the
 * call's `validActions` (a Promise included), or when a request already
 * waited out its time behind a slow earlier answer. An answer that comes
 * back after its time is still sent; the server drops a late answer and
 * plays that same default (section 3.5).
 *
 * The lifecycle hooks of {@link Bot} (`onMatchStart`, `onRoundStart`,
 * `onTurnResult`, `onRoundResult`, `onMatchEnd`, `onDecisionLatency`, ...)
 * all fire as usual.
 *
 * @example
 * ```ts
 * import { Action, BigBadBotsBot, type FightState } from "@chipzen-ai/bot";
 *
 * export class MyFighter extends BigBadBotsBot {
 *   act(state: FightState): Action {
 *     if (state.distance > state.me.strikeRange) return Action.act("move_in");
 *     return Action.act("punch_high");
 *   }
 * }
 * ```
 */
export abstract class BigBadBotsBot extends Bot {
  /**
   * The rule set and its full parameter table, parsed from
   * `match_start.game_config` (or `reconnected.game_config`) by the default
   * `onMatchStart` / `onReconnected`. `null` until then. If you override
   * either hook, call `super` to keep it.
   */
  gameConfig: BigBadBotsGameConfig | null = null;

  /** `gameConfig`'s rule set, typed and read-only; the same object every call gets as `state.rules`. */
  rules: RuleSet | null = null;

  // ------------------------------------------------------------------
  // Routing
  // ------------------------------------------------------------------

  /**
   * Route one `turn_request` to the method for its phase. You do not
   * normally override this; override the per-phase methods. It fills in
   * `state.rules` from {@link rules} before routing.
   *
   * @throws TypeError if `state` is not a Big Bad Bots state (the bot was seated at a poker table).
   */
  override decide(state: GameState | BigBadBotsTurnState): Action {
    if (!isBigBadBotsState(state)) {
      throw new TypeError("BigBadBotsBot received a poker GameState; it only plays bigbadbots");
    }
    const s = this.rules !== null && state.rules === null ? withRules(state, this.rules) : state;
    switch (s.phase) {
      case "fight":
        return this.act(s);
      case "auction":
        return this.bid(s);
      case "body_shop":
        return this.chooseBody(s);
      case "fitting":
        return this.fit(s);
      default:
        throw new RangeError(
          `not a Big Bad Bots phase: ${JSON.stringify((s as { phase?: unknown }).phase)}`,
        );
    }
  }

  // ------------------------------------------------------------------
  // One method per phase
  // ------------------------------------------------------------------

  /**
   * Body shop: buy one body from `state.offer`.
   *
   * The default buys the **first body on the offer**, which is also what the
   * server applies when no answer arrives in time (section 3.5). It is a
   * legal answer, not a strategy.
   */
  chooseBody(state: BodyShopState): Action {
    const first = state.offer[0];
    if (first === undefined) throw new RangeError("the body shop offer is empty");
    return Action.chooseBody(first.id);
  }

  /**
   * Auction: one sealed bid on `state.lot`, from `0` to `state.me.credits`.
   *
   * Called once per lot. The default bids `0` (pass), which is also the
   * server's default for a missed bid.
   */
  bid(_state: AuctionState): Action {
    return Action.bid(0);
  }

  /**
   * Fitting: put parts from `state.owned` into the slots of `state.slots`.
   *
   * Name slots by their index into `state.slots`. The default fits each
   * slot, in order, with the first unused owned part of that slot's type.
   * That is **legal, not good**: it ignores what the parts do. (The server's
   * default for a missed fitting is the empty fitting.)
   */
  fit(state: FittingState): Action {
    const fitting: Record<number, string> = {};
    const used = new Set<number>();
    state.slots.forEach((slotType, slotIndex) => {
      const partIndex = state.owned.findIndex(
        (part, i) => !used.has(i) && part.slot === slotType,
      );
      if (partIndex >= 0) {
        fitting[slotIndex] = state.owned[partIndex]!.id;
        used.add(partIndex);
      }
    });
    return Action.fit(fitting);
  }

  /**
   * Fight: this tick's action, built with `Action.act`.
   *
   * Called once per tick, several times a second, so keep it fast: stay
   * inside `state.rules.deadline.deadlineMs` and the time bank is never
   * touched. `state.validActions` is always all ten fight actions; whether
   * one can be performed now (stamina, a busy fighter, a used special) is
   * decided when the tick resolves.
   */
  abstract act(state: FightState): Action;

  // ------------------------------------------------------------------
  // Match context
  // ------------------------------------------------------------------

  /**
   * Parse `match_start.game_config` into {@link gameConfig} and {@link rules}.
   * If you override this, call `super.onMatchStart(matchInfo)`.
   */
  override onMatchStart(matchInfo: Record<string, unknown>): void {
    this.learnGameConfig(matchInfo);
  }

  /**
   * Re-learn {@link gameConfig} and {@link rules} from a `reconnected` message.
   * If you override this, call `super.onReconnected(message)`.
   */
  override onReconnected(message: Record<string, unknown>): void {
    if ("game_config" in message) this.learnGameConfig(message);
  }

  private learnGameConfig(message: Record<string, unknown>): void {
    this.gameConfig = gameConfigFromMatchStart(message);
    try {
      this.rules = ruleSetFromGameConfig(this.gameConfig);
    } catch (err) {
      if (!(err instanceof StateParseError)) throw err;
      // The server's table lacks a section the typed view needs. Keep
      // playing: gameConfig.ruleSet still holds everything it sent.
      this.rules = null;
      console.warn(
        `chipzen-sdk: rule set ${JSON.stringify(this.gameConfig.ruleSetId)} could not be read ` +
          `into state.rules (${err.message}); it is null for this match, and ` +
          "gameConfig.ruleSet holds the raw table",
      );
    }
  }
}

// ---------------------------------------------------------------------------
// The safe fallback (section 3.5)
// ---------------------------------------------------------------------------

/**
 * The protocol's default answer for a Big Bad Bots `turn_request`.
 *
 * This is exactly what the server applies to a seat that does not answer in
 * time (section 3.5), so sending it never plays something the protocol
 * would not have played anyway:
 *
 * - `body_shop`: the first body on the offer;
 * - `auction`: a bid of `0`;
 * - `fitting`: the empty fitting;
 * - `fight`: `previousFightAction` if it is `block`, `move_in` or
 *   `move_out`, otherwise `idle`.
 *
 * Works on the raw message, so it still answers when the payload itself
 * failed to parse. Returns `null` when no default can be built (an unknown
 * phase, or a body shop with no readable offer); the caller then sends
 * nothing and the server applies its own default when the time runs out.
 */
export function fallbackAction(
  request: Record<string, unknown>,
  previousFightAction: string | null,
): Action | null {
  const state = request?.state;
  if (state === null || typeof state !== "object" || Array.isArray(state)) return null;
  const s = state as Record<string, unknown>;
  switch (s.phase) {
    case "fight":
      if ((REPEATED_ON_MISS as readonly (string | null)[]).includes(previousFightAction)) {
        return Action.act(previousFightAction as FightActionKind);
      }
      return Action.act("idle");
    case "auction":
      return Action.bid(0);
    case "fitting":
      return Action.fit({});
    case "body_shop": {
      const offer = s.offer;
      if (Array.isArray(offer) && offer.length > 0) {
        const first = offer[0] as Record<string, unknown> | null;
        if (first && typeof first === "object" && typeof first.id === "string") {
          return Action.chooseBody(first.id);
        }
      }
      return null;
    }
    default:
      return null;
  }
}

/**
 * Whether `action` is an {@link Action} naming one of `validActions`.
 *
 * Used to catch a handler that returned something the server would reject
 * outright (`undefined`, a Promise, a poker action, a gear-up action in the
 * fight) before it is sent.
 */
export function isAnswerShaped(action: unknown, validActions: readonly string[]): action is Action {
  return action instanceof Action && validActions.includes(action.action);
}
