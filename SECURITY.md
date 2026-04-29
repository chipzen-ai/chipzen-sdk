# Security policy

## Reporting a vulnerability

If you find a security issue in the Chipzen SDK or believe the
protocol spec contains a flaw with security implications, please
report it privately rather than opening a public issue.

Email: **security@chipzen.ai**

Include:

- A description of the issue and the impact you observed.
- Steps to reproduce.
- The affected SDK version / commit SHA.
- Your preferred contact info for follow-up.

We will acknowledge receipt within **48 hours**, share an initial
assessment within **5 business days**, and target a fix within
**30 days** of confirmation (longer for issues that require
coordinated platform-side changes; we'll communicate the timeline if
that applies).

A coordinated-disclosure window of 30 days is the default. Reporters
who give us reasonable time before public disclosure will be credited
in a security acknowledgements section unless they request anonymity.

We don't run a paid bug bounty.

## Scope

In scope for this repo:

- Vulnerabilities in the SDK code itself (starters, reference bot,
  shared client utilities, the protocol adapter once it ships).
- Flaws in the wire-protocol specification that would let a malicious
  client compromise the platform or another bot.

For platform-side issues (hosted services, account/billing flows,
infrastructure), use the same `security@chipzen.ai` address — the
team handles both intakes.

## Bot runtime — what the platform enforces on uploaded bots

External bot authors most often ask "what does the Chipzen platform
actually do to my container at runtime, and what am I allowed to do
from inside it." Short summary of the binding controls — design your
bot accordingly.

### Container hardening

- **Read-only root filesystem.** `/tmp` (tmpfs, capped per tier — see
  [`docs/DEV-MANUAL.md` §7.2](docs/DEV-MANUAL.md#72-resource-limits-per-tier))
  is the only writable path. Anywhere else returns
  `OSError: [Errno 30] Read-only file system`.
- **All Linux capabilities dropped.** The platform spawns your task
  with `--cap-drop=ALL`. You don't get `NET_RAW`, `NET_BIND_SERVICE`,
  `CHOWN`, `SETUID`, etc. Bots only need outbound WebSocket; nothing
  else is granted.
- **No-new-privileges.** Setuid binaries cannot escalate mid-execution.
- **Non-root user.** The container runs as uid `10001`. The starter
  Dockerfiles set this up themselves; the platform also enforces it
  defense-in-depth at the task definition layer.
- **Ephemeral per-match task.** Each (bot, match) pair gets a fresh
  container booted from your pinned image digest. No state persists
  across matches; no container is reused; there is no shared disk.

### Image hygiene (platform runtime template)

The Chipzen-supplied runtime template strips a set of binaries that
have no place in a sandboxed bot image:

- **Shells** (`/bin/sh`, `/bin/bash`, `/bin/dash`) — no exec() escape.
- **Package managers** (`apt`, `dpkg`, `pip`) — no runtime installs.
- **Compilers** (`gcc`, `cc`, `make`) — no post-compromise build.
- **Network tools** (`curl`, `wget`, `nc`, `ncat`) — limits exfil
  primitives.

If you build your own image (rather than starting from the
platform-supplied template), keep these out of the runtime layer.

### Network egress

**Bot tasks have no general internet egress.** Outbound traffic from
inside a match container is restricted to the platform's match
WebSocket endpoint. Concretely:

- You **cannot** call out to a leaderboard service, telemetry
  endpoint, external solver API, model hosting service, or any other
  third-party host from inside `decide()`.
- You **cannot** open additional TCP/UDP sockets.
- DNS for non-platform hosts is not resolvable.

Design accordingly: any external lookup or data your bot needs has to
be baked into the image at build time, or computed locally during
the match. This isn't a soft expectation — it's enforced at the
network layer.

### WebSocket hardening

- **Heartbeat.** The server sends a `ping` every 30s; if no `pong`
  arrives within 10s, the connection is closed with code `1011`. The
  SDK handles `ping`/`pong` automatically — if you write your own
  client, you must respond to `ping` within the timeout.
- **Backpressure.** Per-connection ring buffer (1 MB or 100 messages,
  whichever fills first); the bot connector applies a `drop_oldest`
  policy on overflow because only the latest `turn_request` matters.
  A bot that cannot keep up with the message rate will see older
  messages dropped before the connection is dropped.

### Resource limits

CPU, memory, decision timeout, image size, tmpfs size, and max-bots
are tier-bounded. See
[`docs/DEV-MANUAL.md` §7.2](docs/DEV-MANUAL.md#72-resource-limits-per-tier).

If you exceed the decision timeout, the platform safe-defaults to
`check` (or `fold` if check is illegal) and emits a `bot_error`
event to the human's UI — your bot appears to fold every raise. This
is documented as a common failure mode in
[`docs/DEV-MANUAL.md` §9.2](docs/DEV-MANUAL.md#92-my-bot-always-folds--server-shows-safe-default).
