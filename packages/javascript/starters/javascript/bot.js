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
 * Derive your seat's table position from the button.
 *
 * The protocol is multiway-shaped: `opponentStacks` is a LIST, so the table
 * size is `opponentStacks.length + 1`. Combined with `yourSeat` and
 * `dealerSeat` (both already on the parsed GameState), that is everything you
 * need to know where you sit:
 *
 *     seatsAfterButton = (yourSeat - dealerSeat + numPlayers) % numPlayers
 *
 * Heads-up is the special case the scaffold default below was written for: the
 * button posts the small blind and acts first preflop. See
 * docs/protocol/POKER-GAME-STATE-PROTOCOL.md section 5.9.
 */
export function tablePosition(yourSeat, dealerSeat, numPlayers) {
  if (numPlayers <= 1) return "button";
  const sab = (((yourSeat - dealerSeat) % numPlayers) + numPlayers) % numPlayers;
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
    // N-1), so this bot keeps running unchanged at a 3-6 player table. yourSeat
    // + dealerSeat give your position. When you add real strategy, iterate /
    // aggregate opponentStacks instead of assuming a single opponent (reading
    // opponentStacks[0] sees only one neighbor).
    const numPlayers = state.opponentStacks.length + 1;
    const _position = tablePosition(state.yourSeat, state.dealerSeat, numPlayers);
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
