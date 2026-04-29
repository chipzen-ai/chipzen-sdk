/**
 * Public API for the `@chipzen-ai/bot` package.
 *
 * See https://github.com/chipzen-ai/chipzen-sdk for the full developer
 * docs (DEV-MANUAL, protocol spec, SECURITY policy / bot runtime model,
 * IP-protected starter Dockerfile).
 */

export { Bot } from "./bot.js";
export {
  Action,
  cardFromString,
  cardToString,
  parseGameState,
  type ActionHistoryEntry,
  type ActionKind,
  type Card,
  type GameState,
} from "./models.js";
export {
  runBot,
  SUPPORTED_PROTOCOL_VERSIONS,
  type RunBotOptions,
} from "./client.js";
export {
  scaffoldBot,
  type ScaffoldOptions,
} from "./scaffold.js";
export {
  validateBot,
  DEFAULT_MAX_UPLOAD_BYTES,
  DEFAULT_TIMEOUT_WARN_MS,
  PLATFORM_TIMEOUT_MS,
  type Severity,
  type ValidateOptions,
  type ValidationResult,
} from "./validate.js";
export {
  runConformanceChecks,
  type ConformanceCheck,
  type RunConformanceOptions,
} from "./conformance.js";
