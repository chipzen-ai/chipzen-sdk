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
  await fs.writeFile(path.join(projectDir, "Dockerfile"), DOCKERFILE_PLACEHOLDER, "utf-8");
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

// Top-level await is fine in ESM — keep main() exported so the
// IP-protected Dockerfile (Phase 2 PR 3) can also call it from a
// compiled binary entry point.
if (import.meta.url === \`file://\${process.argv[1]}\`) {
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

const DOCKERFILE_PLACEHOLDER = `# Replace this with the IP-protected starter Dockerfile from
# packages/javascript/starters/javascript/Dockerfile (ships in
# Phase 2 PR 3 — bun build --compile multi-stage that emits a single
# binary with no readable .js source for your strategy).
#
# For now, a minimal Node-based development image:

FROM node:20-slim
WORKDIR /bot
COPY package.json ./
RUN npm install --production
COPY bot.js ./
USER node
ENTRYPOINT ["node", "bot.js"]
`;

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
