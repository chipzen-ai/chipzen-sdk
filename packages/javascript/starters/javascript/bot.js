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

class MyBot extends Bot {
  /** Replace with your strategy. Must return an Action. */
  decide(state) {
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
