# syntax=docker/dockerfile:1.7
#
# Reference "always-check-or-fold" bot image.
#
# Intentionally minimal: it is tiny, deterministic, and does nothing
# interesting — check when you can, otherwise fold. New developers read
# this first; a fuller integration comes after.
#
# Stays under 50 MB compressed (Alpine base, pure-Python deps, no
# checkpoints or native extensions).
#
# Build (from this directory):
#   docker build -t reference-bot:test .
#
# Export for upload:
#   docker save reference-bot:test | gzip > reference-bot.tar.gz

# Base pinned by digest. Tag: python:3.11-alpine
FROM python:3.11-alpine@sha256:8b5bfdb1fd2d78aa94e21c4d61be52487693f54be7f1021647751ff365795703

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /bot

# -----------------------------------------------------------------------------
# 1. Install the Chipzen SDK (pip-installable from PyPI).
#    The SDK pulls in `websockets`. No other runtime deps.
#
#    Alpine keeps the image tiny (~50 MB base vs ~125 MB for slim). We rely
#    on pure-Python wheels only (`websockets` has pure-Python fallbacks),
#    so we do NOT need build-base / gcc.
# -----------------------------------------------------------------------------
RUN pip install chipzen-bot \
    && rm -rf /root/.cache \
    && find /usr/local/lib/python3.11 -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true \
    && find /usr/local/lib/python3.11 -type d -name tests -exec rm -rf {} + 2>/dev/null || true

# -----------------------------------------------------------------------------
# 2. The bot itself.
# -----------------------------------------------------------------------------
COPY bot.py /bot/bot.py

# -----------------------------------------------------------------------------
# 3. Non-root user (defense in depth). The Chipzen runtime additionally
#    applies seccomp, cap-drop, and a read-only root FS; keeping the image
#    itself unprivileged means it works the same way locally and in
#    production. The only writable surface at runtime is /tmp (tmpfs).
# -----------------------------------------------------------------------------
RUN adduser -D -u 10001 -s /bin/sh bot \
    && chown -R bot:bot /bot
USER bot

ENTRYPOINT ["python", "-u", "/bot/bot.py"]
