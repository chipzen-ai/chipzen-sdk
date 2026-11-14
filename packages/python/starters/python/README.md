# Python starter (IP-protected)

A minimal Chipzen poker bot project, ready to modify.

Get a copy with `chipzen-sdk init my-bot` (after `pip install chipzen-bot`),
or copy this folder out of the SDK repo. Both give you the same files:
`init` writes exactly this starter.

The included Dockerfile ships your strategy as a **compiled `.so`
module**, not as readable `.py` source. See
[IP-PROTECTION.md](https://github.com/chipzen-ai/chipzen-sdk/blob/main/packages/python/IP-PROTECTION.md)
for what this protects (and what it doesn't).

## Files

| File | Purpose |
|---|---|
| `bot.py` | Your bot. Subclass `Bot`, implement `decide()`. Keep `main()` at the bottom: the image starts the bot by calling it. |
| `requirements.txt` | Pins the SDK version. Add your own deps here. |
| `Dockerfile` | Multi-stage Cython build. The runtime image contains no `.py` source for the bot. |
| `.dockerignore` | Keeps the build context tight (no caches, virtualenvs, secrets, etc.). |
| `.gitignore` | Keeps caches and your external-API token (`chipzen.toml`) out of git. |

## Try it

```bash
# 1. Edit bot.py: replace decide() with your strategy.
$EDITOR bot.py

# 2. Validate before packaging.
chipzen-sdk validate .

# 3. Build the image (on an Apple Silicon Mac, add --platform linux/amd64).
docker build -t my-bot:v1 .

# 4. Export the upload tarball (on Windows, use Git Bash or WSL, not PowerShell).
docker save my-bot:v1 | gzip > my-bot.tar.gz
```

5. Upload `my-bot.tar.gz` through the Chipzen web UI. The platform runs a
   short automatic review (a scripted mini-match), and the bot becomes
   active when it passes.

`chipzen-sdk validate .` checks the folder the way the upload pipeline
does, including that there is a Dockerfile and that `bot.py` still has the
`main()` the image starts. Add `--check-connectivity` to also play a
scripted hand against an in-process mock server.

Hard upload cap: **250 MB compressed** (the built image is separately
capped at **200 MB**); the upload endpoint rejects anything larger.
Smaller images cold-start faster. The starter image (no extra deps beyond
the SDK) typically lands at ~25 MB compressed.

**Decision time:** an uploaded bot has **5 seconds** per decision, end to
end. If `decide()` is slower, the server plays a safe default for you.
`match_start.turn_timeout_ms` tells your bot the clock for each match.
`chipzen-sdk validate` is stricter on purpose: it times one `decide()` call
on a canned state and fails it above 500 ms.

## Verifying the IP-protection worked

After step 3, you can confirm your `.py` source isn't in the image:

```bash
docker run --rm --entrypoint sh my-bot:v1 -c "ls /bot/"
# Expect: bot.cpython-312-<arch>-linux-gnu.so  (the compiled module)
#         requirements.txt                      (the pinned dep list)
# Should NOT see: bot.py
```

If you see `bot.py` in the listing, the `RUN cythonize -i bot.py && rm bot.py`
step failed silently. Re-build with `--progress=plain` to see why.

## Next

- The full `GameState` fields, `Action` builders and lifecycle hooks:
  [DEV-MANUAL.md](https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/DEV-MANUAL.md).
- Rather not use Docker? The same bot class can run on your own machine
  with `chipzen run-external`:
  [First 30 minutes with the external API](https://github.com/chipzen-ai/chipzen-sdk/blob/main/docs/external-api/FIRST-30-MINUTES.md).
