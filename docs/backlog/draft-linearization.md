---
status: draft
pillar: memory-graph
title: draft linearization — the graph is the truth, a draft is a render of a subgraph in reading order
prio: normal
model: opus
blocked-by: knowledge-mesh
---

# draft linearization — generate the linear text from the graph

## Motivation / why

Reto, 2026-09-30: "all thinking and storage happens in the graph, and
sometimes we generate a linearized draft from it." Today the relation is
the other way round. Drafts are chunk-native truth (`draft` kind), the
argument graph is "the reasoning shadow beside a draft"
(`precis-argument-help`, ADR 0054), and findings, measures and memories are
cited *into* prose an author writes by hand. Inverting this means an
outline is a walk over the graph and the prose is derived: lemmas,
findings, measures and their edges in an order, each rendered through its
gist and body, citations emitted as `[fi<id>]` hubs per the cite-findings-
only policy.

This is a horizon item. It needs the generic walk (`knowledge-mesh.md`)
and benefits from every node kind having a gist (`fisheye-everywhere.md`).

## In scope (v1, render-only — Reto, 2026-09-30)

1. **Outline as data.** A `plan` (or the draft's own root chunk) holds an
   ordered list of handles plus, per entry, the relation filter and depth
   to walk from it. No new kind; `plan` already places in folders and
   links.
2. **Render.** `put(kind='draft', text='', from=<outline handle>)` walks
   each entry, renders each node as a paragraph (title → gist → body, the
   universal row from `knowledge-mesh.md`), emits `[fi<id>]` for finding
   hubs and a measure line for measures, and writes the chunks. Re-render
   is idempotent on an unchanged graph.
3. **Provenance on every chunk.** `links.meta.rendered_from=<handle>` on a
   `derived-from` link from each draft chunk to its source node, so a
   changed node marks its chunk stale (the `STALE:` axis already exists on
   the argument graph).
4. **Hand edits.** Refused on a rendered draft (`Unsupported`, pointing at
   the source node to edit) in v1.
5. **Runtime doc**: `precis-draft-help` gains a "render from the graph"
   section.

## Explicitly NOT in scope

- Two-way sync (editing the prose and pushing changes back into nodes).
  Ruled out for v1 on 2026-09-30; if wanted later it is its own item.
- Replacing the `draft` kind, `tex`, or the existing hand-authored path.
- Prose quality beyond concatenated gists and bodies: the first render is
  a scaffold a human edits *in the graph*, not a paper.
- Ordering heuristics (topological sort over `entails`, citation order):
  v1 takes the outline's explicit order.

## Acceptance criteria

1. From an outline of five nodes (two lemmas, two findings, one measure),
   one `put(from=)` produces a draft whose chunks cite only `[fi<id>]`
   hubs and whose `derived-from` links point at exactly those five nodes.
2. Editing one source lemma marks its rendered chunk `STALE:`; a re-render
   replaces only that chunk (chunk DELETE+INSERT, never in-place).
3. `edit(kind='draft', id=<rendered chunk>, text=…)` raises `Unsupported`
   naming the source node.
4. The render of a 200-node outline completes within the MCP response
   budget or pages via `more()`.

## Target + blast radius

- `src/precis/handlers/draft.py` (from= path), `src/precis/store/
  _draft_ops.py` (rendered-chunk provenance), the walk from
  `knowledge-mesh.md`
- `src/precis/data/skills/precis-draft-help.md`
- tests: render idempotence, stale marking, refusal of hand edits

## Open questions / decisions log

- **[decided 2026-09-30, Reto]** Render-only v1: hand edits on a rendered
  draft are refused; the fork below is the escape hatch.
- **[open]** Whether a rendered draft may be forked (`copy-of`) into a
  hand-editable draft as the escape hatch. Leaning yes: the fork drops the
  `rendered_from` provenance and becomes an ordinary draft.
