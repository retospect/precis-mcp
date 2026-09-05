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

**Revision 2026-09-05 (coordinated with the se track): mint per-consumer,
not all-at-once.** The all-three-in-one-migration plan assumed se_bom
would consume `realizes` — it won't: the se plugin stores its
block→bought-thing binding in plugin-local tables keyed by block *name*
(`se_blocks.bound_kind`/`bound_design`, `se_bom`), because its
retire-all/reinsert-all persist rebuilds row ids on every save — a
`links` row would have nothing stable to point at. Same story for se's
analysis layer (`se-feasibility-and-cost.md`). So the cad track is the
honest consumer for all three, and each row enters with its consumer:

- **`analyzed-by`** (+ inverse `analysis-of`): enters with
  `attached-models-layer.md` v1 — the next migration this track mints.
- **`realizes`** (+ `realized-by`): waits for the cad-side realization
  write (catalog atoms → bom, `cad-machine-spec.md` parallel track).
- **`made-by`** (+ `makes`): waits for `make-tree-vs-design-tree.md`.

Migration numbering: 0152 is claimed by se (`component_geometry_specs`);
pattern to copy is `0095_component_contains.sql`; runtime validation
needs no Python change (`store.valid_relations()` reads the DB; the
`Relation` Literal in `store/types.py` is optional polish). Terminology
note for readers near se: se's `demands_relation`
(`precis_se.joints.MECHANISMS`) is a *tolerance* relation between named
measures, unrelated to the `relations` table.

## Non-goals

- No generic "graph query language" — `view='links'` + search is the
  navigation surface; cards surface the one-hop neighborhood (a design's
  card already carries its ports since cad slice 2; parents/children and
  attached-model summaries join it as those layers land).
- No backfill job in this item — `cad_save` emits on next save; a one-shot
  re-save sweep of existing cad refs is a single ops command, noted here so
  it isn't forgotten.
