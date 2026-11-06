/**
 * `runBot`'s retry budget against a real local WebSocket server (#244).
 *
 * The counter used to reset on every successful connect, so a server that
 * accepted the socket and then closed it before its `hello` (1011, every time)
 * made `runBot` reconnect forever. It now resets only once a session made
 * progress: the server `hello` plus a game frame.
 */
import type { AddressInfo } from "node:net";

import { afterEach, describe, expect, it } from "vitest";
import { WebSocketServer } from "ws";

import { Bot } from "../src/bot.js";
import { runBot } from "../src/client.js";
import { Action } from "../src/models.js";
import { RetryPolicy } from "../src/retry.js";

class NoopBot extends Bot {
  decide(): Action {
    return Action.fold();
  }
}

describe("runBot retry budget on a real socket", () => {
  let wss: WebSocketServer | null = null;

  afterEach(async () => {
    const server = wss;
    wss = null;
    if (server) await new Promise<void>((resolve) => server.close(() => resolve()));
  });

  it("stops after the configured retries when the server accepts then closes 1011", async () => {
    const server = new WebSocketServer({ host: "127.0.0.1", port: 0 });
    wss = server;
    await new Promise<void>((resolve) => server.once("listening", () => resolve()));
    let connections = 0;
    server.on("connection", (sock) => {
      connections += 1;
      sock.once("message", () => sock.close(1011, "internal error")); // after authenticate
    });
    const { port } = server.address() as AddressInfo;

    const policy = new RetryPolicy({ maxReconnectAttempts: 2, initialBackoffMs: 1, maxBackoffMs: 1 });
    const run = runBot(`ws://127.0.0.1:${port}/ws/match/m-live/bot`, new NoopBot(), {
      token: "tok",
      retryPolicy: policy,
    });
    // Fail rather than hang if the budget is not enforced.
    const guard = new Promise<never>((_, reject) =>
      setTimeout(() => reject(new Error("runBot kept reconnecting")), 5000),
    );
    await expect(Promise.race([run, guard])).rejects.toThrow("connection closed before server hello");
    expect(connections).toBe(3); // the first connect + 2 retries
  });
});
