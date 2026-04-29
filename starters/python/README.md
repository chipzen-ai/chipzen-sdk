# Python starter — moved

The Python starter has moved to **[`../../packages/python/starters/python/`](../../packages/python/starters/python/)**
now that the Python SDK has shipped (`pip install chipzen-bot`).

The new location:

- Uses the SDK's `chipzen.Bot` base class — no hand-rolled WebSockets.
- Ships a multi-stage Dockerfile that **compiles your `bot.py` to a
  Cython `.so`** for IP protection (your `.py` source is not in the
  uploaded image). See
  [`packages/python/IP-PROTECTION.md`](../../packages/python/IP-PROTECTION.md).

The previous raw-WebSocket starter that lived here has been retired —
hand-rolling the wire protocol is no longer necessary. The protocol
spec at [`../../docs/protocol/`](../../docs/protocol/) remains
authoritative if you ever need to write a non-Python client from
scratch.
