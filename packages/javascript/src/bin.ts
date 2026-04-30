/**
 * Bin shim for the `chipzen-sdk` CLI.
 *
 * Lives separately from cli.ts so the shebang the tsup banner adds at
 * build time doesn't end up in the library entry point that other
 * packages might import.
 */

import { main } from "./cli.js";

main().catch((err) => {
  console.error(err instanceof Error ? err.stack ?? err.message : String(err));
  process.exit(1);
});
