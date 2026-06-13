/**
 * Environment-aware connection helper for the external-API lobby.
 *
 * Devs running an external-API bot against Chipzen shouldn't need to
 * remember the exact lobby WebSocket URL per environment. This module
 * exposes {@link connectToChipzen} — a small one-liner that returns a
 * fully-populated {@link ConnectionConfig} containing the resolved `url`,
 * `token`, and `retryPolicy`, ready to hand off to `runExternalBot`.
 *
 * Mirrors the Python SDK's `chipzen.connect` (External-API Issue 24,
 * chipzen-ai/chipzen-sdk#43).
 *
 * Environment → URL mapping:
 *
 *     prod    -> wss://chipzen.ai/ws/external/bot/{botId}
 *     staging -> wss://staging.chipzen.ai/ws/external/bot/{botId}
 *     local   -> ws://localhost:8001/ws/external/bot/{botId}
 *
 * Precedence (highest first) for the final WebSocket URL:
 *
 * 1. `[external_api].url` from a discovered `chipzen.toml` — a config-file
 *    URL ALWAYS wins (most explicit, user-managed override).
 * 2. An **explicitly passed** `env` argument.
 * 3. The `CHIPZEN_ENV` environment variable, if set to a recognized value.
 * 4. The default of `"prod"`.
 */

import {
  type ChipzenConfig,
  loadChipzenConfig,
  resolveToken,
  resolveUrl,
} from "./config.js";
import { DEFAULT_RETRY_POLICY, RetryPolicy } from "./retry.js";

/** Recognized target environments. */
export type EnvName = "prod" | "staging" | "local";

/** The canonical env names, in order, for error messages + validation. */
export const ENV_NAMES: readonly EnvName[] = ["prod", "staging", "local"] as const;

/**
 * Name of the environment variable consulted when `env` is not explicitly
 * passed.
 */
export const ENV_VAR_NAME = "CHIPZEN_ENV";

/** Canonical lobby-URL templates per env (`{botId}` substituted at resolve time). */
const ENV_URL_TEMPLATES: Record<EnvName, string> = {
  prod: "wss://chipzen.ai/ws/external/bot/{botId}",
  staging: "wss://staging.chipzen.ai/ws/external/bot/{botId}",
  local: "ws://localhost:8001/ws/external/bot/{botId}",
};

/**
 * Fully-resolved connection parameters ready for `runExternalBot`.
 * Returned by {@link connectToChipzen}.
 */
export interface ConnectionConfig {
  /** WebSocket URL the bot should connect to. */
  readonly url: string;
  /** Long-lived API token from a discovered `chipzen.toml`, or `null`. */
  readonly token: string | null;
  /** {@link RetryPolicy} controlling reconnect pacing. */
  readonly retryPolicy: RetryPolicy;
  /**
   * The resolved environment name, or `null` if the URL was supplied
   * verbatim via a config file (no env mapping applied). Mostly for logs.
   */
  readonly env: EnvName | null;
  /**
   * The {@link ChipzenConfig} discovered during resolution (if any), or
   * `null`. Exposed so callers can pass it through to `runExternalBot`
   * and avoid a second filesystem stat.
   */
  readonly config: ChipzenConfig | null;
}

function isEnvName(value: string): value is EnvName {
  return (ENV_NAMES as readonly string[]).includes(value);
}

/**
 * Pick the env name to use, applying explicit-arg-then-env-var rules.
 *
 * @param explicitEnv The `env` argument (`undefined`/`null` = "not specified").
 * @param envVarValue The current value of `$CHIPZEN_ENV` (or `undefined`).
 * @throws Error if an explicit `env` or the env-var value isn't recognized.
 */
export function resolveEnvName(
  explicitEnv: EnvName | null | undefined,
  envVarValue: string | undefined,
): EnvName {
  if (explicitEnv !== undefined && explicitEnv !== null) {
    if (!isEnvName(explicitEnv)) {
      throw new Error(`Unknown env ${JSON.stringify(explicitEnv)}. Valid values: ${ENV_NAMES.join(", ")}.`);
    }
    return explicitEnv;
  }

  // Empty string is treated as "not set" so an accidental `CHIPZEN_ENV=`
  // falls through to the default rather than tripping the error.
  if (envVarValue) {
    if (!isEnvName(envVarValue)) {
      throw new Error(
        `${ENV_VAR_NAME}=${JSON.stringify(envVarValue)} is not a recognized ` +
          `environment. Valid values: ${ENV_NAMES.join(", ")}.`,
      );
    }
    return envVarValue;
  }

  return "prod";
}

/** Format the canonical lobby URL for a given env + bot id. */
export function urlForEnv(env: EnvName, botId: string): string {
  return ENV_URL_TEMPLATES[env].replace("{botId}", botId);
}

/** Options for {@link connectToChipzen}. */
export interface ConnectToChipzenOptions {
  /** Override the reconnect-pacing policy. Defaults to {@link DEFAULT_RETRY_POLICY}. */
  retryPolicy?: RetryPolicy;
  /** Pre-loaded config to avoid a second filesystem stat. `undefined` triggers discovery. */
  config?: ChipzenConfig | null;
}

/**
 * Resolve a {@link ConnectionConfig} for the external-API lobby.
 *
 * Maps `env` to a canonical lobby URL and combines it with whatever
 * config-file token / URL / retry policy the dev has set up.
 *
 * @param botId External-API bot UUID. Required, non-empty.
 * @param env Target environment. `undefined`/`null` means "look at
 *   `$CHIPZEN_ENV` first, then fall back to `prod`".
 * @param options Optional retry policy + pre-loaded config.
 * @throws Error if `botId` is empty, `env` is unrecognized, or
 *   `$CHIPZEN_ENV` is set to an unrecognized value.
 * @throws ChipzenConfigError if a discovered `chipzen.toml` is malformed.
 */
export function connectToChipzen(
  botId: string,
  env?: EnvName | null,
  options: ConnectToChipzenOptions = {},
): ConnectionConfig {
  if (!botId || typeof botId !== "string") {
    throw new Error(
      "connectToChipzen() requires a non-empty botId string. Pass the " +
        "external-API bot UUID issued by the Chipzen platform, e.g. " +
        "connectToChipzen('abc123', 'prod').",
    );
  }

  // Resolve + validate the env name BEFORE config-file URL resolution so a
  // typo in an explicit `env` / `$CHIPZEN_ENV` always surfaces even when a
  // config file overrides the env-derived URL.
  const resolvedEnv = resolveEnvName(env, process.env[ENV_VAR_NAME]);
  const envDerivedUrl = urlForEnv(resolvedEnv, botId);

  const config = options.config !== undefined ? options.config : loadChipzenConfig();

  const configUrl = resolveUrl({ explicitUrl: null, config });
  let finalUrl: string;
  let envForReturn: EnvName | null;
  if (configUrl === null) {
    finalUrl = envDerivedUrl;
    envForReturn = resolvedEnv;
  } else {
    finalUrl = configUrl;
    // Config file supplied a verbatim URL; the env name is not meaningful.
    envForReturn = null;
  }

  const token = resolveToken({ explicitToken: null, config });
  const retryPolicy = options.retryPolicy ?? DEFAULT_RETRY_POLICY;

  return {
    url: finalUrl,
    token,
    retryPolicy,
    env: envForReturn,
    config: config ?? null,
  };
}
