# Build stage pinned by digest — Dependabot-managed. Tag: rust:1.79-slim
FROM rust:1.79-slim@sha256:073c8e7ae12d2637d1d541cb0f3f9d92bfa22c97e2e45db87a9fc6a90c9a7f22 AS builder
WORKDIR /bot
COPY Cargo.toml .
COPY src/ src/
RUN cargo build --release

# Runtime pinned by digest — Dependabot-managed. Tag: debian:bookworm-slim
FROM debian:bookworm-slim@sha256:4724b8cc51e33e398f0e2e15e18d5ec2851ff0c2280647e1310bc1642182655d
COPY --from=builder /bot/target/release/chipzen-starter-bot /usr/local/bin/

# Run as non-root (defense in depth — the platform also applies seccomp and cap-drop).
RUN groupadd --system --gid 10001 bot && \
    useradd --system --uid 10001 --gid bot --home-dir /nonexistent --shell /usr/sbin/nologin bot
USER 10001

ENTRYPOINT ["chipzen-starter-bot"]
