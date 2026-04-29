/**
 * `chipzen-sdk init <name>` — scaffold a new Chipzen bot project.
 *
 * Mirrors the Python `chipzen.scaffold` shape so cross-language
 * developers see the same outputs. The scaffolded project is plain
 * ESM JavaScript (no TypeScript dep required to run); convert to TS
 * if you want by renaming bot.js -> bot.ts and adding a tsconfig.
 */

import { promises as fs } from "node:fs";
import path from "node:path";

export interface ScaffoldOptions {
  /** Where to create the new project. Defaults to cwd. */
  parentDir?: string;
}

export async function scaffoldBot(name: string, options: ScaffoldOptions = {}): Promise<string> {
  if (!isValidProjectName(name)) {
    throw new Error(
      `Invalid project name: ${JSON.stringify(name)}. ` +
        "Use ASCII letters, digits, underscores, and dashes only.",
    );
  }
  const parent = options.parentDir ?? process.cwd();
  const projectDir = path.join(parent, name);

  // Avoid clobbering an existing directory.
  try {
    await fs.access(projectDir);
    throw new Error(`Directory already exists: ${projectDir}`);
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code !== "ENOENT") {
      throw err;
    }
  }

  await fs.mkdir(projectDir, { recursive: true });

  await fs.writeFile(path.join(projectDir, "bot.js"), BOT_TEMPLATE, "utf-8");
  await fs.writeFile(path.join(projectDir, "package.json"), packageJsonTemplate(name), "utf-8");
  await fs.writeFile(path.join(projectDir, "Dockerfile"), DOCKERFILE_TEMPLATE, "utf-8");
  await fs.writeFile(path.join(projectDir, ".dockerignore"), DOCKERIGNORE, "utf-8");
  await fs.writeFile(path.join(projectDir, ".gitignore"), GITIGNORE, "utf-8");
  await fs.writeFile(path.join(projectDir, "README.md"), readmeTemplate(name), "utf-8");

  return projectDir;
}

function isValidProjectName(name: string): boolean {
  // Same rule the Python scaffold uses — keep cross-language consistency.
  return /^[A-Za-z0-9_-]+$/.test(name);
}

const BOT_TEMPLATE = `// Chipzen starter bot — replace decide() with your strategy.
// The SDK handles WebSocket, handshake, ping/pong, retries, and reconnect.

import { Bot, Action, runBot } from "@chipzen-ai/bot";

class MyBot extends Bot {
  decide(state) {
    // Return one of: Action.fold(), Action.check(), Action.call(),
    // Action.raiseTo(amount), Action.allIn(). The chosen action's
    // wire-form must be in state.validActions; raises must satisfy
    // state.minRaise <= amount <= state.maxRaise.
    if (state.validActions.includes("check")) return Action.check();
    return Action.fold();
  }
}

export async function main() {
  const url = process.env.CHIPZEN_WS_URL ?? process.argv[2];
  if (!url) {
    console.error("error: CHIPZEN_WS_URL not set and no URL passed on the command line");
    process.exit(1);
  }
  await runBot(url, new MyBot(), {
    token: process.env.CHIPZEN_TOKEN ?? null,
    ticket: process.env.CHIPZEN_TICKET ?? null,
  });
}

// Run main() when this file is the entry point — covers both
// \`node bot.js\` (Node sets import.meta.url to a file:// URL matching
// argv[1]) and \`bun build --compile\` binaries (Bun sets
// import.meta.main on the entry module). Importing from a test file
// makes both checks false so MyBot can be exercised in isolation.
if (import.meta.main || import.meta.url === \`file://\${process.argv[1]}\`) {
  await main();
}

export { MyBot };
`;

function packageJsonTemplate(name: string): string {
  return JSON.stringify(
    {
      name,
      version: "0.1.0",
      private: true,
      type: "module",
      main: "bot.js",
      scripts: {
        start: "node bot.js",
      },
      dependencies: {
        "@chipzen-ai/bot": "^0.2.0",
      },
      engines: {
        node: ">=20",
      },
    },
    null,
    2,
  ) + "\n";
}

// Kept identical to packages/javascript/starters/javascript/Dockerfile so a
// scaffolded project and the canonical starter directory ship the same recipe.
// A test enforces this invariant.
const DOCKERFILE_TEMPLATE = `# syntax=docker/dockerfile:1.7
#
# IP-protected Chipzen JavaScript bot image.
#
# Multi-stage build that bundles bot.js + the SDK into a single
# statically-linked binary via \`bun build --compile\` in the builder
# stage, then ships only that binary in the runtime stage. The runtime
# image contains no readable .js source for your strategy code.
#
# See ../../IP-PROTECTION.md for what this protects (and what it doesn't).
#
# Build:   docker build -t my-bot:test .
# Export:  docker save my-bot:test | gzip > my-bot.tar.gz
#
# Build context for this directory should be small (bot.js +
# package.json + this file). The .dockerignore alongside this file
# keeps node_modules, caches, and lockfile metadata out.

# -----------------------------------------------------------------------------
# Stage 1: Bun bundle + compile. The .js source lives only in this stage and
# is discarded before the runtime stage starts.
# -----------------------------------------------------------------------------
# Base pinned by tag — Dependabot can rotate to digest pinning later. Tag:
# oven/bun:1-debian (glibc-based; the runtime stage below also uses glibc
# so the compiled binary's dynamic linker can find what it needs).
FROM oven/bun:1-debian AS builder

WORKDIR /build

# Bring in the bot source + dependency manifest. Only these are copied —
# keep the build context narrow so the .dockerignore is the only allowlist.
COPY package.json bot.js ./

# Resolve @chipzen-ai/bot + ws so \`bun build\` can bundle them. We don't
# need a lockfile committed; the registry pin in package.json is enough
# for a fresh install at build time.
RUN bun install --production

# Compile bot.js -> /build/bot. \`--compile\` emits a single executable
# that bundles the JS, all deps, and the Bun runtime statically.
# \`--minify\` shrinks output; \`--sourcemap=none\` excludes maps so the
# strategy isn't trivially readable from inside the binary.
RUN bun build --compile --minify --sourcemap=none --target=bun-linux-x64 \\
        bot.js --outfile=/build/bot \\
    && rm bot.js \\
    && rm -rf node_modules

# -----------------------------------------------------------------------------
# Stage 2: Runtime. Only the compiled binary + ENTRYPOINT.
# No .js source for the bot's strategy is present.
# -----------------------------------------------------------------------------
# Base pinned by tag — Dependabot can rotate to digest pinning later. Tag:
# debian:12-slim (matches the glibc the Bun --compile output expects).
FROM debian:12-slim

# CA certs are needed for outbound TLS (the platform's WebSocket
# endpoint is wss://). dumb-init reaps any subprocesses cleanly on
# container exit; tiny but useful.
RUN apt-get update \\
    && apt-get install -y --no-install-recommends ca-certificates dumb-init \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /bot

# Copy ONLY the compiled binary from the builder stage.
COPY --from=builder /build/bot /bot/bot
RUN chmod +x /bot/bot

# Run as non-root (defense in depth — the platform also applies seccomp
# and cap-drop on top of this).
RUN groupadd --system --gid 10001 bot \\
    && useradd --system --uid 10001 --gid bot --home-dir /bot --shell /usr/sbin/nologin bot \\
    && chown -R bot:bot /bot
USER 10001

ENTRYPOINT ["dumb-init", "/bot/bot"]
`;
export const _DOCKERFILE_TEMPLATE_FOR_TEST = DOCKERFILE_TEMPLATE;

const DOCKERIGNORE = `node_modules/
.git/
.gitignore
.env
.env.*
*.md
README*
LICENSE*
*.log
coverage/
.DS_Store
`;

const GITIGNORE = `node_modules/
*.log
.DS_Store
.env
.env.*
coverage/
dist/
`;

function readmeTemplate(name: string): string {
  return `# ${name}

A poker bot for the [Chipzen](https://chipzen.ai) platform.

## Quick start

\`\`\`bash
npm install
\`\`\`

Edit \`bot.js\` to implement your strategy in the \`decide()\` method.

Validate before uploading:

\`\`\`bash
chipzen-sdk validate .
\`\`\`

Build and export the upload tarball:

\`\`\`bash
docker build -t ${name}:v1 .
docker save ${name}:v1 | gzip > ${name}.tar.gz
\`\`\`

Then upload via the Chipzen platform UI.
`;
}
