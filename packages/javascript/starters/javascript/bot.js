/**
 * Chipzen starter bot.
 *
 * Subclass `Bot`, implement `decide()`, that's it. The SDK handles the
 * WebSocket connection, the two-layer protocol handshake, ping/pong,
 * `request_id` echoing, `action_rejected` retries, and reconnect.
 *
 * Replace the strategy in `decide()` with your own. Everything else can
 * stay as-is.
 */

import { Bot, Action, runBot } from "@chipzen-ai/bot";

/**
 * The seats still in the match, in seat order.
 *
 * A busted seat stays in `opponentStacks` with a stack of 0, so the list's
 * length counts chairs, not players. A seat all-in this hand also shows 0, but
 * has an entry in `actionHistory`. A busted seat has neither.
 */
export function liveSeats(yourSeat, dealerSeat, opponentStacks, actionHistory) {
  const acted = new Set(actionHistory.map((entry) => entry.seat));
  const others = [];
  for (let seat = 0; seat <= opponentStacks.length; seat++) {
    if (seat !== yourSeat) others.push(seat);
  }
  const live = new Set([yourSeat, dealerSeat]);
  others.forEach((seat, i) => {
    if (opponentStacks[i] > 0 || acted.has(seat)) live.add(seat);
  });
  return [...live].sort((a, b) => a - b);
}

/**
 * Derive your seat's table position from the button, among the seats still in.
 *
 * Heads-up is the special case the scaffold default below was written for: with
 * two players left, at a table of any size, the button posts the small blind and
 * acts first preflop. See docs/protocol/POKER-GAME-STATE-PROTOCOL.md section 5.9.
 */
export function tablePosition(yourSeat, dealerSeat, opponentStacks, actionHistory) {
  const live = liveSeats(yourSeat, dealerSeat, opponentStacks, actionHistory);
  const numPlayers = live.length;
  if (numPlayers <= 1) return "button";
  const sab =
    (live.indexOf(yourSeat) - live.indexOf(dealerSeat) + numPlayers) % numPlayers;
  if (numPlayers === 2) return sab === 0 ? "button_sb" : "big_blind";
  if (sab === 0) return "button";
  if (sab === 1) return "small_blind";
  if (sab === 2) return "big_blind";
  if (sab === numPlayers - 1) return "cutoff";
  if (sab === 3) return "early";
  return "middle";
}

class MyBot extends Bot {
  /** Replace with your strategy. Must return an Action. */
  decide(state) {
    // Seat-count-aware: opponentStacks is a LIST of every other seat (length
    // N-1), so this bot keeps running unchanged at a 3-6 player table.
    // tablePosition counts the seats still in. When you add real strategy,
    // iterate / aggregate opponentStacks instead of assuming a single opponent
    // (reading opponentStacks[0] sees only one neighbor).
    const _position = tablePosition(
      state.yourSeat,
      state.dealerSeat,
      state.opponentStacks,
      state.actionHistory,
    );
    void _position;

    if (state.validActions.includes("check")) return Action.check();
    return Action.fold();
  }
}

export async function main() {
  // The Chipzen platform injects CHIPZEN_WS_URL and CHIPZEN_TOKEN
  // (or CHIPZEN_TICKET) at container launch time. For local testing
  // against your own stack, set them yourself or pass the URL as the
  // first positional argument.
  const url = process.env.CHIPZEN_WS_URL ?? process.argv[2];
  if (!url) {
    console.error("error: CHIPZEN_WS_URL not set and no URL passed on the command line");
    process.exit(1);
  }
  await runBot(url, new MyBot(), {
    token: process.env.CHIPZEN_TOKEN ?? null,
    ticket: process.env.CHIPZEN_TICKET ?? null,
  });
}

// Run main() when this file is the entry point — covers both
// `node bot.js` (Node sets import.meta.url to a file:// URL matching
// argv[1]) and `bun build --compile` binaries (Bun sets
// import.meta.main on the entry module). Importing from a test file
// makes both checks false so MyBot can be exercised in isolation.
if (import.meta.main || import.meta.url === `file://${process.argv[1]}`) {
  await main();
}

export { MyBot };
