# syntax=docker/dockerfile:1.7
#
# IP-protected Chipzen bot image.
#
# Multi-stage build that compiles bot.py into a Cython .so module in
# the builder stage, then ships only the compiled binary in the runtime
# stage. The runtime image contains no readable .py source for your
# strategy code.
#
# See ../IP-PROTECTION.md for what this protects (and what it doesn't).
#
# Build:   docker build -t my-bot:test .
# Export:  docker save my-bot:test | gzip > my-bot.tar.gz
#
# Build context for this directory should be small (bot.py +
# requirements.txt + this file). The .dockerignore alongside this
# file keeps caches, virtualenvs, and __pycache__ out.

# -----------------------------------------------------------------------------
# Stage 1: Cython compilation. The .py source lives only in this stage and is
# discarded before the runtime stage starts.
# -----------------------------------------------------------------------------
# Base pinned by digest — Dependabot-managed. Tag: python:3.12-slim
FROM python:3.12-slim@sha256:da2d7af143dab7cd5b0d5a5c9545fe14e67fc24c394fcf1cf15e8ea16cbd8390 AS builder

WORKDIR /build

# Cython 3.x is the current stable line. setuptools provides the build
# backend cythonize -i invokes under the hood.
RUN pip install --no-cache-dir "cython==3.0.*" "setuptools>=68"

# Bring in the bot source. Only bot.py is copied — keep the build
# context narrow so the .dockerignore is the only allowlist.
COPY bot.py .

# Compile bot.py -> bot.cpython-<abi>-<arch>-linux-gnu.so via Cython.
# The -i flag does an in-place build, leaving the .so next to the .py.
# After compilation, drop the .py so it cannot be copied into stage 2
# even by accident.
RUN cythonize -i bot.py && rm bot.py

# -----------------------------------------------------------------------------
# Stage 2: Runtime. Only the compiled .so + runtime deps + ENTRYPOINT.
# No .py source for the bot's strategy is present.
# -----------------------------------------------------------------------------
FROM python:3.12-slim@sha256:da2d7af143dab7cd5b0d5a5c9545fe14e67fc24c394fcf1cf15e8ea16cbd8390

# Unbuffered stdio — without this, your stdout/stderr can be lost when
# the container exits.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /bot

# Install the SDK from PyPI. chipzen-bot pulls in `websockets`; nothing
# else is strictly required at runtime. Add your own dependencies to
# requirements.txt if your bot uses them (numpy, torch, etc.).
COPY requirements.txt .
RUN pip install -r requirements.txt \
    && find /usr/local/lib/python3.12 -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true \
    && find /usr/local/lib/python3.12 -type d -name tests -exec rm -rf {} + 2>/dev/null || true

# Copy ONLY the compiled .so module from the builder stage. Wildcard
# avoids pinning the Cython output filename to a specific Python ABI /
# architecture suffix.
COPY --from=builder /build/*.so /bot/

# Run as non-root (defense in depth — the platform also applies seccomp
# and cap-drop on top of this).
RUN groupadd --system --gid 10001 bot && \
    useradd --system --uid 10001 --gid bot --home-dir /bot --shell /usr/sbin/nologin bot && \
    chown -R bot:bot /bot
USER 10001

# ENTRYPOINT calls main() from the compiled bot module. The .py source
# isn't present so we can't python bot.py directly; the import path
# resolves to bot.<abi>.so via Python's standard module loader.
ENTRYPOINT ["python", "-c", "from bot import main; main()"]
