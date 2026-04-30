/**
 * `chipzen-sdk validate <path>` — pre-upload conformance checks.
 *
 * Mirrors the Python validator's check shape and severity model so a
 * `(severity, name, message)` tuple from either language renders the
 * same way in client tooling.
 */

import { execSync } from "node:child_process";
import { promises as fs } from "node:fs";
import path from "node:path";

import { type GameState } from "./models.js";

const VALID_ACTION_KINDS = new Set(["fold", "check", "call", "raise", "all_in"]);

/**
 * Duck-type check for an `Action`. We can't use `instanceof Action`
 * because the bot under test imports `Action` from a separate module
 * graph (the published dist or a file:// URL) — those classes are
 * structurally identical to ours but reference-distinct, so
 * `instanceof` would always be false.
 */
function isAction(v: unknown): boolean {
  if (!v || typeof v !== "object") return false;
  const a = v as { action?: unknown; amount?: unknown };
  if (typeof a.action !== "string" || !VALID_ACTION_KINDS.has(a.action)) return false;
  if (a.action === "raise") {
    return typeof a.amount === "number" && Number.isFinite(a.amount) && a.amount >= 0;
  }
  return a.amount === undefined;
}

export type Severity = "pass" | "warn" | "fail";

export interface ValidationResult {
  severity: Severity;
  name: string;
  message: string;
}

export interface ValidateOptions {
  /** Override entry point filename (default: auto-detect bot.js / bot.mjs / bot.cjs). */
  entryPoint?: string;
  /** Hard-fail upload size threshold, in bytes. Defaults to 500 MB (platform cap). */
  maxUploadBytes?: number;
  /** Warn if `decide()` takes longer than this (ms). */
  timeoutWarnMs?: number;
}

export const DEFAULT_MAX_UPLOAD_BYTES = 500 * 1024 * 1024;
export const DEFAULT_TIMEOUT_WARN_MS = 100;
export const PLATFORM_TIMEOUT_MS = 500;

const ALLOWED_ENTRY_POINTS = ["bot.js", "bot.mjs", "bot.cjs", "main.js", "main.mjs"];

/**
 * Modules whose import would either fail in the platform sandbox or
 * indicate a class of bot we don't allow. Mirrors the Python
 * BLOCKED_MODULES set; not exhaustive — caught here as a fast pre-flight.
 */
const BLOCKED_MODULES = new Set([
  "node:child_process",
  "child_process",
  "node:cluster",
  "cluster",
  "node:dgram",
  "dgram",
  "node:dns",
  "dns",
  "node:net",
  "net",
  "node:tls",
  "tls",
  "node:repl",
  "repl",
  "node:vm",
  "vm",
  "node:worker_threads",
  "worker_threads",
]);

/**
 * Modules that might be fine but warrant a warning so the bot author
 * knows they're at the edge of what the sandbox tolerates.
 */
const WARN_MODULES = new Set(["node:fs", "fs", "node:fs/promises"]);

// ---------------------------------------------------------------------------
// Public entry
// ---------------------------------------------------------------------------

export async function validateBot(
  botPath: string,
  options: ValidateOptions = {},
): Promise<ValidationResult[]> {
  const results: ValidationResult[] = [];
  const maxBytes = options.maxUploadBytes ?? DEFAULT_MAX_UPLOAD_BYTES;
  const timeoutWarn = options.timeoutWarnMs ?? DEFAULT_TIMEOUT_WARN_MS;

  let stat;
  try {
    stat = await fs.stat(botPath);
  } catch {
    results.push({
      severity: "fail",
      name: "file_structure",
      message: `Path not found: ${botPath}`,
    });
    return results;
  }
  if (!stat.isDirectory()) {
    results.push({
      severity: "fail",
      name: "file_structure",
      message: `Path is not a directory: ${botPath}`,
    });
    return results;
  }

  results.push(...(await checkDirectorySize(botPath, maxBytes)));
  results.push(...(await checkDirectory(botPath, options.entryPoint, timeoutWarn)));
  return results;
}

// ---------------------------------------------------------------------------
// Per-check implementations
// ---------------------------------------------------------------------------

async function checkDirectorySize(
  dir: string,
  maxBytes: number,
): Promise<ValidationResult[]> {
  const total = await dirTotalBytes(dir);
  const mb = total / (1024 * 1024);
  const limitMb = maxBytes / (1024 * 1024);
  if (total > maxBytes) {
    return [
      {
        severity: "fail",
        name: "size",
        message: `Directory is ${mb.toFixed(1)} MB, exceeds ${limitMb.toFixed(0)} MB upload limit`,
      },
    ];
  }
  return [
    {
      severity: "pass",
      name: "size",
      message: `Size OK (${mb.toFixed(1)} MB uncompressed / ${limitMb.toFixed(0)} MB limit)`,
    },
  ];
}

async function checkDirectory(
  dir: string,
  entryPointOverride: string | undefined,
  timeoutWarnMs: number,
): Promise<ValidationResult[]> {
  const results: ValidationResult[] = [];

  const entry = await findEntryPoint(dir, entryPointOverride);
  if (!entry) {
    results.push({
      severity: "fail",
      name: "file_structure",
      message: `No entry point found. Expected one of: ${ALLOWED_ENTRY_POINTS.join(", ")}`,
    });
    return results;
  }
  results.push({
    severity: "pass",
    name: "file_structure",
    message: `Entry point found: ${path.basename(entry)}`,
  });

  // syntax — defer to `node --check` so we get Node's actual parser.
  const syntaxResult = await checkSyntax(entry);
  results.push(syntaxResult);
  if (syntaxResult.severity === "fail") return results;

  // imports — regex scan for blocked / warned modules.
  const source = await fs.readFile(entry, "utf-8");
  results.push(...checkImports(source, path.basename(entry)));

  // bot_class — regex scan for `class X extends Bot`.
  const botClassResult = checkBotClass(source);
  results.push(botClassResult);
  if (botClassResult.severity === "fail") return results;
  const botClassName = botClassResult.message.replace("Found bot class: ", "");

  // decide_method — regex scan for `decide(` inside the class body.
  const decideResult = checkDecideMethod(source, botClassName);
  results.push(decideResult);
  if (decideResult.severity === "fail") return results;

  // smoke_test + timeout — dynamic import + invoke decide() once.
  results.push(...(await smokeTest(entry, botClassName, timeoutWarnMs)));

  return results;
}

async function findEntryPoint(
  dir: string,
  override: string | undefined,
): Promise<string | null> {
  const candidates = override ? [override] : ALLOWED_ENTRY_POINTS;
  for (const name of candidates) {
    const candidate = path.join(dir, name);
    try {
      const s = await fs.stat(candidate);
      if (s.isFile()) return candidate;
    } catch {
      // try next
    }
  }
  return null;
}

async function checkSyntax(filePath: string): Promise<ValidationResult> {
  try {
    execSync(`node --check ${JSON.stringify(filePath)}`, {
      stdio: ["ignore", "pipe", "pipe"],
      timeout: 5000,
    });
    return { severity: "pass", name: "syntax", message: "Valid JavaScript syntax" };
  } catch (err) {
    const stderr = (err as { stderr?: Buffer }).stderr?.toString() ?? String(err);
    const firstLine = stderr.split("\n").find((l) => l.includes("SyntaxError")) ?? stderr.split("\n")[0] ?? stderr;
    return {
      severity: "fail",
      name: "syntax",
      message: `Syntax error: ${firstLine.trim()}`,
    };
  }
}

function checkImports(source: string, filename: string): ValidationResult[] {
  const results: ValidationResult[] = [];
  // Match `import ... from "X"` and `import("X")` and `require("X")`
  const importRe = /(?:from|import|require)\s*\(?\s*["']([^"']+)["']/g;
  const found = new Set<string>();
  let m;
  while ((m = importRe.exec(source)) !== null) {
    if (m[1]) found.add(m[1]);
  }

  const blocked: string[] = [];
  const warned: string[] = [];
  for (const mod of found) {
    if (BLOCKED_MODULES.has(mod)) blocked.push(mod);
    else if (WARN_MODULES.has(mod)) warned.push(mod);
  }

  if (blocked.length) {
    results.push({
      severity: "fail",
      name: "imports",
      message: `Blocked imports detected in ${filename}: ${blocked.join(", ")}`,
    });
  } else {
    results.push({
      severity: "pass",
      name: "imports",
      message: "No blocked imports detected",
    });
  }
  for (const w of warned) {
    results.push({
      severity: "warn",
      name: "imports",
      message: `Imports ${JSON.stringify(w)} — usable but the platform sandbox restricts what it can do`,
    });
  }
  return results;
}

function checkBotClass(source: string): ValidationResult {
  // Look for `class XYZ extends Bot` or `class XYZ extends ChipzenBot`.
  const m = /class\s+([A-Za-z_$][A-Za-z0-9_$]*)\s+extends\s+(?:Bot|ChipzenBot)\b/.exec(source);
  if (!m || !m[1]) {
    return {
      severity: "fail",
      name: "bot_class",
      message: "No class extending Bot (or ChipzenBot) found in entry point",
    };
  }
  return {
    severity: "pass",
    name: "bot_class",
    message: `Found bot class: ${m[1]}`,
  };
}

function checkDecideMethod(source: string, className: string): ValidationResult {
  // Find the class block, then look for `decide(` inside it.
  // Imperfect (regex-based; nested classes / minified code can fool it),
  // but matches the Python validator's level of scrutiny for an alpha-tier
  // pre-flight check. The smoke_test below catches the actual runtime case.
  const classBlock = extractClassBlock(source, className);
  if (!classBlock) {
    return {
      severity: "fail",
      name: "decide_method",
      message: `Could not isolate ${className}'s class body for decide() check`,
    };
  }
  if (!/\bdecide\s*\(/.test(stripComments(classBlock))) {
    return {
      severity: "fail",
      name: "decide_method",
      message: `${className} does not implement decide()`,
    };
  }
  return {
    severity: "pass",
    name: "decide_method",
    message: `${className}.decide() implemented`,
  };
}

function extractClassBlock(source: string, className: string): string | null {
  const start = source.search(new RegExp(`class\\s+${className}\\s+extends\\s+(?:Bot|ChipzenBot)\\b`));
  if (start < 0) return null;
  // Find the opening brace, then scan forward counting braces.
  const braceIdx = source.indexOf("{", start);
  if (braceIdx < 0) return null;
  let depth = 1;
  for (let i = braceIdx + 1; i < source.length; i++) {
    if (source[i] === "{") depth++;
    else if (source[i] === "}") {
      depth--;
      if (depth === 0) return source.slice(braceIdx + 1, i);
    }
  }
  return null;
}

async function smokeTest(
  entry: string,
  className: string,
  timeoutWarnMs: number,
): Promise<ValidationResult[]> {
  const results: ValidationResult[] = [];

  let mod: Record<string, unknown>;
  try {
    // file:// URL is required for ESM dynamic import of an absolute path.
    const url = new URL(`file://${path.resolve(entry)}`).toString();
    mod = (await import(url)) as Record<string, unknown>;
  } catch (err) {
    results.push({
      severity: "fail",
      name: "smoke_test",
      message: `Failed to import bot: ${(err as Error).message}`,
    });
    return results;
  }

  const Cls = mod[className];
  if (typeof Cls !== "function") {
    results.push({
      severity: "fail",
      name: "smoke_test",
      message: `Bot class ${className} not exported from entry point — add \`export { ${className} }\``,
    });
    return results;
  }

  let bot: { decide: (state: GameState) => unknown };
  try {
    bot = new (Cls as { new (): { decide: (state: GameState) => unknown } })();
  } catch (err) {
    results.push({
      severity: "fail",
      name: "smoke_test",
      message: `${className}() constructor threw: ${(err as Error).message}`,
    });
    return results;
  }

  const mockState = mockGameState();
  const start = performance.now();
  let action: unknown;
  try {
    action = bot.decide(mockState);
  } catch (err) {
    results.push({
      severity: "fail",
      name: "smoke_test",
      message: `${className}.decide() threw: ${(err as Error).message}`,
    });
    return results;
  }
  const elapsedMs = performance.now() - start;

  if (!isAction(action)) {
    results.push({
      severity: "fail",
      name: "smoke_test",
      message: `${className}.decide() returned ${describe(action)} — expected an Action`,
    });
    return results;
  }

  results.push({
    severity: "pass",
    name: "smoke_test",
    message: `decide() returned ${(action as { action: string }).action} successfully`,
  });

  // Timeout sanity check.
  if (elapsedMs > PLATFORM_TIMEOUT_MS) {
    results.push({
      severity: "fail",
      name: "timeout",
      message: `decide() took ${elapsedMs.toFixed(1)} ms — exceeds platform default ${PLATFORM_TIMEOUT_MS} ms`,
    });
  } else if (elapsedMs > timeoutWarnMs) {
    results.push({
      severity: "warn",
      name: "timeout",
      message: `decide() took ${elapsedMs.toFixed(1)} ms — over the warn threshold of ${timeoutWarnMs} ms`,
    });
  } else {
    results.push({
      severity: "pass",
      name: "timeout",
      message: `decide() completed in ${elapsedMs.toFixed(1)} ms`,
    });
  }

  return results;
}

function mockGameState(): GameState {
  return {
    handNumber: 1,
    phase: "preflop",
    holeCards: [
      { rank: "A", suit: "h" },
      { rank: "K", suit: "d" },
    ],
    board: [],
    pot: 150,
    yourStack: 9900,
    opponentStacks: [9850],
    yourSeat: 0,
    dealerSeat: 0,
    toCall: 50,
    minRaise: 200,
    maxRaise: 9900,
    validActions: ["fold", "call", "raise"],
    actionHistory: [],
    roundId: "",
    requestId: "",
  };
}

function stripComments(source: string): string {
  // Strip /* ... */ block comments and // ... line comments. Doesn't try
  // to be string-literal-aware — false positives there are harmless for
  // the regex check that comes after.
  return source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/[^\n]*/g, "");
}

function describe(v: unknown): string {
  if (v === null) return "null";
  if (v === undefined) return "undefined";
  return `${typeof v} ${JSON.stringify(v)}`;
}

async function dirTotalBytes(dir: string): Promise<number> {
  let total = 0;
  const entries = await fs.readdir(dir, { withFileTypes: true });
  for (const entry of entries) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      // Skip node_modules — it's not part of the upload context.
      if (entry.name === "node_modules" || entry.name === ".git") continue;
      total += await dirTotalBytes(full);
    } else if (entry.isFile()) {
      const s = await fs.stat(full);
      total += s.size;
    }
  }
  return total;
}
