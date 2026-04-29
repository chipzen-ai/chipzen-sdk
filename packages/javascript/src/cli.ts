/**
 * CLI dispatcher for `chipzen-sdk` (init / validate).
 *
 * Mirrors the Python CLI's two-command surface. Uses Node's built-in
 * `parseArgs` so we don't pull in commander/yargs as a dep.
 */

import { parseArgs } from "node:util";
import path from "node:path";

import { scaffoldBot } from "./scaffold.js";
import {
  type ValidationResult,
  validateBot,
  DEFAULT_MAX_UPLOAD_BYTES,
  DEFAULT_TIMEOUT_WARN_MS,
} from "./validate.js";

const COMMANDS = {
  init: "Scaffold a new bot project from a starter template",
  validate: "Run pre-upload checks: size, syntax, imports, smoke test, timeout",
} as const;

export async function main(argv: string[] = process.argv.slice(2)): Promise<void> {
  if (argv.length === 0 || argv[0] === "--help" || argv[0] === "-h") {
    printHelp();
    process.exit(argv.length === 0 ? 1 : 0);
  }

  const [command, ...rest] = argv;
  switch (command) {
    case "init":
      await initCli(rest);
      return;
    case "validate":
      await validateCli(rest);
      return;
    default:
      console.error(`Unknown command: ${command}`);
      console.error("");
      printHelp();
      process.exit(1);
  }
}

function printHelp(): void {
  console.log("Chipzen Poker Bot SDK");
  console.log("");
  console.log("Usage: chipzen-sdk <command> [options]");
  console.log("");
  console.log("Commands:");
  for (const [name, desc] of Object.entries(COMMANDS)) {
    console.log(`  ${name.padEnd(12)} ${desc}`);
  }
  console.log("");
  console.log("Run 'chipzen-sdk <command> --help' for details on a specific command.");
}

// ---------------------------------------------------------------------------
// init
// ---------------------------------------------------------------------------

async function initCli(args: string[]): Promise<void> {
  if (args[0] === "--help" || args[0] === "-h") {
    console.log("Usage: chipzen-sdk init <name> [--dir <parent>]");
    console.log("");
    console.log("Scaffold a new Chipzen bot project under <parent>/<name>.");
    console.log("Default parent is the current directory.");
    return;
  }

  const { values, positionals } = parseArgs({
    args,
    options: { dir: { type: "string" } },
    allowPositionals: true,
  });

  const name = positionals[0];
  if (!name) {
    console.error("error: chipzen-sdk init requires a <name> positional argument");
    process.exit(2);
  }

  try {
    const created = await scaffoldBot(name, { parentDir: values.dir });
    console.log(`Created bot project: ${created}`);
    console.log("");
    console.log("Next steps:");
    console.log(`  cd ${name}`);
    console.log("  npm install");
    console.log("  # Edit bot.js to implement your strategy");
    console.log("  chipzen-sdk validate .");
  } catch (err) {
    console.error(`error: ${(err as Error).message}`);
    process.exit(1);
  }
}

// ---------------------------------------------------------------------------
// validate
// ---------------------------------------------------------------------------

async function validateCli(args: string[]): Promise<void> {
  if (args[0] === "--help" || args[0] === "-h") {
    console.log("Usage: chipzen-sdk validate <path> [options]");
    console.log("");
    console.log("Pre-upload validation. Checks performed:");
    console.log("  size            Directory size within upload limits");
    console.log("  file_structure  Entry point file (bot.js / bot.mjs / bot.cjs / main.js)");
    console.log("  syntax          Valid JavaScript via `node --check`");
    console.log("  imports         No blocked sandbox modules; warn on fs/fs-promises");
    console.log("  bot_class       Class extending Bot or ChipzenBot exists");
    console.log("  decide_method   Bot class implements decide()");
    console.log("  smoke_test      Bot can be instantiated; decide() returns an Action");
    console.log("  timeout         decide() completes within time limits");
    console.log("");
    console.log("Options:");
    console.log("  --entry-point <name>      Override entry-point filename");
    console.log("  --max-size-mb <int>       Max upload size in MB (default: " +
      `${DEFAULT_MAX_UPLOAD_BYTES / (1024 * 1024)})`);
    console.log("  --timeout-warn-ms <int>   Warn-threshold for decide() in ms (default: " +
      `${DEFAULT_TIMEOUT_WARN_MS})`);
    console.log("  --check-connectivity      Run the protocol-conformance harness");
    console.log("                            (drives the bot through 1 canned match)");
    console.log("  --no-color                Disable colored output");
    return;
  }

  const { values, positionals } = parseArgs({
    args,
    options: {
      "entry-point": { type: "string" },
      "max-size-mb": { type: "string" },
      "timeout-warn-ms": { type: "string" },
      "check-connectivity": { type: "boolean" },
      "no-color": { type: "boolean" },
    },
    allowPositionals: true,
  });

  const target = positionals[0];
  if (!target) {
    console.error("error: chipzen-sdk validate requires a <path> positional argument");
    process.exit(2);
  }

  const maxBytes = values["max-size-mb"]
    ? parseInt(values["max-size-mb"], 10) * 1024 * 1024
    : DEFAULT_MAX_UPLOAD_BYTES;
  const timeoutWarn = values["timeout-warn-ms"]
    ? parseInt(values["timeout-warn-ms"], 10)
    : DEFAULT_TIMEOUT_WARN_MS;

  const results = await validateBot(path.resolve(target), {
    entryPoint: values["entry-point"],
    maxUploadBytes: maxBytes,
    timeoutWarnMs: timeoutWarn,
    checkConnectivity: values["check-connectivity"] ?? false,
  });

  printResults(results, !values["no-color"]);

  if (results.some((r) => r.severity === "fail")) process.exit(1);
}

function printResults(results: ValidationResult[], color: boolean): void {
  const supportsColor = color && process.stdout.isTTY;
  const GREEN = supportsColor ? "\x1b[92m" : "";
  const YELLOW = supportsColor ? "\x1b[93m" : "";
  const RED = supportsColor ? "\x1b[91m" : "";
  const RESET = supportsColor ? "\x1b[0m" : "";

  console.log("");
  console.log("Chipzen Bot Validation");
  console.log("=".repeat(50));
  for (const r of results) {
    const icon =
      r.severity === "pass"
        ? `${GREEN}PASS${RESET}`
        : r.severity === "warn"
          ? `${YELLOW}WARN${RESET}`
          : `${RED}FAIL${RESET}`;
    console.log(`  [${icon}] ${r.name}: ${r.message}`);
  }

  console.log("");
  const fails = results.filter((r) => r.severity === "fail").length;
  if (fails > 0) {
    console.log(`${RED}${fails} check${fails === 1 ? "" : "s"} failed.${RESET}`);
  } else {
    console.log(`${GREEN}All checks passed! Your bot is ready to upload.${RESET}`);
  }
}
