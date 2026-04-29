import { promises as fs } from "node:fs";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { scaffoldBot } from "../src/scaffold.js";

let workDir: string;

beforeAll(() => {
  workDir = mkdtempSync(path.join(tmpdir(), "chipzen-scaffold-"));
});

afterAll(() => {
  rmSync(workDir, { recursive: true, force: true });
});

describe("scaffoldBot", () => {
  it("creates the project directory under parentDir", async () => {
    const projectDir = await scaffoldBot("alpha_bot", { parentDir: workDir });
    expect(projectDir).toBe(path.join(workDir, "alpha_bot"));
    const stat = await fs.stat(projectDir);
    expect(stat.isDirectory()).toBe(true);
  });

  it("emits all expected files", async () => {
    const projectDir = await scaffoldBot("beta_bot", { parentDir: workDir });
    for (const name of ["bot.js", "package.json", "Dockerfile", ".dockerignore", ".gitignore", "README.md"]) {
      const stat = await fs.stat(path.join(projectDir, name));
      expect(stat.isFile()).toBe(true);
    }
  });

  it("scaffolded package.json has the SDK as a dependency", async () => {
    const projectDir = await scaffoldBot("gamma_bot", { parentDir: workDir });
    const pkg = JSON.parse(
      await fs.readFile(path.join(projectDir, "package.json"), "utf-8"),
    );
    expect(pkg.name).toBe("gamma_bot");
    expect(pkg.dependencies).toMatchObject({ "@chipzen-ai/bot": expect.stringMatching(/^\^?\d+/) });
    expect(pkg.type).toBe("module");
  });

  it("scaffolded bot.js exports a Bot subclass and a main()", async () => {
    const projectDir = await scaffoldBot("delta_bot", { parentDir: workDir });
    const source = await fs.readFile(path.join(projectDir, "bot.js"), "utf-8");
    expect(source).toMatch(/import\s+\{[^}]*Bot[^}]*\}\s+from\s+["']@chipzen-ai\/bot["']/);
    expect(source).toMatch(/class\s+MyBot\s+extends\s+Bot\b/);
    expect(source).toMatch(/decide\s*\(/);
    expect(source).toMatch(/export\s+async\s+function\s+main\s*\(/);
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
});
