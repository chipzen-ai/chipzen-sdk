import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { type ChipzenConfig } from "../src/config.js";
import {
  ENV_NAMES,
  ENV_VAR_NAME,
  connectToChipzen,
  resolveEnvName,
  urlForEnv,
} from "../src/connect.js";
import { DEFAULT_RETRY_POLICY, RetryPolicy } from "../src/retry.js";

const BOT_ID = "abc12345-6789-4def-9012-3456789abcde";

// Save/restore CHIPZEN_ENV around each test so the host shell value (if
// any) doesn't leak in and the tests are independent.
let savedEnv: string | undefined;
beforeEach(() => {
  savedEnv = process.env[ENV_VAR_NAME];
  delete process.env[ENV_VAR_NAME];
});
afterEach(() => {
  if (savedEnv === undefined) delete process.env[ENV_VAR_NAME];
  else process.env[ENV_VAR_NAME] = savedEnv;
});

function cfg(fields: Partial<ChipzenConfig>): ChipzenConfig {
  return { path: "/x", token: null, url: null, botId: null, ...fields };
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

describe("connect constants", () => {
  it("ENV_NAMES is the canonical three", () => {
    expect(ENV_NAMES).toEqual(["prod", "staging", "local"]);
  });
  it("ENV_VAR_NAME is CHIPZEN_ENV", () => {
    expect(ENV_VAR_NAME).toBe("CHIPZEN_ENV");
  });
});

// ---------------------------------------------------------------------------
// Env -> URL mapping
// ---------------------------------------------------------------------------

describe("urlForEnv", () => {
  it("maps each env to the canonical template", () => {
    expect(urlForEnv("prod", BOT_ID)).toBe(`wss://chipzen.ai/ws/external/bot/${BOT_ID}`);
    expect(urlForEnv("staging", BOT_ID)).toBe(`wss://staging.chipzen.ai/ws/external/bot/${BOT_ID}`);
    expect(urlForEnv("local", BOT_ID)).toBe(`ws://localhost:8001/ws/external/bot/${BOT_ID}`);
  });

  it("only local uses unencrypted ws://", () => {
    expect(urlForEnv("prod", BOT_ID).startsWith("wss://")).toBe(true);
    expect(urlForEnv("staging", BOT_ID).startsWith("wss://")).toBe(true);
    expect(urlForEnv("local", BOT_ID).startsWith("ws://")).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// connectToChipzen — env resolution (config discovery bypassed via config:null)
// ---------------------------------------------------------------------------

describe("connectToChipzen env resolution", () => {
  it("explicit env=prod resolves to the prod lobby URL", () => {
    const c = connectToChipzen(BOT_ID, "prod", { config: null });
    expect(c.url).toBe(`wss://chipzen.ai/ws/external/bot/${BOT_ID}`);
    expect(c.env).toBe("prod");
  });

  it("explicit env=staging resolves to the staging lobby URL", () => {
    const c = connectToChipzen(BOT_ID, "staging", { config: null });
    expect(c.url).toBe(`wss://staging.chipzen.ai/ws/external/bot/${BOT_ID}`);
    expect(c.env).toBe("staging");
  });

  it("explicit env=local resolves to localhost:8001", () => {
    const c = connectToChipzen(BOT_ID, "local", { config: null });
    expect(c.url).toBe(`ws://localhost:8001/ws/external/bot/${BOT_ID}`);
    expect(c.env).toBe("local");
  });

  it("defaults to prod when nothing is set", () => {
    const c = connectToChipzen(BOT_ID, undefined, { config: null });
    expect(c.url).toBe(`wss://chipzen.ai/ws/external/bot/${BOT_ID}`);
    expect(c.env).toBe("prod");
  });

  it("$CHIPZEN_ENV drives the env when no explicit arg", () => {
    process.env[ENV_VAR_NAME] = "staging";
    const c = connectToChipzen(BOT_ID, undefined, { config: null });
    expect(c.env).toBe("staging");
    expect(c.url).toContain(`/ws/external/bot/${BOT_ID}`);
  });

  it("explicit env arg wins over $CHIPZEN_ENV", () => {
    process.env[ENV_VAR_NAME] = "prod";
    const c = connectToChipzen(BOT_ID, "staging", { config: null });
    expect(c.env).toBe("staging");
    expect(c.url).toBe(`wss://staging.chipzen.ai/ws/external/bot/${BOT_ID}`);
  });

  it("empty $CHIPZEN_ENV is treated as unset", () => {
    process.env[ENV_VAR_NAME] = "";
    const c = connectToChipzen(BOT_ID, undefined, { config: null });
    expect(c.env).toBe("prod");
  });

  it("unknown $CHIPZEN_ENV raises", () => {
    process.env[ENV_VAR_NAME] = "production";
    expect(() => connectToChipzen(BOT_ID, undefined, { config: null })).toThrow(
      /not a recognized environment/,
    );
  });

  it("unknown explicit env raises and lists the legal values", () => {
    expect(() => connectToChipzen(BOT_ID, "prd" as never, { config: null })).toThrow(/Unknown env/);
    try {
      connectToChipzen(BOT_ID, "prd" as never, { config: null });
    } catch (err) {
      const msg = (err as Error).message;
      expect(msg).toContain("prod");
      expect(msg).toContain("staging");
      expect(msg).toContain("local");
    }
  });
});

// ---------------------------------------------------------------------------
// Config-file URL override
// ---------------------------------------------------------------------------

describe("connectToChipzen config-file override", () => {
  it("config url always wins over env-derived url", () => {
    const c = connectToChipzen(BOT_ID, "staging", {
      config: cfg({ url: "wss://custom.example/ws/external/bot/xyz" }),
    });
    expect(c.url).toBe("wss://custom.example/ws/external/bot/xyz");
    // env is null because the URL was supplied verbatim.
    expect(c.env).toBeNull();
    expect(c.config?.url).toBe("wss://custom.example/ws/external/bot/xyz");
  });

  it("config token is surfaced on the result", () => {
    const c = connectToChipzen(BOT_ID, "staging", {
      config: cfg({ token: "cz_extbot_from_file" }),
    });
    expect(c.token).toBe("cz_extbot_from_file");
    expect(c.url).toBe(`wss://staging.chipzen.ai/ws/external/bot/${BOT_ID}`);
    expect(c.env).toBe("staging");
  });

  it("no config -> token null", () => {
    const c = connectToChipzen(BOT_ID, "prod", { config: null });
    expect(c.token).toBeNull();
    expect(c.config).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// bot_id validation
// ---------------------------------------------------------------------------

describe("connectToChipzen bot_id validation", () => {
  it("empty bot_id raises", () => {
    expect(() => connectToChipzen("", "prod", { config: null })).toThrow(/botId/);
  });
});

// ---------------------------------------------------------------------------
// Retry policy plumbing
// ---------------------------------------------------------------------------

describe("connectToChipzen retry policy", () => {
  it("uses DEFAULT_RETRY_POLICY when unspecified", () => {
    const c = connectToChipzen(BOT_ID, "prod", { config: null });
    expect(c.retryPolicy).toBe(DEFAULT_RETRY_POLICY);
  });

  it("preserves a custom policy verbatim", () => {
    const custom = new RetryPolicy({ maxReconnectAttempts: 10, initialBackoffMs: 250 });
    const c = connectToChipzen(BOT_ID, "prod", { config: null, retryPolicy: custom });
    expect(c.retryPolicy).toBe(custom);
  });
});

// ---------------------------------------------------------------------------
// resolveEnvName helper
// ---------------------------------------------------------------------------

describe("resolveEnvName", () => {
  it("no explicit + no env var -> prod", () => {
    expect(resolveEnvName(undefined, undefined)).toBe("prod");
  });
  it("env var used when no explicit", () => {
    expect(resolveEnvName(undefined, "staging")).toBe("staging");
  });
  it("explicit wins over env var", () => {
    expect(resolveEnvName("local", "prod")).toBe("local");
  });
  it("empty env var falls through to prod", () => {
    expect(resolveEnvName(undefined, "")).toBe("prod");
  });
  it("bad env var raises", () => {
    expect(() => resolveEnvName(undefined, "bogus")).toThrow(/not a recognized environment/);
  });
  it("bad explicit raises", () => {
    expect(() => resolveEnvName("bogus" as never, undefined)).toThrow(/Unknown env/);
  });
});
