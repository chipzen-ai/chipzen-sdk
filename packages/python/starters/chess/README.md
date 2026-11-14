# Chess starters (preview)

> **Preview: chess is not live on Chipzen yet.** These starters build against
> the chess support in this repository, which is not in a released
> `chipzen-bot` yet. Their `requirements.txt` will point at the first release
> that carries it. Field names may still change before chess goes live.

Two ready-to-run chess bots, one folder each:

| Starter | `chipzen-sdk init` | What it does |
|---|---|---|
| [`legal-mover/`](legal-mover/) | `chipzen-sdk init my_bot --template chess` | Picks a move from the request's legal moves: the biggest capture, else a queen promotion, else a random move. No engine, no chess library. |
| [`uci-bridge/`](uci-bridge/) | `chipzen-sdk init my_bot --template chess-uci` | Runs a UCI engine you supply (no engine is bundled) and plays its `bestmove`, with a legal fallback. |

Each folder is self-contained (bot, `requirements.txt`, `Dockerfile`,
`.dockerignore`, README), so copy one and work from there. Check either with
`chipzen-sdk validate <folder>`: it answers every recorded chess request in
the SDK's fixtures and judges each answer the way the platform does.

How chess bots work (messages, clocks, rejections, colours): see
[`docs/CHESS-BOTS.md`](../../../../docs/CHESS-BOTS.md).
