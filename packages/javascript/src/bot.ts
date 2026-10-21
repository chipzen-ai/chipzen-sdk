/**
 * `Bot` abstract base class.
 *
 * Subclass and override `decide()`. Lifecycle hooks
 * (`onMatchStart` / `onRoundStart` / `onPhaseChange` / `onTurnResult` /
 * `onRoundResult` / `onMatchEnd` / `onReconnected`) are optional —
 * defaults are no-ops.
 * Override the ones you need; the SDK's session loop will call them
 * at the right point.
 */

import { Action, type GameState } from "./models.js";

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

  // ------------------------------------------------------------------
  // Optional variant hooks
  //
  // DEFAULTED, and never called by the SDK on their own: `decide()` stays
  // the single required method and the only one the session loop invokes.
  // They exist so a bot that plays a variant table can split its logic
  // without inventing its own dispatch. The dispatch is yours, in decide():
  //
  //     decide(state: GameState): Action {
  //       if (state.isDrawPhase) return this.decideDraw(state); // 2-7 Triple Draw
  //       ...                                                  // your betting logic
  //     }
  // ------------------------------------------------------------------

  /**
   * 2-7 Triple Draw: return your `draw` action for a draw turn. Called only
   * if your own `decide()` delegates to it.
   *
   * The default is a **stand pat** (`Action.standPat()`), which is also
   * what the server substitutes for a seat that times out in a draw phase.
   * It is legal in every draw phase, so the default is never rejected; it
   * is simply not a strategy.
   *
   * Discard at most `state.maxDiscard` cards with `Action.discard(...)`.
   * The bound lives in the state, not in `state.validActions` (which is
   * exactly `["draw"]` on a draw turn).
   */
  decideDraw(_state: GameState): Action {
    return Action.standPat();
  }

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

  /**
   * Called when the server resumes an in-flight match after a reconnect.
   *
   * On re-attach the server sends `reconnected` — NOT `match_start` — so
   * `onMatchStart` does not fire for a session that (re)joined a match
   * already in progress. Override this to (re)learn match context: the
   * message carries `match_id`, `seats`, and `game_config` in the same
   * shape as `match_start`, plus the current `round_number` and `state`
   * snapshot (see `docs/protocol/TRANSPORT-PROTOCOL.md` §8.15).
   * See chipzen-ai/chipzen-sdk#121.
   */
  onReconnected(_message: Record<string, unknown>): void {
    /* default: no-op */
  }

  /**
   * Called after each `turn_action` is sent, with the wall-clock time
   * `decide()` took in milliseconds.
   *
   * Default is a no-op. Override to track decision latency — useful for
   * spotting when your bot is drifting toward the platform's turn-timeout
   * budget. See chipzen-ai/chipzen-sdk#46.
   */
  onDecisionLatency(_latencyMs: number): void {
    /* default: no-op */
  }
}
