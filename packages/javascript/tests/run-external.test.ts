import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { type ChipzenConfig } from "../src/config.js";
import {
  findBotSubclasses,
  loadBotModule,
  parseRunExternalArgs,
  resolveConnection,
  runExternalCli,
  selectBotClass,
  type BotConstructor,
} from "../src/run_external.js";

// ---------------------------------------------------------------------------
// Temp-dir + env helpers
// ---------------------------------------------------------------------------

let tmpRoot: string;
let savedEnv: string | undefined;

beforeEach(() => {
  tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), "chipzen-runext-"));
  savedEnv = process.env.CHIPZEN_ENV;
  delete process.env.CHIPZEN_ENV;
});
afterEach(() => {
  fs.rmSync(tmpRoot, { recursive: true, force: true });
  if (savedEnv === undefined) delete process.env.CHIPZEN_ENV;
  else process.env.CHIPZEN_ENV = savedEnv;
  vi.restoreAllMocks();
});

function writeFile(name: string, body: string): string {
  const filePath = path.join(tmpRoot, name);
  fs.writeFileSync(filePath, body, "utf-8");
  return filePath;
}

function cfg(fields: Partial<ChipzenConfig>): ChipzenConfig {
  return { path: "/x", token: null, url: null, botId: null, ...fields };
}

// ---------------------------------------------------------------------------
// Arg parsing
// ---------------------------------------------------------------------------

describe("parseRunExternalArgs", () => {
  it("requires a positional bot file", () => {
    expect(() => parseRunExternalArgs([])).toThrow(/requires a <bot-file>/);
  });

  it("parses the bot file positional", () => {
    expect(parseRunExternalArgs(["my-bot.js"]).botFile).toBe("my-bot.js");
  });

  it("env defaults to null", () => {
    expect(parseRunExternalArgs(["b.js"]).env).toBeNull();
  });

  it("accepts the three canonical env values", () => {
    for (const env of ["prod", "staging", "local"] as const) {
      expect(parseRunExternalArgs(["b.js", "--env", env]).env).toBe(env);
    }
  });

  it("rejects an unknown env value", () => {
    expect(() => parseRunExternalArgs(["b.js", "--env", "dev"])).toThrow(/--env must be one of/);
  });

  it("parses --token / --bot-id / --bot-class", () => {
    const parsed = parseRunExternalArgs([
      "b.js",
      "--token",
      "cz_extbot_xyz",
      "--bot-id",
      "abc123",
      "--bot-class",
      "TightAggressive",
    ]);
    expect(parsed.token).toBe("cz_extbot_xyz");
    expect(parsed.botId).toBe("abc123");
    expect(parsed.botClass).toBe("TightAggressive");
  });

  it("parses --max-matches as an int", () => {
    expect(parseRunExternalArgs(["b.js", "--max-matches", "3"]).maxMatches).toBe(3);
  });

  it("--no-safe-mode flips safeMode off (default on)", () => {
    expect(parseRunExternalArgs(["b.js"]).safeMode).toBe(true);
    expect(parseRunExternalArgs(["b.js", "--no-safe-mode"]).safeMode).toBe(false);
  });

  it("rejects an unknown option", () => {
    expect(() => parseRunExternalArgs(["b.js", "--bogus"])).toThrow(/Unknown option/);
  });

  it("rejects a flag missing its value", () => {
    expect(() => parseRunExternalArgs(["b.js", "--token"])).toThrow(/requires a value/);
  });
});

// ---------------------------------------------------------------------------
// Bot subclass discovery + selection
// ---------------------------------------------------------------------------

class FakeBotBase {
  decide(): void {
    /* placeholder */
  }
}

describe("findBotSubclasses", () => {
  it("returns Bot subclasses, excluding the base and non-classes", async () => {
    // Use the real Bot base so instanceof checks fire.
    const { Bot } = await import("../src/bot.js");
    class A extends Bot {
      decide() {
        return null as never;
      }
    }
    class B extends Bot {
      decide() {
        return null as never;
      }
    }
    const module = { A, B, Bot, helper: () => 1, value: 42 };
    const found = findBotSubclasses(module as unknown as Record<string, unknown>);
    const names = found.map((c) => c.name).sort();
    expect(names).toEqual(["A", "B"]);
    expect(found).not.toContain(Bot);
  });

  it("returns empty when no subclass is present", () => {
    const module = { helper: () => 1, NotABot: FakeBotBase };
    expect(findBotSubclasses(module as unknown as Record<string, unknown>)).toEqual([]);
  });
});

describe("selectBotClass", () => {
  const make = (name: string): BotConstructor => {
    const c = class {
      decide(): void {}
    };
    Object.defineProperty(c, "name", { value: name });
    return c as unknown as BotConstructor;
  };

  it("auto-selects a single candidate", () => {
    const only = make("Only");
    expect(selectBotClass([only], null, "x.js")).toBe(only);
  });

  it("no candidates raises", () => {
    expect(() => selectBotClass([], null, "x.js")).toThrow(/No Bot subclass found/);
  });

  it("multiple candidates without --bot-class raises", () => {
    expect(() => selectBotClass([make("A"), make("B")], null, "x.js")).toThrow(
      /Multiple Bot subclasses/,
    );
  });

  it("multiple candidates with an explicit pick works", () => {
    const a = make("Alpha");
    const b = make("Beta");
    expect(selectBotClass([a, b], "Beta", "x.js")).toBe(b);
  });

  it("an unknown explicit name raises and lists candidates", () => {
    expect(() => selectBotClass([make("Alpha")], "Ghost", "x.js")).toThrow(
      /No Bot subclass named "Ghost"/,
    );
  });
});

// ---------------------------------------------------------------------------
// Connection resolution
// ---------------------------------------------------------------------------

describe("resolveConnection", () => {
  it("uses the config url when present", () => {
    const c = cfg({ token: "cz_extbot_cfg", url: "wss://override.example/ws/external/bot/x" });
    const r = resolveConnection({ config: c, env: null, token: null, botId: null });
    expect(r.url).toBe("wss://override.example/ws/external/bot/x");
    expect(r.token).toBe("cz_extbot_cfg");
    expect(r.config).toBe(c);
  });

  it("explicit token overrides config token", () => {
    const c = cfg({ token: "cz_extbot_cfg", url: "wss://override.example/ws/external/bot/x" });
    const r = resolveConnection({ config: c, env: null, token: "cz_extbot_arg", botId: null });
    expect(r.token).toBe("cz_extbot_arg");
  });

  it("builds an env-derived url when no config url", () => {
    const c = cfg({ token: "cz_extbot_cfg", botId: "bot-uuid-1234" });
    const r = resolveConnection({ config: c, env: "staging", token: null, botId: null });
    expect(r.url).toBe("wss://staging.chipzen.ai/ws/external/bot/bot-uuid-1234");
    expect(r.token).toBe("cz_extbot_cfg");
  });

  it("env-derived default is prod", () => {
    const c = cfg({ botId: "bot-id-x" });
    const r = resolveConnection({ config: c, env: null, token: null, botId: null });
    expect(r.url).toBe("wss://chipzen.ai/ws/external/bot/bot-id-x");
  });

  it("$CHIPZEN_ENV used when no explicit env", () => {
    process.env.CHIPZEN_ENV = "local";
    const c = cfg({ botId: "bot-id-x" });
    const r = resolveConnection({ config: c, env: null, token: null, botId: null });
    expect(r.url).toBe("ws://localhost:8001/ws/external/bot/bot-id-x");
  });

  it("explicit bot-id overrides config bot_id", () => {
    const c = cfg({ botId: "from-config" });
    const r = resolveConnection({ config: c, env: "prod", token: null, botId: "from-arg" });
    expect(r.url).toBe("wss://chipzen.ai/ws/external/bot/from-arg");
  });

  it("no url + no bot_id raises", () => {
    const c = cfg({ token: "cz_extbot_x" });
    expect(() => resolveConnection({ config: c, env: "prod", token: null, botId: null })).toThrow(
      /No lobby URL is configured/,
    );
  });

  it("no config + no bot_id raises", () => {
    expect(() => resolveConnection({ config: null, env: "prod", token: null, botId: null })).toThrow(
      /No lobby URL is configured/,
    );
  });

  it("no config + explicit bot-id works", () => {
    const r = resolveConnection({ config: null, env: "prod", token: "cz_extbot_arg", botId: "bot-x" });
    expect(r.url).toBe("wss://chipzen.ai/ws/external/bot/bot-x");
    expect(r.token).toBe("cz_extbot_arg");
  });
});

// ---------------------------------------------------------------------------
// loadBotModule — round-trips a real JS file
// ---------------------------------------------------------------------------

describe("loadBotModule", () => {
  it("loads a plain module file and exposes its exports", async () => {
    const f = writeFile("plain.mjs", "export const marker = 123;\nexport function helper() { return 1; }\n");
    const module = await loadBotModule(f);
    expect(module.marker).toBe(123);
    expect(typeof module.helper).toBe("function");
  });

  it("a missing file raises a clear error", async () => {
    await expect(loadBotModule(path.join(tmpRoot, "nope.mjs"))).rejects.toThrow(/Failed to load/);
  });

  it("a syntax error raises a clear error", async () => {
    const f = writeFile("broken.mjs", "export class Oops {  // unterminated\n");
    await expect(loadBotModule(f)).rejects.toThrow(/Failed to load/);
  });
});

// ---------------------------------------------------------------------------
// runExternalCli — setup error exits (no network reached)
// ---------------------------------------------------------------------------

/** Run the CLI capturing the exit code; process.exit is stubbed to throw. */
async function runCliExpectExit(args: string[]): Promise<number> {
  const exitErr = new Error("__exit__");
  const exitSpy = vi.spyOn(process, "exit").mockImplementation(((code?: number) => {
    (exitErr as Error & { code?: number }).code = code ?? 0;
    throw exitErr;
  }) as never);
  vi.spyOn(console, "error").mockImplementation(() => {});
  vi.spyOn(console, "log").mockImplementation(() => {});
  try {
    await runExternalCli(args);
    exitSpy.mockRestore();
    return 0; // returned without exiting
  } catch (err) {
    if (err === exitErr) {
      return (exitErr as Error & { code?: number }).code ?? 0;
    }
    throw err;
  }
}

describe("runExternalCli setup errors", () => {
  it("missing bot file exits 2 (no bot_id/url -> resolution fails first)", async () => {
    // With no config and no --bot-id, resolveConnection fails (exit 2)
    // before the module load is even attempted.
    const code = await runCliExpectExit([path.join(tmpRoot, "does_not_exist.js")]);
    expect(code).toBe(2);
  });

  it("no bot_id and no url exits 2", async () => {
    const f = writeFile("ok.mjs", "export const x = 1;\n");
    const code = await runCliExpectExit([f, "--token", "cz_extbot_x"]);
    expect(code).toBe(2);
  });

  it("a missing bot file with a resolvable URL exits 2 at load", async () => {
    const code = await runCliExpectExit([
      path.join(tmpRoot, "missing.mjs"),
      "--bot-id",
      "x",
      "--token",
      "cz_extbot_x",
    ]);
    expect(code).toBe(2);
  });

  it("no Bot subclass in the file exits 2", async () => {
    const f = writeFile("nobot.mjs", "export function helper() { return 42; }\n");
    const code = await runCliExpectExit([f, "--bot-id", "x", "--token", "cz_extbot_x"]);
    expect(code).toBe(2);
  });
});
