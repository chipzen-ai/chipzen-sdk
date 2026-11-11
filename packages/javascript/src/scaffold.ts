/**
 * `chipzen-sdk init <name>` — scaffold a new Chipzen bot project.
 *
 * Writes a copy of the canonical JavaScript starter at
 * `packages/javascript/starters/javascript/`: `bot.js` with its `main()` and
 * entry block, the IP-protected `Dockerfile`, `.dockerignore`, `package.json`,
 * `README.md` and `.gitignore`. The only change is the project name in
 * `package.json`.
 *
 * The files are not duplicated in this module. The npm package ships the
 * starter folder (see `files` in package.json), and this module reads it at
 * run time, so `init` and the starter cannot drift apart again (issue #239:
 * the old inline templates pinned `@chipzen-ai/bot` at `^0.2.0`, which only
 * admits 0.2.x). The one exception is `.gitignore`, which npm never packs:
 * it lives in `STARTER_GITIGNORE` below, and a test keeps it equal to the
 * starter's file.
 */

import { promises as fs } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

export interface ScaffoldOptions {
  /** Where to create the new project. Defaults to cwd. */
  parentDir?: string;
}

/**
 * Every file `init` copies from the starter folder, in write order. A test
 * pins this list against the starter folder and against `files` in the SDK's
 * package.json, so a file added to one is not silently left out of the other.
 */
export const STARTER_FILES = [
  "bot.js",
  "package.json",
  "Dockerfile",
  ".dockerignore",
  "README.md",
] as const;

/** The starter's `.gitignore`, which npm leaves out of every package. */
export const STARTER_GITIGNORE = `node_modules/
*.log
.DS_Store
.env
.env.*
coverage/
dist/
# The upload tarball (docker save | gzip).
*.tar.gz
`;

/**
 * The starter folder. `src/scaffold.ts` (tests) and `dist/*.js` (the
 * published package) both sit one level below the package root, so the same
 * relative path works for both.
 */
export function starterDir(): string {
  // The CJS build gets import.meta.url from tsup's shims (tsup.config.ts).
  const here = path.dirname(fileURLToPath(import.meta.url));
  return path.resolve(here, "..", "starters", "javascript");
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

  // Read everything before creating the folder, so a broken install leaves
  // nothing half-written behind.
  const source = starterDir();
  const contents = new Map<string, Buffer>();
  for (const file of STARTER_FILES) {
    try {
      contents.set(file, await fs.readFile(path.join(source, file)));
    } catch {
      throw new Error(
        `The JavaScript starter is missing from this @chipzen-ai/bot install ` +
          `(no ${path.join(source, file)}). Reinstall the package.`,
      );
    }
  }

  await fs.mkdir(projectDir, { recursive: true });
  for (const [file, data] of contents) {
    const out = file === "package.json" ? withProjectName(data.toString("utf-8"), name) : data;
    // Bytes, not text: the copy is exact, line endings included.
    await fs.writeFile(path.join(projectDir, file), out);
  }
  await fs.writeFile(path.join(projectDir, ".gitignore"), STARTER_GITIGNORE, "utf-8");

  return projectDir;
}

/**
 * The starter's package.json with `name` set to the project's. Only the name
 * line changes, so the SDK pin and everything else stay exactly the starter's.
 */
export function withProjectName(packageJson: string, name: string): string {
  const pattern = /("name"\s*:\s*)"[^"]*"/;
  if (!pattern.test(packageJson)) {
    throw new Error("The starter's package.json has no \"name\" field");
  }
  return packageJson.replace(pattern, `$1${JSON.stringify(name)}`);
}

function isValidProjectName(name: string): boolean {
  // Same rule the Python scaffold uses — keep cross-language consistency.
  return /^[A-Za-z0-9_-]+$/.test(name);
}
