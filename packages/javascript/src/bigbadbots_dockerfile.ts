/**
 * `chipzen-sdk validate` on a Big Bad Bots bot: does its Dockerfile start the
 * bot through the platform's log-cap wrapper?
 *
 * Every Big Bad Bots bot runs under the platform's per-match log cap, and the
 * platform's review rejects an image whose ENTRYPOINT does not run it. The
 * starters ship the wrapper (`log_cap.py`) next to their Dockerfile, copy it to
 * `/opt/chipzen/log_cap.py`, and make it the ENTRYPOINT with the bot command as
 * CMD. Same check as the Python SDK's `check_bigbadbots_dockerfile`.
 */

import { promises as fs } from "node:fs";
import path from "node:path";

import type { ValidationResult } from "./validate.js";

/** Where a Big Bad Bots image keeps the platform's log-cap wrapper. */
export const LOG_CAP_SCRIPT = "/opt/chipzen/log_cap.py";

/**
 * The ENTRYPOINTs a Big Bad Bots image may use: the wrapper, with the bot
 * command as CMD. `python3` in the JavaScript starter (a Debian Python that
 * only runs the wrapper), `python` in the Python starters.
 */
export const BIGBADBOTS_ENTRYPOINTS: readonly (readonly string[])[] = [
  ["python3", "-I", "-S", "-u", LOG_CAP_SCRIPT, "--"],
  ["python", "-I", "-S", "-u", LOG_CAP_SCRIPT, "--"],
];

const FIX =
  "Copy log_cap.py from a Big Bad Bots starter folder into this folder, keep the " +
  `starter's \`COPY log_cap.py ${LOG_CAP_SCRIPT}\` line and its ENTRYPOINT ` +
  `${JSON.stringify(BIGBADBOTS_ENTRYPOINTS[0])}, and put your bot command in CMD.`;

type Instruction = { keyword: string; args: string };

/** One entry per instruction, comments and line continuations resolved. */
export function dockerfileInstructions(text: string): Instruction[] {
  const out: Instruction[] = [];
  let current = "";
  const push = (raw: string) => {
    const trimmed = raw.trim();
    if (!trimmed) return;
    const match = /^(\S+)\s*([\s\S]*)$/.exec(trimmed);
    if (match) out.push({ keyword: match[1]!.toUpperCase(), args: match[2]!.trim() });
  };
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    if (line.endsWith("\\")) {
      current += line.slice(0, -1) + " ";
      continue;
    }
    push(current + line);
    current = "";
  }
  push(current);
  return out;
}

function copyOperands(args: string): { flags: string[]; operands: string[] } {
  const words = args.split(/\s+/).filter(Boolean);
  const flags: string[] = [];
  while (words.length > 0 && words[0]!.startsWith("--")) flags.push(words.shift()!);
  const rest = words.join(" ");
  if (rest.startsWith("[")) {
    try {
      const parsed: unknown = JSON.parse(rest);
      if (Array.isArray(parsed) && parsed.every((p) => typeof p === "string")) {
        return { flags, operands: parsed as string[] };
      }
    } catch {
      // shell form after all
    }
  }
  return { flags, operands: words };
}

async function isFile(p: string): Promise<boolean> {
  try {
    return (await fs.stat(p)).isFile();
  } catch {
    return false;
  }
}

/**
 * Check a Big Bad Bots bot's `Dockerfile`. No Dockerfile in `dir`: nothing to
 * check (an empty list). Otherwise the final build stage must copy
 * `log_cap.py` to {@link LOG_CAP_SCRIPT} (from a file in the build context)
 * and set one of {@link BIGBADBOTS_ENTRYPOINTS}, exec form.
 */
export async function checkBigBadBotsDockerfile(dir: string): Promise<ValidationResult[]> {
  const dockerfile = path.join(dir, "Dockerfile");
  if (!(await isFile(dockerfile))) return [];
  const instructions = dockerfileInstructions(await fs.readFile(dockerfile, "utf-8"));
  let lastFrom = -1;
  instructions.forEach((ins, i) => {
    if (ins.keyword === "FROM") lastFrom = i;
  });
  const finalStage = instructions.slice(lastFrom + 1);

  const problems: string[] = [];
  let copied = false;
  for (const { keyword, args } of finalStage) {
    if (keyword !== "COPY" && keyword !== "ADD") continue;
    const { flags, operands } = copyOperands(args);
    if (operands.length < 2) continue;
    const sources = operands.slice(0, -1);
    const destination = operands[operands.length - 1]!;
    let wrapperSources: string[];
    if (destination === LOG_CAP_SCRIPT) wrapperSources = sources;
    else if (destination.replace(/\/+$/, "") === "/opt/chipzen") {
      wrapperSources = sources.filter((s) => s.split("/").pop() === "log_cap.py");
    } else continue;
    if (wrapperSources.length === 0) continue;
    copied = true;
    const fromContext = !flags.some((f) => f.startsWith("--from"));
    for (const source of wrapperSources) {
      if (fromContext && !(await isFile(path.join(dir, source)))) {
        problems.push(
          `the Dockerfile copies ${source} to ${LOG_CAP_SCRIPT}, but there is no ${source} in ${dir}`,
        );
      }
    }
  }
  if (!copied) {
    problems.push(`the Dockerfile's final stage does not copy log_cap.py to ${LOG_CAP_SCRIPT}`);
  }

  const entrypoints = finalStage.filter((ins) => ins.keyword === "ENTRYPOINT");
  if (entrypoints.length === 0) {
    problems.push("the Dockerfile's final stage sets no ENTRYPOINT");
  } else {
    const found = entrypoints[entrypoints.length - 1]!.args;
    let parsed: unknown = null;
    try {
      parsed = JSON.parse(found);
    } catch {
      parsed = null;
    }
    const ok =
      Array.isArray(parsed) &&
      BIGBADBOTS_ENTRYPOINTS.some(
        (form) => form.length === parsed.length && form.every((part, i) => part === parsed[i]),
      );
    if (!ok) problems.push(`its ENTRYPOINT is ${found}`);
  }

  if (problems.length > 0) {
    return [
      {
        severity: "fail",
        name: "bigbadbots_dockerfile",
        message: `Big Bad Bots images must start through the log-cap wrapper: ${problems.join("; ")}. ${FIX}`,
      },
    ];
  }
  return [
    {
      severity: "pass",
      name: "bigbadbots_dockerfile",
      message: "Dockerfile starts the bot through the log-cap wrapper",
    },
  ];
}
