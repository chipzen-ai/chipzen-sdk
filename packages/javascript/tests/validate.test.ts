import { promises as fs, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { scaffoldBot } from "../src/scaffold.js";
import { validateBot } from "../src/validate.js";

let workDir: string;

beforeAll(() => {
  workDir = mkdtempSync(path.join(tmpdir(), "chipzen-validate-"));
});

afterAll(() => {
  rmSync(workDir, { recursive: true, force: true });
});

/**
 * Write a one-off bot directory with a custom bot.js source. The
 * @chipzen-ai/bot import is rewritten to a relative path into the SDK
 * source so the smoke_test can dynamic-import the bot without needing
 * an npm install of an unpublished package.
 */
async function writeBot(
  name: string,
  source: string,
  options: { skipPackageJson?: boolean } = {},
): Promise<string> {
  const dir = path.join(workDir, name);
  await fs.mkdir(dir, { recursive: true });

  // Resolve `@chipzen-ai/bot` to the in-repo SDK build via the file:// URL
  // form. validateBot's smoke_test does a dynamic import that respects
  // string-replaced paths; the regex check still sees the original import
  // statement (we only rewrite for runtime).
  const sdkDist = path.resolve(__dirname, "..", "dist", "index.js");
  const rewritten = source.replace(
    /from\s+["']@chipzen-ai\/bot["']/g,
    `from ${JSON.stringify(`file://${sdkDist}`)}`,
  );

  await fs.writeFile(path.join(dir, "bot.js"), rewritten, "utf-8");
  if (!options.skipPackageJson) {
    await fs.writeFile(
      path.join(dir, "package.json"),
      JSON.stringify({ name, version: "0.0.0", type: "module", main: "bot.js" }, null, 2),
      "utf-8",
    );
  }
  return dir;
}

describe("validateBot — happy paths", () => {
  it("scaffolded bot passes every static check", async () => {
    // The default scaffold imports @chipzen-ai/bot bare. In this workspace
    // pnpm self-resolves the package so the smoke_test can dynamic-import
    // it; outside the workspace the dev would need `npm install` first.
    // Either way, every static check (size/structure/syntax/imports/
    // bot_class/decide_method) must pass for a freshly-scaffolded project.
    const dir = await scaffoldBot("scaffold_for_validate", { parentDir: workDir });
    const results = await validateBot(dir);

    const passes = results.filter((r) => r.severity === "pass").map((r) => r.name);
    expect(passes).toContain("size");
    expect(passes).toContain("file_structure");
    expect(passes).toContain("syntax");
    expect(passes).toContain("imports");
    expect(passes).toContain("bot_class");
    expect(passes).toContain("decide_method");
  });

  it("a bot with the SDK import rewritten to in-repo dist passes everything", async () => {
    const dir = await writeBot(
      "happy_bot",
      `
import { Bot, Action } from "@chipzen-ai/bot";

class MyBot extends Bot {
  decide(state) {
    if (state.validActions.includes("check")) return Action.check();
    return Action.fold();
  }
}

export { MyBot };
`,
    );
    const results = await validateBot(dir);
    const fails = results.filter((r) => r.severity === "fail");
    expect(fails).toEqual([]);
  });
});

describe("validateBot — failure modes", () => {
  it("reports missing entry point", async () => {
    const dir = path.join(workDir, "empty_dir");
    await fs.mkdir(dir);
    const results = await validateBot(dir);
    expect(results.some((r) => r.severity === "fail" && r.name === "file_structure")).toBe(true);
  });

  it("reports nonexistent path", async () => {
    const results = await validateBot(path.join(workDir, "does-not-exist"));
    expect(results[0]).toMatchObject({ severity: "fail", name: "file_structure" });
  });

  it("reports syntax errors", async () => {
    const dir = await writeBot("bad_syntax", "this is not js {");
    const results = await validateBot(dir);
    const syntax = results.find((r) => r.name === "syntax");
    expect(syntax?.severity).toBe("fail");
  });

  it("reports blocked imports", async () => {
    const dir = await writeBot(
      "blocked_imports",
      `
import { Bot, Action } from "@chipzen-ai/bot";
import { spawn } from "node:child_process";  // blocked

class MyBot extends Bot {
  decide(state) {
    return Action.fold();
  }
}

export { MyBot };
`,
    );
    const results = await validateBot(dir);
    const imports = results.find((r) => r.name === "imports");
    expect(imports?.severity).toBe("fail");
    expect(imports?.message).toMatch(/child_process/);
  });

  it("reports missing Bot subclass", async () => {
    const dir = await writeBot(
      "no_bot_class",
      `
export function notABot() {
  return "I'm just a function";
}
`,
    );
    const results = await validateBot(dir);
    expect(results.find((r) => r.name === "bot_class")?.severity).toBe("fail");
  });

  it("reports missing decide() method", async () => {
    const dir = await writeBot(
      "no_decide",
      `
import { Bot } from "@chipzen-ai/bot";

class MyBot extends Bot {
  // Missing decide() — should fail.
  somethingElse() {}
}

export { MyBot };
`,
    );
    const results = await validateBot(dir);
    expect(results.find((r) => r.name === "decide_method")?.severity).toBe("fail");
  });

  it("reports decide() returning the wrong type", async () => {
    const dir = await writeBot(
      "wrong_return_type",
      `
import { Bot } from "@chipzen-ai/bot";

class MyBot extends Bot {
  decide(state) {
    return "not an Action";
  }
}

export { MyBot };
`,
    );
    const results = await validateBot(dir);
    const smoke = results.find((r) => r.name === "smoke_test");
    expect(smoke?.severity).toBe("fail");
    expect(smoke?.message).toMatch(/expected an Action/);
  });
});

describe("validateBot — warnings", () => {
  it("warns on fs imports without failing the run", async () => {
    const dir = await writeBot(
      "warn_fs",
      `
import { Bot, Action } from "@chipzen-ai/bot";
import { readFileSync } from "node:fs";

class MyBot extends Bot {
  decide(state) {
    return Action.fold();
  }
}

export { MyBot };
`,
    );
    const results = await validateBot(dir);
    const warnings = results.filter((r) => r.severity === "warn");
    expect(warnings.some((w) => w.message.includes("node:fs"))).toBe(true);
    // No hard fails:
    expect(results.find((r) => r.severity === "fail")).toBeUndefined();
  });
});
