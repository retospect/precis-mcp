---
status: idea
title: A per-session context selector living on the shared MCP server
pillar: memory-graph
---

# A per-session context selector living on the shared MCP server

What: the shared session MCP server keys per-session state on
`Mcp-Session-Id` (`serve_ledger.py` binds the current session per call).
Skill dedup used to be the server-side seam deciding what a session sees;
it is now client-held (`get(kind='skill', id=X, args={'have': '<sha8>'})`,
gr477798), so that motivation is gone and the seam is session binding only.
The idea: widen it into a per-session context selector — recency, `SPACE:` tag
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
A sibling session does not hold the text, so cross-session dedup would hand
it a stub pointing at content it has never seen. The stub is keyed on what
the *client* holds (`have=`); the shared process is not a shared context.

Precondition: `td458385` (every session moves onto the shared HTTP server —
decided, Reto 2026-09-30, "supervisor and watchdog, and we move to HTTP").
Until that lands, most sessions are still on per-session stdio containers
with no shared server to hang a selector on.

Owner anchor: `src/precis/server.py` (session binding);
`mcp-shared-server-multiprocess.md` (per-process state must leave memory
before a second server process is safe — a per-session selector inherits
that constraint).

test: two concurrent sessions with different `SPACE:`/thread context get
measurably different search bias or skill-serve behavior from the same
server process, without either client passing extra parameters.

Closest existing items: `context-memory-hierarchy.md`, `session-mcp-http-server.md`,
`mcp-shared-server-multiprocess.md`.
