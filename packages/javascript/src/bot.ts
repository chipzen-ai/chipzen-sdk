/**
 * `Bot` abstract base class.
 *
 * Subclass and override `decide()`. Lifecycle hooks
 * (`onMatchStart` / `onRoundStart` / `onPhaseChange` / `onTurnResult` /
 * `onRoundResult` / `onMatchEnd`) are optional — defaults are no-ops.
 * Override the ones you need; the SDK's session loop will call them
 * at the right point.
 */

import type { Action, GameState } from "./models.js";

export abstract class Bot {
  /**
   * The only required override. Called every time the server sends a
   * `turn_request` for your seat. Return one of:
   * `Action.fold()`, `Action.check()`, `Action.call()`,
   * `Action.raiseTo(amount)`, `Action.allIn()`.
   *
   * The returned action's wire-form `action` string MUST be in
   * `state.validActions`; raise amounts MUST satisfy
   * `state.minRaise <= amount <= state.maxRaise`. If the bot returns
   * an illegal action, the SDK's safe-fallback substitutes `check`
   * (or `fold` if check is illegal) and the platform sends a
   * `bot_error` event to the human's UI.
   */
  abstract decide(state: GameState): Action;

  /** Called once when the `match_start` message arrives. */
  onMatchStart(_matchInfo: Record<string, unknown>): void {
    /* default: no-op */
  }

  /**
   * Called at the start of every hand with the raw `round_start` message.
   *
   * Override if you need the Layer 1 envelope (`round_id`,
   * `round_number`) or the full Layer 2 `state` payload. For most bots,
   * `onHandStart` is the simpler hook.
   */
  onRoundStart(_message: Record<string, unknown>): void {
    /* default: no-op */
  }

  /**
   * Called when the flop, turn, or river is dealt. Useful for triggering
   * postflop planning *between* your turns rather than inside `decide`.
   */
  onPhaseChange(_message: Record<string, unknown>): void {
    /* default: no-op */
  }

  /**
   * Called after every participant's action is broadcast — yours and
   * every opponent's. Use for opponent modeling, timing analysis, or
   * stack tracking.
   *
   * This hook runs *before* the next `turn_request` is dispatched, and
   * runs serially. Slow work here eats into your decide budget — see
   * the DEV-MANUAL §6 for the "queue drain" failure mode.
   */
  onTurnResult(_message: Record<string, unknown>): void {
    /* default: no-op */
  }

  /** Called when a hand ends (`round_result` message). */
  onRoundResult(_message: Record<string, unknown>): void {
    /* default: no-op */
  }

  /** Called once when the match ends. */
  onMatchEnd(_results: Record<string, unknown>): void {
    /* default: no-op */
  }
}
