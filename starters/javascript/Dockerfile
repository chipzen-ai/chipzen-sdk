# Base pinned by digest — Dependabot-managed. Tag: node:20-slim
FROM node:20-slim@sha256:f93745c153377ee2fbbdd6e24efcd03cd2e86d6ab1d8aa9916a3790c40313a55
WORKDIR /bot
COPY package.json .
RUN npm install --production
COPY bot.js .

# Run as the stock non-root `node` user (uid 1000) that ships with node:20-slim.
RUN chown -R node:node /bot
USER node

ENTRYPOINT ["node", "bot.js"]
