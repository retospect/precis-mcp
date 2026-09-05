---
status: draft
title: design-graph relations — one migration minting the typed edges the multi-scale design mesh hangs on
prio: high
model: opus
---

# Design-graph relations — `contains` / `realizes` / `analyzed-by` / `made-by`

Design session 2026-09-04 (Reto + agent), imperative-plotting-hare worktree.
Resolves `cad-machine-spec.md` open decision 4 (**yes** — Reto: "a dense
graph with all the info within a hop or two is our goal") and widens it: not
one relation but the *set* the whole multi-scale design mesh hangs on, minted
in a single forward-only migration so cad, se, nm, structure, and pcb all
link against the same vocabulary. Best-practice grounding:
`perplexity-research:317035` (the report's cross-cutting principle is
*local-global linkage via typed edges*; its per-area patterns each name one
of these relations).

## The relations

**Update 2026-09-05:** `contains`/`part-of` already existed (migration
0095, component kind) — the cad emission SHIPPED with cad slice 3
(`CadHandler._sync_contains`: `use` lines → design→design `contains`
links, pruned on drop). Remaining in this item: the migration for the
three relations below, each landing WITH its first consumer.

One migration inserts the missing rows into `relations(slug)` (the FK
target of `links.relation` — the reason this needs a migration at all):

- ~~`contains`~~ — SHIPPED (see update above). Strictly the design tree —
  NEVER the make-order tree (`make-tree-vs-design-tree.md`).
- **`realizes`** — block → the thing that makes it real (axis 1): a
  manufacturing-mode realization, a bought `component`/se_bom part, a
  synthesized molecule. Many-to-one is legal (candidate realizations).
- **`analyzed-by`** — block → an attached model result (axis 3): a
  finding/estimate carrying an FEA / multiphysics / DFT / ML-potential
  number with validity scope (see `attached-models-layer.md`).
- **`made-by`** — block → a step in a make-order (assembly/synthesis) tree.
  The cross-tree alignment edge of the EBOM/MBOM bipartite pattern;
  explicitly many-to-many.

## First consumers (ship with the migration, not after)

Registry hygiene rule from se-kind: a relation enters only with its first
consumer.

- `contains`: SHIPPED — `cad_save`/`derive` sync design→design links
  from the `use` lines; `view='links'` shows the assembly tree.
- The other three ship dark with the row but gain their first consumers from
  their owning backlog items (`attached-models-layer.md`,
  `make-tree-vs-design-tree.md`, se_bom's realization write). Minting them
  in the same migration is deliberate: relation rows are pure vocabulary,
  and a second migration per consumer is churn without safety.

## Non-goals

- No generic "graph query language" — `view='links'` + search is the
  navigation surface; cards surface the one-hop neighborhood (a design's
  card already carries its ports since cad slice 2; parents/children and
  attached-model summaries join it as those layers land).
- No backfill job in this item — `cad_save` emits on next save; a one-shot
  re-save sweep of existing cad refs is a single ops command, noted here so
  it isn't forgotten.
