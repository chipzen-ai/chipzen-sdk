# JavaScript starter — moved

The JavaScript starter has moved to **[`../../packages/javascript/starters/javascript/`](../../packages/javascript/starters/javascript/)**
now that the JavaScript SDK has shipped (`npm install @chipzen-ai/bot`).

The new location:

- Uses the SDK's `Bot` base class — no hand-rolled WebSockets.
- Ships a multi-stage Dockerfile that **compiles your `bot.js` to a
  single statically-linked binary via `bun build --compile`** for IP
  protection (your `.js` source is not in the uploaded image). See
  [`packages/javascript/IP-PROTECTION.md`](../../packages/javascript/IP-PROTECTION.md).

The previous raw-WebSocket starter that lived here has been retired —
hand-rolling the wire protocol is no longer necessary. The protocol
spec at [`../../docs/protocol/`](../../docs/protocol/) remains
authoritative if you ever need to write a non-JS client from scratch.
