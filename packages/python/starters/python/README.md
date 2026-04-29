# Python starter — IP-protected

A minimal Chipzen Python bot project, ready to copy + modify.

The included Dockerfile ships your strategy as a **compiled `.so`
module**, not as readable `.py` source. See
[`../../IP-PROTECTION.md`](../../IP-PROTECTION.md) for what this
protects (and what it doesn't).

## Files

| File | Purpose |
|---|---|
| [`bot.py`](bot.py) | Your bot. Subclass `Bot`, implement `decide()`. |
| [`requirements.txt`](requirements.txt) | Pins the SDK version. Add your own deps here. |
| [`Dockerfile`](Dockerfile) | Multi-stage Cython build. The runtime image contains no `.py` source for the bot. |
| [`.dockerignore`](.dockerignore) | Keeps the build context tight (no caches, virtualenvs, secrets, etc.). |

## Try it

Copy this directory somewhere outside the SDK repo, then:

```bash
# 1. Edit bot.py — replace decide() with your strategy.
$EDITOR bot.py

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
~25 MB compressed.

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
