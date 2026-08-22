---
status: draft
title: unify backlog + gripes + coding rules into one searchable, discoverable surface
---

# Unify backlog/gripes/rules — searchable + discoverable

Parked idea (Reto, 2026-08-21, during the context-economy work). Three
overlapping stores of "things to know/do" exist with different access paths:

- `docs/backlog/` — files, greppable, but invisible to `search()` and to
  fleet agents; this session twice re-derived designs the backlog already
  held (`tool-call-ledger`, the codereview args= constraint).
- `kind='gripe'` — searchable via MCP, but dev-session friction rarely lands
  there.
- Coding rules — split across CLAUDE.md / AGENTS.md / `docs/conventions/`;
  loaded up-front (context cost) instead of discovered at need-time.

Direction: make backlog items and conventions reachable through the same
discovery reflex as skills (`search(kind=…)`) — e.g. ingest/index them as a
kind or expose a view — so need-time lookup beats standing context and beats
memory. Interacts with: `mcp-command-profile.md` (executable hints),
`dev-context-diet.md` (eviction), `mcp-tool-ledger.md` (detour detection).

Not designed yet — this is a pointer, not a plan.
