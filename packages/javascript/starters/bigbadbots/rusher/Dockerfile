# syntax=docker/dockerfile:1.7
#
# IP-protected Big Bad Bots image for the JavaScript Rusher starter.
#
# The same packaging as the poker starter (../../javascript/Dockerfile): a
# multi-stage build that bundles bot.js and the SDK into one executable with
# `bun build --compile` in the builder stage, then ships only that executable
# in the runtime stage. The runtime image contains no readable .js source for
# your strategy code.
#
# See ../../../IP-PROTECTION.md for what this protects (and what it doesn't).
#
# Build:   docker build -t my-bigbadbots-bot:test .
# Export:  docker save my-bigbadbots-bot:test | gzip > my-bigbadbots-bot.tar.gz
#
# Build context for this directory should be small (bot.js + package.json +
# log_cap.py + this file). The .dockerignore alongside this file keeps
# node_modules, caches, and lockfile metadata out.
#
# log_cap.py is the platform's per-match log cap, and it is this image's
# ENTRYPOINT: every Big Bad Bots bot runs under it, and the platform's review
# rejects an image that does not start through it. Do not edit log_cap.py or
# change the ENTRYPOINT; change CMD (your bot command) instead.

# -----------------------------------------------------------------------------
# Stage 1: bundle + compile the bot. The .js source lives only in this stage
# and is discarded before the runtime stage starts.
# -----------------------------------------------------------------------------
# Base pinned by digest. Tag: oven/bun:1.4.3-debian (glibc-based; the runtime
# stage below also uses glibc so the compiled binary's dynamic linker can find
# what it needs).
FROM oven/bun:1.4.3-debian@sha256:923bedb355f7746666659f71e724998159a5c8c54bd1cb1030e1975a0698415b AS builder

WORKDIR /build

# Bring in the bot source + dependency manifest. Only these are copied:
# keep the build context narrow so the .dockerignore is the only allowlist.
COPY package.json bot.js ./

# Resolve @chipzen-ai/bot (Big Bad Bots support is in 0.4.0 and later; the starter pins ^0.5.0) + ws so
# `bun build` can bundle them. No lockfile is committed; the registry pin in
# package.json is enough for a fresh install at build time.
RUN bun install --production

# Compile bot.js -> /build/bot. `--compile` emits a single executable that
# bundles the JS, all deps, and the Bun runtime statically. `--minify`
# shrinks output; `--sourcemap=none` excludes maps so the strategy isn't
# trivially readable from inside the binary.
RUN bun build --compile --minify --sourcemap=none --target=bun-linux-x64 \
        bot.js --outfile=/build/bot \
    && rm bot.js \
    && rm -rf node_modules

# -----------------------------------------------------------------------------
# Stage 2: Runtime. Only the compiled binary + the log-cap wrapper as
# ENTRYPOINT.
# No .js source for the bot's strategy is present.
# -----------------------------------------------------------------------------
# Base pinned by digest. Tag: debian:12-slim (matches the glibc the Bun
# --compile output expects).
FROM debian:12-slim@sha256:7ff6ba31527b9e336162fa061746c366c4b2032f4d2cff2078bea43f78efe3f8

# CA certs are needed for outbound TLS (the platform's WebSocket endpoint is
# wss://). dumb-init reaps any subprocesses cleanly on container exit.
# python3-minimal + libpython3.11-stdlib run only the log-cap wrapper (a
# stdlib-only script); the bot never uses them. That pair is the slimmest
# Debian Python that runs it: python3-minimal alone lacks modules the
# wrapper imports (dataclasses, ctypes).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates dumb-init \
        python3-minimal libpython3.11-stdlib \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /bot

# Copy ONLY the compiled binary from the builder stage.
COPY --from=builder /build/bot /bot/bot
RUN chmod +x /bot/bot

# The platform's per-match log cap. It lives outside /bot, owned by root and
# read-only, so nothing the bot does can replace it.
COPY log_cap.py /opt/chipzen/log_cap.py

# Run as non-root (defense in depth: the platform also applies seccomp and
# cap-drop on top of this). Review rejects an image that runs as root.
RUN groupadd --system --gid 10001 bot \
    && useradd --system --uid 10001 --gid bot --home-dir /bot --shell /usr/sbin/nologin bot \
    && chown -R bot:bot /bot \
    && chmod 0444 /opt/chipzen/log_cap.py
USER 10001

# Every Big Bad Bots bot runs under the log cap: the ENTRYPOINT is the
# wrapper, and the bot command is the CMD it runs. Keep this ENTRYPOINT
# exactly as it is (the platform's review checks it); change only CMD.
# `-I -S` keep the wrapper's own Python isolated from the bot's files and
# environment; the bot command still gets the full environment.
ENTRYPOINT ["python3", "-I", "-S", "-u", "/opt/chipzen/log_cap.py", "--"]
CMD ["dumb-init", "/bot/bot"]
