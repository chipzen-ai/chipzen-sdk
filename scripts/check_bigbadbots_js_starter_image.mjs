#!/usr/bin/env node
/**
 * Check a built JavaScript Big Bad Bots starter image plays every call with its
 * COMPILED bot.
 *
 * The JavaScript starters compile bot.js and the SDK into one executable
 * (`bun build --compile`), so unlike the Python image check there is no module
 * to import inside the image. Instead this script plays the image a short match
 * over a real WebSocket, the way the platform does:
 *
 * - the image runs as a non-root user and holds no .js source in /bot;
 * - the container starts from its ENTRYPOINT (the log-cap wrapper, running the
 *   bot as its CMD under the platform's per-match limits) with `CHIPZEN_WS_URL`
 *   and `CHIPZEN_TOKEN`, authenticates, and answers a Big Bad Bots `hello` with
 *   `supported_games: ["bigbadbots"]`;
 * - after `match_start` (the protocol doc's published `game_config`), it answers
 *   each published `turn_request` of BIGBADBOTS-GAME-STATE-PROTOCOL.md (body
 *   shop, one auction lot, fitting, one fight tick) with a `turn_action` the
 *   SDK's server-side checker rates `ok`;
 * - after `match_end` the process exits 0.
 *
 * Needs the JavaScript SDK built (`pnpm install && pnpm build` in
 * packages/javascript): it uses that package's `ws` and its checker.
 *
 * Usage (from the repo root, after `docker build -t <tag> <starter dir>`):
 *
 *     node scripts/check_bigbadbots_js_starter_image.mjs <tag> \
 *       docs/protocol/BIGBADBOTS-GAME-STATE-PROTOCOL.md
 */

import { execFileSync, spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const JS_PKG = path.resolve(HERE, "..", "packages", "javascript");
const require = createRequire(path.join(JS_PKG, "package.json"));
const { WebSocketServer } = require("ws");
const sdk = await import(pathToFileURL(path.join(JS_PKG, "dist", "index.js")).href);

const MATCH_ID = "m_image_check";
const STEP_TIMEOUT_MS = 30_000;

function fail(message) {
  console.error(`FAIL: ${message}`);
  process.exit(1);
}

function docBlocks(docPath) {
  const text = readFileSync(docPath, "utf-8").replace(/\r\n/g, "\n");
  return [...text.matchAll(/^```json\n([\s\S]*?)\n```$/gm)].map((m) => JSON.parse(m[1]));
}

function docker(args) {
  return execFileSync("docker", args, { encoding: "utf-8" }).trim();
}

/** Static checks on the image: non-root, and no strategy source in /bot. */
function checkImage(image) {
  const user = docker(["image", "inspect", image, "--format", "{{.Config.User}}"]);
  if (user === "" || user === "0" || user === "root") fail(`the image runs as root (User=${JSON.stringify(user)})`);
  const files = docker(["run", "--rm", "--entrypoint", "ls", image, "-A", "/bot"]).split(/\s+/);
  const leaked = files.filter((f) => /\.(c|m)?(j|t)s$/.test(f));
  if (leaked.length > 0) fail(`strategy source leaked into the runtime image: ${leaked.join(", ")}`);
  if (!files.includes("bot")) fail(`no compiled /bot/bot in the image (found: ${files.join(", ")})`);
  console.log(`image OK: runs as user ${user}; /bot holds ${files.join(", ")}`);
}

/** A queue of the messages one client sends, read one at a time with a timeout. */
function inbox(socket) {
  const queued = [];
  const waiting = [];
  socket.on("message", (data) => {
    const text = data.toString();
    const next = waiting.shift();
    if (next) next.resolve(text);
    else queued.push(text);
  });
  return (what) => {
    if (queued.length > 0) return Promise.resolve(queued.shift());
    return new Promise((resolve, reject) => {
      const timer = setTimeout(
        () => reject(new Error(`timed out waiting for ${what}`)),
        STEP_TIMEOUT_MS,
      );
      waiting.push({
        resolve: (text) => {
          clearTimeout(timer);
          resolve(text);
        },
      });
    });
  };
}

async function playMatch(socket, blocks) {
  const descriptor = blocks.find((b) => b.state_shape === "bigbadbots");
  const gameConfig = blocks.find((b) => "rule_set_id" in b);
  const requests = blocks.filter((b) => b.type === "turn_request");
  if (!descriptor || !gameConfig || requests.length !== 4) {
    fail(`the protocol doc's examples changed shape (requests: ${requests.length})`);
  }

  const next = inbox(socket);
  let seq = 0;
  const send = (message) => socket.send(JSON.stringify({ match_id: MATCH_ID, seq: ++seq, ...message }));
  const receive = async (what) => {
    const message = JSON.parse(await next(what));
    if (message.type === "pong") return receive(what);
    return message;
  };

  const auth = await receive("authenticate");
  if (auth.type !== "authenticate" || auth.token !== "ci-token") {
    fail(`expected authenticate with the token, got ${JSON.stringify(auth)}`);
  }
  send({
    type: "hello",
    server_ts: "2026-10-21T00:00:00.000Z",
    supported_versions: ["1.0"],
    selected_version: "1.0",
    game_type: "bigbadbots",
    game: descriptor,
    capabilities: [],
  });
  const hello = await receive("client hello");
  if (hello.type !== "hello" || !hello.supported_games?.includes("bigbadbots")) {
    fail(`expected a hello declaring bigbadbots, got ${JSON.stringify(hello)}`);
  }
  send({
    type: "match_start",
    server_ts: "2026-10-21T00:00:01.000Z",
    seats: [
      { seat: 0, participant_id: "p0", display_name: "Starter", is_self: true },
      { seat: 1, participant_id: "p1", display_name: "House", is_self: false },
    ],
    game_config: gameConfig,
    turn_timeout_ms: 1000,
  });

  for (const published of requests) {
    const request = { ...published, match_id: MATCH_ID, seq: ++seq };
    socket.send(JSON.stringify(request));
    const raw = await next(`the answer to ${request.request_id}`);
    const verdict = sdk.checkBigBadBotsTurnAction(request, raw);
    if (verdict.verdict !== "ok") {
      fail(`${request.state.phase}: ${verdict.verdict} answer ${raw} (${verdict.reason})`);
    }
    console.log(`${request.state.phase}: ${raw}`);
  }

  send({
    type: "match_end",
    server_ts: "2026-10-21T00:05:00.000Z",
    reason: "complete",
    results: [
      { seat: 0, participant_id: "p0", rank: 1, score: 1 },
      { seat: 1, participant_id: "p1", rank: 2, score: 0 },
    ],
  });
}

async function main([image, docPath]) {
  if (!image || !docPath) fail("usage: check_bigbadbots_js_starter_image.mjs <image> <protocol doc>");
  checkImage(image);
  const blocks = docBlocks(docPath);

  const server = new WebSocketServer({ host: "0.0.0.0", port: 0 });
  await new Promise((resolve) => server.once("listening", resolve));
  const { port } = server.address();

  const played = new Promise((resolve, reject) => {
    server.once("connection", (socket) => playMatch(socket, blocks).then(resolve, reject));
  });

  const container = spawn(
    "docker",
    [
      "run",
      "--rm",
      "--add-host=host.docker.internal:host-gateway",
      "-e",
      `CHIPZEN_WS_URL=ws://host.docker.internal:${port}/ws/match/${MATCH_ID}/bot`,
      "-e",
      "CHIPZEN_TOKEN=ci-token",
      // The per-match log-cap limits the platform hands every Big Bad Bots
      // seat, so the match runs with the log-cap wrapper as the bot's parent
      // process, as it does on the platform.
      "-e",
      "CHIPZEN_LOG_CAP_LINES=3000",
      "-e",
      "CHIPZEN_LOG_CAP_BYTES=262144",
      image,
    ],
    { stdio: ["ignore", "inherit", "inherit"] },
  );
  const exited = new Promise((resolve) => container.once("exit", (code) => resolve(code)));

  const timer = setTimeout(() => {
    container.kill();
    fail("the match did not finish in time");
  }, 4 * STEP_TIMEOUT_MS);
  let outcome;
  try {
    outcome = await Promise.race([played.then(() => "played"), exited.then((code) => ({ code }))]);
  } catch (err) {
    container.kill();
    fail(err.message);
  }
  if (outcome !== "played") {
    fail(`the container exited (code ${outcome.code}) before the match finished`);
  }
  const code = await exited;
  clearTimeout(timer);
  server.close();
  if (code !== 0) fail(`the bot exited with code ${code} after match_end`);
  console.log("compiled JavaScript starter OK");
}

await main(process.argv.slice(2));
