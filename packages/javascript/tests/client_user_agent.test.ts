/**
 * Integration test for the chipzen-sdk-javascript User-Agent header.
 *
 * `_runSession` accepts an arbitrary `SessionWebSocket` so the
 * client.test.ts unit tests can't observe the real constructor call.
 * This file uses `vi.mock` to substitute the `ws` package with a
 * spying WebSocket whose constructor records the options object.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

// Captured constructor args from the mocked `ws` package.
const capturedConstructorCalls: Array<{
  url: string;
  options: Record<string, unknown> | undefined;
}> = [];

vi.mock("ws", () => {
  // Minimal stand-in that records the constructor args and behaves
  // enough like a `ws` WebSocket for `runBot` to drive _runSession on
  // it. We expose `readyState` and `OPEN` so `_waitForOpen` short-
  // circuits.
  class FakeWebSocket {
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSING = 2;
    static CLOSED = 3;

    readyState = FakeWebSocket.OPEN;
    private listeners: Record<string, Array<(...args: unknown[]) => void>> = {};

    constructor(url: string, options?: Record<string, unknown>) {
      capturedConstructorCalls.push({ url, options });
      // Defer message delivery until `_runSession` has had a chance to
      // attach its `message` listener via `_NodeWebSocketReader`. A
      // bare microtask fires *before* the reader is constructed (the
      // open-await scheduled before us), so we use setTimeout to push
      // delivery to the next macrotask.
      setTimeout(() => {
        this.emit(
          "message",
          Buffer.from(
            JSON.stringify({ type: "hello", match_id: "m_test", seq: 1 }),
          ),
        );
        this.emit(
          "message",
          Buffer.from(
            JSON.stringify({ type: "match_end", match_id: "m_test", seq: 2 }),
          ),
        );
      }, 0);
    }

    on(event: string, cb: (...args: unknown[]) => void): this {
      (this.listeners[event] ??= []).push(cb);
      return this;
    }

    once(event: string, cb: (...args: unknown[]) => void): this {
      const wrapped = (...args: unknown[]): void => {
        this.removeListener(event, wrapped);
        cb(...args);
      };
      return this.on(event, wrapped);
    }

    removeListener(event: string, cb: (...args: unknown[]) => void): this {
      const arr = this.listeners[event];
      if (arr) {
        this.listeners[event] = arr.filter((l) => l !== cb);
      }
      return this;
    }

    private emit(event: string, ...args: unknown[]): void {
      for (const cb of this.listeners[event] ?? []) {
        cb(...args);
      }
    }

    send(_data: string): void {
      /* swallow sent frames */
    }

    close(): void {
      this.readyState = FakeWebSocket.CLOSED;
      this.emit("close");
    }
  }

  return { default: FakeWebSocket };
});

// Imports must come after vi.mock so the mock is in place.
import { Bot } from "../src/bot.js";
import { runBot, USER_AGENT } from "../src/client.js";
import { Action, type GameState } from "../src/models.js";

class NoopBot extends Bot {
  decide(_state: GameState): Action {
    return Action.fold();
  }
}

afterEach(() => {
  capturedConstructorCalls.length = 0;
});

describe("runBot WebSocket handshake", () => {
  it("passes the chipzen-sdk-javascript User-Agent header to the ws constructor", async () => {
    await runBot("ws://localhost:8001/ws/match/m_test/bot", new NoopBot(), {
      maxRetries: 0,
      token: "",
    });

    expect(capturedConstructorCalls).toHaveLength(1);
    const call = capturedConstructorCalls[0]!;
    expect(call.url).toBe("ws://localhost:8001/ws/match/m_test/bot");
    expect(call.options).toBeDefined();
    const headers = call.options?.headers as Record<string, string> | undefined;
    expect(headers).toBeDefined();
    expect(headers?.["User-Agent"]).toBe(USER_AGENT);
    expect(headers?.["User-Agent"]).toMatch(
      /^chipzen-sdk-javascript\/[0-9]+\.[0-9]+\.[0-9]+/,
    );
  });
});
