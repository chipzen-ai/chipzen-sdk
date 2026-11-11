/**
 * `chipzen-sdk validate` on a poker bot: can the uploaded image start it?
 *
 * The platform runs the Docker image a builder uploads, not the folder
 * `validate` reads. A folder can pass every check on `decide()` and still
 * build an image that starts nothing (chipzen-sdk#260, #239). Two checks:
 *
 * - `dockerfile`: the folder has a `Dockerfile` whose final stage sets an
 *   ENTRYPOINT or CMD, and when the Dockerfile names a `.js` file to run or
 *   compile, it is the entry point `validate` checked.
 * - `entry_main`: the entry point starts the bot when it runs as the program.
 *   The starter defines `main()` and calls it from
 *   `if (import.meta.main || ...) { await main(); }`; `bun build --compile`
 *   runs the file as the entry module, so without that call the binary
 *   defines the bot class and exits. A top-level `runBot(...)` call counts too.
 *
 * Same checks as the Python SDK's `chipzen/dockerfile.py`.
 */

import { promises as fs } from "node:fs";
import path from "node:path";

import { dockerfileInstructions } from "./bigbadbots_dockerfile.js";
import type { ValidationResult } from "./validate.js";

const STARTER_HINT =
  "Run `npx -p @chipzen-ai/bot chipzen-sdk init <name>` for a fresh project to copy " +
  "from (it writes the JavaScript starter, packages/javascript/starters/javascript in " +
  "the SDK repo).";

const JS_FILE = /([\w./-]+\.(?:js|mjs|cjs))\b/;
const DEFINES_MAIN = /\bfunction\s+main\s*\(|\b(?:const|let|var)\s+main\s*=/;
// `main()` as a statement or a promise chain: `await main();`, `main().catch(...)`.
// A definition (`function main() {`) is followed by `{`, so it does not match.
const CALLS_MAIN = /(?:^|[\s;{(])(?:await\s+|void\s+)?main\s*\(\s*\)\s*(?:;|\.|$)/m;
// A top-level `runBot(...)` / `await runBot(...)`: at the start of a line, not
// indented inside a function body.
const TOP_LEVEL_RUN_BOT = /^(?:await\s+|void\s+)?runBot\s*\(/m;

function stripComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/[^\n]*/g, "");
}

function commandWords(args: string): string[] {
  if (args.startsWith("[")) {
    try {
      const parsed: unknown = JSON.parse(args);
      if (Array.isArray(parsed) && parsed.every((p) => typeof p === "string")) {
        return parsed as string[];
      }
    } catch {
      // shell form after all
    }
  }
  return args.split(/\s+/).filter(Boolean);
}

/** What the image starts: the final stage's ENTRYPOINT + CMD, or null if neither is set. */
export function startCommand(dockerfileText: string): string | null {
  const instructions = dockerfileInstructions(dockerfileText);
  let lastFrom = -1;
  instructions.forEach((ins, i) => {
    if (ins.keyword === "FROM") lastFrom = i;
  });
  const finalStage = instructions.slice(lastFrom + 1);
  const words: string[] = [];
  for (const keyword of ["ENTRYPOINT", "CMD"]) {
    const found = finalStage.filter((ins) => ins.keyword === keyword);
    if (found.length > 0) words.push(...commandWords(found[found.length - 1]!.args));
  }
  return words.length > 0 ? words.join(" ") : null;
}

/** The `.js` file the image runs, or compiles with `bun build`; null if it names none. */
function startedFile(dockerfileText: string, command: string): string | null {
  const direct = JS_FILE.exec(command);
  if (direct) return direct[1]!.split("/").pop()!;
  for (const { keyword, args } of dockerfileInstructions(dockerfileText)) {
    if (keyword !== "RUN" || !/\bbun\s+build\b/.test(args)) continue;
    const compiled = JS_FILE.exec(args.replace(/--\S+/g, " "));
    if (compiled) return compiled[1]!.split("/").pop()!;
  }
  return null;
}

async function readIfFile(p: string): Promise<string | null> {
  try {
    return await fs.readFile(p, "utf-8");
  } catch {
    return null;
  }
}

/** The `dockerfile` and `entry_main` results for a poker bot folder. */
export async function checkPokerImage(
  dir: string,
  entry: string,
  source: string,
): Promise<ValidationResult[]> {
  const results: ValidationResult[] = [];
  const entryName = path.basename(entry);

  const dockerfileText = await readIfFile(path.join(dir, "Dockerfile"));
  if (dockerfileText === null) {
    results.push({
      severity: "fail",
      name: "dockerfile",
      message:
        `No Dockerfile in ${dir}. The platform runs the Docker image you upload ` +
        "(docker build, then docker save | gzip), so this folder cannot be packaged. " +
        `Copy Dockerfile and .dockerignore into it. ${STARTER_HINT}`,
    });
  } else {
    const command = startCommand(dockerfileText);
    const started = command === null ? null : startedFile(dockerfileText, command);
    if (command === null) {
      results.push({
        severity: "fail",
        name: "dockerfile",
        message:
          "The Dockerfile's final stage sets no ENTRYPOINT or CMD, so the image runs " +
          "its base image's default command, not your bot. The starter's is " +
          '`ENTRYPOINT ["dumb-init", "/bot/bot"]`.',
      });
    } else if (started !== null && started !== entryName) {
      results.push({
        severity: "fail",
        name: "dockerfile",
        message:
          `The Dockerfile runs ${started}, but the entry point is ${entryName}. ` +
          "Rename the file or change the Dockerfile so they match.",
      });
    } else {
      results.push({
        severity: "pass",
        name: "dockerfile",
        message: `Dockerfile starts the bot with: ${command}`,
      });
    }
  }

  const code = stripComments(source);
  if ((DEFINES_MAIN.test(code) && CALLS_MAIN.test(code)) || TOP_LEVEL_RUN_BOT.test(code)) {
    results.push({ severity: "pass", name: "entry_main", message: `${entryName} starts the bot` });
  } else {
    const missing = DEFINES_MAIN.test(code)
      ? `${entryName} defines main() but never calls it`
      : `${entryName} has no main() that starts the bot`;
    results.push({
      severity: "fail",
      name: "entry_main",
      message:
        `${missing}. The image runs ${entryName} as its program (the starter compiles it ` +
        "with `bun build --compile`), so it must connect when run: keep the starter's " +
        "`main()`, which reads CHIPZEN_WS_URL and calls runBot, and its " +
        "`if (import.meta.main || ...) { await main(); }` block. Without them the image " +
        `builds but the bot never plays after upload. ${STARTER_HINT}`,
    });
  }
  return results;
}
