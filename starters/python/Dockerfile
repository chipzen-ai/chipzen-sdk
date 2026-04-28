# Base pinned by digest — Dependabot-managed (#1256). Tag: python:3.12-slim
FROM python:3.12-slim@sha256:804ddf3251a60bbf9c92e73b7566c40428d54d0e79d3428194edf40da6521286
WORKDIR /bot
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .

# Run as non-root (CZ#1262, defense-in-depth on top of executor seccomp + cap-drop).
RUN groupadd --system --gid 10001 bot && \
    useradd --system --uid 10001 --gid bot --home-dir /bot --shell /usr/sbin/nologin bot && \
    chown -R bot:bot /bot
USER 10001

ENTRYPOINT ["python", "bot.py"]
