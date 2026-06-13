import { describe, expect, it } from "vitest";

import { DEFAULT_RETRY_POLICY, RetryPolicy } from "../src/retry.js";

describe("RetryPolicy defaults", () => {
  it("uses the spec defaults", () => {
    const p = new RetryPolicy();
    expect(p.maxReconnectAttempts).toBe(5);
    expect(p.initialBackoffMs).toBe(500);
    expect(p.maxBackoffMs).toBe(30_000);
    expect(p.backoffMultiplier).toBe(2.0);
  });

  it("DEFAULT_RETRY_POLICY matches a freshly-constructed default", () => {
    expect(DEFAULT_RETRY_POLICY.maxReconnectAttempts).toBe(5);
    expect(DEFAULT_RETRY_POLICY.initialBackoffMs).toBe(500);
  });
});

describe("RetryPolicy.backoffMs progression", () => {
  it("doubles each attempt under the default policy", () => {
    const p = new RetryPolicy();
    expect(p.backoffMs(1)).toBe(500);
    expect(p.backoffMs(2)).toBe(1000);
    expect(p.backoffMs(3)).toBe(2000);
    expect(p.backoffMs(4)).toBe(4000);
    expect(p.backoffMs(5)).toBe(8000);
  });

  it("caps at maxBackoffMs", () => {
    const p = new RetryPolicy({ initialBackoffMs: 1000, maxBackoffMs: 4000, backoffMultiplier: 2 });
    expect(p.backoffMs(1)).toBe(1000);
    expect(p.backoffMs(2)).toBe(2000);
    expect(p.backoffMs(3)).toBe(4000);
    expect(p.backoffMs(4)).toBe(4000); // capped
    expect(p.backoffMs(10)).toBe(4000); // still capped
  });

  it("constant backoff when multiplier is 1.0", () => {
    const p = new RetryPolicy({ initialBackoffMs: 250, backoffMultiplier: 1.0 });
    expect(p.backoffMs(1)).toBe(250);
    expect(p.backoffMs(5)).toBe(250);
  });

  it("throws on attempt < 1", () => {
    const p = new RetryPolicy();
    expect(() => p.backoffMs(0)).toThrow(/attempt must be >= 1/);
  });
});

describe("RetryPolicy validation", () => {
  it("rejects negative maxReconnectAttempts", () => {
    expect(() => new RetryPolicy({ maxReconnectAttempts: -1 })).toThrow(/maxReconnectAttempts/);
  });

  it("rejects negative initialBackoffMs", () => {
    expect(() => new RetryPolicy({ initialBackoffMs: -1 })).toThrow(/initialBackoffMs/);
  });

  it("rejects maxBackoffMs < initialBackoffMs", () => {
    expect(() => new RetryPolicy({ initialBackoffMs: 1000, maxBackoffMs: 500 })).toThrow(
      /maxBackoffMs must be >= initialBackoffMs/,
    );
  });

  it("rejects backoffMultiplier < 1.0", () => {
    expect(() => new RetryPolicy({ backoffMultiplier: 0.5 })).toThrow(/backoffMultiplier/);
  });

  it("allows maxReconnectAttempts of 0 (disables reconnection)", () => {
    const p = new RetryPolicy({ maxReconnectAttempts: 0 });
    expect(p.maxReconnectAttempts).toBe(0);
  });
});
