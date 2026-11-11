/**
 * Lobby liveness: a lobby that stops sending frames is reconnected (#136).
 *
 * The server pings the lobby every 15s. When the host sleeps, the server times
 * the bot out and closes the lobby, but the local socket can stay "open" for a
 * long time after the host wakes, so the SDK never noticed and never
 * reconnected. `runExternalBot` now treats total silence for
 * `lobbySilenceTimeoutMs` as a dead lobby and reconnects with the retry
 * policy's backoff. Mirrors the Python SDK's `test_external_liveness.py`.
 *
 * The silent-lobby test uses a raw TCP server that sends `hello` and then never
 * answers anything (not even a close frame) while keeping the TCP connection
 * up. The other socket tests use a real local `ws` server.
 */
import { createHash } from "node:crypto";
import { createServer, type AddressInfo, type Server, type Socket } from "node:net";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WebSocketServer } from "ws";

import { Bot } from "../src/bot.js";
import {
  _setLobbyRecvTimeoutMs,
  runExternalBot,
  type ExternalConnection,
  type Transport,
} from "../src/external.js";
import { DEFAULT_LOBBY_SILENCE_TIMEOUT_MS as PUBLIC_DEFAULT } from "../src/index.js";
import {
  DEFAULT_LOBBY_SILENCE_TIMEOUT_MS,
  LobbyLiveness,
  type LivenessClocks,
} from "../src/lobby_liveness.js";
import { Action } from "../src/models.js";
import { RetryPolicy } from "../src/retry.js";

const SILENCE_LINE = "nothing from the server for";

const FAST_POLICY = new RetryPolicy({ maxReconnectAttempts: 3, initialBackoffMs: 10, maxBackoffMs: 10 });

class FoldBot extends Bot {
  decide(): Action {
    return Action.fold();
  }
}

type Json = Record<string, unknown>;

let warn: ReturnType<typeof vi.spyOn>;
const silenceLines = (): string[] =>
  warn.mock.calls.map((c) => String(c[0])).filter((line) => line.includes(SILENCE_LINE));

beforeEach(() => {
  // Wake the lobby loop often so a short silence timeout is noticed quickly.
  _setLobbyRecvTimeoutMs(20);
  warn = vi.spyOn(console, "warn").mockImplementation(() => {});
});
afterEach(() => {
  _setLobbyRecvTimeoutMs(2000);
  warn.mockRestore();
});

/** Fail rather than hang if the run never ends. */
function withGuard<T>(run: Promise<T>, ms: number): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const guard = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error(`run did not finish in ${ms}ms`)), ms);
  });
  return Promise.race([run, guard]).finally(() => clearTimeout(timer));
}

// ---------------------------------------------------------------------------
// Real sockets
// ---------------------------------------------------------------------------

/** RFC 6455 handshake GUID, for the hand-rolled server below. */
const WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";

/** One unmasked server-to-client text frame (payload under 126 bytes). */
function wsTextFrame(msg: Json): Buffer {
  const payload = Buffer.from(JSON.stringify(msg));
  expect(payload.length).toBeLessThan(126);
  return Buffer.concat([Buffer.from([0x81, payload.length]), payload]);
}

/** The next chunk `sock` receives. */
function onceData(sock: Socket): Promise<Buffer> {
  return new Promise((resolve) => sock.once("data", (d: Buffer) => resolve(d)));
}

describe("a lobby on a real socket", () => {
  let tcp: Server | null = null;
  let wss: WebSocketServer | null = null;

  afterEach(async () => {
    const t = tcp;
    tcp = null;
    if (t) await new Promise<void>((resolve) => t.close(() => resolve()));
    const w = wss;
    wss = null;
    if (w) await new Promise<void>((resolve) => w.close(() => resolve()));
  });

  async function serveWs(handler: (sock: import("ws").WebSocket) => void): Promise<string> {
    const server = new WebSocketServer({ host: "127.0.0.1", port: 0 });
    wss = server;
    await new Promise<void>((resolve) => server.once("listening", () => resolve()));
    server.on("connection", handler);
    const { port } = server.address() as AddressInfo;
    return `ws://127.0.0.1:${port}/ws/external/bot/b1`;
  }

  it("drops a lobby that goes silent after its hello and reconnects", async () => {
    // The first connection completes the upgrade, sends `hello` and then never
    // answers anything again: no pings, no pong, and no reply to a close frame,
    // as a socket the server already closed while the host slept. A polite
    // close would wait out ws's 30s close timeout; the SDK aborts instead.
    let connects = 0;
    let resolveDropped: () => void = () => {};
    const firstDropped = new Promise<void>((resolve) => {
      resolveDropped = resolve;
    });

    const server = createServer((sock) => {
      connects += 1;
      const n = connects;
      sock.on("error", () => {});
      void (async () => {
        const request = (await onceData(sock)).toString();
        const key = /sec-websocket-key:\s*(\S+)/i.exec(request)![1]!;
        const accept = createHash("sha1").update(key + WS_GUID).digest("base64");
        sock.write(
          "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n" +
            `Connection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`,
        );
        await onceData(sock); // the client's authenticate frame
        sock.write(wsTextFrame({ type: "hello", endpoint: "lobby" }));
        if (n > 1) {
          // The reconnect: end the session cleanly with an evict.
          sock.write(wsTextFrame({ type: "evict" }));
          sock.write(Buffer.from([0x88, 0x02, 0x03, 0xe8])); // close 1000
          sock.end();
          return;
        }
        // Dead from here: ignore everything until the client drops the TCP
        // connection.
        sock.on("data", () => {});
        sock.on("close", () => resolveDropped());
      })();
    });
    tcp = server;
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", () => resolve()));
    const { port } = server.address() as AddressInfo;

    const started = Date.now();
    const results = await withGuard(
      runExternalBot(new FoldBot(), {
        url: `ws://127.0.0.1:${port}/ws/external/bot/b1`,
        token: "cz_extbot_x",
        config: null,
        retryPolicy: FAST_POLICY,
        lobbySilenceTimeoutMs: 400,
      }),
      4000,
    );
    const elapsed = Date.now() - started;
    await withGuard(firstDropped, 2000);

    expect(connects).toBe(2); // the silent lobby was dropped and reconnected
    expect(results).toEqual([]);
    expect(silenceLines()).toHaveLength(1);
    // Aborted, not closed with a handshake the dead peer never answers.
    expect(elapsed).toBeLessThan(3000);
  });

  it("keeps a lobby that pings, and answers every ping", async () => {
    // Also the regression test for frames lost after an idle recv timeout:
    // the loop wakes several times between pings, and every ping still lands.
    let connects = 0;
    let pongs = 0;
    const url = await serveWs((sock) => {
      connects += 1;
      let sent = 0;
      sock.on("message", (data) => {
        const msg = JSON.parse(data.toString()) as Json;
        if (msg.type === "authenticate") {
          sock.send(JSON.stringify({ type: "hello", endpoint: "lobby" }));
        } else if (msg.type === "pong") {
          pongs += 1;
        }
      });
      // 8 x 100ms = 4x the 200ms silence timeout.
      const timer = setInterval(() => {
        if (sent < 8) {
          sent += 1;
          sock.send(JSON.stringify({ type: "ping" }));
        } else {
          clearInterval(timer);
          sock.send(JSON.stringify({ type: "evict" }));
        }
      }, 100);
      sock.on("close", () => clearInterval(timer));
    });

    await withGuard(
      runExternalBot(new FoldBot(), {
        url,
        token: "cz_extbot_x",
        config: null,
        retryPolicy: FAST_POLICY,
        lobbySilenceTimeoutMs: 200,
      }),
      4000,
    );

    expect(connects).toBe(1);
    expect(pongs).toBe(8);
    expect(silenceLines()).toEqual([]);
  });

  it("leaves a quiet lobby alone when the check is turned off", async () => {
    let connects = 0;
    const url = await serveWs((sock) => {
      connects += 1;
      sock.once("message", () => {
        sock.send(JSON.stringify({ type: "hello", endpoint: "lobby" }));
        setTimeout(() => sock.send(JSON.stringify({ type: "evict" })), 600);
      });
    });

    await withGuard(
      runExternalBot(new FoldBot(), {
        url,
        token: "cz_extbot_x",
        config: null,
        retryPolicy: FAST_POLICY,
        lobbySilenceTimeoutMs: null,
      }),
      4000,
    );

    expect(connects).toBe(1);
    expect(silenceLines()).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// The option
// ---------------------------------------------------------------------------

describe("lobbySilenceTimeoutMs", () => {
  it.each([0, -1, Number.NaN])("rejects %s", async (bad) => {
    await expect(
      runExternalBot(new FoldBot(), {
        url: "ws://127.0.0.1:1/ws/external/bot/b1",
        token: "cz_extbot_x",
        config: null,
        lobbySilenceTimeoutMs: bad,
      }),
    ).rejects.toThrow(/lobbySilenceTimeoutMs/);
  });

  it("defaults to three missed heartbeats", () => {
    // The server pings every 15s; the default must tolerate two missed pings.
    expect(DEFAULT_LOBBY_SILENCE_TIMEOUT_MS).toBe(45_000);
    expect(PUBLIC_DEFAULT).toBe(45_000);
  });
});

// ---------------------------------------------------------------------------
// Dropping the socket: abort when the transport has it, close otherwise
// ---------------------------------------------------------------------------

describe("dropping a silent lobby", () => {
  /**
   * A transport whose first lobby sends `hello` and then nothing; the
   * reconnect sends `hello` and an `evict`, which ends the run.
   */
  function silentTransport(withAbort: boolean): {
    transport: Transport;
    calls: { connects: number; aborts: number; closes: number };
  } {
    const calls = { connects: 0, aborts: 0, closes: 0 };
    const transport: Transport = async () => {
      calls.connects += 1;
      const frames: Json[] = [{ type: "hello", endpoint: "lobby" }];
      if (calls.connects > 1) frames.push({ type: "evict" });
      const conn: ExternalConnection = {
        send: () => {},
        reader: {
          next: () => {
            const frame = frames.shift();
            if (frame) return Promise.resolve(JSON.stringify(frame));
            return new Promise<string | null>(() => {}); // silent forever
          },
        },
        close: () => {
          calls.closes += 1;
        },
      };
      if (withAbort) {
        conn.abort = () => {
          calls.aborts += 1;
        };
      }
      return conn;
    };
    return { transport, calls };
  }

  const policy = new RetryPolicy({ maxReconnectAttempts: 3, initialBackoffMs: 1, maxBackoffMs: 1 });

  it("aborts the connection when the transport can", async () => {
    const { transport, calls } = silentTransport(true);
    await withGuard(
      runExternalBot(new FoldBot(), {
        url: "ws://lobby.test/ws/external/bot/b1",
        token: "cz_extbot_x",
        config: null,
        retryPolicy: policy,
        lobbySilenceTimeoutMs: 50,
        transport,
      }),
      4000,
    );
    expect(calls.connects).toBe(2); // the silent lobby, then the reconnect
    expect(calls.aborts).toBe(1);
    expect(silenceLines()).toHaveLength(1);
  });

  it("falls back to close for a transport without abort", async () => {
    const { transport, calls } = silentTransport(false);
    await withGuard(
      runExternalBot(new FoldBot(), {
        url: "ws://lobby.test/ws/external/bot/b1",
        token: "cz_extbot_x",
        config: null,
        retryPolicy: policy,
        lobbySilenceTimeoutMs: 50,
        transport,
      }),
      4000,
    );
    expect(calls.connects).toBe(2);
    expect(calls.closes).toBe(2); // the dropped lobby, then the evicted one
    expect(silenceLines()).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// LobbyLiveness: host sleep and clock steps
// ---------------------------------------------------------------------------

/** Stand-ins for `performance.now()` and `Date.now()`. */
class FakeClocks implements LivenessClocks {
  mono = 1_000_000;
  wallMs = 1_700_000_000_000;
  monotonic(): number {
    return this.mono;
  }
  wall(): number {
    return this.wallMs;
  }
  advance(ms: number): void {
    this.mono += ms;
    this.wallMs += ms;
  }
}

describe("LobbyLiveness", () => {
  it("expires after the timeout", () => {
    const clocks = new FakeClocks();
    const live = new LobbyLiveness(45_000, clocks);
    clocks.advance(44_000);
    expect(live.expired()).toBe(false);
    clocks.advance(1_000);
    expect(live.expired()).toBe(true);
  });

  it("resets the silence on a frame", () => {
    const clocks = new FakeClocks();
    const live = new LobbyLiveness(45_000, clocks);
    clocks.advance(40_000);
    live.mark();
    clocks.advance(40_000);
    expect(live.expired()).toBe(false);
    expect(live.silentForMs()).toBe(40_000);
  });

  it("sees host sleep through the wall clock", () => {
    // macOS and Linux stop the monotonic clock while asleep; the wall clock
    // keeps going, so the first check after waking sees the whole gap.
    const clocks = new FakeClocks();
    const live = new LobbyLiveness(45_000, clocks);
    clocks.wallMs += 45 * 60_000; // 45 minutes asleep: monotonic did not move
    clocks.advance(2_000); // the first poll after waking
    expect(live.expired()).toBe(true);
    expect(live.silentForMs()).toBe(45 * 60_000 + 2_000);
  });

  it("still expires on the monotonic clock when the wall clock steps back", () => {
    const clocks = new FakeClocks();
    const live = new LobbyLiveness(45_000, clocks);
    clocks.wallMs -= 3_600_000; // NTP steps the wall clock back an hour
    clocks.advance(45_000);
    expect(live.expired()).toBe(true);
  });

  it("never expires when turned off", () => {
    const clocks = new FakeClocks();
    const live = new LobbyLiveness(null, clocks);
    clocks.advance(10_000_000);
    expect(live.expired()).toBe(false);
  });

  it("reads the real clocks by default", () => {
    const live = new LobbyLiveness(45_000);
    expect(live.silentForMs()).toBeGreaterThanOrEqual(0);
    expect(live.expired()).toBe(false);
  });
});
