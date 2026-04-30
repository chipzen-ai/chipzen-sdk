# IP protection — what shipping a Rust release binary does and doesn't do

The Rust starter at [`starters/rust/`](starters/rust/) ships a multi-
stage Dockerfile that compiles your `lib.rs` + `main.rs` into a
single statically-linked release binary in a builder stage, then copies
only that binary into the runtime image.

This is the **alpha-tier IP-protection recipe**. Anything stronger is
on the future-hardening list (see the bottom of this file).

## What this protects against

- **Casual source disclosure.** Anyone who somehow obtains read access
  to your image (e.g., a misconfigured backup, a leaked tarball) can
  no longer `cat src/lib.rs` to see your strategy. The `.rs` source is
  discarded in the builder stage and never enters the runtime image.
- **Direct copy-paste forks.** A compromised image can't be re-uploaded
  as-is by a different account because the published contents are
  obfuscated enough that the next reviewer would ask questions. Not a
  hard guarantee, but a friction layer.
- **Trivial introspection from inside a running container.** Even if
  an attacker gains code execution inside your bot's container during
  a match, they can't read your strategy directly — they'd be poking
  at machine code, not source.

## What this does NOT protect against

- **Determined reverse engineering.** Rust compiles to native machine
  code. The release profile in the starter does `strip = "symbols"`
  and `lto = "thin"` which removes function names and debug info, but
  a motivated attacker with a disassembler (Ghidra, IDA Pro) can
  recover the control flow given enough time. Treat this as "raises
  the cost", not "makes it impossible". Rust's monomorphization and
  zero-cost abstractions inflate the binary in ways that make
  decompilation harder than equivalent C, but not impossible.
- **The Chipzen platform owner.** The platform stores your uploaded
  image in its own infrastructure. The platform owner can technically
  inspect what you uploaded. This SDK assumes you trust the platform
  with your image; if you don't, this Dockerfile won't help you.
- **Side-channel leakage from observed gameplay.** Opponents who play
  against your bot at the table will infer aspects of your strategy
  from its actions, regardless of how the source was packaged. Bet
  sizing, timing, action distributions, and showdown information are
  all visible to opponents and the platform.
- **Symbol leakage from function names.** Even with `strip = "symbols"`,
  certain things (panic messages with file paths, error types via
  `#[derive(Debug)]`, public API symbols if you make anything `pub`)
  can leak strategy intent. Use `cargo bloat` and `nm` on your binary
  to audit. The starter sets `strip = "symbols"` which removes most
  of this — verify with `nm /bot/bot` showing minimal output.

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
adversary with disassembler". A stripped Rust release binary handles
the former cleanly.

## How the recipe works (step by step)

```
Stage 1 (builder):
  - rust:1-slim
  - apt-get install pkg-config libssl-dev   # for tokio-tungstenite native-tls
  - COPY Cargo.toml src/
  - cargo build --release --bin chipzen-starter-bot
  - cp target/release/chipzen-starter-bot /build/bot
  - rm -rf src/ target/ Cargo.toml          # remove source so stage 2 cannot copy it

Stage 2 (runtime):
  - debian:12-slim
  - apt-get install ca-certificates libssl3 dumb-init
  - COPY --from=builder /build/bot /bot/bot
  - non-root user (uid 10001)
  - ENTRYPOINT ["dumb-init", "/bot/bot"]
```

The release profile in the starter's `Cargo.toml` (`lto = "thin"`,
`strip = "symbols"`, `opt-level = 3`, `codegen-units = 1`) is what
makes the binary small and symbol-stripped. If you remove these,
expect the binary to grow ~3-5x and carry much more reversible
metadata.

## Build-time / runtime ABI compatibility

The builder uses `rust:1-slim` (debian-bookworm-based) and the runtime
uses `debian:12-slim` (also bookworm). Same glibc, same libssl3 ABI,
binary loads cleanly.

If you switch the runtime to alpine (musl), the release binary will
fail because rust:1-slim links against glibc. Either build with
`rust:1-alpine` in stage 1 (musl-target) or stick with debian-slim in
stage 2.

If you want to ship for a different architecture (e.g., arm64), use
Docker Buildx with `--platform=linux/amd64,linux/arm64`. Both base
images support both arches; cargo will target the host architecture
of the build runner unless you pass `--target=...`.

## Future hardening (not in alpha)

The implementation plan calls these out as follow-ups, not blockers:

- **Switch to `rustls-tls-webpki-roots`** instead of `native-tls` to
  drop the runtime libssl dependency entirely. Smaller image, no
  system openssl ABI to track, but ~2 MB more binary size from the
  embedded TLS stack.
- **`strip --strip-all`** as a post-cargo step to remove the few
  symbols `strip = "symbols"` leaves behind (section names, BuildID).
- **Encrypted-at-rest module loading** — distribute the strategy as
  encrypted bytes embedded in the binary, decrypt at startup using a
  per-match key the platform injects. Adds startup latency and
  requires platform-side coordination.
- **Custom rustc obfuscation pass** — there are out-of-tree
  llvm-passes for control-flow flattening / instruction substitution
  that meaningfully raise reverse-engineering cost. Requires custom
  toolchain plumbing.
- **Distroless base** — `gcr.io/distroless/cc-debian12` instead of
  `debian:12-slim` removes ~30 MB of unused userland and reduces the
  attack surface inside the running container. Drop-in compatible
  with the binary; you lose the convenience of `docker exec` shell.
