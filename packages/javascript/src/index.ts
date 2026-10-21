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
  parseTurnRequest,
  stateShapeOf,
  POKER_STATE_SHAPES,
  UnsupportedStateShapeError,
  type ActionHistoryEntry,
  type ActionKind,
  type Card,
  type Draw27ActionKind,
  type Draw27Phase,
  type GameState,
} from "./models.js";
export {
  BIGBADBOTS_STATE_SHAPE,
  DEFAULT_TIMEOUT_TIEBREAK,
  FIGHT_ACTIONS,
  GEARUP_ACTIONS,
  PHASES,
  RULESETS_URL,
  StateParseError,
  gameConfigFromMatchStart,
  isBigBadBotsState,
  parseBigBadBotsGameConfig,
  parseBigBadBotsTurnRequest,
  parseRuleSet,
  ruleSetFromGameConfig,
  ruleSetToWire,
  toWireState,
  withRules,
  type ArenaRules,
  type AuctionState,
  type BigBadBotsActionKind,
  type BigBadBotsGameConfig,
  type BigBadBotsPhase,
  type BigBadBotsState,
  type BigBadBotsTurnState,
  type Body,
  type BodyShopState,
  type DeadlineRules,
  type FightActionKind,
  type Fighter,
  type FightState,
  type FittingState,
  type ForfeitRules,
  type GearUpActionKind,
  type GearUpRules,
  type GearUpState,
  type Part,
  type RoundRules,
  type RuleSet,
  type Side,
  type SoldLot,
} from "./bigbadbots.js";
export {
  BigBadBotsBot,
  REPEATED_ON_MISS,
  fallbackAction,
  isAnswerShaped,
} from "./bigbadbots_bot.js";
export {
  publishedRuleSet,
  publishedRuleSetIds,
  ruleSetWarnings,
  type PublishedRuleSet,
} from "./bigbadbots_rules.js";
export {
  BIGBADBOTS_CALLS,
  BIGBADBOTS_VALIDATE_GAME_CONFIG,
  MAX_BOT_MESSAGE_BYTES,
  bigBadBotsCallOf,
  checkBigBadBotsAnswer,
  checkBigBadBotsTurnAction,
  loadBigBadBotsFixtures,
  runBigBadBotsChecks,
  type BigBadBotsAnswerCheck,
  type BigBadBotsCall,
  type BigBadBotsCheck,
  type BigBadBotsFixture,
  type RunBigBadBotsChecksOptions,
} from "./bigbadbots_validate.js";
export {
  runBot,
  BotDecisionError,
  BIGBADBOTS_SEND_MARGIN_MS,
  SUPPORTED_PROTOCOL_VERSIONS,
  type RunBotOptions,
} from "./client.js";
export {
  runExternalBot,
  botTokenSubprotocols,
  resolveGatewayUrl,
  BOT_TOKEN_SUBPROTOCOL,
  type RunExternalBotOptions,
  type MatchResult,
  type BotFactory,
} from "./external.js";
export {
  connectToChipzen,
  type ConnectionConfig,
  type ConnectToChipzenOptions,
  type EnvName,
  ENV_NAMES,
  ENV_VAR_NAME,
} from "./connect.js";
export {
  loadChipzenConfig,
  resolveToken,
  resolveUrl,
  ChipzenConfigError,
  CONFIG_FILENAME,
  SECTION_NAME,
  type ChipzenConfig,
} from "./config.js";
export {
  RetryPolicy,
  DEFAULT_RETRY_POLICY,
  type RetryPolicyOptions,
} from "./retry.js";
export { VERSION } from "./version.js";
export {
  scaffoldBot,
  type ScaffoldOptions,
} from "./scaffold.js";
export {
  validateBot,
  DEFAULT_MAX_UPLOAD_BYTES,
  DEFAULT_TIMEOUT_WARN_MS,
  PLATFORM_TIMEOUT_MS,
  VALIDATE_GAMES,
  type Severity,
  type ValidateGame,
  type ValidateOptions,
  type ValidationResult,
} from "./validate.js";
export {
  runConformanceChecks,
  type ConformanceCheck,
  type RunConformanceOptions,
} from "./conformance.js";
