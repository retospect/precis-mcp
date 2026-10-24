---
status: draft
title: Hide write recipes from read-only callers (skills + Next: hints)
pillar: platform
---

# Hide write recipes from read-only callers

Origin: gr512233 (Reto 2026-10-24, chat-99). Thread: `threads/agent-surface.md`.

## Motivation / why

Every caller gets the same skill text, toc and index rows, and the same
`Next:` trailer under each response — including the put/edit/delete/link/tag
recipes. Read-only agents (cluster-ops, extract, navigator, explore,
reviewer, root-cause) pay tokens for write material they will not use, and
the hints invite mutation. "Read-only" is not a tool-list fact: those agents
still carry `put` and `tag` to file their own gripe, so the scope cannot be
inferred from which verbs an agent definition lists.

## Design (decided 2026-10-24)

**Caller scope comes from the MCP connection**, not the tool list: a
read-scoped bearer token or the DB role the session runs as (`agent_ro` vs
`agent_rw`). The server derives `read | write` per session; nothing has to
opt in, so an agent cannot forget.

- **Skill fence.** Skill markdown marks write sections with a fence tag
  (`<!-- write -->` … `<!-- /write -->`, or a heading tag; pick one in
  slice 2). The skill renderer drops fenced sections when scope is `read`.
  One source file, filtered on the server.
- **`Next:` filter.** The hint renderer drops every row whose call verb is
  put/edit/delete/link/tag when scope is `read`. Exception: filing a gripe,
  `put(kind='gripe', ...)`, stays visible.
- **toc / index / search rows** for skills drop write-only entries by the
  same fence (a skill whose whole body is fenced does not list).

## In scope

- Slice 1 (measure first): the `Next:` generator and skill renderer honour
  a `read` request flag (`args={'mode':'read'}`) set by one read-only agent
  definition; gripe filing exempt. Measures tokens saved on that agent.
- Slice 2: derive the mode from connection scope; mark write sections across
  `src/precis/data/skills/`; lint that every put/edit/delete example sits
  inside a write fence.
- Before/after numbers: tokens per skill fetch, and the share of `Next:`
  rows that are write verbs.

## Explicitly NOT in scope

- Enforcing read-only: the DB role and handler refusals stay the real
  guard. This only trims what is shown.
- Splitting skills into `-help` / `-write-help` files (rejected: doubles the
  files, drifts, and leaves `Next:` unfiltered).
- Removing `put`/`tag` from read-only agent definitions.

## Acceptance criteria

- A read-scoped session fetching any skill sees no fenced section and no
  put/edit/delete/link/tag `Next:` row except `put(kind='gripe')`.
- A write-scoped session's output is byte-identical to today's.
- The fence lint fails on an unfenced write example in a skill.
- Token saving measured and recorded in the shipping commit body.

## Target + blast radius

- `src/precis/handlers/skill.py::SkillHandler` (`get`, `search`, toc rows).
- `src/precis/data/skills/` (fence markup).
- `src/precis/utils/next_block.py::render_next_section` and `format_next_block`
  — the common `Next:` renderer; 38 handler files call it, so the filter
  lives here, not per handler.
- Session scope plumbing: wherever the hub resolves the connection's
  principal (to be located in slice 2).
- Tests: `tests/hintcheck.py` round-trip guard must run in both scopes; the
  tool-description budget test (`tool-description-budgets`) is the token
  baseline.

## Open questions / decisions log

1. Scope source: bearer-token claim or DB role? Which does the hub already
   know per session, and is there a session id the hint ledger can key on?
2. Fence syntax: HTML comment pair vs heading tag. Must survive
   `precis-md` indexing and the skill chunker.
3. Mixed sections (a read example followed by its write variant): fence the
   write lines inline, or require a separate sub-heading?
4. Skill search/lexical index: index the filtered or the full text? A
   read-scoped `search` must not surface a hit whose snippet is fenced.
5. Related token work: `tool-description-budgets`, gr509449 (token review).
