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
# this file). The .dockerignore alongside this file keeps node_modules,
# caches, and lockfile metadata out.

# -----------------------------------------------------------------------------
# Stage 1: build the SDK, then bundle + compile the bot. The .js source lives
# only in this stage and is discarded before the runtime stage starts.
# -----------------------------------------------------------------------------
# Base pinned by digest. Tag: oven/bun:1.4.3-debian (glibc-based; the runtime
# stage below also uses glibc so the compiled binary's dynamic linker can find
# what it needs).
FROM oven/bun:1.4.3-debian@sha256:923bedb355f7746666659f71e724998159a5c8c54bd1cb1030e1975a0698415b AS builder

# Big Bad Bots support (BigBadBotsBot) is merged into @chipzen-ai/bot but is
# not in an npm release yet. Until it is, this stage builds the SDK from the
# chipzen-sdk repository on GitHub, at CHIPZEN_SDK_REF (a branch, tag or
# commit; `--build-arg CHIPZEN_SDK_REF=<ref>` picks another). When an
# @chipzen-ai/bot release lists Big Bad Bots in its CHANGELOG, add it to
# package.json and replace this block and the SDK copy below with the poker
# starter's single `RUN bun install --production`.
# Default pinned to a commit (the merge of chipzen-sdk #185) so a build is
# reproducible; a branch name would change under you.
ARG CHIPZEN_SDK_REF=bbc04571ed3043945747d13b348a0ac6819b30de
ADD https://github.com/chipzen-ai/chipzen-sdk/archive/${CHIPZEN_SDK_REF}.tar.gz /tmp/chipzen-sdk.tar.gz
WORKDIR /sdk
RUN tar -xzf /tmp/chipzen-sdk.tar.gz --strip-components=3 --wildcards '*/packages/javascript/*' \
    && rm /tmp/chipzen-sdk.tar.gz \
    && bun install --production \
    && bun build ./src/index.ts --outdir ./dist --target bun --splitting --external ws

WORKDIR /build

# Bring in the bot source + dependency manifest. Only these are copied:
# keep the build context narrow so the .dockerignore is the only allowlist.
COPY package.json bot.js ./

# Install the bot's own dependencies. Any @chipzen-ai/bot entry (for example
# a local path added by `npm install <sdk checkout>`) is dropped first: the
# SDK built above is the one that goes into the image.
RUN bun -e 'const fs = require("node:fs"); const p = JSON.parse(fs.readFileSync("package.json", "utf8")); if (p.dependencies) delete p.dependencies["@chipzen-ai/bot"]; fs.writeFileSync("package.json", JSON.stringify(p, null, 2));' \
    && bun install --production \
    && mkdir -p node_modules/@chipzen-ai \
    && cp -r /sdk node_modules/@chipzen-ai/bot

# Compile bot.js -> /build/bot. `--compile` emits a single executable that
# bundles the JS, all deps, and the Bun runtime statically. `--minify`
# shrinks output; `--sourcemap=none` excludes maps so the strategy isn't
# trivially readable from inside the binary.
RUN bun build --compile --minify --sourcemap=none --target=bun-linux-x64 \
        bot.js --outfile=/build/bot \
    && rm bot.js \
    && rm -rf node_modules

# -----------------------------------------------------------------------------
# Stage 2: Runtime. Only the compiled binary + ENTRYPOINT.
# No .js source for the bot's strategy is present.
# -----------------------------------------------------------------------------
# Base pinned by digest. Tag: debian:12-slim (matches the glibc the Bun
# --compile output expects).
FROM debian:12-slim@sha256:7ff6ba31527b9e336162fa061746c366c4b2032f4d2cff2078bea43f78efe3f8

# CA certs are needed for outbound TLS (the platform's WebSocket endpoint is
# wss://). dumb-init reaps any subprocesses cleanly on container exit.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates dumb-init \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /bot

# Copy ONLY the compiled binary from the builder stage.
COPY --from=builder /build/bot /bot/bot
RUN chmod +x /bot/bot

# Run as non-root (defense in depth: the platform also applies seccomp and
# cap-drop on top of this). Review rejects an image that runs as root.
RUN groupadd --system --gid 10001 bot \
    && useradd --system --uid 10001 --gid bot --home-dir /bot --shell /usr/sbin/nologin bot \
    && chown -R bot:bot /bot
USER 10001

ENTRYPOINT ["dumb-init", "/bot/bot"]
