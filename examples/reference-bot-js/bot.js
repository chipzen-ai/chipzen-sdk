/**
 * Reference Chipzen bot — non-trivial demonstration (JavaScript port of
 * examples/reference-bot/bot.py).
 *
 * Intentionally simple but **competent** — the goal is to show, in
 * one file, that the protocol carries real strategy state cleanly:
 *
 *   - Per-match state via onMatchStart (seat assignment).
 *   - Per-hand state via onRoundStart (reset trackers each hand).
 *   - Live observation via onTurnResult (count opponent aggression).
 *   - Branching on state.phase for preflop vs postflop.
 *   - Heuristic hand-strength bucketing using state.holeCards.
 *   - Made-hand detection from state.holeCards + state.board.
 *   - Action history awareness via this._opponentRaisesThisHand.
 *   - Strict state.validActions checking — this bot will never
 *     return an action the server hasn't offered.
 *
 * The strategy is **not strong** — it folds too much, doesn't bluff,
 * ignores pot odds, has no postflop draw recognition, and uses a
 * crude rank-bucket model. That's fine: the point is to show that a
 * bot author **can** express real logic against the SDK, not that
 * the bot itself is competitive.
 *
 * If you're starting your own bot, scaffold one with
 * `chipzen-sdk init <name>` instead — that gives you a thin starter
 * with the IP-protected Bun-compile Dockerfile.
 *
 * Environment:
 *   CHIPZEN_WS_URL   WebSocket URL injected by the platform at launch.
 *   CHIPZEN_TOKEN    Bot API token (empty string is fine for localhost dev).
 *   CHIPZEN_TICKET   Alternative single-use ticket; unused here but
 *                    forwarded so the image works in either flow.
 */

import { Action, Bot, runBot } from "@chipzen-ai/bot";

// ---------------------------------------------------------------------------
// Card / hand helpers — pure functions, no SDK state
// ---------------------------------------------------------------------------

// Rank order, weakest to strongest. Index = numeric strength.
const RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "T", "J", "Q", "K", "A"];
const RANK_INDEX = Object.fromEntries(RANKS.map((r, i) => [r, i]));

/**
 * Coarse preflop hand bucket: "premium" / "strong" / "medium" / "weak".
 *
 * Crude on purpose. A real bot would use range tables, position
 * adjustments, and equity vs. an opponent range model. This buckets
 * enough to demonstrate that the SDK's hole-card data is shaped
 * correctly for that kind of work.
 */
function preflopBucket(holeCards) {
  if (!holeCards || holeCards.length !== 2) return "weak";

  const [c0, c1] = holeCards;
  const r1 = c0.rank;
  const r2 = c1.rank;
  const suited = c0.suit === c1.suit;
  const [high, low] = RANK_INDEX[r1] >= RANK_INDEX[r2] ? [r1, r2] : [r2, r1];

  // Pocket pairs
  if (r1 === r2) {
    if (["J", "Q", "K", "A"].includes(r1)) return "premium";
    if (["9", "T"].includes(r1)) return "strong";
    return "medium";
  }

  // AK
  if ((high === "A" && low === "K") || (high === "K" && low === "A")) return "premium";

  // Broadways with an ace
  if (high === "A" && ["Q", "J", "T"].includes(low)) return suited ? "strong" : "medium";

  // KQ, KJ
  if (high === "K" && ["Q", "J"].includes(low)) return suited ? "strong" : "medium";

  // Connected broadways
  if (["Q", "J"].includes(high) && ["T", "9"].includes(low)) {
    return suited ? "strong" : "medium";
  }

  // Weak ace (any)
  if (high === "A") return "medium";

  return "weak";
}

/**
 * Crude category of the best 7-card holding so far.
 *
 * Returns:
 *   0 — no pair (high card only).
 *   1 — one pair.
 *   2 — two pair.
 *   3 — three of a kind or better.
 */
function madeHandClass(holeCards, board) {
  const ranks = [...(holeCards ?? []), ...(board ?? [])].map((c) => c.rank);
  if (ranks.length === 0) return 0;
  const counts = new Map();
  for (const r of ranks) counts.set(r, (counts.get(r) ?? 0) + 1);
  const sorted = [...counts.values()].sort((a, b) => b - a);
  if (sorted[0] >= 3) return 3;
  if (sorted.length >= 2 && sorted[0] === 2 && sorted[1] === 2) return 2;
  if (sorted[0] === 2) return 1;
  return 0;
}

/**
 * Return `target` clamped to [minRaise, maxRaise], or null if raising
 * is illegal at this turn (the SDK reports min_raise == 0 and
 * max_raise == 0 in that case).
 */
function boundedRaise(target, state) {
  if (state.minRaise === 0 || state.maxRaise === 0) return null;
  if (target < state.minRaise) return state.minRaise;
  if (target > state.maxRaise) return state.maxRaise;
  return target;
}

/** Count raise / all_in actions by anyone other than `mySeat` in this hand. */
function opponentRaisesInHistory(history, mySeat) {
  let n = 0;
  for (const entry of history ?? []) {
    if (entry.seat === mySeat) continue;
    if (entry.action === "raise" || entry.action === "all_in") n++;
  }
  return n;
}

// ---------------------------------------------------------------------------
// The bot
// ---------------------------------------------------------------------------

class ReferenceBot extends Bot {
  constructor() {
    super();
    // Per-match state
    this._mySeat = null;
    // Per-hand state — reset by onRoundStart
    this._opponentRaisesThisHand = 0;
  }

  // ----- lifecycle hooks ----------------------------------------------------

  onMatchStart(message) {
    for (const seat of message.seats ?? []) {
      if (seat.is_self) {
        this._mySeat = seat.seat;
        break;
      }
    }
    log(`match_start my_seat=${this._mySeat}`);
  }

  onRoundStart(_message) {
    this._opponentRaisesThisHand = 0;
  }

  onTurnResult(message) {
    const details = message.details ?? {};
    if (details.seat === this._mySeat) return;
    if (details.action === "raise" || details.action === "all_in") {
      this._opponentRaisesThisHand++;
    }
  }

  // ----- decision -----------------------------------------------------------

  decide(state) {
    const valid = state.validActions ?? [];

    // Action history is also exposed on the GameState for bots that
    // prefer to derive aggression from the canonical history rather
    // than the onTurnResult hook. Use the hook-tracked counter as the
    // primary source and reconcile against history as a sanity check —
    // they should agree.
    const historyRaises = opponentRaisesInHistory(state.actionHistory, this._mySeat);
    const oppAggression = Math.max(this._opponentRaisesThisHand, historyRaises);

    const chosen =
      state.phase === "preflop"
        ? this._decidePreflop(state, valid)
        : this._decidePostflop(state, valid, oppAggression);

    log(
      `decide hand=${state.handNumber} phase=${state.phase} ` +
        `legal=${valid.join(",") || "-"} opp_aggro=${oppAggression} ` +
        `action=${chosen.action}`,
    );
    return chosen;
  }

  _decidePreflop(state, valid) {
    const bucket = preflopBucket(state.holeCards);

    // Premium: open-raise to ~3x BB if we can, otherwise call.
    if (bucket === "premium") {
      const target = boundedRaise(state.minRaise > 0 ? state.minRaise * 3 : 0, state);
      if (target !== null && valid.includes("raise")) return Action.raiseTo(target);
      if (valid.includes("call")) return Action.call();
      return valid.includes("check") ? Action.check() : Action.fold();
    }

    // Strong: raise unopened pots, otherwise call cheap.
    if (bucket === "strong") {
      if (state.toCall === 0 && valid.includes("raise")) {
        const target = boundedRaise(state.minRaise > 0 ? state.minRaise * 2 : 0, state);
        if (target !== null) return Action.raiseTo(target);
      }
      if (valid.includes("call") && state.toCall <= Math.floor(state.yourStack / 10)) {
        return Action.call();
      }
      return valid.includes("check") ? Action.check() : Action.fold();
    }

    // Medium: only call free / very cheap.
    if (bucket === "medium") {
      if (valid.includes("check")) return Action.check();
      if (valid.includes("call") && state.toCall <= Math.floor(state.yourStack / 30)) {
        return Action.call();
      }
      return Action.fold();
    }

    // Weak: check / fold.
    if (valid.includes("check")) return Action.check();
    return Action.fold();
  }

  _decidePostflop(state, valid, oppAggression) {
    const klass = madeHandClass(state.holeCards, state.board);

    // Two pair or better: bet 2/3 pot if we can, else call.
    if (klass >= 2) {
      if (valid.includes("raise") && oppAggression === 0) {
        const target = boundedRaise(Math.floor(state.pot * 0.66), state);
        if (target !== null) return Action.raiseTo(target);
      }
      if (valid.includes("call")) return Action.call();
      return valid.includes("check") ? Action.check() : Action.fold();
    }

    // One pair: check, call small bets, fold to pressure.
    if (klass === 1) {
      if (valid.includes("check")) return Action.check();
      if (valid.includes("call") && state.toCall <= Math.floor(state.pot / 3)) {
        return Action.call();
      }
      return Action.fold();
    }

    // Nothing made: check or fold. (No bluffs in the reference bot.)
    if (valid.includes("check")) return Action.check();
    return Action.fold();
  }
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

function log(msg) {
  process.stderr.write(`[reference-bot] ${msg}\n`);
}

export async function main() {
  const url = process.env.CHIPZEN_WS_URL ?? process.argv[2];
  if (!url) {
    process.stderr.write("error: CHIPZEN_WS_URL not set and no URL passed\n");
    process.exit(2);
  }
  log(`reference-bot ready; connecting to ${url}`);
  await runBot(url, new ReferenceBot(), {
    token: process.env.CHIPZEN_TOKEN ?? null,
    ticket: process.env.CHIPZEN_TICKET ?? null,
    clientName: "reference-bot",
    clientVersion: "0.2.0",
  });
}

if (import.meta.main || import.meta.url === `file://${process.argv[1]}`) {
  await main();
}

export { ReferenceBot };
