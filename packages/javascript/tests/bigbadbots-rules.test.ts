/**
 * The published Big Bad Bots rule sets bundled with the JS SDK, and the
 * warnings a `BigBadBotsBot` logs at `match_start` (chipzen-sdk#151, parity
 * with the Python SDK's #144).
 */

import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, describe, expect, it, vi } from "vitest";

import { RULESETS_URL, parseRuleSet, type FightState } from "../src/bigbadbots.js";
import { BigBadBotsBot } from "../src/bigbadbots_bot.js";
import {
  parsePublishedRuleSet,
  publishedRuleSet,
  publishedRuleSetIds,
  ruleSetWarnings,
} from "../src/bigbadbots_rules.js";
import * as sdk from "../src/index.js";
import { Action } from "../src/models.js";

type Json = Record<string, unknown>;

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, "..", "..", "..");
const PY_RULESETS = path.join(REPO, "packages", "python", "src", "chipzen", "rulesets", "bigbadbots");
const JS_RULESETS = path.join(HERE, "..", "src", "data", "bigbadbots", "rulesets");
const PUBLISHED_SECTIONS = ["arena", "deadline", "forfeit", "gearup", "rounds"];

function jsonFiles(dir: string): string[] {
  return readdirSync(dir)
    .filter((f) => f.endsWith(".json"))
    .sort();
}

function published(id: string): Json {
  return JSON.parse(readFileSync(path.join(PY_RULESETS, `${id}.json`), "utf-8")) as Json;
}

function table(id: string): Json {
  return structuredClone(published(id).rule_set) as Json;
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("the bundled rule-set files", () => {
  it("are the Python SDK's files, byte for byte", () => {
    expect(jsonFiles(JS_RULESETS)).toEqual(jsonFiles(PY_RULESETS));
    for (const file of jsonFiles(PY_RULESETS)) {
      expect(readFileSync(path.join(JS_RULESETS, file), "utf-8"), file).toBe(
        readFileSync(path.join(PY_RULESETS, file), "utf-8"),
      );
    }
  });

  it("are every released rule set, sorted", () => {
    expect(publishedRuleSetIds()).toEqual(["bigbadbots-1.0", "bigbadbots-1.1", "bigbadbots-1.2"]);
    expect(jsonFiles(JS_RULESETS).map((f) => f.replace(/\.json$/, ""))).toEqual(
      publishedRuleSetIds(),
    );
  });

  it.each(["bigbadbots-1.0", "bigbadbots-1.1", "bigbadbots-1.2"])(
    "%s carries its metadata and only the published sections",
    (id) => {
      const entry = publishedRuleSet(id)!;
      const file = published(id);
      expect(entry).toMatchObject({
        id,
        state: file.state,
        engine: file.engine,
        frozenOn: file.frozen_on,
        sha256: file.sha256,
      });
      expect(Object.keys(entry.sections).sort()).toEqual(PUBLISHED_SECTIONS);
      expect(entry.sections).toEqual(file.rule_set);
      // Complete enough to read as a rule set.
      expect(parseRuleSet(id, entry.sections).gearup.budget).toBe(1000);
    },
  );

  it("an unknown id has no published entry", () => {
    expect(publishedRuleSet("bigbadbots-9.0")).toBeNull();
    expect(publishedRuleSet("bigbadbots-1.1-dev")).toBeNull();
    expect(publishedRuleSet("fighter-1.0")).toBeNull();
  });

  it("entries are read-only", () => {
    const entry = publishedRuleSet("bigbadbots-1.2")!;
    expect(Object.isFrozen(entry)).toBe(true);
    expect(() => {
      (entry.sections.gearup as Json).budget = 5000;
    }).toThrow(TypeError);
    expect(publishedRuleSet("bigbadbots-1.2")!.sections).toEqual(published("bigbadbots-1.2").rule_set);
  });

  it("a malformed file is refused", () => {
    expect(() => parsePublishedRuleSet({ rule_set_id: "x" })).toThrow(sdk.StateParseError);
    expect(() => parsePublishedRuleSet({ ...published("bigbadbots-1.0"), sha256: 1 })).toThrow(
      /sha256/,
    );
  });

  it("is on the public surface", () => {
    expect(sdk.publishedRuleSet).toBe(publishedRuleSet);
    expect(sdk.publishedRuleSetIds).toBe(publishedRuleSetIds);
    expect(sdk.ruleSetWarnings).toBe(ruleSetWarnings);
  });
});

describe("ruleSetWarnings", () => {
  it("is empty for a known rule set with its published numbers", () => {
    for (const id of publishedRuleSetIds()) {
      expect(ruleSetWarnings(parseRuleSet(id, table(id)))).toEqual([]);
    }
  });

  it("names an unknown rule set, what it does know, and the spec", () => {
    const warnings = ruleSetWarnings(parseRuleSet("bigbadbots-9.0", table("bigbadbots-1.2")));
    expect(warnings).toHaveLength(1);
    expect(warnings[0]).toContain('"bigbadbots-9.0"');
    expect(warnings[0]).toContain("bigbadbots-1.2");
    expect(warnings[0]).toContain(RULESETS_URL);
  });

  it("reports a value that differs from the published one, and the server's value is played", () => {
    const t = table("bigbadbots-1.2");
    (t.deadline as Json).deadline_ms = 80;
    const rules = parseRuleSet("bigbadbots-1.2", t);
    const warnings = ruleSetWarnings(rules);
    expect(warnings).toHaveLength(1);
    expect(warnings[0]).toContain("rule_set.deadline.deadline_ms is 80 (published: 100)");
    expect(rules.deadline.deadlineMs).toBe(80);
  });

  it("reports a published key the server left out", () => {
    const t = table("bigbadbots-1.1");
    delete (t.rounds as Json).stall_close_after;
    const warnings = ruleSetWarnings(parseRuleSet("bigbadbots-1.1", t));
    expect(warnings).toEqual([
      expect.stringContaining("rule_set.rounds.stall_close_after is missing (published: 50)"),
    ]);
  });

  it("ignores sections the published file does not carry", () => {
    const t = { ...table("bigbadbots-1.2"), moves: { punch_high: { startup: 2 } } };
    expect(ruleSetWarnings(parseRuleSet("bigbadbots-1.2", t))).toEqual([]);
  });
});

class Fighter extends BigBadBotsBot {
  act(_state: FightState): Action {
    return Action.act("block");
  }
}

function matchStart(ruleSetId: string, ruleSet: Json): Json {
  return {
    type: "match_start",
    game_config: { variant: "bigbadbots", rule_set_id: ruleSetId, num_players: 2, rule_set: ruleSet },
  };
}

describe("BigBadBotsBot at match_start", () => {
  it("logs nothing for a published rule set", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const bot = new Fighter();
    bot.onMatchStart(matchStart("bigbadbots-1.2", table("bigbadbots-1.2")));
    expect(bot.rules?.id).toBe("bigbadbots-1.2");
    expect(warn).not.toHaveBeenCalled();
  });

  it("warns on an unknown rule set and plays on its numbers", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const t = table("bigbadbots-1.2");
    (t.gearup as Json).budget = 1500;
    const bot = new Fighter();
    bot.onMatchStart(matchStart("bigbadbots-1.3-dev", t));
    expect(bot.rules?.gearup.budget).toBe(1500);
    expect(warn).toHaveBeenCalledTimes(1);
    expect(String(warn.mock.calls[0]![0])).toMatch(
      /^chipzen-sdk: rule set "bigbadbots-1.3-dev" is not one this SDK release knows/,
    );
  });

  it("warns once per published value that differs", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const t = table("bigbadbots-1.0");
    (t.gearup as Json).budget = 900;
    (t.arena as Json).width = 14.0;
    new Fighter().onMatchStart(matchStart("bigbadbots-1.0", t));
    expect(warn.mock.calls.map((c) => String(c[0]))).toEqual([
      expect.stringContaining("rule_set.gearup.budget is 900 (published: 1000)"),
      expect.stringContaining("rule_set.arena.width is 14 (published: 12)"),
    ]);
  });
});
