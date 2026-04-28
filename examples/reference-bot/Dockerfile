# syntax=docker/dockerfile:1.7
#
# Reference "always-check-or-fold" bot image.
#
# Intentionally minimal. This is the sanity target for load tests, the
# upload-pipeline smoke test, and the starter example referenced from the
# dev quickstart. The whole point of the image is that it is tiny,
# deterministic, and does nothing interesting -- check when you can,
# otherwise fold.
#
# Contrast with the PluriBot image (numpy/scipy + a 900k CFR checkpoint,
# ~136 MB compressed); this one stays under 50 MB compressed.
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
# 3. Non-root user. The platform's upload pipeline adds seccomp, --read-only,
#    cap-drop, and --user 10001:10001 on top of this; keeping the image
#    unprivileged is defense in depth. uid 10001 matches the executor's
#    --user flag. The container's root FS is read-only at runtime so /bot
#    ownership is symbolic -- the only writable surface is /tmp tmpfs.
# -----------------------------------------------------------------------------
RUN adduser -D -u 10001 -s /bin/sh bot \
    && chown -R bot:bot /bot
USER bot

ENTRYPOINT ["python", "-u", "/bot/bot.py"]
