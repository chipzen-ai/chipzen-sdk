/**
 * The client's `supported_games` declaration (chipzen-sdk#171). Mirrors the
 * Python SDK's `tests/test_supported_games.py`.
 *
 * A bot may declare the games it can actually play; the declaration rides
 * the Layer 1 client `hello` as `supported_games`, a list of `game_type`
 * strings (`docs/protocol/LAYER2-COMMON.md` section 2). The platform reads
 * an **absent** declaration as "poker only", which is what every deployed
 * bot relies on, so this file pins two things at once:
 *
 * - declaring something puts exactly that list on the wire, under exactly
 *   that key, on the client `hello` (nowhere else); and
 * - declaring nothing leaves the `hello` frame **byte-identical** to the one
 *   the SDK has always sent: same keys, same order, same JSON text.
 *
 * The second is the load-bearing one. The server closes the socket on any
 * handshake key outside its allowlist, and it reads an absent list ("assume
 * poker") and an empty one ("supports nothing") as different answers. A
 * default that sent `"supported_games": []` would break every bot in the
 * fleet the first time a variant table existed.
 *
 * Both runners are covered: `runBot` (the container runner) against a real
 * local WebSocket server, and `runExternalBot` over a fake transport.
 */

import type { AddressInfo } from "node:net";

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { WebSocketServer } from "ws";

import { Bot } from "../src/bot.js";
import {
  _runSession,
  runBot,
  type AsyncMessageReader,
  type SessionWebSocket,
} from "../src/client.js";
import {
  _resetSleep,
  _setLobbyRecvTimeoutMs,
  runExternalBot,
  type ExternalConnection,
  type Transport,
} from "../src/external.js";
import { Action, type GameState } from "../src/models.js";

type Json = Record<string, unknown>;

const MATCH_ID = "m_test_abc123";

/**
 * The platform's handshake field allowlist (`ALLOWED_HANDSHAKE_FIELDS`), the
 * same set the Python test pins. A `hello` carrying any key outside it is
 * closed before the bot is seated, so the SDK may only add keys from here.
 */
const SERVER_ALLOWED_HANDSHAKE_FIELDS = new Set([
  "type",
  "match_id",
  "ticket",
  "token",
  "supported_versions",
  "client_name",
  "client_version",
  "supported_games",
]);

/**
 * The exact client `hello` text the JS SDK has always sent. Frozen as a
 * literal string rather than an object so a change in key ORDER (which
 * `JSON.stringify` preserves) fails this test too.
 */
const FROZEN_HELLO_JSON = '{"type":"hello","match_id":"m_test_abc123","supported_versions":["1.0"]}';

/** A poker table's server hello: no `game` descriptor, so state shape NLHE. */
const SERVER_HELLO: Json = {
  type: "hello",
  match_id: MATCH_ID,
  seq: 1,
  supported_versions: ["1.0"],
  selected_version: "1.0",
  game_type: "nlhe_6max",
  capabilities: [],
};

const MATCH_END: Json = { type: "match_end", match_id: MATCH_ID, seq: 2, reason: "complete", results: [] };

class NoopBot extends Bot {
  decide(_state: GameState): Action {
    return Action.fold();
  }
}

/** Replays the given frames, then reports the socket closed. */
class ScriptedReader implements AsyncMessageReader {
  private idx = 0;
  constructor(private readonly frames: Json[]) {}
  async next(): Promise<string | null> {
    if (this.idx >= this.frames.length) return null;
    return JSON.stringify(this.frames[this.idx++]);
  }
}

/** Keeps every frame the client sent as the raw text that went on the wire. */
class RawCapturingSocket implements SessionWebSocket {
  readonly sent: string[] = [];
  send(data: string): void {
    this.sent.push(data);
  }
  close(): void {}
}

/** Run one handshake through `_runSession` and return the raw frames the client sent. */
async function handshake(supportedGames?: readonly string[] | null): Promise<string[]> {
  const ws = new RawCapturingSocket();
  const ctx = {
    matchId: MATCH_ID,
    token: "test_token",
    ticket: null,
    clientName: "chipzen-sdk-test",
    clientVersion: "0.2.0",
    ...(supportedGames === undefined ? {} : { supportedGames }),
  };
  await _runSession(ws, new NoopBot(), ctx, new ScriptedReader([SERVER_HELLO]));
  return ws.sent;
}

// ---------------------------------------------------------------------------
// Default: nothing declared -> nothing on the wire
// ---------------------------------------------------------------------------

describe("default hello (nothing declared)", () => {
  it("is byte-identical to the frozen frame", async () => {
    const hello = (await handshake())[1]!;
    expect(hello).toBe(FROZEN_HELLO_JSON);
    expect(hello).not.toContain("supported_games");
  });

  it("omits the key entirely rather than sending an empty list", async () => {
    const parsed = JSON.parse((await handshake())[1]!) as Json;
    expect("supported_games" in parsed).toBe(false);
  });

  it("an explicit null is the same as omitting the option", async () => {
    expect(await handshake(null)).toEqual(await handshake());
  });
});

// ---------------------------------------------------------------------------
// Declaring
// ---------------------------------------------------------------------------

describe("declaring supportedGames", () => {
  it("rides the client hello with the right shape, and not the authenticate frame", async () => {
    const sent = await handshake(["poker", "draw27"]);
    const authenticate = JSON.parse(sent[0]!) as Json;
    const hello = JSON.parse(sent[1]!) as Json;

    expect(authenticate.type).toBe("authenticate");
    expect("supported_games" in authenticate).toBe(false);

    expect(hello.type).toBe("hello");
    expect(hello.supported_games).toEqual(["poker", "draw27"]);
    expect((hello.supported_games as unknown[]).every((g) => typeof g === "string")).toBe(true);
  });

  it("is appended last, leaving the prior keys and their order untouched", async () => {
    const hello = JSON.parse((await handshake(["draw27"]))[1]!) as Json;
    const frozen = JSON.parse(FROZEN_HELLO_JSON) as Json;
    expect(Object.keys(hello)).toEqual([...Object.keys(frozen), "supported_games"]);
    for (const [k, v] of Object.entries(frozen)) expect(hello[k]).toEqual(v);
  });

  it("keeps the hello keys inside the server's handshake field allowlist", async () => {
    for (const declared of [undefined, ["poker", "draw27", "ofc"]]) {
      const hello = JSON.parse((await handshake(declared))[1]!) as Json;
      for (const key of Object.keys(hello)) expect(SERVER_ALLOWED_HANDSHAKE_FIELDS.has(key)).toBe(true);
    }
  });

  it("sends an empty list verbatim rather than dropping it", async () => {
    // `[]` means "I support nothing". Rewriting it to "absent" would tell
    // the server "assume poker", the opposite of what the caller said.
    const hello = JSON.parse((await handshake([]))[1]!) as Json;
    expect(hello.supported_games).toEqual([]);
  });

  it("copies the caller's list rather than aliasing it", async () => {
    const declared = ["poker"];
    const sent = await handshake(declared);
    declared.push("draw27");
    expect((JSON.parse(sent[1]!) as Json).supported_games).toEqual(["poker"]);
  });
});

// ---------------------------------------------------------------------------
// The container runner: runBot against a real local WebSocket server
// ---------------------------------------------------------------------------

describe("runBot threads supportedGames to the client hello", () => {
  let wss: WebSocketServer | null = null;

  afterEach(async () => {
    const server = wss;
    wss = null;
    if (server) await new Promise<void>((resolve) => server.close(() => resolve()));
  });

  /** Serve one scripted match and resolve with the raw client hello text. */
  async function serveOneMatch(): Promise<{ url: string; hello: Promise<string> }> {
    const server = new WebSocketServer({ host: "127.0.0.1", port: 0 });
    wss = server;
    await new Promise<void>((resolve) => server.once("listening", () => resolve()));
    const { port } = server.address() as AddressInfo;
    const hello = new Promise<string>((resolve) => {
      server.once("connection", (sock) => {
        sock.on("message", (data) => {
          const text = data.toString();
          const msg = JSON.parse(text) as Json;
          if (msg.type === "authenticate") {
            sock.send(JSON.stringify(SERVER_HELLO));
          } else if (msg.type === "hello") {
            resolve(text);
            sock.send(JSON.stringify(MATCH_END));
          }
        });
      });
    });
    return { url: `ws://127.0.0.1:${port}/ws/match/${MATCH_ID}/bot`, hello };
  }

  it("a poker bot declaring poker and draw27 carries the list", async () => {
    const { url, hello } = await serveOneMatch();
    const end = await runBot(url, new NoopBot(), {
      token: "tok",
      maxRetries: 0,
      supportedGames: ["poker", "draw27"],
    });
    expect(end?.type).toBe("match_end");
    expect((JSON.parse(await hello) as Json).supported_games).toEqual(["poker", "draw27"]);
  });

  it("the default runBot hello is the frozen frame", async () => {
    const { url, hello } = await serveOneMatch();
    await runBot(url, new NoopBot(), { token: "tok", maxRetries: 0 });
    expect(await hello).toBe(FROZEN_HELLO_JSON);
  });
});

// ---------------------------------------------------------------------------
// The external-API runner threads the same declaration to every match
// ---------------------------------------------------------------------------

describe("runExternalBot threads supportedGames to each match hello", () => {
  beforeEach(() => _setLobbyRecvTimeoutMs(5));
  afterEach(() => {
    _setLobbyRecvTimeoutMs(2000);
    _resetSleep();
  });

  /** Script the lobby (one `matched`) and a gateway that plays one empty match. */
  function transport(): { transport: Transport; sent: string[] } {
    const sent: string[] = [];
    const reader = (frames: Json[], block: boolean): AsyncMessageReader => {
      let i = 0;
      return {
        next: async () => {
          if (i < frames.length) return JSON.stringify(frames[i++]);
          return block ? new Promise<string | null>(() => {}) : null;
        },
      };
    };
    const t: Transport = async (url) => {
      const gateway = url.includes("/ws/external/match/");
      const conn: ExternalConnection = {
        send: (data: string) => {
          if (gateway) sent.push(data);
        },
        reader: gateway
          ? reader([SERVER_HELLO, MATCH_END], false)
          : reader(
              [
                { type: "hello", endpoint: "lobby" },
                {
                  type: "matched",
                  match_id: MATCH_ID,
                  participant_id: "p1",
                  gateway_ws_url: `/ws/external/match/${MATCH_ID}/p1`,
                  rated: false,
                },
              ],
              true,
            ),
        close: () => {},
      };
      return conn;
    };
    return { transport: t, sent };
  }

  const clientHello = (sent: string[]): Json | undefined =>
    sent.map((s) => JSON.parse(s) as Json).find((m) => m.type === "hello");

  it("forwards the declaration to the match", async () => {
    const { transport: t, sent } = transport();
    const results = await runExternalBot(new NoopBot(), {
      url: "wss://example.test/ws/external/bot/b1",
      token: "cz_extbot_x",
      config: null,
      maxMatches: 1,
      supportedGames: ["poker", "draw27"],
      transport: t,
    });
    expect(results).toHaveLength(1);
    expect(clientHello(sent)?.supported_games).toEqual(["poker", "draw27"]);
  });

  it("declares nothing by default", async () => {
    const { transport: t, sent } = transport();
    await runExternalBot(new NoopBot(), {
      url: "wss://example.test/ws/external/bot/b1",
      token: "cz_extbot_x",
      config: null,
      maxMatches: 1,
      transport: t,
    });
    const hello = clientHello(sent);
    expect(hello).toBeDefined();
    expect("supported_games" in hello!).toBe(false);
  });
});
