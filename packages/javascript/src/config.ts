/**
 * `chipzen.toml` discovery and parsing for the SDK.
 *
 * Devs running an external-API bot should be able to drop their long-lived
 * API token into a config file once and forget about it, instead of
 * hard-coding `token="cz_extbot_..."` into source. This module implements
 * the discovery + parsing half of that convention; `runExternalBot` (and
 * the `chipzen-sdk run-external` CLI) consume the result and prefer
 * explicit kwargs over config-file values.
 *
 * Mirrors the Python SDK's `chipzen.config` (External-API Issue 23,
 * chipzen-ai/chipzen-sdk#42).
 *
 * Discovery
 * ---------
 *
 * Search order, first match wins:
 *
 * 1. `./chipzen.toml` (current working directory)
 * 2. `~/.chipzen/chipzen.toml` (user-home config)
 * 3. `/etc/chipzen/chipzen.toml` (system config, POSIX only — silently
 *    skipped on Windows where `/etc` does not exist)
 *
 * If no file is found, `loadChipzenConfig` returns `null` and the caller
 * falls back to whatever explicit arguments were passed. A clear error is
 * only raised when a file IS found but is malformed or missing the
 * expected section.
 *
 * File format
 * -----------
 *
 *     [external_api]
 *     token  = "cz_extbot_<32-char-base62-random>"
 *     url    = "wss://chipzen.ai/ws/external/bot/<bot_id>"  # optional
 *     bot_id = "<bot-uuid>"                                 # optional
 *
 * All three fields are optional and must be quoted strings. We parse the
 * single `[external_api]` table with a minimal inline reader rather than
 * pulling in a TOML dependency — keeping the package's single runtime dep
 * (`ws`).
 */

import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export const CONFIG_FILENAME = "chipzen.toml";
export const SECTION_NAME = "external_api";

/**
 * Parsed contents of a `chipzen.toml` file. All `[external_api]` fields
 * are optional; absence yields `null`.
 */
export interface ChipzenConfig {
  /** Filesystem path the config was loaded from (for error messages). */
  readonly path: string;
  /** Value of `[external_api] token` if present, else `null`. */
  readonly token: string | null;
  /** Value of `[external_api] url` if present, else `null`. */
  readonly url: string | null;
  /** Value of `[external_api] bot_id` if present, else `null`. */
  readonly botId: string | null;
}

/**
 * Raised when a `chipzen.toml` is found but cannot be used (malformed,
 * missing the `[external_api]` section, or a field is the wrong type).
 *
 * A "found but unusable" file is always a hard error — silent fallback
 * would mask typos that would otherwise be obvious.
 */
export class ChipzenConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ChipzenConfigError";
  }
}

/**
 * Return the ordered list of candidate config-file locations.
 *
 * 1. `./chipzen.toml` in the current working directory.
 * 2. `~/.chipzen/chipzen.toml` (user home).
 * 3. `/etc/chipzen/chipzen.toml` — POSIX only; omitted on Windows where
 *    `/etc` is not a meaningful path (the home-dir entry is enough for
 *    the typical Windows dev workflow).
 */
export function searchPaths(): string[] {
  const paths = [
    path.join(process.cwd(), CONFIG_FILENAME),
    path.join(os.homedir(), ".chipzen", CONFIG_FILENAME),
  ];
  if (process.platform !== "win32") {
    paths.push(path.join("/etc/chipzen", CONFIG_FILENAME));
  }
  return paths;
}

/**
 * Return the first existing `chipzen.toml` on the search path, or `null`.
 *
 * @param paths Override the default search order (mostly for tests). When
 *   omitted, uses {@link searchPaths}.
 */
export function discoverConfigPath(paths?: string[]): string | null {
  const candidates = paths ?? searchPaths();
  for (const candidate of candidates) {
    try {
      if (fs.statSync(candidate).isFile()) {
        return candidate;
      }
    } catch {
      // Not found / not statable — treat as "not present" and continue.
      continue;
    }
  }
  return null;
}

/**
 * Minimal parser for the `[external_api]` table of a `chipzen.toml`.
 *
 * Deliberately tiny — it understands exactly what the SDK needs: a single
 * `[external_api]` table with `key = "string"` entries. This keeps the
 * package's runtime dependency surface at one (`ws`) instead of pulling
 * in a full TOML library for three string fields.
 *
 * Recognized syntax inside the section:
 *
 * - `key = "value"` or `key = 'value'` — a quoted string assignment.
 * - `# comment` lines and blank lines — ignored.
 * - Other top-level `[sections]` — ignored (the dev may keep one config
 *   file for several tools).
 *
 * Anything that doesn't fit (an unquoted/typed value for a recognized
 * key, an array-of-tables `[[external_api]]`, a syntactically broken
 * assignment) raises {@link ChipzenConfigError}, matching the Python
 * reader's "found-but-malformed is a hard error" contract.
 *
 * @throws ChipzenConfigError on a malformed file or missing section.
 */
function parseExternalApiSection(
  raw: string,
  filePath: string,
): { token: string | null; url: string | null; botId: string | null } {
  const lines = raw.split(/\r?\n/);
  let inSection = false;
  let sawSection = false;
  const values: Record<string, string> = {};

  for (let i = 0; i < lines.length; i++) {
    const rawLine = lines[i] ?? "";
    const line = stripComment(rawLine).trim();
    if (line === "") continue;

    // Section header?
    if (line.startsWith("[")) {
      if (line.startsWith("[[")) {
        const inner = line.slice(2, line.indexOf("]]"));
        if (inner.trim() === SECTION_NAME) {
          throw new ChipzenConfigError(
            `${filePath}: [${SECTION_NAME}] must be a table (key=value pairs), ` +
              `got an array-of-tables ([[${SECTION_NAME}]]).`,
          );
        }
        inSection = false;
        continue;
      }
      const closeIdx = line.indexOf("]");
      if (closeIdx < 0) {
        throw new ChipzenConfigError(
          `${filePath}: malformed section header on line ${i + 1}: ${JSON.stringify(rawLine)}.`,
        );
      }
      const name = line.slice(1, closeIdx).trim();
      inSection = name === SECTION_NAME;
      if (inSection) sawSection = true;
      continue;
    }

    if (!inSection) continue;

    // key = value assignment.
    const eq = line.indexOf("=");
    if (eq < 0) {
      throw new ChipzenConfigError(
        `${filePath}: Failed to parse line ${i + 1} in [${SECTION_NAME}]: ` +
          `${JSON.stringify(rawLine)} (expected key = "value").`,
      );
    }
    const key = line.slice(0, eq).trim();
    const valueText = line.slice(eq + 1).trim();
    if (key === "") {
      throw new ChipzenConfigError(
        `${filePath}: Failed to parse line ${i + 1} in [${SECTION_NAME}]: empty key.`,
      );
    }

    // Only the three recognized keys are type-checked; unknown keys are
    // forward-compat ignored (mirrors the Python reader). But a malformed
    // value for a RECOGNIZED key is a hard error.
    if (key === "token" || key === "url" || key === "bot_id") {
      const parsed = parseQuotedString(valueText);
      if (parsed === null) {
        const display = key === "bot_id" ? "bot_id" : key;
        throw new ChipzenConfigError(
          `${filePath}: [${SECTION_NAME}].${display} must be a string, ` +
            `got ${JSON.stringify(valueText)}.`,
        );
      }
      values[key] = parsed;
    }
    // Unknown keys: ignored for forward-compat.
  }

  if (!sawSection) {
    throw new ChipzenConfigError(
      `${filePath} has no [${SECTION_NAME}] section. Add one with at least:\n` +
        `\n  [${SECTION_NAME}]\n  token = "cz_extbot_..."\n`,
    );
  }

  return {
    token: values.token ?? null,
    url: values.url ?? null,
    botId: values.bot_id ?? null,
  };
}

/** Strip a trailing `# comment` that is outside a quoted string. */
function stripComment(line: string): string {
  let inSingle = false;
  let inDouble = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (ch === "'" && !inDouble) inSingle = !inSingle;
    else if (ch === '"' && !inSingle) inDouble = !inDouble;
    else if (ch === "#" && !inSingle && !inDouble) return line.slice(0, i);
  }
  return line;
}

/**
 * Parse a quoted-string TOML value. Returns the unquoted content, or
 * `null` if the text is not a single well-formed quoted string (an
 * unquoted number/bool/bare word, or an unterminated quote).
 */
function parseQuotedString(text: string): string | null {
  if (text.length < 2) return null;
  const quote = text[0];
  if (quote !== '"' && quote !== "'") return null;
  if (text[text.length - 1] !== quote) return null;
  const inner = text.slice(1, -1);
  // Reject an embedded un-escaped closing quote, which would mean the
  // value isn't a single string (e.g. `"a" "b"`).
  if (quote === "'") {
    // Literal strings: no escapes, so any inner single-quote is illegal.
    if (inner.includes("'")) return null;
    return inner;
  }
  // Basic strings: allow `\"` escapes; reject a bare un-escaped `"`.
  let out = "";
  for (let i = 0; i < inner.length; i++) {
    const ch = inner[i];
    if (ch === "\\") {
      const next = inner[i + 1];
      if (next === '"' || next === "\\") {
        out += next;
        i++;
        continue;
      }
      if (next === "n") {
        out += "\n";
        i++;
        continue;
      }
      if (next === "t") {
        out += "\t";
        i++;
        continue;
      }
      out += ch;
      continue;
    }
    if (ch === '"') return null;
    out += ch;
  }
  return out;
}

/**
 * Discover and parse a `chipzen.toml` from the search path.
 *
 * @param paths Override the default search order. When omitted, uses
 *   cwd → `~/.chipzen/` → `/etc/chipzen/` (POSIX only).
 * @returns A {@link ChipzenConfig} if a file was found and parsed; `null`
 *   if no file exists on the search path. The "no file" case is NOT an
 *   error — the SDK falls back to explicit kwargs in that case.
 * @throws ChipzenConfigError if a file is found but is malformed, lacks
 *   the `[external_api]` section, or has a wrong-typed token / url / bot_id.
 */
export function loadChipzenConfig(paths?: string[]): ChipzenConfig | null {
  const filePath = discoverConfigPath(paths);
  if (filePath === null) return null;

  let raw: string;
  try {
    raw = fs.readFileSync(filePath, "utf-8");
  } catch (err) {
    throw new ChipzenConfigError(`Failed to read ${filePath}: ${(err as Error).message}`);
  }

  const { token, url, botId } = parseExternalApiSection(raw, filePath);
  return { path: filePath, token, url, botId };
}

/**
 * Return the token to use, honoring the precedence rules.
 *
 * 1. If `explicitToken` is non-`undefined`/non-`null`, return it. Even an
 *    empty string wins — the dev was explicit.
 * 2. If `explicitTicket` is set, return `null` (ticket-auth; no token).
 * 3. Otherwise, if `config` carries a token, return it.
 * 4. Otherwise, return `null`.
 */
export function resolveToken(opts: {
  explicitToken?: string | null;
  explicitTicket?: string | null;
  config?: ChipzenConfig | null;
}): string | null {
  if (opts.explicitToken !== undefined && opts.explicitToken !== null) {
    return opts.explicitToken;
  }
  if (opts.explicitTicket !== undefined && opts.explicitTicket !== null) {
    return null;
  }
  if (opts.config && opts.config.token !== null) {
    return opts.config.token;
  }
  return null;
}

/**
 * Return the URL override to use, honoring the precedence rules.
 *
 * 1. If `explicitUrl` is set, return it.
 * 2. Otherwise, if `config` carries a `url`, return it.
 * 3. Otherwise, return `null`.
 */
export function resolveUrl(opts: {
  explicitUrl?: string | null;
  config?: ChipzenConfig | null;
}): string | null {
  if (opts.explicitUrl !== undefined && opts.explicitUrl !== null) {
    return opts.explicitUrl;
  }
  if (opts.config && opts.config.url !== null) {
    return opts.config.url;
  }
  return null;
}
