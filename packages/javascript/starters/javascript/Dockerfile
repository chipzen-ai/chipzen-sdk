# syntax=docker/dockerfile:1.7
#
# IP-protected Chipzen JavaScript bot image.
#
# Multi-stage build that bundles bot.js + the SDK into a single
# statically-linked binary via `bun build --compile` in the builder
# stage, then ships only that binary in the runtime stage. The runtime
# image contains no readable .js source for your strategy code.
#
# See ../../IP-PROTECTION.md for what this protects (and what it doesn't).
#
# Build:   docker build -t my-bot:test .
# Export:  docker save my-bot:test | gzip > my-bot.tar.gz
#
# Build context for this directory should be small (bot.js +
# package.json + this file). The .dockerignore alongside this file
# keeps node_modules, caches, and lockfile metadata out.

# -----------------------------------------------------------------------------
# Stage 1: Bun bundle + compile. The .js source lives only in this stage and
# is discarded before the runtime stage starts.
# -----------------------------------------------------------------------------
# Base pinned by tag — Dependabot can rotate to digest pinning later. Tag:
# oven/bun:1-debian (glibc-based; the runtime stage below also uses glibc
# so the compiled binary's dynamic linker can find what it needs).
FROM oven/bun:1-debian AS builder

WORKDIR /build

# Bring in the bot source + dependency manifest. Only these are copied —
# keep the build context narrow so the .dockerignore is the only allowlist.
COPY package.json bot.js ./

# Resolve @chipzen-ai/bot + ws so `bun build` can bundle them. We don't
# need a lockfile committed; the registry pin in package.json is enough
# for a fresh install at build time.
RUN bun install --production

# Compile bot.js -> /build/bot. `--compile` emits a single executable
# that bundles the JS, all deps, and the Bun runtime statically.
# `--minify` shrinks output; `--sourcemap=none` excludes maps so the
# strategy isn't trivially readable from inside the binary.
RUN bun build --compile --minify --sourcemap=none --target=bun-linux-x64 \
        bot.js --outfile=/build/bot \
    && rm bot.js \
    && rm -rf node_modules

# -----------------------------------------------------------------------------
# Stage 2: Runtime. Only the compiled binary + ENTRYPOINT.
# No .js source for the bot's strategy is present.
# -----------------------------------------------------------------------------
# Base pinned by tag — Dependabot can rotate to digest pinning later. Tag:
# debian:12-slim (matches the glibc the Bun --compile output expects).
FROM debian:12-slim

# CA certs are needed for outbound TLS (the platform's WebSocket
# endpoint is wss://). dumb-init reaps any subprocesses cleanly on
# container exit; tiny but useful.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates dumb-init \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /bot

# Copy ONLY the compiled binary from the builder stage.
COPY --from=builder /build/bot /bot/bot
RUN chmod +x /bot/bot

# Run as non-root (defense in depth — the platform also applies seccomp
# and cap-drop on top of this).
RUN groupadd --system --gid 10001 bot \
    && useradd --system --uid 10001 --gid bot --home-dir /bot --shell /usr/sbin/nologin bot \
    && chown -R bot:bot /bot
USER 10001

ENTRYPOINT ["dumb-init", "/bot/bot"]
