# The chipzen-mcp MCP server (packages/mcp), run over stdio.
#
# For MCP directories that build a server from this repo and introspect it
# (Glama). Built from source, so the image runs the code in this checkout.
# Without credentials the server still starts and lists its tools; to play,
# pass CHIPZEN_EXTBOT_TOKEN and CHIPZEN_BOT_ID (see packages/mcp/QUICKSTART.md):
#
#   docker build -t chipzen-mcp .
#   docker run -i --rm -e CHIPZEN_EXTBOT_TOKEN -e CHIPZEN_BOT_ID chipzen-mcp

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY packages/mcp /src/chipzen-mcp
RUN pip install /src/chipzen-mcp \
    && useradd --create-home --shell /usr/sbin/nologin chipzen

USER chipzen
ENTRYPOINT ["chipzen-mcp"]
