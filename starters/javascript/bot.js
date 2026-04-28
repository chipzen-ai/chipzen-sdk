#!/usr/bin/env node
/**
 * Chipzen starter bot — tight-aggressive preflop, check-call postflop.
 *
 * Implements the Chipzen two-layer protocol:
 *   Layer 1 (Transport): docs/arch/TRANSPORT-PROTOCOL.md
 *   Layer 2 (Poker):     docs/arch/POKER-GAME-STATE-PROTOCOL.md
 *
 * Usage:  node bot.js ws://localhost:8001/ws/match/{match_id}/bot
 * Env:    CHIPZEN_WS_URL    — WebSocket URL (alternative to CLI arg)
 *         CHIPZEN_TOKEN     — Bot API token (for /bot endpoints)
 *         CHIPZEN_TICKET    — Single-use ticket (for competitive endpoints)
 *         CHIPZEN_MATCH_ID  — Match UUID (auto-extracted from URL if omitted)
 */

const WebSocket = require("ws");

// -----------------------------------------------------------------------------
// Strategy: tight-aggressive preflop, check-call postflop
// -----------------------------------------------------------------------------

// Strong preflop hands: pairs 77+, broadways, suited aces
const STRONG = new Set([
  "AA", "KK", "QQ", "JJ", "TT", "99", "88", "77",
  "AKs", "AKo", "AQs", "AQo", "AJs", "ATs",
  "KQs", "KQo", "KJs", "QJs", "JTs",
]);

const PROTOCOL_VERSIONS = ["1.0"];
const CLIENT_NAME = "chipzen-starter-javascript";
const CLIENT_VERSION = "0.2.0";

/** Log to stderr so stdout stays clean for piping. */
function log(msg) { process.stderr.write(`[bot] ${msg}\n`); }

/** Convert ["Ah","Kd"] to "AKo" shorthand (rank-ordered, "s"/"o" for suit). */
function handKey(cards) {
  if (!cards || cards.length !== 2) return "";
  const order = "23456789TJQKA";
  let [r0, s0] = [cards[0].slice(0, -1), cards[0].slice(-1)];
  let [r1, s1] = [cards[1].slice(0, -1), cards[1].slice(-1)];
  if (order.indexOf(r0) < order.indexOf(r1)) {
    [r0, s0, r1, s1] = [r1, s1, r0, s0];
  }
  return r0 === r1 ? `${r0}${r1}` : `${r0}${r1}${s0 === s1 ? "s" : "o"}`;
}

/** Pick an action from a Layer 2 turn_request state. */
function decide(state, validActions) {
  const phase = state.phase || "preflop";
  const toCall = state.to_call || 0;
  const pot = state.pot || 0;
  const minRaise = state.min_raise || 0;
  const maxRaise = state.max_raise || 0;
  const key = handKey(state.your_hole_cards);
  const has = (a) => validActions.includes(a);

  // Preflop: raise strong, call medium, fold junk
  if (phase === "preflop") {
    if (STRONG.has(key) && has("raise") && minRaise > 0) {
      const amount = maxRaise ? Math.min(Math.max(minRaise, 1), maxRaise) : minRaise;
      log(`Preflop raise with ${key} to ${amount}`);
      return { action: "raise", params: { amount } };
    }
    if (toCall > 0 && has("call")) {
      log(`Preflop call (${key})`);
      return { action: "call", params: {} };
    }
    if (has("check")) return { action: "check", params: {} };
    return { action: "fold", params: {} };
  }

  // Postflop: check free, call small, fold large
  if (has("check")) return { action: "check", params: {} };
  if (has("call") && toCall <= pot * 0.5) {
    log(`Postflop call ${toCall} into ${pot}`);
    return { action: "call", params: {} };
  }
  if (has("fold")) return { action: "fold", params: {} };
  return { action: "check", params: {} };
}

// -----------------------------------------------------------------------------
// Protocol helpers
// -----------------------------------------------------------------------------

/** Extract match UUID from a `/ws/.../match/{match_id}/...` URL. */
function extractMatchId(url) {
  const parts = url.replace(/\/+$/, "").split("/");
  const idx = parts.indexOf("match");
  if (idx >= 0 && idx + 1 < parts.length) return parts[idx + 1];
  return "unknown";
}

function sendJson(ws, msg) { ws.send(JSON.stringify(msg)); }

// -----------------------------------------------------------------------------
// Main loop
// -----------------------------------------------------------------------------

const url = process.argv[2] || process.env.CHIPZEN_WS_URL;
if (!url) {
  process.stderr.write("Usage: node bot.js <ws_url>\n");
  process.stderr.write("Env:   CHIPZEN_WS_URL, CHIPZEN_TOKEN, CHIPZEN_TICKET\n");
  process.exit(1);
}

const token = process.env.CHIPZEN_TOKEN || null;
const ticket = process.env.CHIPZEN_TICKET || null;
const matchId = process.env.CHIPZEN_MATCH_ID || extractMatchId(url);

log(`Connecting to ${url}`);
const ws = new WebSocket(url);

// Handshake state
let serverHelloReceived = false;

ws.on("open", () => {
  log("Connected!");
  // 1. Send authenticate (must be the first bot message).
  const authMsg = { type: "authenticate", match_id: matchId };
  if (token) authMsg.token = token;
  else if (ticket) authMsg.ticket = ticket;
  else authMsg.token = ""; // sidecar/localhost dev fallback
  sendJson(ws, authMsg);
});

ws.on("message", (data) => {
  let msg;
  try { msg = JSON.parse(data.toString()); }
  catch { return; }  // Ignore malformed

  const mtype = msg.type;

  // 2. First server message should be `hello`. Reply with client hello.
  if (!serverHelloReceived) {
    if (mtype !== "hello") {
      log(`Expected 'hello' from server, got ${JSON.stringify(mtype)}`);
      ws.close();
      return;
    }
    serverHelloReceived = true;
    log(`Server hello: version=${msg.selected_version} game_type=${msg.game_type}`);
    sendJson(ws, {
      type: "hello",
      match_id: matchId,
      supported_versions: PROTOCOL_VERSIONS,
      client_name: CLIENT_NAME,
      client_version: CLIENT_VERSION,
    });
    return;
  }

  switch (mtype) {
    case "ping":
      // Heartbeat: must respond within 5000ms.
      sendJson(ws, { type: "pong", match_id: matchId });
      break;

    case "match_start": {
      const cfg = msg.game_config || {};
      log(`Match start: blinds ${cfg.small_blind}/${cfg.big_blind} stacks ${cfg.starting_stack}`);
      break;
    }

    case "round_start": {
      const s = msg.state || {};
      log(`Hand ${s.hand_number ?? "?"} dealt: ${JSON.stringify(s.your_hole_cards || [])}`);
      break;
    }

    case "turn_request": {
      const state = msg.state || {};
      const valid = msg.valid_actions || [];
      const requestId = msg.request_id;
      const action = decide(state, valid);
      log(`Turn h${state.hand_number ?? "?"} ${state.phase ?? "?"}: ${JSON.stringify(action)}`);
      sendJson(ws, {
        type: "turn_action",
        match_id: matchId,
        request_id: requestId,  // MUST echo
        action: action.action,
        params: action.params || {},
      });
      break;
    }

    case "action_rejected": {
      // Retry within remaining_ms using the SAME request_id.
      const remaining = msg.remaining_ms || 0;
      log(`Action rejected (${msg.reason}): ${msg.message} — ${remaining}ms remaining`);
      sendJson(ws, {
        type: "turn_action",
        match_id: matchId,
        request_id: msg.request_id,
        action: "check",
        params: {},
      });
      break;
    }

    case "turn_result": {
      const d = msg.details || {};
      log(`Turn result seat=${d.seat} action=${d.action} amount=${d.amount || 0}`);
      break;
    }

    case "phase_change":
      log(`Phase -> ${(msg.state || {}).phase} board=${JSON.stringify((msg.state || {}).board || [])}`);
      break;

    case "round_result": {
      const r = msg.result || {};
      log(`Hand ${r.hand_number ?? "?"} over: winners=${JSON.stringify(r.winner_seats)} pot=${r.pot}`);
      break;
    }

    case "action_timeout":
      log(`Timed out; server auto-applied: ${msg.auto_action}`);
      break;

    case "session_control":
      log(`Session control: ${msg.action} (${msg.reason})`);
      break;

    case "error":
      log(`Error [${msg.code}]: ${msg.message}`);
      break;

    case "match_end":
      log(`Match ended: ${msg.reason}`);
      ws.close();
      break;

    default:
      // Forward-compat: silently ignore unknown message types.
      break;
  }
});

ws.on("close", () => log("Disconnected"));
ws.on("error", (err) => log(`WS error: ${err.message}`));
