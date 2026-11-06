/**
 * A match seat taken over by another connection of the same bot (#242),
 * against a real local WebSocket server, so the real `ws` reader's close-code
 * capture is what the session loop sees.
 *
 * The external-API runner's behaviour is covered in `external.test.ts` with a
 * scripted transport; this file covers `runBot` and `_NodeWebSocketReader`.
 */
import type { AddressInfo } from "node:net";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import WebSocket, { WebSocketServer } from "ws";

import { Bot } from "../src/bot.js";
import { _NodeWebSocketReader, runBot } from "../src/client.js";
import { Action } from "../src/models.js";

const MATCH_ID = "m-seat";
const SEAT_LINE = `chipzen-sdk: match ${MATCH_ID}: seat taken over by another connection of this bot; not reconnecting`;

class NoopBot extends Bot {
  decide(): Action {
    return Action.fold();
  }
}

type Json = Record<string, unknown>;
type OnAuthenticate = (sock: WebSocket) => void;

describe("a 4000 close on a real socket", () => {
  let wss: WebSocketServer | null = null;
  let warn: ReturnType<typeof vi.spyOn>;
  let connections = 0;

  beforeEach(() => {
    connections = 0;
    warn = vi.spyOn(console, "warn").mockImplementation(() => {});
  });
  afterEach(async () => {
    warn.mockRestore();
    const server = wss;
    wss = null;
    if (server) await new Promise<void>((resolve) => server.close(() => resolve()));
  });

  /** Serve every connection with `onAuthenticate` once its authenticate arrives. */
  async function serve(onAuthenticate: OnAuthenticate): Promise<string> {
    const server = new WebSocketServer({ host: "127.0.0.1", port: 0 });
    wss = server;
    await new Promise<void>((resolve) => server.once("listening", () => resolve()));
    server.on("connection", (sock) => {
      connections += 1;
      sock.on("message", (data) => {
        const msg = JSON.parse(data.toString()) as Json;
        if (msg.type === "authenticate") onAuthenticate(sock);
      });
    });
    const { port } = server.address() as AddressInfo;
    return `ws://127.0.0.1:${port}/ws/match/${MATCH_ID}/bot`;
  }

  const seatLines = (): string[] =>
    warn.mock.calls.map((c) => String(c[0])).filter((line) => line === SEAT_LINE);

  it("runBot returns null without reconnecting and logs one line", async () => {
    const url = await serve((sock) => sock.close(4000, "replaced_by_new_connection"));
    const end = await runBot(url, new NoopBot(), { token: "tok", maxRetries: 3 });
    expect(end).toBeNull();
    expect(connections).toBe(1);
    expect(seatLines()).toEqual([SEAT_LINE]);
  });

  it("runBot treats the error frame then the close the same way", async () => {
    const url = await serve((sock) => {
      sock.send(JSON.stringify({ type: "hello", selected_version: "1.0" }));
      sock.send(JSON.stringify({ type: "error", code: "replaced_by_new_connection" }));
      sock.close(4000, "replaced_by_new_connection");
    });
    const end = await runBot(url, new NoopBot(), { token: "tok", maxRetries: 3 });
    expect(end).toBeNull();
    expect(connections).toBe(1);
    expect(seatLines()).toEqual([SEAT_LINE]);
  });

  it("the reader reports the server's close code", async () => {
    const url = await serve((sock) => sock.close(4000, "replaced_by_new_connection"));
    const ws = new WebSocket(url);
    const reader = new _NodeWebSocketReader(ws);
    await new Promise<void>((resolve) => ws.once("open", () => resolve()));
    expect(reader.closeCode()).toBeUndefined();
    ws.send(JSON.stringify({ type: "authenticate" }));
    expect(await reader.next()).toBeNull();
    expect(reader.closeCode()).toBe(4000);
  });
});
