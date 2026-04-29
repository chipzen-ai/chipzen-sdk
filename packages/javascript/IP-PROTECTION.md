# IP protection — what shipping a Bun-compiled bot does and doesn't do

The JavaScript starter at
[`starters/javascript/`](starters/javascript/) ships a multi-stage
Dockerfile that compiles `bot.js` (and the SDK + the Bun runtime) into
a single statically-linked binary in a builder stage, then copies only
that binary into the runtime image.

This is the **alpha-tier IP-protection recipe**. Anything stronger is
on the future-hardening list (see the bottom of this file).

## What this protects against

- **Casual source disclosure.** Anyone who somehow obtains read access
  to your image (e.g., a misconfigured backup, a leaked tarball) can
  no longer `cat bot.js` to see your strategy. The `.js` source is
  discarded in the builder stage and never enters the runtime image.
- **Direct copy-paste forks.** A compromised image can't be
  re-uploaded as-is by a different account because the published
  contents are obfuscated enough that the next reviewer would ask
  questions. Not a hard guarantee, but a friction layer.
- **Trivial introspection from inside a running container.** Even if
  an attacker gains code execution inside your bot's container during
  a match, they can't read your strategy directly — they'd be poking
  at the JavaScriptCore bytecode bundled inside the binary, not at
  source.

## What this does NOT protect against

- **Determined reverse engineering.** Bun's `--compile` output is a
  single binary that embeds your bundled JS as a self-extracting blob
  (zlib-compressed, parsed at startup). Tools exist that can recover
  the bundled JS from a Bun executable in a few minutes. Treat this as
  "raises the cost", not "makes it impossible". Minification + no
  source-maps reduces readability of what they recover but does not
  prevent recovery.
- **The Chipzen platform owner.** The platform stores your uploaded
  image in its own infrastructure. The platform owner can technically
  inspect what you uploaded. This SDK assumes you trust the platform
  with your image; if you don't, this Dockerfile won't help you.
- **Side-channel leakage from observed gameplay.** Opponents who play
  against your bot at the table will infer aspects of your strategy
  from its actions, regardless of how the source was packaged. Bet
  sizing, timing, action distributions, and showdown information are
  all visible to opponents and the platform.
- **The `package.json` you ship.** Your dependency list lives in the
  builder stage only — `bun install --production` is run there and
  the bundled binary contains the resolved deps. The runtime image
  does NOT carry `package.json`. If you need to verify this, run
  `docker run --entrypoint sh my-bot:v1 -c "ls /bot/"` and confirm
  only `bot` (the compiled binary) is present.

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
adversary with disassembler". Bun-compiled binaries handle the former.

## How the recipe works (step by step)

```
Stage 1 (builder):
  - oven/bun:1-debian
  - COPY package.json bot.js
  - bun install --production    # resolves @chipzen-ai/bot + ws
  - bun build --compile --minify --sourcemap=none \
      --target=bun-linux-x64 bot.js --outfile=/build/bot
  - rm bot.js                    # remove source so stage 2 cannot copy it
  - rm -rf node_modules          # ditto for resolved deps

Stage 2 (runtime):
  - debian:12-slim
  - apt-get install ca-certificates dumb-init
  - COPY --from=builder /build/bot /bot/bot
  - non-root user (uid 10001)
  - ENTRYPOINT ["dumb-init", "/bot/bot"]
```

Bun's `--compile` flag bundles every module the entry point reaches
(via static analysis of `import` statements) into a single zlib blob,
prepends the Bun runtime, and emits a statically-linked ELF. The
runtime image needs glibc + ca-certs + nothing else — that's why it's
~50 MB compressed instead of ~150 MB.

## Build-time / runtime ABI compatibility

Bun's `--compile --target=bun-linux-x64` output is glibc-linked. The
builder and runtime stages must use compatible glibc versions. This
Dockerfile pins both stages to Debian 12 (bookworm) so they line up. If
you switch to alpine (musl), the binary will fail with `not found` on
`ld-linux-x86-64.so.2` because Alpine doesn't ship a glibc loader.

If you want to ship for a different architecture (e.g., arm64), pass
`--target=bun-linux-arm64` to `bun build --compile` and switch the
runtime stage's base image to an arm64 variant of debian:12-slim. Use
Docker Buildx with `--platform=linux/amd64,linux/arm64` to produce a
multi-arch image.

## Future hardening (not in alpha)

The implementation plan calls these out as follow-ups, not blockers:

- **Native code emission** — embed the hot path of your strategy
  (e.g., card-eval inner loops) in a small Rust crate compiled to a
  WASM module or a native `.so`. Bun loads both, and decompilation of
  the native portion is materially harder than decompiling JS.
- **Encrypted-at-rest module loading** — the bot decrypts the bundled
  bytecode at runtime using a per-match key the platform injects at
  launch. Adds startup latency and requires platform-side coordination.
- **Custom Bun build with patched compile path** — modify the bundle
  format so off-the-shelf Bun-binary-extraction tools no longer work
  out of the box. Raises the floor against script-kiddie reversers
  but doesn't stop a determined one.
- **Strip the binary** — `strip --strip-all` on the compiled output
  to remove debug symbols. Easy to add, modest effect.
