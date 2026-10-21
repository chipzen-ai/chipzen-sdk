/**
 * Big Bad Bots rule sets: the published rule sets the SDK bundles, and the
 * warnings a bot gets when `match_start` sends something else.
 *
 * Every match is played under exactly one rule set, named by
 * `match_start.game_config.rule_set_id` (e.g. `"bigbadbots-1.2"`), whose
 * full parameter table comes in `game_config.rule_set`. A
 * {@link BigBadBotsBot} hands that table to every call as `state.rules`.
 *
 * - {@link publishedRuleSet} looks up a released rule set's published
 *   numbers offline, from the files bundled with the SDK.
 * - {@link ruleSetWarnings} reports a `rule_set_id` this release does not
 *   know, or a published value that differs from the one `match_start`
 *   sent. `BigBadBotsBot` logs them at `match_start` and plays on.
 *
 * **The server's numbers always win.** The bundled files are a reference;
 * the SDK never plays a bundled value in place of one `match_start` sent.
 *
 * Mirrors the Python SDK's `chipzen.bigbadbots_rules` and bundles the same
 * files. The id scheme, the lifecycle and the changelog:
 * https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/RULESETS.md
 */

import { RULESETS_URL, StateParseError, type RuleSet } from "./bigbadbots.js";
import ruleSet10 from "./data/bigbadbots/rulesets/bigbadbots-1.0.json" with { type: "json" };
import ruleSet11 from "./data/bigbadbots/rulesets/bigbadbots-1.1.json" with { type: "json" };
import ruleSet12 from "./data/bigbadbots/rulesets/bigbadbots-1.2.json" with { type: "json" };

/** The bundled rule-set files, as JSON. One per released rule set. */
const BUNDLED_FILES: readonly unknown[] = [ruleSet10, ruleSet11, ruleSet12];

/**
 * A released rule set as published in `docs/RULESETS.md`, bundled with the SDK.
 *
 * `sections` holds only the **published** sections of its table (`gearup`,
 * `arena`, `rounds`, `forfeit`, `deadline`), read-only. A match always sends
 * the full table in `match_start`.
 */
export interface PublishedRuleSet {
  /** The rule set's id, e.g. `"bigbadbots-1.2"`. */
  readonly id: string;
  /** Its lifecycle state when this SDK release was cut (RULESETS.md section 3). */
  readonly state: string;
  /** The engine release it was frozen with. */
  readonly engine: string;
  /** The date it was frozen (UTC), `YYYY-MM-DD`. */
  readonly frozenOn: string;
  /** The sha256 of its canonical content, as published. */
  readonly sha256: string;
  /** The published sections of its parameter table, with their wire keys. */
  readonly sections: Readonly<Record<string, unknown>>;
}

function deepFreeze<T>(value: T): T {
  if (value !== null && typeof value === "object") {
    for (const child of Object.values(value as Record<string, unknown>)) deepFreeze(child);
    Object.freeze(value);
  }
  return value;
}

function isObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function stringField(data: Record<string, unknown>, key: string, where: string): string {
  const value = data[key];
  if (typeof value !== "string") {
    throw new StateParseError(`${where}.${key}: expected a string, got ${JSON.stringify(value)}`);
  }
  return value;
}

/** Parse one bundled rule-set file. Exported for the tests. */
export function parsePublishedRuleSet(data: unknown, where = "rule set file"): PublishedRuleSet {
  if (!isObject(data)) throw new StateParseError(`${where}: expected an object`);
  const sections = data.rule_set;
  if (!isObject(sections)) throw new StateParseError(`${where}.rule_set: expected an object`);
  return deepFreeze({
    id: stringField(data, "rule_set_id", where),
    state: stringField(data, "state", where),
    engine: stringField(data, "engine", where),
    frozenOn: stringField(data, "frozen_on", where),
    sha256: stringField(data, "sha256", where),
    sections: structuredClone(sections),
  });
}

let bundledCache: ReadonlyMap<string, PublishedRuleSet> | null = null;

function bundled(): ReadonlyMap<string, PublishedRuleSet> {
  if (bundledCache === null) {
    const entries = BUNDLED_FILES.map((file) => parsePublishedRuleSet(file));
    entries.sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
    bundledCache = new Map(entries.map((entry) => [entry.id, entry]));
  }
  return bundledCache;
}

/** The ids of every published rule set this SDK release bundles, sorted. */
export function publishedRuleSetIds(): string[] {
  return [...bundled().keys()];
}

/**
 * A released rule set's published numbers, from the files bundled with the SDK.
 *
 * Works offline. Returns `null` for an id this SDK release does not know: a
 * rule set released after it, or a local or test id such as
 * `"bigbadbots-1.1-dev"`. Never a substitute for the numbers `match_start`
 * sends; see https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/RULESETS.md
 */
export function publishedRuleSet(ruleSetId: string): PublishedRuleSet | null {
  return bundled().get(ruleSetId) ?? null;
}

function sameValue(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (Array.isArray(a) && Array.isArray(b)) {
    return a.length === b.length && a.every((item, i) => sameValue(item, b[i]));
  }
  if (isObject(a) && isObject(b)) {
    const keys = Object.keys(a);
    return (
      keys.length === Object.keys(b).length &&
      keys.every((key) => key in b && sameValue(a[key], b[key]))
    );
  }
  return false;
}

function* differences(published: unknown, played: unknown, path: string): Generator<string> {
  if (isObject(published)) {
    for (const [key, value] of Object.entries(published)) {
      const where = `${path}.${key}`;
      if (!isObject(played) || !(key in played)) {
        yield `${where} is missing (published: ${JSON.stringify(value)})`;
      } else {
        yield* differences(value, played[key], where);
      }
    }
  } else if (!sameValue(published, played)) {
    yield `${path} is ${JSON.stringify(played)} (published: ${JSON.stringify(published)})`;
  }
}

/**
 * What a bot should be warned about in the rule set `match_start` sent.
 *
 * - The `rule_set_id` is not one this SDK release bundles (a newer rule
 *   set, or a local or test id).
 * - A published value differs from the one `match_start` sent.
 *
 * Either way the bot plays on the values `match_start` sent: these are
 * warnings, never errors, and nothing is substituted. Returns `[]` when
 * there is nothing to report.
 */
export function ruleSetWarnings(rules: RuleSet): string[] {
  const published = publishedRuleSet(rules.id);
  if (published === null) {
    const known = publishedRuleSetIds().join(", ") || "none";
    return [
      `rule set ${JSON.stringify(rules.id)} is not one this SDK release knows (it knows: ${known}); ` +
        "playing on the values match_start sent. Upgrade @chipzen-ai/bot for its " +
        `published numbers, and see ${RULESETS_URL}`,
    ];
  }
  return [...differences(published.sections, rules.params, "rule_set")].map(
    (difference) =>
      `rule set ${JSON.stringify(rules.id)}: match_start's ${difference}; playing on match_start's value`,
  );
}
