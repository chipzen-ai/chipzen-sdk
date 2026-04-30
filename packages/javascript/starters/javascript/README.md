# JavaScript starter — IP-protected

A minimal Chipzen JavaScript bot project, ready to copy + modify.

The included Dockerfile ships your strategy as a **single compiled
binary** (via `bun build --compile`), not as readable `.js` source.
See [`../../IP-PROTECTION.md`](../../IP-PROTECTION.md) for what this
protects (and what it doesn't).

## Files

| File | Purpose |
|---|---|
| [`bot.js`](bot.js) | Your bot. Subclass `Bot`, implement `decide()`. |
| [`package.json`](package.json) | Pins the SDK version. Add your own deps here. |
| [`Dockerfile`](Dockerfile) | Multi-stage Bun build. The runtime image contains no `.js` source for the bot. |
| [`.dockerignore`](.dockerignore) | Keeps the build context tight (no node_modules, caches, secrets, etc.). |

## Try it

Copy this directory somewhere outside the SDK repo, then:

```bash
# 1. Edit bot.js — replace decide() with your strategy.
$EDITOR bot.js

# 2. Validate before packaging.
chipzen-sdk validate .

# 3. Build the image.
docker build -t my-bot:v1 .

# 4. Export the upload tarball.
docker save my-bot:v1 | gzip > my-bot.tar.gz

# 5. Upload via the Chipzen platform UI.
```

Recommended max upload size: **300 MB compressed**. Hard cap: **500 MB**.
The starter image (no extra deps beyond the SDK) typically lands at
~50 MB compressed — most of that is the Bun runtime statically linked
into the binary.

## Verifying the IP-protection worked

After step 3, you can confirm your `.js` source isn't in the image:

```bash
docker run --rm --entrypoint sh my-bot:v1 -c "ls /bot/"
# Expect: bot         (the compiled binary — strategy bundled inside)
# Should NOT see: bot.js, package.json, node_modules
```

If you see `bot.js` in the listing, the `RUN bun build --compile && rm bot.js`
step failed silently. Re-build with `--progress=plain` to see why.

## Running the binary directly (for local debugging)

The compiled binary is a self-contained executable on linux-x64. You
can run it outside Docker if your host platform matches:

```bash
docker create --name extract my-bot:v1
docker cp extract:/bot/bot ./bot-binary
docker rm extract

# Run it (Linux x64 host or via Docker on macOS/Windows).
CHIPZEN_WS_URL=ws://localhost:8001/ws/match/m_test/bot ./bot-binary
```

For cross-architecture builds (e.g., arm64 for Apple Silicon dev), pass
`--target=bun-linux-arm64` to the `bun build --compile` invocation in
the Dockerfile and update the runtime base image to match.
