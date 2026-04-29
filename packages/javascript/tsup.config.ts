import { defineConfig } from "tsup";

export default defineConfig([
  // Library entry — dual ESM + CJS + dual .d.ts.
  {
    entry: { index: "src/index.ts" },
    format: ["esm", "cjs"],
    dts: true,
    sourcemap: true,
    clean: true,
    // Keep `ws` external — it's a runtime dep the consumer resolves.
    external: ["ws"],
    target: "node20",
    outDir: "dist",
  },
  // CLI bin entry — ESM only, with the shebang preserved by tsup.
  // No .d.ts needed (it's an executable, not a library import).
  {
    entry: { bin: "src/bin.ts" },
    format: ["esm"],
    dts: false,
    sourcemap: true,
    clean: false, // already cleaned by the library entry above
    external: ["ws"],
    target: "node20",
    outDir: "dist",
    banner: { js: "#!/usr/bin/env node" },
  },
]);
