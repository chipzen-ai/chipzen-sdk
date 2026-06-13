/**
 * Single source of truth for the SDK version string.
 *
 * Reads `version` from the package's own `package.json` so the value the
 * handshake reports always tracks the published package (chipzen-ai/chipzen-sdk#41)
 * — no hardcoded literal to drift out of sync on a release bump.
 *
 * The import is resolved against `../package.json` (one level up from
 * `src/`). `tsup` inlines the JSON at build time, so the bundled output
 * carries the literal value with no runtime filesystem read.
 */

// eslint-disable-next-line @typescript-eslint/consistent-type-imports
import pkg from "../package.json" with { type: "json" };

/** The installed SDK version, e.g. `"0.3.0"`. */
export const VERSION: string = (pkg as { version: string }).version;
