import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  CONFIG_FILENAME,
  ChipzenConfigError,
  SECTION_NAME,
  discoverConfigPath,
  loadChipzenConfig,
  resolveToken,
  resolveUrl,
  type ChipzenConfig,
} from "../src/config.js";

// ---------------------------------------------------------------------------
// Temp-dir helpers
// ---------------------------------------------------------------------------

let tmpRoot: string;

beforeEach(() => {
  tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), "chipzen-cfg-"));
});

afterEach(() => {
  fs.rmSync(tmpRoot, { recursive: true, force: true });
});

function writeToml(rel: string, body: string): string {
  const filePath = path.join(tmpRoot, rel);
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, body, "utf-8");
  return filePath;
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

describe("config constants", () => {
  it("match the spec strings", () => {
    expect(CONFIG_FILENAME).toBe("chipzen.toml");
    expect(SECTION_NAME).toBe("external_api");
  });

  it("ChipzenConfigError is an Error", () => {
    expect(new ChipzenConfigError("x")).toBeInstanceOf(Error);
  });
});

// ---------------------------------------------------------------------------
// Discovery — first match wins
// ---------------------------------------------------------------------------

describe("discoverConfigPath", () => {
  it("returns null when nothing exists", () => {
    expect(discoverConfigPath([path.join(tmpRoot, "nope.toml")])).toBeNull();
    expect(discoverConfigPath([])).toBeNull();
  });

  it("cwd candidate wins over home candidate", () => {
    const cwdFile = writeToml(path.join("cwd", CONFIG_FILENAME), "[external_api]\ntoken='cwd'\n");
    const homeFile = writeToml(path.join("home", CONFIG_FILENAME), "[external_api]\ntoken='home'\n");
    expect(discoverConfigPath([cwdFile, homeFile])).toBe(cwdFile);
  });

  it("falls through to the home candidate when cwd is missing", () => {
    const cwdMissing = path.join(tmpRoot, "cwd", CONFIG_FILENAME);
    const homeFile = writeToml(path.join("home", CONFIG_FILENAME), "[external_api]\ntoken='home'\n");
    expect(discoverConfigPath([cwdMissing, homeFile])).toBe(homeFile);
  });

  it("skips a directory at the config path and falls through", () => {
    const weird = path.join(tmpRoot, CONFIG_FILENAME);
    fs.mkdirSync(weird);
    const fallback = writeToml(path.join("elsewhere", CONFIG_FILENAME), "[external_api]\ntoken='ok'\n");
    expect(discoverConfigPath([weird, fallback])).toBe(fallback);
  });
});

// ---------------------------------------------------------------------------
// Parsing — happy path
// ---------------------------------------------------------------------------

describe("loadChipzenConfig parsing", () => {
  it("parses token + url + bot_id", () => {
    const p = writeToml(
      CONFIG_FILENAME,
      '[external_api]\ntoken = "cz_extbot_xyz"\nurl = "wss://chipzen.ai/ws/external/bot/abc"\nbot_id = "abc"\n',
    );
    const cfg = loadChipzenConfig([p]);
    expect(cfg).not.toBeNull();
    expect(cfg!.token).toBe("cz_extbot_xyz");
    expect(cfg!.url).toBe("wss://chipzen.ai/ws/external/bot/abc");
    expect(cfg!.botId).toBe("abc");
    expect(cfg!.path).toBe(p);
  });

  it("token only — url + bot_id null", () => {
    const p = writeToml(CONFIG_FILENAME, '[external_api]\ntoken = "cz_extbot_xyz"\n');
    const cfg = loadChipzenConfig([p]);
    expect(cfg!.token).toBe("cz_extbot_xyz");
    expect(cfg!.url).toBeNull();
    expect(cfg!.botId).toBeNull();
  });

  it("url only — token null", () => {
    const p = writeToml(CONFIG_FILENAME, '[external_api]\nurl = "wss://staging.chipzen.ai/ws/external/bot/x"\n');
    const cfg = loadChipzenConfig([p]);
    expect(cfg!.token).toBeNull();
    expect(cfg!.url).toBe("wss://staging.chipzen.ai/ws/external/bot/x");
  });

  it("empty [external_api] section -> all fields null", () => {
    const p = writeToml(CONFIG_FILENAME, "[external_api]\n");
    const cfg = loadChipzenConfig([p]);
    expect(cfg).not.toBeNull();
    expect(cfg!.token).toBeNull();
    expect(cfg!.url).toBeNull();
    expect(cfg!.botId).toBeNull();
  });

  it("no file on the search path -> null (not an error)", () => {
    expect(loadChipzenConfig([path.join(tmpRoot, "missing.toml")])).toBeNull();
  });

  it("ignores unknown fields under [external_api] (forward-compat)", () => {
    const p = writeToml(CONFIG_FILENAME, '[external_api]\ntoken = "cz_extbot_xyz"\nfuture_field = "42"\n');
    const cfg = loadChipzenConfig([p]);
    expect(cfg!.token).toBe("cz_extbot_xyz");
  });

  it("ignores unrelated top-level sections", () => {
    const p = writeToml(CONFIG_FILENAME, '[other_tool]\nsetting = "x"\n[external_api]\ntoken = "cz_extbot_ok"\n');
    const cfg = loadChipzenConfig([p]);
    expect(cfg!.token).toBe("cz_extbot_ok");
  });

  it("accepts single-quoted (literal) string values", () => {
    const p = writeToml(CONFIG_FILENAME, "[external_api]\ntoken = 'cz_extbot_literal'\n");
    const cfg = loadChipzenConfig([p]);
    expect(cfg!.token).toBe("cz_extbot_literal");
  });

  it("strips trailing comments outside the quoted value", () => {
    const p = writeToml(CONFIG_FILENAME, '[external_api]\ntoken = "cz_extbot_x"  # my token\n');
    const cfg = loadChipzenConfig([p]);
    expect(cfg!.token).toBe("cz_extbot_x");
  });
});

// ---------------------------------------------------------------------------
// Parsing — error surfaces
// ---------------------------------------------------------------------------

describe("loadChipzenConfig errors", () => {
  it("missing [external_api] section is a hard error", () => {
    const p = writeToml(CONFIG_FILENAME, '[other]\nfoo = "bar"\n');
    expect(() => loadChipzenConfig([p])).toThrow(/\[external_api\]/);
  });

  it("non-string token raises", () => {
    const p = writeToml(CONFIG_FILENAME, "[external_api]\ntoken = 42\n");
    expect(() => loadChipzenConfig([p])).toThrow(/token must be a string/);
  });

  it("non-string url raises", () => {
    const p = writeToml(CONFIG_FILENAME, "[external_api]\nurl = true\n");
    expect(() => loadChipzenConfig([p])).toThrow(/url must be a string/);
  });

  it("non-string bot_id raises", () => {
    const p = writeToml(CONFIG_FILENAME, "[external_api]\nbot_id = 42\n");
    expect(() => loadChipzenConfig([p])).toThrow(/bot_id must be a string/);
  });

  it("array-of-tables [[external_api]] is the wrong shape", () => {
    const p = writeToml(CONFIG_FILENAME, '[[external_api]]\ntoken = "cz_extbot_x"\n');
    expect(() => loadChipzenConfig([p])).toThrow(/\[external_api\] must be a table/);
  });

  it("thrown errors are ChipzenConfigError instances", () => {
    const p = writeToml(CONFIG_FILENAME, "[external_api]\ntoken = 42\n");
    expect(() => loadChipzenConfig([p])).toThrow(ChipzenConfigError);
  });
});

// ---------------------------------------------------------------------------
// resolveToken + resolveUrl precedence
// ---------------------------------------------------------------------------

function cfg(fields: Partial<ChipzenConfig>): ChipzenConfig {
  return { path: "/x", token: null, url: null, botId: null, ...fields };
}

describe("resolveToken precedence", () => {
  it("explicit wins over config", () => {
    expect(
      resolveToken({ explicitToken: "cz_extbot_kwarg", config: cfg({ token: "cz_extbot_config" }) }),
    ).toBe("cz_extbot_kwarg");
  });

  it("explicit empty string wins over config", () => {
    expect(resolveToken({ explicitToken: "", config: cfg({ token: "cz_extbot_config" }) })).toBe("");
  });

  it("config used when no explicit token", () => {
    expect(resolveToken({ explicitToken: null, config: cfg({ token: "cz_extbot_config" }) })).toBe(
      "cz_extbot_config",
    );
  });

  it("null when nothing set", () => {
    expect(resolveToken({ explicitToken: null })).toBeNull();
    expect(resolveToken({ explicitToken: null, config: null })).toBeNull();
  });

  it("explicit ticket suppresses the config token", () => {
    expect(
      resolveToken({ explicitToken: null, explicitTicket: "ticket-abc", config: cfg({ token: "cz_extbot_config" }) }),
    ).toBeNull();
  });
});

describe("resolveUrl precedence", () => {
  it("explicit wins over config", () => {
    expect(resolveUrl({ explicitUrl: "wss://kwarg/ws", config: cfg({ url: "wss://config/ws" }) })).toBe(
      "wss://kwarg/ws",
    );
  });

  it("config used when no explicit url", () => {
    expect(resolveUrl({ explicitUrl: null, config: cfg({ url: "wss://config/ws" }) })).toBe("wss://config/ws");
  });

  it("null when nothing set", () => {
    expect(resolveUrl({ explicitUrl: null, config: null })).toBeNull();
    expect(resolveUrl({ explicitUrl: null })).toBeNull();
  });
});
