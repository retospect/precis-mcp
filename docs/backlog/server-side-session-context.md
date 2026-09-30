---
status: idea
title: A per-session context selector living on the shared MCP server
pillar: memory-graph
---

# A per-session context selector living on the shared MCP server

What: the shared session MCP server keys per-session state on
`Mcp-Session-Id`, and the serve ledger (`serve_ledger.py`,
`WeakKeyDictionary`-backed per `mcp-shared-server-multiprocess.md`) already
decides *per session* whether to serve a skill stub or the full skill text.
That is the seam where the server, not the client, decides what a session
sees from the graph — today it only dedups repeated skill serves. The idea:
widen that seam into a per-session context selector — recency, `SPACE:` tag
(`file-mirror.md` §"Pillar-review deltas"), active thread — driven server-side instead
of by each client re-deciding what to preload.

Why: `context-memory-hierarchy.md` designs the *client-side* resident/
discovered split for one session's own preamble; this is the same question
from the other end, for a server that now serves every session in one
process. A server-side selector could, e.g., bias a session's search results
toward its own `SPACE:`/thread without every client re-implementing that
filter.

One example to strike before anyone builds it: "don't re-serve a skill a
sibling session just fetched" is the one thing this seam must *not* do.
The ledger stubs a repeat serve because that session already has the text
in its context; a sibling session does not, so cross-session dedup would
hand it a stub pointing at content it has never seen. Dedup stays keyed
per session — the shared process is an implementation detail, not a shared
context. (The parked "live cross-session serve-ledger check" on
`threads/session-mcp-shared-server.md` exists to confirm the current
behaviour is per-session, which is the behaviour to preserve.)

Precondition: `td458385` (every session moves onto the shared HTTP server —
decided, Reto 2026-09-30, "supervisor and watchdog, and we move to HTTP").
Until that lands, most sessions are still on per-session stdio containers
with no shared ledger to hang a selector on.

Owner anchor: `src/precis/server.py` (serve ledger + session keying);
`mcp-shared-server-multiprocess.md` (the constraint that the ledger must
leave process memory before a second server process is safe — a per-session
selector inherits that same constraint).

test: two concurrent sessions with different `SPACE:`/thread context get
measurably different search bias or skill-serve behavior from the same
server process, without either client passing extra parameters.

Closest existing items: `context-memory-hierarchy.md`, `session-mcp-http-server.md`,
`mcp-shared-server-multiprocess.md`.
