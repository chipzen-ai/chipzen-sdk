# JavaScript starter (IP-protected)

A minimal Chipzen JavaScript poker bot project, ready to modify.

Get a copy with `npx -p @chipzen-ai/bot chipzen-sdk init my-bot`, or copy
this folder out of the SDK repo. Both give you the same files: `init`
writes exactly this starter (it only sets the project name in
`package.json`).

The included Dockerfile ships your strategy as a **single compiled
binary** (via `bun build --compile`), not as readable `.js` source.
See [IP-PROTECTION.md](https://github.com/chipzen-ai/chipzen-sdk/blob/main/packages/javascript/IP-PROTECTION.md)
for what this protects (and what it doesn't).

## Files

| File | Purpose |
|---|---|
| `bot.js` | Your bot. Subclass `Bot`, implement `decide()`. Keep `main()` and the `if (import.meta.main ...)` block at the bottom: the compiled image starts the bot through them. |
| `package.json` | Pins the SDK version. Add your own deps here. |
| `Dockerfile` | Multi-stage Bun build. The runtime image contains no `.js` source for the bot. |
| `.dockerignore` | Keeps the build context tight (no node_modules, caches, secrets, etc.). |
| `.gitignore` | Keeps node_modules, logs and `.env` files out of git. |

## Try it

```bash
# 1. Install the SDK (this also gives you the chipzen-sdk command).
npm install

# 2. Edit bot.js: replace decide() with your strategy.
$EDITOR bot.js

# 3. Validate before packaging.
npx chipzen-sdk validate .

# 4. Build the image (on an Apple Silicon Mac, add --platform linux/amd64).
docker build -t my-bot:v1 .

# 5. Export the upload tarball (on Windows, use Git Bash or WSL, not PowerShell).
docker save my-bot:v1 | gzip > my-bot.tar.gz
```

6. Upload `my-bot.tar.gz` through the Chipzen web UI. The platform runs a
   short automatic review (a scripted mini-match), and the bot becomes
   active when it passes.

`chipzen-sdk validate .` checks the folder the way the upload pipeline
does, including that there is a Dockerfile and that `bot.js` still calls
the `main()` the image runs. Add `--check-connectivity` to also play a
scripted hand against an in-process mock server.

Hard upload cap: **250 MB compressed** (the built image is separately
capped at **200 MB**); the upload endpoint rejects anything larger.
Smaller images cold-start faster. The starter image (no extra deps beyond
the SDK) typically lands at ~50 MB compressed, most of it the Bun runtime
statically linked into the binary.

**Decision time:** an uploaded bot has **5 seconds** per decision, end to
end. If `decide()` is slower, the server plays a safe default for you.
`match_start.turn_timeout_ms` tells your bot the clock for each match.
`chipzen-sdk validate` is stricter on purpose: it times one `decide()` call
on a canned state and fails it above 500 ms.

## Verifying the IP-protection worked

After step 4, you can confirm your `.js` source isn't in the image:

```bash
docker run --rm --entrypoint sh my-bot:v1 -c "ls /bot/"
# Expect: bot         (the compiled binary, strategy bundled inside)
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

## Next

The full `GameState` fields, `Action` builders and lifecycle hooks:
[DEV-MANUAL.md](https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/DEV-MANUAL.md).
