import { defineConfig } from "tsup";

export default defineConfig({
  entry: ["src/index.ts"],
  format: ["esm", "cjs"],
  dts: true,
  sourcemap: true,
  clean: true,
  // Keep `ws` external — it's a peer-dep that the consumer's package
  // manager will resolve. Bundling it would balloon the wheel and
  // shadow any version pin the consumer applied themselves.
  external: ["ws"],
  target: "node20",
  outDir: "dist",
});
