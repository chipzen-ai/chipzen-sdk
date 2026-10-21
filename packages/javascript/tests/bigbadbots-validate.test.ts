/**
 * The Big Bad Bots conformance profile of `chipzen-sdk validate`
 * (chipzen-sdk#151 slice 2), and the JavaScript Rusher starter run through it.
 *
 * The checker is held to the reference engine's own verdicts: every pair in
 * the bundled fixtures (the Python SDK's, byte for byte) carries the label the
 * engine gave its answer, and `checkBigBadBotsTurnAction` must agree on each.
 */

import { promises as fs, mkdtempSync, readFileSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";

import { type BigBadBotsTurnState, type FightState } from "../src/bigbadbots.js";
import { BigBadBotsBot } from "../src/bigbadbots_bot.js";
import {
  BIGBADBOTS_CALLS,
  BIGBADBOTS_VALIDATE_GAME_CONFIG,
  bigBadBotsCallOf,
  checkBigBadBotsAnswer,
  checkBigBadBotsTurnAction,
  loadBigBadBotsFixtures,
  runBigBadBotsChecks,
  type BigBadBotsCheck,
} from "../src/bigbadbots_validate.js";
import * as sdk from "../src/index.js";
import { Action } from "../src/models.js";
import { validateBot } from "../src/validate.js";

type Json = Record<string, unknown>;

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PKG = path.resolve(HERE, "..");
const REPO = path.resolve(PKG, "..", "..");
const PY_FIXTURES = path.join(REPO, "packages", "python", "src", "chipzen", "fixtures", "bigbadbots");
const JS_FIXTURES = path.join(PKG, "src", "data", "bigbadbots", "fixtures");
const SDK_DIST = pathToFileURL(path.join(PKG, "dist", "index.js")).href;
const STARTER = path.join(PKG, "starters", "bigbadbots", "rusher");

let workDir: string;

beforeAll(() => {
  workDir = mkdtempSync(path.join(tmpdir(), "chipzen-bbb-validate-"));
});

afterAll(() => {
  rmSync(workDir, { recursive: true, force: true });
});

/** Write a bot directory whose `@chipzen-ai/bot` import resolves to the in-repo build. */
async function writeBot(name: string, source: string): Promise<string> {
  const dir = path.join(workDir, name);
  await fs.mkdir(dir, { recursive: true });
  await fs.writeFile(
    path.join(dir, "bot.js"),
    source.replace(/from\s+["']@chipzen-ai\/bot["']/g, `from ${JSON.stringify(SDK_DIST)}`),
    "utf-8",
  );
  await fs.writeFile(
    path.join(dir, "package.json"),
    JSON.stringify({ name, version: "0.0.0", type: "module", main: "bot.js" }),
    "utf-8",
  );
  return dir;
}

function byName(results: BigBadBotsCheck[] | { name: string; severity: string }[], name: string) {
  return results.filter((r) => r.name === name);
}

function fails(results: { severity: string }[]) {
  return results.filter((r) => r.severity === "fail");
}

// ---------------------------------------------------------------------------
// The fixtures
// ---------------------------------------------------------------------------

describe("the bundled conformance fixtures", () => {
  it("are the Python SDK's files, byte for byte", () => {
    const files = (dir: string) =>
      readdirSync(dir)
        .filter((f) => f.endsWith(".json"))
        .sort();
    expect(files(JS_FIXTURES)).toEqual(files(PY_FIXTURES));
    for (const file of files(PY_FIXTURES)) {
      expect(readFileSync(path.join(JS_FIXTURES, file), "utf-8"), file).toBe(
        readFileSync(path.join(PY_FIXTURES, file), "utf-8"),
      );
    }
  });

  it("load in match order, one call or all, as fresh copies", async () => {
    const all = await loadBigBadBotsFixtures();
    expect(all.length).toBeGreaterThan(100);
    const calls = all.map((p) => bigBadBotsCallOf(p.request));
    expect([...new Set(calls)]).toEqual([...BIGBADBOTS_CALLS]);
    const bids = await loadBigBadBotsFixtures("bid");
    expect(bids.every((p) => bigBadBotsCallOf(p.request) === "bid")).toBe(true);
    (bids[0]!.request as Json).request_id = "changed";
    expect((await loadBigBadBotsFixtures("bid"))[0]!.request.request_id).not.toBe("changed");
  });

  it("hold accepted and rejected answers", async () => {
    const labels = new Set((await loadBigBadBotsFixtures()).map((p) => p.expect));
    expect(labels.has("ok")).toBe(true);
    expect(labels.has("malformed")).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Judging one answer
// ---------------------------------------------------------------------------

describe("checkBigBadBotsTurnAction agrees with the reference engine", async () => {
  const pairs = await loadBigBadBotsFixtures();
  it.each(pairs.map((p) => [`${p.source} ${String(p.request.request_id)}`, p] as const))(
    "%s",
    (_name, pair) => {
      expect(checkBigBadBotsTurnAction(pair.request, pair.answer).verdict).toBe(pair.expect);
    },
  );
});

describe("checkBigBadBotsAnswer", async () => {
  const [bodyShop] = await loadBigBadBotsFixtures("choose_body");
  const auction = (await loadBigBadBotsFixtures("bid")).find((p) => p.expect === "ok")!;
  const fitting = (await loadBigBadBotsFixtures("fit")).find((p) => p.expect === "ok")!;
  const [fight] = await loadBigBadBotsFixtures("act");

  it("passes the SDK's own legal answers", () => {
    const offer = (bodyShop!.request.state as Json).offer as Json[];
    expect(checkBigBadBotsAnswer(bodyShop!.request, Action.chooseBody(offer[0]!.id as string))).toEqual({
      verdict: "ok",
      reason: "",
    });
    expect(checkBigBadBotsAnswer(auction.request, Action.bid(0)).verdict).toBe("ok");
    expect(checkBigBadBotsAnswer(fitting.request, Action.fit({})).verdict).toBe("ok");
    expect(checkBigBadBotsAnswer(fight!.request, Action.act("block")).verdict).toBe("ok");
  });

  it("refuses what is not an Action", () => {
    expect(checkBigBadBotsAnswer(fight!.request, "block")).toMatchObject({ verdict: "malformed" });
    expect(checkBigBadBotsAnswer(fight!.request, undefined).reason).toContain("not an Action");
    expect(checkBigBadBotsAnswer(fight!.request, Promise.resolve(Action.act("idle"))).reason).toContain(
      "Promise",
    );
  });

  it("judges an answer by its shape, not its class (a bot may import its own SDK copy)", () => {
    const lookalike = { toWire: () => ({ action: "block", params: {} }) };
    expect(checkBigBadBotsAnswer(fight!.request, lookalike).verdict).toBe("ok");
    const throwing = {
      toWire: () => {
        throw new Error("boom");
      },
    };
    expect(checkBigBadBotsAnswer(fight!.request, throwing)).toMatchObject({
      verdict: "malformed",
      reason: expect.stringContaining("boom"),
    });
  });

  it("corrects a bid above the credits and a body that is not on the offer", () => {
    const credits = ((auction.request.state as Json).me as Json).credits as number;
    expect(checkBigBadBotsAnswer(auction.request, Action.bid(credits + 1))).toMatchObject({
      verdict: "corrected",
      reason: `bid ${credits + 1} is outside 0..${credits}; it is clamped to ${credits}`,
    });
    expect(checkBigBadBotsAnswer(bodyShop!.request, Action.chooseBody("nobody")).verdict).toBe(
      "corrected",
    );
    expect(checkBigBadBotsAnswer(fitting.request, Action.fit({ 7: "capacitor" })).verdict).toBe(
      "corrected",
    );
  });

  it("reads a bid as the server does, from the text", () => {
    const text = (amount: string) =>
      `{"type": "turn_action", "match_id": "m", "request_id": ${JSON.stringify(
        auction.request.request_id,
      )}, "action": "bid", "params": {"amount": ${amount}}}`;
    expect(checkBigBadBotsTurnAction(auction.request, text("5")).verdict).toBe("ok");
    expect(checkBigBadBotsTurnAction(auction.request, text("5.0")).reason).toContain(
      "not a JSON integer",
    );
    expect(checkBigBadBotsTurnAction(auction.request, text("1e2")).verdict).toBe("malformed");
    expect(checkBigBadBotsTurnAction(auction.request, text("9223372036854775808")).reason).toContain(
      "signed 64-bit",
    );
    expect(checkBigBadBotsTurnAction(auction.request, text("-1")).verdict).toBe("corrected");
    expect(checkBigBadBotsTurnAction(auction.request, text('"5"')).verdict).toBe("malformed");
    expect(checkBigBadBotsTurnAction(auction.request, text("NaN")).verdict).toBe("malformed");
  });

  it("refuses an oversized message and a missing request_id echo", () => {
    expect(checkBigBadBotsTurnAction(fight!.request, " ".repeat(5000)).reason).toContain("4096");
    expect(
      checkBigBadBotsTurnAction(
        fight!.request,
        '{"type": "turn_action", "request_id": "other", "action": "idle"}',
      ).reason,
    ).toContain("request_id");
  });

  it("is on the public surface", () => {
    expect(sdk.checkBigBadBotsAnswer).toBe(checkBigBadBotsAnswer);
    expect(sdk.runBigBadBotsChecks).toBe(runBigBadBotsChecks);
    expect(sdk.VALIDATE_GAMES).toEqual(["nlhe", "bigbadbots"]);
  });
});

// ---------------------------------------------------------------------------
// Running a bot through the fixtures
// ---------------------------------------------------------------------------

class Blocker extends BigBadBotsBot {
  act(_state: FightState): Action {
    return Action.act("block");
  }
}

describe("runBigBadBotsChecks", () => {
  it("passes a bot that answers every call legally", async () => {
    const results = await runBigBadBotsChecks(new Blocker());
    expect(fails(results)).toEqual([]);
    for (const call of BIGBADBOTS_CALLS) {
      expect(byName(results, `bigbadbots_${call}`)).toEqual([
        expect.objectContaining({ severity: "pass" }),
      ]);
    }
    expect(byName(results, "bigbadbots_timing")[0]?.severity).toBe("pass");
    expect(byName(results, "bigbadbots_state_mutation")[0]?.severity).toBe("pass");
  });

  it("hands the bot the published game config first", async () => {
    const bot = new Blocker();
    await runBigBadBotsChecks(bot);
    expect(bot.gameConfig?.ruleSetId).toBe(BIGBADBOTS_VALIDATE_GAME_CONFIG.rule_set_id);
    expect(bot.rules?.deadline.deadlineMs).toBe(100);
  });

  it("fails a bot that throws on every tick", async () => {
    class Crasher extends BigBadBotsBot {
      act(): Action {
        throw new Error("no fight logic yet");
      }
    }
    const results = await runBigBadBotsChecks(new Crasher());
    expect(byName(results, "bigbadbots_act")[0]).toMatchObject({
      severity: "fail",
      message: expect.stringContaining("no fight logic yet"),
    });
    expect(byName(results, "bigbadbots_crash_every_tick")[0]?.severity).toBe("fail");
    expect(byName(results, "bigbadbots_bid")[0]?.severity).toBe("pass");
  });

  it("fails malformed answers and warns on corrected ones", async () => {
    class Sloppy extends BigBadBotsBot {
      override bid(state: sdk.AuctionState): Action {
        return Action.bid(state.me.credits + 50);
      }
      act(): Action {
        return Action.chooseBody("brawler");
      }
    }
    const results = await runBigBadBotsChecks(new Sloppy());
    expect(byName(results, "bigbadbots_bid")[0]?.severity).toBe("warn");
    expect(byName(results, "bigbadbots_act")[0]).toMatchObject({
      severity: "fail",
      message: expect.stringContaining("malformed"),
    });
  });

  it("catches process.exit() instead of exiting, and reports it", async () => {
    const realExit = process.exit;
    class Quitter extends BigBadBotsBot {
      act(): Action {
        process.exit(3);
      }
    }
    const results = await runBigBadBotsChecks(new Quitter());
    expect(process.exit).toBe(realExit);
    expect(byName(results, "bigbadbots_exit")[0]).toMatchObject({
      severity: "fail",
      message: expect.stringContaining("act() called process.exit(3)"),
    });
  });

  it("reports onMatchStart throwing, and keeps going", async () => {
    class BadStart extends Blocker {
      override onMatchStart(): void {
        throw new Error("setup failed");
      }
    }
    const results = await runBigBadBotsChecks(new BadStart());
    expect(byName(results, "bigbadbots_match_start")[0]).toMatchObject({ severity: "fail" });
    expect(byName(results, "bigbadbots_act")[0]?.severity).toBe("pass");
  });

  it("warns when act() is slow for the per-tick deadline", async () => {
    vi.useFakeTimers({ toFake: ["performance"] });
    try {
      class Slow extends BigBadBotsBot {
        act(): Action {
          vi.advanceTimersByTime(150);
          return Action.act("idle");
        }
      }
      const [pair] = await loadBigBadBotsFixtures("act");
      const results = await runBigBadBotsChecks(new Slow(), {
        fixtures: [{ ...pair!, request: { ...pair!.request, timeout_ms: 1000 } }],
      });
      expect(byName(results, "bigbadbots_timing")[0]).toMatchObject({
        severity: "warn",
        message: expect.stringContaining("100ms per-tick deadline"),
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it("accepts a plain decide() bot (validate --game bigbadbots)", async () => {
    const bot = {
      decide: (state: BigBadBotsTurnState) =>
        state.phase === "fight" ? Action.act("idle") : Action.bid(0),
    };
    const results = await runBigBadBotsChecks(bot);
    expect(byName(results, "bigbadbots_act")[0]?.severity).toBe("pass");
    expect(byName(results, "bigbadbots_choose_body")[0]?.severity).toBe("fail");
  });
});

// ---------------------------------------------------------------------------
// validateBot picks the Big Bad Bots profile
// ---------------------------------------------------------------------------

describe("validateBot on a Big Bad Bots bot", () => {
  it("runs the Big Bad Bots checks for a BigBadBotsBot subclass", async () => {
    const dir = await writeBot(
      "bbb_blocker",
      `
import { Action, BigBadBotsBot } from "@chipzen-ai/bot";

export class Blocker extends BigBadBotsBot {
  act(state) {
    return Action.act("block");
  }
}
`,
    );
    const results = await validateBot(dir, { checkConnectivity: true });
    expect(fails(results)).toEqual([]);
    const names = results.map((r) => r.name);
    expect(names).toContain("act_method");
    expect(names).toContain("bigbadbots_act");
    expect(names).not.toContain("decide_method");
    expect(byName(results, "connectivity")[0]?.severity).toBe("warn");
  });

  it("fails a BigBadBotsBot without act()", async () => {
    const dir = await writeBot(
      "bbb_no_act",
      `
import { Action, BigBadBotsBot } from "@chipzen-ai/bot";

export class Idle extends BigBadBotsBot {
  bid(state) {
    return Action.bid(0);
  }
}
`,
    );
    const results = await validateBot(dir);
    expect(byName(results, "act_method")[0]).toMatchObject({ severity: "fail" });
  });

  it("--game bigbadbots runs the checks on a plain Bot through decide()", async () => {
    const dir = await writeBot(
      "bbb_plain",
      `
import { Action, Bot } from "@chipzen-ai/bot";

export class Plain extends Bot {
  decide(state) {
    switch (state.phase) {
      case "body_shop": return Action.chooseBody(state.offer[0].id);
      case "auction": return Action.bid(0);
      case "fitting": return Action.fit({});
      default: return Action.act("idle");
    }
  }
}
`,
    );
    const auto = await validateBot(dir);
    expect(auto.map((r) => r.name)).not.toContain("bigbadbots_act");
    const results = await validateBot(dir, { game: "bigbadbots" });
    expect(fails(results)).toEqual([]);
    expect(results.map((r) => r.name)).toContain("decide_method");
  });

  it("refuses an unknown game", async () => {
    const results = await validateBot(workDir, { game: "chess" as never });
    expect(results).toEqual([expect.objectContaining({ severity: "fail", name: "game" })]);
  });
});

// ---------------------------------------------------------------------------
// The JavaScript Rusher starter
// ---------------------------------------------------------------------------

describe("the JavaScript Rusher starter", () => {
  async function starterCopy(): Promise<string> {
    const source = readFileSync(path.join(STARTER, "bot.js"), "utf-8");
    return writeBot("rusher", source);
  }

  it("passes chipzen-sdk validate with no failure and no warning", async () => {
    const results = await validateBot(await starterCopy());
    expect(fails(results)).toEqual([]);
    expect(results.filter((r) => r.severity === "warn")).toEqual([]);
    for (const call of BIGBADBOTS_CALLS) {
      expect(byName(results, `bigbadbots_${call}`)[0]?.severity).toBe("pass");
    }
    expect(byName(results, "bot_class")[0]?.message).toBe("Found bot class: Rusher");
  });

  it("plays from the rule set: rushes in, then jabs once in range", async () => {
    const dir = await starterCopy();
    const { Rusher } = (await import(pathToFileURL(path.join(dir, "bot.js")).href)) as {
      Rusher: new () => BigBadBotsBot;
    };
    const bot = new Rusher();
    bot.onMatchStart({
      game_config: {
        ...BIGBADBOTS_VALIDATE_GAME_CONFIG,
        rule_set: {
          ...(BIGBADBOTS_VALIDATE_GAME_CONFIG.rule_set as Json),
          moves: {
            punch_high: { startup: 2, stamina: 6, damage: 6, reach: 1.0 },
            kick_high: { startup: 5, stamina: 14, damage: 13, reach: 1.4 },
          },
        },
      },
    });
    const [pair] = await loadBigBadBotsFixtures("act");
    const free = sdk.parseBigBadBotsTurnRequest(pair!.request) as FightState;
    const at = (distance: number) => bot.decide({ ...free, distance } as FightState).toWire().action;
    expect(at(8.0)).toBe("move_in");
    expect(at(free.me.strikeRange * 0.9)).toBe("punch_high");
  });

  it("ships the files a starter needs, and a non-root, digest-pinned Dockerfile", () => {
    const files = readdirSync(STARTER).sort();
    expect(files).toEqual([".dockerignore", "Dockerfile", "README.md", "bot.js", "package.json"]);
    const dockerfile = readFileSync(path.join(STARTER, "Dockerfile"), "utf-8");
    const froms = [...dockerfile.matchAll(/^FROM\s+(\S+)/gm)].map((m) => m[1]!);
    expect(froms.length).toBe(2);
    for (const image of froms) expect(image).toMatch(/@sha256:[0-9a-f]{64}$/);
    expect(dockerfile).toMatch(/^USER 10001$/m);
    const pkg = JSON.parse(readFileSync(path.join(STARTER, "package.json"), "utf-8")) as Json;
    expect(pkg.type).toBe("module");
    expect(pkg.main).toBe("bot.js");
  });
});
