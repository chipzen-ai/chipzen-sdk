# syntax=docker/dockerfile:1.7
#
# IP-protected Big Bad Bots image for the Turtle starter.
#
# The same packaging as the poker starter (../../python/Dockerfile): a
# multi-stage build that compiles bot.py into a Cython .so module in the
# builder stage, then ships only the compiled binary in the runtime stage.
# The runtime image contains no readable .py source for your strategy code.
#
# See ../../../IP-PROTECTION.md for what this protects (and what it doesn't).
#
# Build:   docker build -t my-bigbadbots-bot:test .
# Export:  docker save my-bigbadbots-bot:test | gzip > my-bigbadbots-bot.tar.gz
#
# Build context for this directory should be small (bot.py +
# requirements.txt + log_cap.py + this file). The .dockerignore alongside
# this file keeps caches, virtualenvs, and __pycache__ out.
#
# log_cap.py is the platform's per-match log cap, and it is this image's
# ENTRYPOINT: every Big Bad Bots bot runs under it, and the platform's review
# rejects an image that does not start through it. Do not edit log_cap.py or
# change the ENTRYPOINT; change CMD (your bot command) instead.

# -----------------------------------------------------------------------------
# Stage 1: Cython compilation. The .py source lives only in this stage and is
# discarded before the runtime stage starts.
# -----------------------------------------------------------------------------
# Base pinned by digest: Dependabot-managed. Tag: python:3.12-slim
FROM python:3.12-slim@sha256:dd29372629eeba2dd003fd9e9d35a5b8236c44727875a0364254b5127af88e65 AS builder

WORKDIR /build

# cythonize generates C and then compiles it, so this stage needs a C
# toolchain. python:3.12-slim ships none, which made every build die at
# the cythonize step (issue #74).
#
# libc6-dev is required alongside gcc: it carries the standard C headers
# (stdlib.h et al) that Python.h includes. It is only a *Recommends* of
# the gcc package, so --no-install-recommends drops it and the compile
# fails one step later on `fatal error: stdlib.h: No such file`. These two
# packages are still much lighter than build-essential.
#
# The toolchain cost stays in the builder stage: the runtime stage below
# only ever receives the compiled .so.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

# Cython 3.x is the current stable line. setuptools provides the build
# backend cythonize -i invokes under the hood.
RUN pip install --no-cache-dir "cython==3.0.*" "setuptools>=68"

# Bring in the bot source. Only bot.py is copied: keep the build
# context narrow so the .dockerignore is the only allowlist.
COPY bot.py .

# Compile bot.py -> bot.cpython-<abi>-<arch>-linux-gnu.so via Cython.
# The -i flag does an in-place build, leaving the .so next to the .py.
# After compilation, drop the .py so it cannot be copied into stage 2
# even by accident.
RUN cythonize -i bot.py && rm bot.py

# -----------------------------------------------------------------------------
# Stage 2: Runtime. Only the compiled .so + runtime deps + the log-cap
# wrapper as ENTRYPOINT.
# No .py source for the bot's strategy is present.
# -----------------------------------------------------------------------------
FROM python:3.12-slim@sha256:dd29372629eeba2dd003fd9e9d35a5b8236c44727875a0364254b5127af88e65

# Unbuffered stdio: without this, your stdout/stderr can be lost when
# the container exits.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /bot

# Install the SDK (pinned in requirements.txt). chipzen-bot
# pulls in `websockets`; nothing else is required at runtime. Add your own
# dependencies to requirements.txt if your bot uses them (numpy, etc.).
COPY requirements.txt .
RUN pip install -r requirements.txt \
    && find /usr/local/lib/python3.12 -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true \
    && find /usr/local/lib/python3.12 -type d -name tests -exec rm -rf {} + 2>/dev/null || true

# Copy ONLY the compiled .so module from the builder stage. Wildcard
# avoids pinning the Cython output filename to a specific Python ABI /
# architecture suffix.
COPY --from=builder /build/*.so /bot/

# The platform's per-match log cap (stdlib only). It lives outside /bot,
# owned by root and read-only, so nothing the bot does can replace it.
COPY log_cap.py /opt/chipzen/log_cap.py

# Run as non-root (defense in depth: the platform also applies seccomp
# and cap-drop on top of this).
RUN groupadd --system --gid 10001 bot && \
    useradd --system --uid 10001 --gid bot --home-dir /bot --shell /usr/sbin/nologin bot && \
    chown -R bot:bot /bot && \
    chmod 0444 /opt/chipzen/log_cap.py
USER 10001

# Every Big Bad Bots bot runs under the log cap: the ENTRYPOINT is the
# wrapper, and the bot command is the CMD it runs. Keep this ENTRYPOINT
# exactly as it is (the platform's review checks it); change only CMD.
# `-I -S` keep the wrapper's own Python isolated from the bot's files and
# environment; the bot command still gets the full environment.
ENTRYPOINT ["python", "-I", "-S", "-u", "/opt/chipzen/log_cap.py", "--"]

# CMD calls main() from the compiled bot module. The .py source isn't
# present so we can't python bot.py directly; the import path resolves to
# bot.<abi>.so via Python's standard module loader.
CMD ["python", "-u", "-c", "from bot import main; main()"]
