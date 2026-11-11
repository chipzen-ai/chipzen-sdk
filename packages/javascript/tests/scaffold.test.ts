import { promises as fs } from "node:fs";
import { mkdtempSync, readFileSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import {
  STARTER_FILES,
  STARTER_GITIGNORE,
  scaffoldBot,
  starterDir,
  withProjectName,
} from "../src/scaffold.js";
import { validateBot } from "../src/validate.js";

const PKG = path.resolve(__dirname, "..");
const STARTER = path.join(PKG, "starters", "javascript");

let workDir: string;

beforeAll(() => {
  workDir = mkdtempSync(path.join(tmpdir(), "chipzen-scaffold-"));
});

afterAll(() => {
  rmSync(workDir, { recursive: true, force: true });
});

describe("init writes the starter, so the two cannot drift (#239, #260)", () => {
  it("reads the starter folder in the repo", () => {
    expect(path.resolve(starterDir())).toBe(STARTER);
  });

  it("the starter folder holds exactly STARTER_FILES plus .gitignore", () => {
    const onDisk = readdirSync(STARTER).filter((n) => n !== "node_modules").sort();
    expect(onDisk).toEqual([...STARTER_FILES, ".gitignore"].sort());
  });

  it("the npm package ships every starter file init reads", () => {
    const pkg = JSON.parse(readFileSync(path.join(PKG, "package.json"), "utf-8"));
    for (const file of STARTER_FILES) {
      expect(pkg.files).toContain(`starters/javascript/${file}`);
    }
  });

  it("STARTER_GITIGNORE is the starter's .gitignore (npm never packs that file)", () => {
    expect(readFileSync(path.join(STARTER, ".gitignore"), "utf-8").replace(/\r\n/g, "\n")).toBe(
      STARTER_GITIGNORE,
    );
  });

  it("every file is byte-identical to the starter, except the name in package.json", async () => {
    const projectDir = await scaffoldBot("drift_bot", { parentDir: workDir });
    expect(readdirSync(projectDir).sort()).toEqual([...STARTER_FILES, ".gitignore"].sort());
    for (const file of STARTER_FILES) {
      const written = readFileSync(path.join(projectDir, file));
      const starter = readFileSync(path.join(STARTER, file));
      if (file === "package.json") {
        expect(written.toString("utf-8")).toBe(withProjectName(starter.toString("utf-8"), "drift_bot"));
      } else {
        expect(written.equals(starter), file).toBe(true);
      }
    }
  });

  it("package.json keeps the starter's SDK pin and gets the project name", async () => {
    const projectDir = await scaffoldBot("gamma_bot", { parentDir: workDir });
    const pkg = JSON.parse(await fs.readFile(path.join(projectDir, "package.json"), "utf-8"));
    const starterPkg = JSON.parse(readFileSync(path.join(STARTER, "package.json"), "utf-8"));
    expect(pkg.name).toBe("gamma_bot");
    expect(pkg.dependencies["@chipzen-ai/bot"]).toBe(starterPkg.dependencies["@chipzen-ai/bot"]);
    // The pin #239 reported: ^0.2.0 only admits 0.2.x.
    expect(pkg.dependencies["@chipzen-ai/bot"]).not.toBe("^0.2.0");
    expect(pkg.type).toBe("module");
  });
});

describe("scaffoldBot", () => {
  it("creates the project directory under parentDir", async () => {
    const projectDir = await scaffoldBot("alpha_bot", { parentDir: workDir });
    expect(projectDir).toBe(path.join(workDir, "alpha_bot"));
    const stat = await fs.stat(projectDir);
    expect(stat.isDirectory()).toBe(true);
  });

  it("scaffolded bot.js exports a Bot subclass, a main() and the entry block", async () => {
    const projectDir = await scaffoldBot("delta_bot", { parentDir: workDir });
    const source = await fs.readFile(path.join(projectDir, "bot.js"), "utf-8");
    expect(source).toMatch(/import\s+\{[^}]*Bot[^}]*\}\s+from\s+["']@chipzen-ai\/bot["']/);
    expect(source).toMatch(/class\s+MyBot\s+extends\s+Bot\b/);
    expect(source).toMatch(/decide\s*\(/);
    expect(source).toMatch(/export\s+async\s+function\s+main\s*\(/);
    expect(source).toMatch(/if \(import\.meta\.main[^)]*\) \{\s*await main\(\);/);
  });

  it("scaffolded Dockerfile is the IP-protected Bun-compile multi-stage build", async () => {
    const projectDir = await scaffoldBot("zeta_bot", { parentDir: workDir });
    const dockerfile = await fs.readFile(path.join(projectDir, "Dockerfile"), "utf-8");
    expect(dockerfile).toMatch(/FROM oven\/bun:1-debian AS builder/);
    expect(dockerfile).toMatch(/bun build --compile --minify --sourcemap=none/);
    expect(dockerfile).toMatch(/rm bot\.js/);
    expect(dockerfile).toMatch(/FROM debian:12-slim/);
    expect(dockerfile).toMatch(/COPY --from=builder \/build\/bot \/bot\/bot/);
  });

  it("the README has the current upload flow and decision clock", async () => {
    const projectDir = await scaffoldBot("readme_bot", { parentDir: workDir });
    const readme = await fs.readFile(path.join(projectDir, "README.md"), "utf-8");
    expect(readme).toContain("docker save my-bot:v1 | gzip > my-bot.tar.gz");
    expect(readme).toContain("5 seconds");
    expect(readme.toLowerCase()).not.toContain(".zip");
    expect(readme).not.toContain("](../");
  });

  it("init then validate passes every check up to the smoke test", async () => {
    // The scaffold imports @chipzen-ai/bot bare; in this workspace pnpm
    // self-resolves it, outside it `npm install` comes first. The static and
    // image checks must pass for a fresh project either way.
    const projectDir = await scaffoldBot("validate_bot", { parentDir: workDir });
    const results = await validateBot(projectDir);
    const byName = new Map(results.map((r) => [r.name, r.severity]));
    for (const name of [
      "size",
      "file_structure",
      "syntax",
      "imports",
      "bot_class",
      "decide_method",
      "dockerfile",
      "entry_main",
    ]) {
      expect(byName.get(name), name).toBe("pass");
    }
  });

  it("rejects invalid project names", async () => {
    await expect(scaffoldBot("has spaces", { parentDir: workDir })).rejects.toThrow(/Invalid/);
    await expect(scaffoldBot("with/slash", { parentDir: workDir })).rejects.toThrow(/Invalid/);
    await expect(scaffoldBot("", { parentDir: workDir })).rejects.toThrow(/Invalid/);
  });

  it("refuses to clobber an existing directory", async () => {
    await scaffoldBot("epsilon_bot", { parentDir: workDir });
    await expect(scaffoldBot("epsilon_bot", { parentDir: workDir })).rejects.toThrow(
      /already exists/,
    );
  });

  it("withProjectName changes only the name", () => {
    const original = '{\n  "name": "chipzen-starter-bot",\n  "version": "0.2.0"\n}\n';
    expect(withProjectName(original, "my-bot")).toBe(
      '{\n  "name": "my-bot",\n  "version": "0.2.0"\n}\n',
    );
    expect(() => withProjectName("{}", "x")).toThrow(/no "name"/);
  });
});
