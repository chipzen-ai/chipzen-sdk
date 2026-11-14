/**
 * `validate` fails a poker bot the uploaded image could not start
 * (chipzen-sdk#260, #239): the `dockerfile` and `entry_main` checks.
 */

import { promises as fs, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { checkPokerImage, startCommand } from "../src/image_checks.js";
import { scaffoldBot } from "../src/scaffold.js";
import { validateBot, type ValidationResult } from "../src/validate.js";

let workDir: string;
let counter = 0;

beforeAll(() => {
  workDir = mkdtempSync(path.join(tmpdir(), "chipzen-image-"));
});

afterAll(() => {
  rmSync(workDir, { recursive: true, force: true });
});

const BOT_CLASS = `import { Bot, Action, runBot } from "@chipzen-ai/bot";

class MyBot extends Bot {
  decide(state) {
    return Action.fold();
  }
}
`;

const MAIN = `
export async function main() {
  await runBot(process.env.CHIPZEN_WS_URL, new MyBot());
}
`;

const ENTRY_BLOCK = `
if (import.meta.main || import.meta.url === \`file://\${process.argv[1]}\`) {
  await main();
}
`;

const STARTER_DOCKERFILE = [
  "FROM oven/bun:1-debian AS builder",
  "COPY package.json bot.js ./",
  "RUN bun build --compile --minify --target=bun-linux-x64 \\",
  "        bot.js --outfile=/build/bot \\",
  "    && rm bot.js",
  "FROM debian:12-slim",
  "COPY --from=builder /build/bot /bot/bot",
  'ENTRYPOINT ["dumb-init", "/bot/bot"]',
  "",
].join("\n");

function one(results: ValidationResult[], name: string): ValidationResult {
  const found = results.filter((r) => r.name === name);
  expect(found, name).toHaveLength(1);
  return found[0]!;
}

async function folder(dockerfile: string | null): Promise<string> {
  const dir = path.join(workDir, `bot_${counter++}`);
  await fs.mkdir(dir, { recursive: true });
  if (dockerfile !== null) await fs.writeFile(path.join(dir, "Dockerfile"), dockerfile, "utf-8");
  return dir;
}

async function check(source: string, dockerfile: string | null, entry = "bot.js") {
  const dir = await folder(dockerfile);
  return checkPokerImage(dir, path.join(dir, entry), source);
}

describe("a fresh scaffold, then the pieces removed", () => {
  it("passes both checks", async () => {
    const dir = await scaffoldBot("ok_bot", { parentDir: workDir });
    const results = await validateBot(dir);
    expect(one(results, "dockerfile").severity).toBe("pass");
    expect(one(results, "entry_main")).toMatchObject({ severity: "pass" });
  });

  it("fails entry_main, with the fix, when the entry block is removed", async () => {
    const dir = await scaffoldBot("no_entry_block", { parentDir: workDir });
    const botJs = path.join(dir, "bot.js");
    const source = await fs.readFile(botJs, "utf-8");
    const without = source.replace(/\nif \(import\.meta\.main[\s\S]*?\n\}\n/, "\n");
    expect(without).not.toContain("await main()");
    await fs.writeFile(botJs, without, "utf-8");

    const result = one(await validateBot(dir), "entry_main");
    expect(result.severity).toBe("fail");
    expect(result.message).toContain("bot.js defines main() but never calls it");
    expect(result.message).toContain("chipzen-sdk init");
  });

  it("fails entry_main when main() itself is removed", async () => {
    const dir = await scaffoldBot("no_main", { parentDir: workDir });
    const botJs = path.join(dir, "bot.js");
    const source = await fs.readFile(botJs, "utf-8");
    const without = source
      .replace(/\nexport async function main\(\) \{[\s\S]*?\n\}\n/, "\n")
      .replace(/\nif \(import\.meta\.main[\s\S]*?\n\}\n/, "\n");
    expect(without).not.toMatch(/function main/);
    await fs.writeFile(botJs, without, "utf-8");

    const result = one(await validateBot(dir), "entry_main");
    expect(result.severity).toBe("fail");
    expect(result.message).toContain("bot.js has no main() that starts the bot");
  });

  it("fails dockerfile, with the fix, when the Dockerfile is removed", async () => {
    const dir = await scaffoldBot("no_dockerfile", { parentDir: workDir });
    await fs.rm(path.join(dir, "Dockerfile"));
    const result = one(await validateBot(dir), "dockerfile");
    expect(result.severity).toBe("fail");
    expect(result.message).toMatch(/^No Dockerfile in /);
    expect(result.message).toContain("docker save");
  });
});

describe("dockerfile", () => {
  it("passes the starter's compiled-binary form", async () => {
    const result = one(await check(BOT_CLASS + MAIN + ENTRY_BLOCK, STARTER_DOCKERFILE), "dockerfile");
    expect(result).toEqual({
      severity: "pass",
      name: "dockerfile",
      message: "Dockerfile starts the bot with: dumb-init /bot/bot",
    });
  });

  it("fails with no ENTRYPOINT or CMD in the final stage", async () => {
    const dockerfile = 'FROM node:20 AS build\nENTRYPOINT ["node", "bot.js"]\nFROM node:20-slim\n';
    const result = one(await check(BOT_CLASS + MAIN + ENTRY_BLOCK, dockerfile), "dockerfile");
    expect(result.severity).toBe("fail");
    expect(result.message).toContain("no ENTRYPOINT or CMD");
  });

  it("fails when the Dockerfile runs another file", async () => {
    const dockerfile = 'FROM node:20-slim\nCMD ["node", "/bot/main.js"]\n';
    const result = one(await check(BOT_CLASS + MAIN + ENTRY_BLOCK, dockerfile), "dockerfile");
    expect(result.severity).toBe("fail");
    expect(result.message).toContain("runs main.js, but the entry point is bot.js");
  });

  it("fails when the Dockerfile compiles another file", async () => {
    const dockerfile = STARTER_DOCKERFILE.replace("        bot.js --outfile", "        index.js --outfile");
    const result = one(await check(BOT_CLASS + MAIN + ENTRY_BLOCK, dockerfile), "dockerfile");
    expect(result.severity).toBe("fail");
    expect(result.message).toContain("runs index.js");
  });

  it("reads shell form and joins ENTRYPOINT with CMD", () => {
    expect(startCommand("FROM node:20\nENTRYPOINT node\nCMD [\"bot.js\"]\n")).toBe("node bot.js");
    expect(startCommand("FROM node:20\nRUN true\n")).toBeNull();
  });
});

describe("entry_main", () => {
  it("passes an unconditional top-level call", async () => {
    const result = one(await check(`${BOT_CLASS}${MAIN}\nmain().catch(console.error);\n`, null), "entry_main");
    expect(result.severity).toBe("pass");
  });

  it("passes a top-level runBot() with no main()", async () => {
    const source = `${BOT_CLASS}\nawait runBot(process.env.CHIPZEN_WS_URL, new MyBot());\n`;
    expect(one(await check(source, null), "entry_main").severity).toBe("pass");
  });

  it("does not count runBot() inside a function nobody calls", async () => {
    expect(one(await check(BOT_CLASS + MAIN, null), "entry_main").severity).toBe("fail");
  });

  it("does not count a call that is commented out", async () => {
    const source = `${BOT_CLASS}${MAIN}\n// await main();\n/* main(); */\n`;
    expect(one(await check(source, null), "entry_main").severity).toBe("fail");
  });
});
