# syntax=docker/dockerfile:1.7
#
# IP-protected Chipzen Rust bot image.
#
# Multi-stage build that compiles the bot to a single, statically-
# linked release binary in the builder stage, then ships only that
# binary in the runtime stage. The runtime image contains no readable
# Rust source for your strategy code — only the stripped binary.
#
# See ../../IP-PROTECTION.md for what this protects (and what it doesn't).
#
# Build:   docker build -t my-bot:test .
# Export:  docker save my-bot:test | gzip > my-bot.tar.gz
#
# Build context for this directory should be small (Cargo.toml +
# src/ + this file). The .dockerignore alongside this file keeps the
# target/ build cache and editor metadata out.

# -----------------------------------------------------------------------------
# Stage 1: cargo build --release. The .rs source lives only in this stage and
# is discarded before the runtime stage starts.
# -----------------------------------------------------------------------------
# Base pinned by tag — Dependabot can rotate to digest pinning later. Tag:
# rust:1-slim (debian-bookworm-based; the runtime stage below also uses
# debian-bookworm so glibc + libssl line up for the compiled binary).
FROM rust:1-slim AS builder

WORKDIR /build

# Build tools needed by tokio-tungstenite's `native-tls` feature
# (links against system openssl). pkg-config tells the openssl-sys
# build script where libssl + libcrypto live.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        pkg-config \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Bring in the bot source + manifest. Only these are copied — keep
# the build context narrow so the .dockerignore is the only allowlist.
COPY Cargo.toml ./
COPY src/ ./src/

# Build the release binary. The starter's [profile.release] is already
# tuned for a small, symbol-stripped binary (lto=thin, opt-level=3,
# codegen-units=1).
RUN cargo build --release --bin bot \
    && cp target/release/bot /build/bot \
    && rm -rf src/ target/ Cargo.toml

# -----------------------------------------------------------------------------
# Stage 2: Runtime. Only the compiled binary + ENTRYPOINT.
# No .rs source for the bot's strategy is present.
# -----------------------------------------------------------------------------
# Base pinned by tag — Dependabot can rotate to digest pinning later. Tag:
# debian:12-slim. Matches the glibc the builder stage links against and
# carries libssl3 + ca-certs for outbound TLS to the platform's wss://.
FROM debian:12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        libssl3 \
        dumb-init \
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
