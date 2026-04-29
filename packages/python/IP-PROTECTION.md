# IP protection — what shipping a Cython-compiled bot does and doesn't do

The Python starter at
[`starters/python/`](starters/python/) ships a multi-stage Dockerfile
that compiles `bot.py` to a Cython `.so` module in a builder stage,
then copies only that compiled binary into the runtime image.

This is the **alpha-tier IP-protection recipe**. Anything stronger is
on the future-hardening list (see the bottom of this file).

## What this protects against

- **Casual source disclosure.** Anyone who somehow obtains read access
  to your image (e.g., a misconfigured backup, a leaked tarball) can
  no longer `cat bot.py` to see your strategy. The `.py` source is
  discarded in the builder stage and never enters the runtime image.
- **Direct copy-paste forks.** A compromised image can't be
  re-uploaded as-is by a different account because the published
  contents are obfuscated enough that the next reviewer would ask
  questions. Not a hard guarantee, but a friction layer.
- **Trivial introspection from inside a running container.** Even if
  an attacker gains code execution inside your bot's container during
  a match, they can't read your strategy directly — they'd be reading
  C-extension memory, not Python source.

## What this does NOT protect against

- **Determined reverse engineering.** Cython compiles Python to C and
  then to a `.so`. Decompilation is harder than reading Python source
  but absolutely possible with disassemblers (Ghidra, Hex-Rays). A
  motivated attacker with infinite time can recover your algorithm.
  Treat this as "raises the cost", not "makes it impossible".
- **The Chipzen platform owner.** The platform stores your uploaded
  image in its own infrastructure. The platform owner can technically
  inspect what you uploaded. This SDK assumes you trust the platform
  with your image; if you don't, this Dockerfile won't help you.
- **Side-channel leakage from observed gameplay.** Opponents who play
  against your bot at the table will infer aspects of your strategy
  from its actions, regardless of how the source was packaged. Bet
  sizing, timing, action distributions, and showdown information are
  all visible to opponents and the platform.
- **The `requirements.txt` you ship.** Your dependency list is shipped
  in the runtime image as plain text. If you depend on a unique
  combination of solver / model libraries, the requirements.txt is a
  meaningful tell. Strip it from the runtime image (after `pip install`
  succeeds in the builder stage) if this matters to you.

## Why this is sufficient for alpha

The Chipzen platform's posture (see
[`../../SECURITY.md`](../../SECURITY.md#bot-runtime--what-the-platform-enforces-on-uploaded-bots)
for the full version):

- Uploaded bot images go directly to platform-controlled storage,
  encrypted at rest.
- No public access to bot images.
- Bot containers run with strict network egress controls — your
  competitors can't pull your image while a match is running.

Combined, the practical threat model for an alpha bot author is
"casual access via an unexpected leak" rather than "well-resourced
adversary with disassembler". Cython compilation handles the former.

## How the recipe works (step by step)

```
Stage 1 (builder):
  - python:3.12-slim @ <pinned digest>
  - pip install cython==3.0.* setuptools
  - COPY bot.py
  - cythonize -i bot.py        # produces bot.cpython-312-<arch>-linux-gnu.so
  - rm bot.py                  # remove the source so stage 2 cannot copy it

Stage 2 (runtime):
  - python:3.12-slim @ <same pinned digest>
  - PYTHONUNBUFFERED=1, no .pyc emission, no pip cache
  - pip install -r requirements.txt
  - strip /usr/local/lib/python3.12/**/__pycache__ and **/tests
  - COPY --from=builder /build/*.so /bot/
  - non-root user (uid 10001)
  - ENTRYPOINT ["python", "-c", "from bot import main; main()"]
```

The `from bot import main` form works because Python's standard
import system finds `bot.cpython-312-<arch>-linux-gnu.so` and loads
it as the module named `bot`. The `main()` callable inside the
compiled module is whatever you defined in your `bot.py` before
compilation.

## Build-time / runtime Python ABI compatibility

Cython output is ABI-specific. The builder and runtime stages must
use the **same Python version + platform**. This Dockerfile pins
both stages to identical `python:3.12-slim` digests so the ABI line
up. If you change one, change both.

If you want to ship for multiple Python versions or architectures
(e.g., arm64 for Apple Silicon devs running locally), use Docker
Buildx with `--platform=linux/amd64,linux/arm64` and a Dockerfile
template that parameterizes the digest per platform.

## Future hardening (not in alpha)

The implementation plan calls these out as follow-ups, not blockers:

- **Nuitka** instead of Cython for stronger optimization + harder
  reverse engineering. Larger binaries, longer build times.
- **PyArmor** obfuscation as a layer on top of Cython for
  resistance against the easy decompilation paths.
- **Encrypted-at-rest module loading** — the bot decrypts itself at
  runtime using a per-match key the platform injects at launch. Adds
  startup latency and requires platform-side coordination.
- **ELF binary stripping** of the Cython output (`strip --strip-all`
  on the `.so`) to remove debug symbols and harden against quick
  symbol-table inspection. Easy to add when the platform supports it.
