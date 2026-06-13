/**
 * `chipzen-sdk run-external` CLI wrapper for external-API bots.
 *
 * Mirrors the Python SDK's `chipzen.run_external` (External-API Issue 25,
 * chipzen-ai/chipzen-sdk#44). Wraps the boilerplate of "load config,
 * connect, run bot" so a dev's main loop is just:
 *
 *     chipzen-sdk run-external my-bot.js
 *
 * The wrapper:
 *
 * 1. Loads token + url + bot_id from a discovered `chipzen.toml`.
 * 2. Resolves the env-aware lobby URL via `connectToChipzen` when no
 *    explicit `url` is set in `chipzen.toml`.
 * 3. Dynamically imports the user's bot module from a filesystem path.
 * 4. Discovers the `Bot` subclass exported by that file (single subclass
 *    auto-selected; multiple require `--bot-class <name>`).
 * 5. Hands off to `runExternalBot` with the resolved url + token + policy.
 *
 * Precedence (highest first):
 *
 * - **token**:  `--token` > `[external_api].token`.
 * - **url**:    `[external_api].url` > env-derived URL from bot_id + env.
 * - **env**:    `--env` > `$CHIPZEN_ENV` > `prod`.
 * - **bot_id**: `--bot-id` > `[external_api].bot_id`. Required only when no
 *   explicit URL is configured.
 */

import path from "node:path";
import { pathToFileURL } from "node:url";

import { Bot } from "./bot.js";
import {
  type ChipzenConfig,
  ChipzenConfigError,
  loadChipzenConfig,
} from "./config.js";
import { connectToChipzen, type EnvName, ENV_NAMES } from "./connect.js";
import { DEFAULT_RETRY_POLICY, type RetryPolicy } from "./retry.js";
import { runExternalBot } from "./external.js";

/** Parsed `run-external` arguments. */
export interface RunExternalArgs {
  botFile: string;
  env: EnvName | null;
  token: string | null;
  botId: string | null;
  botClass: string | null;
  maxMatches: number | null;
  safeMode: boolean;
}

/**
 * Parse `run-external` argv (excluding the `run-external` token itself).
 *
 * Flags mirror the Python CLI: `--env`, `--token`, `--bot-id`,
 * `--bot-class`, `--max-matches`, `--no-safe-mode`. Throws on an unknown
 * flag, a missing value, or a bad `--env` choice so the caller can map it
 * to a clean non-zero exit.
 */
export function parseRunExternalArgs(args: string[]): RunExternalArgs {
  const out: RunExternalArgs = {
    botFile: "",
    env: null,
    token: null,
    botId: null,
    botClass: null,
    maxMatches: null,
    safeMode: true,
  };
  const positionals: string[] = [];

  for (let i = 0; i < args.length; i++) {
    const arg = args[i]!;
    const takeValue = (name: string): string => {
      const value = args[++i];
      if (value === undefined) throw new Error(`${name} requires a value`);
      return value;
    };
    switch (arg) {
      case "--env": {
        const value = takeValue("--env");
        if (!(ENV_NAMES as readonly string[]).includes(value)) {
          throw new Error(
            `--env must be one of ${ENV_NAMES.join(", ")} (got ${JSON.stringify(value)})`,
          );
        }
        out.env = value as EnvName;
        break;
      }
      case "--token":
        out.token = takeValue("--token");
        break;
      case "--bot-id":
        out.botId = takeValue("--bot-id");
        break;
      case "--bot-class":
        out.botClass = takeValue("--bot-class");
        break;
      case "--max-matches": {
        const value = takeValue("--max-matches");
        const n = Number.parseInt(value, 10);
        if (!Number.isFinite(n)) {
          throw new Error(`--max-matches must be an integer (got ${JSON.stringify(value)})`);
        }
        out.maxMatches = n;
        break;
      }
      case "--no-safe-mode":
        out.safeMode = false;
        break;
      default:
        if (arg.startsWith("-")) {
          throw new Error(`Unknown option: ${arg}`);
        }
        positionals.push(arg);
        break;
    }
  }

  const botFile = positionals[0];
  if (!botFile) {
    throw new Error("run-external requires a <bot-file> positional argument");
  }
  out.botFile = botFile;
  return out;
}

/**
 * Dynamically import a bot module from a filesystem path.
 *
 * Uses a `file://` URL so the import works regardless of cwd. Supports
 * `.js` / `.mjs` / `.cjs` modules (whatever Node's loader resolves).
 *
 * @throws Error if the file cannot be loaded (missing, syntax error,
 *   missing dependency). The message names the file.
 */
export async function loadBotModule(botFile: string): Promise<Record<string, unknown>> {
  const abs = path.resolve(botFile);
  try {
    // Cache-bust so repeated loads of the same path (tests) re-evaluate.
    const url = `${pathToFileURL(abs).href}?t=${Date.now()}`;
    return (await import(url)) as Record<string, unknown>;
  } catch (err) {
    throw new Error(`Failed to load ${botFile}: ${(err as Error).message}`);
  }
}

/** A Bot subclass constructor (zero-arg construction). */
export type BotConstructor = new () => Bot;

/**
 * Return all `Bot` subclass constructors exported by `module`.
 *
 * Scans the module's exported values for classes whose prototype chain
 * includes {@link Bot} (and that aren't `Bot` itself). Both `export class`
 * and `export default class` are considered.
 */
export function findBotSubclasses(module: Record<string, unknown>): BotConstructor[] {
  const found: BotConstructor[] = [];
  const seen = new Set<unknown>();
  for (const value of Object.values(module)) {
    if (typeof value !== "function") continue;
    if (value === Bot) continue;
    if (seen.has(value)) continue;
    // A subclass of Bot: Bot is in its prototype chain.
    if (value.prototype instanceof Bot) {
      seen.add(value);
      found.push(value as BotConstructor);
    }
  }
  return found;
}

/**
 * Pick a single Bot subclass from the discovered list.
 *
 * @throws Error if no candidates exist, the explicit name doesn't match,
 *   or multiple candidates exist without an explicit pick. The message
 *   lists the options.
 */
export function selectBotClass(
  candidates: BotConstructor[],
  botClassName: string | null,
  botFile: string,
): BotConstructor {
  if (botClassName !== null) {
    const matches = candidates.filter((c) => c.name === botClassName);
    if (matches.length === 0) {
      const available = candidates.map((c) => c.name).join(", ") || "<none>";
      throw new Error(
        `No Bot subclass named ${JSON.stringify(botClassName)} in ${botFile}. ` +
          `Available subclasses: ${available}.`,
      );
    }
    return matches[0]!;
  }

  if (candidates.length === 0) {
    throw new Error(
      `No Bot subclass found in ${botFile}. Export a class that extends ` +
        `Bot (e.g. export class MyBot extends Bot { ... }).`,
    );
  }

  if (candidates.length > 1) {
    const names = candidates.map((c) => c.name).join(", ");
    throw new Error(
      `Multiple Bot subclasses found in ${botFile}: ${names}. ` +
        `Pick one with --bot-class <name>.`,
    );
  }

  return candidates[0]!;
}

/**
 * Resolve `(url, token, retryPolicy, config)` for `runExternalBot`.
 *
 * 1. A verbatim `[external_api].url` in config wins outright; token comes
 *    from `--token` > config token.
 * 2. Otherwise the URL is env-derived: need a `bot_id` (`--bot-id` >
 *    config) and call `connectToChipzen` to build the lobby URL.
 *
 * @throws Error if neither a config URL nor a bot_id is available.
 */
export function resolveConnection(opts: {
  config: ChipzenConfig | null;
  env: EnvName | null;
  token: string | null;
  botId: string | null;
  retryPolicy?: RetryPolicy;
}): { url: string; token: string | null; retryPolicy: RetryPolicy; config: ChipzenConfig | null } {
  const policy = opts.retryPolicy ?? DEFAULT_RETRY_POLICY;

  // Branch 1: verbatim URL in config wins outright. No bot_id resolution
  // and no env mapping needed.
  const configUrl = opts.config ? opts.config.url : null;
  if (configUrl !== null) {
    const token = opts.token !== null ? opts.token : opts.config ? opts.config.token : null;
    return { url: configUrl, token, retryPolicy: policy, config: opts.config };
  }

  const botId = opts.botId ?? (opts.config ? opts.config.botId : null);
  if (!botId) {
    throw new Error(
      "No lobby URL is configured. Either:\n" +
        "  - Pass --bot-id <id> on the command line, or\n" +
        "  - Set [external_api].bot_id in chipzen.toml, or\n" +
        "  - Set [external_api].url in chipzen.toml for a verbatim URL.",
    );
  }

  const conn = connectToChipzen(botId, opts.env, {
    retryPolicy: policy,
    config: opts.config,
  });
  const token = opts.token !== null ? opts.token : conn.token;
  return { url: conn.url, token, retryPolicy: conn.retryPolicy, config: conn.config };
}

/** Print `run-external` help text. */
export function printRunExternalHelp(): void {
  console.log("Usage: chipzen-sdk run-external <bot-file> [options]");
  console.log("");
  console.log("Run a Chipzen external-API bot from a JavaScript file. Loads config");
  console.log("from chipzen.toml, resolves the env-aware lobby URL, and plays via");
  console.log("the SDK's runExternalBot() entry point.");
  console.log("");
  console.log("Options:");
  console.log("  --env <prod|staging|local>  Target environment. Defaults to $CHIPZEN_ENV, else prod.");
  console.log("  --token <cz_extbot_...>      External-API token. Overrides [external_api].token.");
  console.log("  --bot-id <uuid>              External-API bot UUID. Overrides [external_api].bot_id.");
  console.log("  --bot-class <name>           Bot subclass to run when the file exports more than one.");
  console.log("  --max-matches <int>          Stop after this many matches. Default: run until lobby closes.");
  console.log("  --no-safe-mode               Let a decide() error crash the process (exit non-zero).");
  console.log("");
  console.log("Examples:");
  console.log("  chipzen-sdk run-external my-bot.js");
  console.log("  chipzen-sdk run-external my-bot.js --env staging");
  console.log("  CHIPZEN_ENV=staging chipzen-sdk run-external my-bot.js");
  console.log("  chipzen-sdk run-external my-bot.js --token cz_extbot_xyz --bot-id abc123");
  console.log("  chipzen-sdk run-external my-bot.js --bot-class TightAggressive");
}

/**
 * CLI entry point for `chipzen-sdk run-external`.
 *
 * Exits the process via `process.exit` on error (code 2 for setup errors,
 * code 1 for a bot-run failure) so a harness / CI sees the failure.
 */
export async function runExternalCli(args: string[]): Promise<void> {
  if (args[0] === "--help" || args[0] === "-h") {
    printRunExternalHelp();
    return;
  }

  let parsed: RunExternalArgs;
  try {
    parsed = parseRunExternalArgs(args);
  } catch (err) {
    console.error(`error: ${(err as Error).message}`);
    process.exit(2);
  }

  // Discover chipzen.toml once up front.
  let config: ChipzenConfig | null;
  try {
    config = loadChipzenConfig();
  } catch (err) {
    if (err instanceof ChipzenConfigError) {
      console.error(`error: invalid chipzen.toml: ${err.message}`);
      process.exit(2);
    }
    throw err;
  }

  // Resolve url + token + retry policy.
  let resolved;
  try {
    resolved = resolveConnection({
      config,
      env: parsed.env,
      token: parsed.token,
      botId: parsed.botId,
    });
  } catch (err) {
    console.error(`error: ${(err as Error).message}`);
    process.exit(2);
  }

  // Dynamically import the user's bot module + pick a Bot subclass.
  let module: Record<string, unknown>;
  try {
    module = await loadBotModule(parsed.botFile);
  } catch (err) {
    console.error(`error: ${(err as Error).message}`);
    process.exit(2);
  }

  let BotClass: BotConstructor;
  try {
    BotClass = selectBotClass(findBotSubclasses(module), parsed.botClass, parsed.botFile);
  } catch (err) {
    console.error(`error: ${(err as Error).message}`);
    process.exit(2);
  }

  let botInstance: Bot;
  try {
    botInstance = new BotClass();
  } catch (err) {
    console.error(`error: failed to instantiate ${BotClass.name}: ${(err as Error).message}`);
    process.exit(2);
  }

  console.error(`Connecting ${BotClass.name} -> ${resolved.url}`);

  try {
    await runExternalBot(botInstance, {
      url: resolved.url,
      token: resolved.token,
      retryPolicy: resolved.retryPolicy,
      config: resolved.config,
      safeMode: parsed.safeMode,
      maxMatches: parsed.maxMatches,
    });
  } catch (err) {
    // Includes BotDecisionError from --no-safe-mode: a bot bug should exit
    // non-zero so a harness / CI sees the failure.
    console.error(`error: bot run failed: ${(err as Error).message}`);
    process.exit(1);
  }
}
