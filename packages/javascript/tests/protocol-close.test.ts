/**
 * A terminal 4xxx close on a match socket (4013 first) ends the match, with
 * no reconnect (Chipzen #7008).
 *
 * The external-API gateway now passes the executor's 4013 version refusal
 * through to the bot instead of masking it as `match_end` + 1000. Before this
 * change the SDK read that close as a drop and reconnected until the retry
 * budget ran out. These tests use a real local WebSocket server, so the real
 * `ws` reader's close capture is what the session loop sees. They mirror the
 * Python and Rust tests.
 */
import type { AddressInfo } from "node:net";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import WebSocket, { WebSocketServer } from "ws";

import { Bot } from "../src/bot.js";
import { _NodeWebSocketReader, runBot } from "../src/client.js";
import { ProtocolCloseError, TERMINAL_CLOSE_CODES } from "../src/close_codes.js";
import { BOT_TOKEN_SUBPROTOCOL, runExternalBot } from "../src/external.js";
import { ProtocolCloseError as ExportedError, TERMINAL_CLOSE_CODES as Exported } from "../src/index.js";
import { Action } from "../src/models.js";
import { RetryPolicy } from "../src/retry.js";

const NO_MUTUAL = "No mutually supported protocol version";
const NOT_RECONNECTING = "; not reconnecting";

class NoopBot extends Bot {
  decide(): Action {
    return Action.fold();
  }
}

type Json = Record<string, unknown>;
type OnFrame = (sock: WebSocket, msg: Json, path: string) => void;

const quickPolicy = (attempts: number): RetryPolicy =>
  new RetryPolicy({ maxReconnectAttempts: attempts, initialBackoffMs: 1, maxBackoffMs: 1 });

describe("terminal close codes", () => {
  it("the set is pinned", () => {
    // Widening this set changes what a deployed bot does on that close; the
    // gateway's pass-through list may only include codes in here.
    expect([...TERMINAL_CLOSE_CODES].sort()).toEqual([4003, 4005, 4006, 4010, 4012, 4013]);
    expect(Exported).toBe(TERMINAL_CLOSE_CODES);
    expect(ExportedError).toBe(ProtocolCloseError);
  });

  it("the error carries the code and the reason", () => {
    const err = new ProtocolCloseError(4013, NO_MUTUAL);
    expect([err.code, err.reason]).toEqual([4013, NO_MUTUAL]);
    expect(err.message).toBe(
      `server closed the match socket with 4013 protocol_mismatch: ${NO_MUTUAL}`,
    );
    expect(new ProtocolCloseError(4010).message).toContain("no reason given");
  });
});

describe("a terminal close on a real socket", () => {
  let wss: WebSocketServer | null = null;
  let error: ReturnType<typeof vi.spyOn>;
  let connections: string[] = [];

  beforeEach(() => {
    connections = [];
    error = vi.spyOn(console, "error").mockImplementation(() => {});
  });
  afterEach(async () => {
    error.mockRestore();
    const server = wss;
    wss = null;
    if (server) await new Promise<void>((resolve) => server.close(() => resolve()));
  });

  /** Serve every connection; `onFrame` sees each parsed frame. Returns the port. */
  async function serve(onFrame: OnFrame): Promise<number> {
    const server = new WebSocketServer({
      host: "127.0.0.1",
      port: 0,
      handleProtocols: (protocols) =>
        protocols.has(BOT_TOKEN_SUBPROTOCOL) ? BOT_TOKEN_SUBPROTOCOL : false,
    });
    wss = server;
    await new Promise<void>((resolve) => server.once("listening", () => resolve()));
    server.on("connection", (sock, req) => {
      const path = req.url ?? "";
      connections.push(path);
      sock.on("message", (data) => onFrame(sock, JSON.parse(data.toString()) as Json, path));
    });
    return (server.address() as AddressInfo).port;
  }

  const matchUrl = (port: number): string => `ws://127.0.0.1:${port}/ws/match/m-live/bot`;
  const closeLines = (): string[] =>
    error.mock.calls
      .map((c) => String(c[0]))
      .filter((line) => line.endsWith(NOT_RECONNECTING));

  it("runBot stops on a 4013 before the server hello and logs one line", async () => {
    const port = await serve((sock, msg) => {
      if (msg.type === "authenticate") sock.close(4013, NO_MUTUAL);
    });
    const run = runBot(matchUrl(port), new NoopBot(), { token: "t", retryPolicy: quickPolicy(3) });
    await expect(run).rejects.toMatchObject({
      name: "ProtocolCloseError",
      code: 4013,
      reason: NO_MUTUAL,
    });
    expect(connections).toHaveLength(1);
    expect(closeLines()).toEqual([
      `chipzen-sdk: match m-live: server closed the match socket with 4013 protocol_mismatch: ${NO_MUTUAL}${NOT_RECONNECTING}`,
    ]);
  });

  it("runBot stops on a 4013 after its own hello", async () => {
    const port = await serve((sock, msg) => {
      if (msg.type === "authenticate") {
        sock.send(JSON.stringify({ type: "hello", selected_version: "1.0" }));
      } else if (msg.type === "hello") {
        sock.close(4013, "Missing or invalid supported_versions");
      }
    });
    const run = runBot(matchUrl(port), new NoopBot(), { token: "t", retryPolicy: quickPolicy(3) });
    await expect(run).rejects.toBeInstanceOf(ProtocolCloseError);
    expect(connections).toHaveLength(1);
    expect(closeLines()).toHaveLength(1);
  });

  it.each([...TERMINAL_CLOSE_CODES])("runBot stops on %i", async (code) => {
    const port = await serve((sock, msg) => {
      if (msg.type === "authenticate") sock.close(code, "why");
    });
    const run = runBot(matchUrl(port), new NoopBot(), { token: "t", retryPolicy: quickPolicy(2) });
    await expect(run).rejects.toMatchObject({ code, reason: "why" });
    expect(connections).toHaveLength(1);
  });

  it.each([1011, 4001, 4002, 4008, 4011])("runBot still retries %i", async (code) => {
    const port = await serve((sock, msg) => {
      if (msg.type === "authenticate") sock.close(code, "x");
    });
    const run = runBot(matchUrl(port), new NoopBot(), { token: "t", retryPolicy: quickPolicy(2) });
    await expect(run).rejects.not.toBeInstanceOf(ProtocolCloseError);
    expect(connections).toHaveLength(3); // 1 + 2 reconnects, as before
    expect(closeLines()).toEqual([]);
  });

  it.each([1000, 1001])("a %i close after the handshake is unchanged", async (code) => {
    const port = await serve((sock, msg) => {
      if (msg.type === "authenticate") {
        sock.send(JSON.stringify({ type: "hello", selected_version: "1.0" }));
      } else if (msg.type === "hello") {
        sock.close(code);
      }
    });
    const end = await runBot(matchUrl(port), new NoopBot(), {
      token: "t",
      retryPolicy: quickPolicy(2),
    });
    expect(end).toBeNull();
    expect(connections).toHaveLength(1);
  });

  it("the reader reports the server's close reason", async () => {
    const port = await serve((sock) => sock.close(4013, NO_MUTUAL));
    const ws = new WebSocket(matchUrl(port));
    const reader = new _NodeWebSocketReader(ws);
    await new Promise<void>((resolve) => ws.once("open", () => resolve()));
    expect(reader.closeReason()).toBeUndefined();
    ws.send(JSON.stringify({ type: "authenticate" }));
    expect(await reader.next()).toBeNull();
    expect([reader.closeCode(), reader.closeReason()]).toEqual([4013, NO_MUTUAL]);
  });

  it("runExternalBot records the close, does not reconnect, and counts the match", async () => {
    const port = await serve((sock, msg, path) => {
      if (path.startsWith("/ws/external/match/")) {
        if (msg.type === "authenticate") sock.close(4013, NO_MUTUAL);
        return;
      }
      if (msg.type === "authenticate") {
        sock.send(JSON.stringify({ type: "hello", endpoint: "lobby" }));
        sock.send(
          JSON.stringify({
            type: "matched",
            match_id: "m1",
            participant_id: "p1",
            gateway_ws_url: "/ws/external/match/m1/p1",
            rated: false,
          }),
        );
      }
    });
    const results = await runExternalBot(new NoopBot(), {
      url: `ws://127.0.0.1:${port}/ws/external/bot/b1`,
      token: "cz_extbot_x",
      retryPolicy: new RetryPolicy({
        maxReconnectAttempts: 3,
        initialBackoffMs: 50,
        maxBackoffMs: 50,
      }),
      maxMatches: 1, // the close counts as the match finishing
    });
    expect(connections.filter((p) => p.startsWith("/ws/external/match/"))).toHaveLength(1);
    expect(results).toEqual([
      { matchId: "m1", end: null, closeCode: 4013, closeReason: NO_MUTUAL },
    ]);
    expect(closeLines()).toEqual([
      `chipzen-sdk: match m1: server closed the match socket with 4013 protocol_mismatch: ${NO_MUTUAL}${NOT_RECONNECTING}`,
    ]);
  }, 15_000);
});
