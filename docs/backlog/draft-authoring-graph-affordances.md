---
status: draft
title: Four small graph affordances a draft/finding author is missing
prio: high
---

# Four small graph affordances a draft/finding author is missing

Pillar: memory-graph

Evidence from peer session nanobuds (dr173020 work, 2026-09-30). Four
separate authoring moments each had to route around a missing affordance:

(a) **No "proposed replacement for chunk X" node.** A proposed rewrite for
Reto's review had nowhere graph-native to live and ended up in `/tmp`.
(b) **Chunk history is unreachable through any verb.** Pre-backfill caption
handles had to be dug out of `chunk_events` by `scripts/prod-psql` — no
handler exposes a `view='history'` or similar over a chunk's edit log.
(c) **No "did my edit land" affordance.** Every write check was a raw SQL
query against the draft/chunk tables — no verb answers "what does the
server currently think this chunk says" cheaply after a `put`/`edit`.
(d) **Finding dedup search hides live hubs by default.** `status='*'` is
required to see non-`established` hubs in a semantic dedup search
(`src/precis/data/skills/precis-taproot-mint-help.md`); the default filter
made dedup rely on remembering the flag rather than the tool surfacing it.

## Motivation / why

None of these is a new subsystem — each is a read affordance over data the
append-only chunk/`chunk_events` model already carries. Their absence pushes
routine authoring work (propose a change, check history, confirm a write,
dedup-search before minting) off the MCP surface and onto ad-hoc SQL or
`/tmp` files, which is exactly the failure `docs/conventions/` exists to
prevent for code and has no analogue for graph authoring.

## In scope

- **(a) A "proposed chunk replacement" affordance** on the draft/finding
  handlers — a way to attach a proposed rewrite to a specific chunk for
  review, distinct from the live chunk (reuses the draft-edit store's
  edit-in-place discipline per `CLAUDE.md`'s append-only rule — a proposal
  is not yet a write).
- **(b) A `view='history'` (or equivalent) read over a chunk's
  `chunk_events` row, surfaced through the same handler that reads the
  chunk** — no new storage, just a read path onto data that already exists.
- **(c) A post-write confirmation read** — after `put`/`edit`, a cheap
  "what does the chunk say now" round-trip that doesn't require a fresh
  `get` + manual diff.
- **(d) Finding dedup search default reconsidered** — either default to
  `status='*'` for dedup-flavoured calls, or teach the skill/tool
  description to surface the flag more visibly than a skill-body caveat.

## Explicitly NOT in scope

- **A general audit log kind.** This is four narrow read affordances over
  existing append-only data, not a new cross-kind logging subsystem.
- Changing the append-only chunk-body rule itself, or the draft-edit
  in-place exception — both stay as specified in `CLAUDE.md`.
- Building the proposal-review UI — (a) is the data affordance; a review
  surface (web or MCP) is separate, later work.

## Acceptance criteria

- A proposed replacement for a specific chunk is a graph-addressable
  object an agent can `get`/`put`/link to, not a `/tmp` file.
- A chunk's edit history (at minimum: prior `ord`, event type, timestamp)
  is readable through a verb, without `prod-psql`.
- A `put`/`edit` call's response (or an immediate follow-up read) confirms
  the landed state without a second hand-written query.
- Dedup search either defaults to seeing live (non-established) hubs, or
  the mint-path skill/tool surfaces the `status='*'` requirement at the
  call site, not only in prose.

## Target + blast radius

`src/precis/handlers/draft.py`, `src/precis/handlers/_finding_hub_mint.py`
(or wherever the mint dedup search lives), `src/precis/data/skills/precis-taproot-mint-help.md`,
`src/precis/data/skills/precis-draft-*-help.md`; read-only additions except
(a), which is a new proposal object.

## Open questions / decisions log

- Whether (a)'s proposal object is a new `kind` or a `meta` flag on an
  existing draft chunk — undecided; a new kind is heavier but keeps the
  append-only chunk table untouched.

Closest existing items: `draft-refresh.md`, `draft-inline-editor.md`,
`finding-stable-identity.md` (the same "still broken vs. broken again"
problem, one level up — for checks, not chunks), `taproot-merge-mcp-surface.md`.

Related (2026-09-30): `draft-linearization.md` — a draft as a render of a
subgraph. That item makes the graph the truth; this one gives the author
the affordances to edit through the graph while a draft still owns text.
