/**
 * `chipzen-sdk run-external` builds a fresh bot for every match, so
 * overlapping matches do not share one instance and its per-match state
 * (chipzen-ai/chipzen-sdk#290).
 *
 * The CLI calls the real `runExternalBot`; the mock below only adds a fake
 * transport to its options, so the lobby -> matched -> gateway path runs
 * without a server.
 */

import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Bot } from "../src/bot.js";
import {
  _setLobbyRecvTimeoutMs,
  type ExternalConnection,
  type Transport,
} from "../src/external.js";
import { Action } from "../src/models.js";
import { perMatchFactory, runExternalCli } from "../src/run_external.js";

const testTransport = vi.hoisted(() => ({ current: undefined as unknown }));

vi.mock("../src/external.js", async (importOriginal) => {
  const real = await importOriginal<typeof import("../src/external.js")>();
  return {
    ...real,
    runExternalBot: (bot: Parameters<typeof real.runExternalBot>[0], options = {}) =>
      real.runExternalBot(bot, {
        ...options,
        transport: testTransport.current as Transport,
      }),
  };
});

// ---------------------------------------------------------------------------
// Fake transport: the lobby dispatches every match id back to back; each
// gateway plays one short match and holds back `match_end` until all the
// matches have started, so they overlap.
// ---------------------------------------------------------------------------

/** A reader over a frame list; an async function entry runs before the next frame. */
class Reader {
  private idx = 0;
  constructor(
    private readonly frames: Array<Record<string, unknown> | (() => Promise<void>)>,
    private readonly blockWhenDone: boolean,
  ) {}
  async next(): Promise<string | null> {
    if (this.idx < this.frames.length) {
      const frame = this.frames[this.idx++]!;
      if (typeof frame === "function") {
        await frame();
        return this.next();
      }
      return JSON.stringify(frame);
    }
    if (!this.blockWhenDone) return null;
    return new Promise<string | null>(() => {
      /* the lobby blocks; its recv timeout re-checks the stop signal */
    });
  }
}

interface OverlapState {
  gateway: string[];
  started: Set<string>;
}

function overlapTransport(matchIds: string[]): { transport: Transport; state: OverlapState } {
  const state: OverlapState = { gateway: [], started: new Set() };
  let openAll!: () => void;
  const allStarted = new Promise<void>((resolve) => {
    openAll = resolve;
  });

  const transport: Transport = async (url) => {
    const conn = (reader: Reader): ExternalConnection => ({
      send: () => {},
      reader,
      close: () => {},
    });
    if (!url.includes("/ws/external/match/")) {
      return conn(
        new Reader(
          [
            { type: "hello", endpoint: "lobby" },
            ...matchIds.map((id) => ({
              type: "matched",
              match_id: id,
              participant_id: "p1",
              gateway_ws_url: `/ws/external/match/${id}/p1`,
              rated: false,
            })),
          ],
          true,
        ),
      );
    }
    const id = url.split("/ws/external/match/")[1]!.split("/")[0]!;
    state.gateway.push(id);
    return conn(
      new Reader(
        [
          { type: "hello", selected_version: "1.0", game_type: "nlhe_6max" },
          {
            type: "match_start",
            match_id: id,
            seats: [
              { seat: 0, is_self: true },
              { seat: 1, is_self: false },
            ],
            turn_timeout_ms: 5000,
          },
          async () => {
            state.started.add(id);
            if (matchIds.every((m) => state.started.has(m))) openAll();
          },
          {
            type: "turn_request",
            match_id: id,
            request_id: `req_${id}`,
            valid_actions: ["fold", "call", "raise"],
            state: {
              phase: "preflop",
              board: [],
              your_hole_cards: ["Ah", "Kd"],
              pot: 15,
              to_call: 5,
              min_raise: 20,
              max_raise: 995,
            },
          },
          () => allStarted,
          { type: "match_end", match_id: id, reason: "complete" },
        ],
        false,
      ),
    );
  };
  return { transport, state };
}

// ---------------------------------------------------------------------------
// The bot file the CLI loads. It gets the SDK's `Bot` from a global, so the
// file needs no package resolution from the temp dir, and it registers every
// instance it builds so the test can inspect them.
// ---------------------------------------------------------------------------

interface StatefulBot {
  matchIds: string[];
  decisions: number;
}

const STATEFUL_BOT_FILE = `
const { Bot, Action, instances } = globalThis.__chipzenPerMatchTest;

export class StatefulBot extends Bot {
  constructor() {
    super();
    this.matchIds = [];
    this.decisions = 0;
    instances.push(this);
  }
  onMatchStart(matchInfo) {
    this.matchIds.push(matchInfo.match_id);
  }
  decide() {
    this.decisions += 1;
    return Action.fold();
  }
}
`;

let tmpRoot: string;
let savedEnv: string | undefined;
let instances: StatefulBot[];

beforeEach(() => {
  tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), "chipzen-permatch-"));
  savedEnv = process.env.CHIPZEN_ENV;
  delete process.env.CHIPZEN_ENV;
  instances = [];
  (globalThis as Record<string, unknown>).__chipzenPerMatchTest = { Bot, Action, instances };
  _setLobbyRecvTimeoutMs(5);
  vi.spyOn(console, "error").mockImplementation(() => {});
  vi.spyOn(process, "exit").mockImplementation(((code?: number) => {
    throw new Error(`process.exit(${code})`);
  }) as never);
});
afterEach(() => {
  fs.rmSync(tmpRoot, { recursive: true, force: true });
  if (savedEnv === undefined) delete process.env.CHIPZEN_ENV;
  else process.env.CHIPZEN_ENV = savedEnv;
  delete (globalThis as Record<string, unknown>).__chipzenPerMatchTest;
  _setLobbyRecvTimeoutMs(2000);
  testTransport.current = undefined;
  vi.restoreAllMocks();
});

async function runCli(matchIds: string[]): Promise<OverlapState> {
  const { transport, state } = overlapTransport(matchIds);
  testTransport.current = transport;
  const botFile = path.join(tmpRoot, "stateful-bot.mjs");
  fs.writeFileSync(botFile, STATEFUL_BOT_FILE, "utf-8");
  await runExternalCli([
    botFile,
    "--bot-id",
    "overlap-bot",
    "--token",
    "cz_extbot_x",
    "--max-matches",
    String(matchIds.length),
  ]);
  return state;
}

describe("run-external builds one bot per match", () => {
  it("two overlapping matches get two distinct bots", async () => {
    const state = await runCli(["m1", "m2"]);

    // Both matches really ran, and at the same time.
    expect([...state.gateway].sort()).toEqual(["m1", "m2"]);
    expect(state.started).toEqual(new Set(["m1", "m2"]));

    expect(instances).toHaveLength(2);
    expect(instances[0]).not.toBe(instances[1]);
    // Per-match state stays with its own match: each bot saw exactly one
    // match start and one decision.
    expect(instances.map((b) => b.matchIds).sort()).toEqual([["m1"], ["m2"]]);
    expect(instances.map((b) => b.decisions)).toEqual([1, 1]);
  });

  it("a single match builds one bot", async () => {
    await runCli(["m1"]);
    expect(instances).toHaveLength(1);
    expect(instances[0]!.matchIds).toEqual(["m1"]);
    expect(instances[0]!.decisions).toBe(1);
  });
});

describe("perMatchFactory", () => {
  class B extends Bot {
    decide(): Action {
      return Action.fold();
    }
  }

  it("hands out the first instance, then fresh ones", () => {
    const first = new B();
    const factory = perMatchFactory(B, first);
    const [a, b, c] = [factory(), factory(), factory()];
    expect(a).toBe(first);
    expect(b).toBeInstanceOf(B);
    expect(c).toBeInstanceOf(B);
    expect(new Set([a, b, c]).size).toBe(3);
  });

  it("names the class when a later build fails", () => {
    let n = 0;
    class Flaky extends Bot {
      constructor() {
        super();
        n += 1;
        if (n > 1) throw new Error("out of memory");
      }
      decide(): Action {
        return Action.fold();
      }
    }
    const factory = perMatchFactory(Flaky, new Flaky());
    factory();
    expect(() => factory()).toThrow("failed to instantiate Flaky: out of memory");
  });
});
