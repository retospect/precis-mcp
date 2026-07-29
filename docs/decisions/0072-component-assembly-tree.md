# 0072 — The component assembly tree: `contains → component` BOM with rollup + consistency queries

- **Status**: accepted (2026-07-29) · **built + verified** (this commit;
  migration `0095_component_contains.sql`). Graduated from
  `docs/proposals/component-assembly-tree.md` (passed an ADR-0048 `ready`
  review — 1 blocker + 5 advisories, all resolved; the proposal is deleted per
  the proposals lifecycle, this ADR is its durable record).
- **Deciders**: Reto + agent

## Context

`component` (ADR 0071) stores discrete procurable things, their specs, and
`made-of → material` (substance composition). It could not express **structural
composition** — an enclosure *contains* a bracket, four screws, and a control
board. Designs are multi-level assemblies, and two questions fall out that a
flat parts list answers poorly: **rollup** (what does this assembly cost/weigh,
summing part × quantity, recursively) and — the motivating one —
**consistency/propagation** ("are the washer *and* the screw both galvanized?"),
which needs a walk of the tree comparing a spec across its members.

The design was captured YAGNI in `OPEN-ITEMS.md`, then picked up. The **tree is
the source of truth; a flat BOM is a *view*** of it.

## Decision

A new `contains` / `part-of` relation pair over the existing `links` table
(migration `0095`; no new tables), with the composition graph read as a tree:

- **`contains` (parent → component child), inverse `part-of`** — registered in
  the `relations` table and added to the `Relation` Literal + `_INVERSE_RELATIONS`
  in `store/types.py` (lockstep, or mypy reds the gate). Structural composition,
  **orthogonal to `made-of`** (a bracket is *made of* aluminium; an enclosure
  *contains* the bracket). Endpoints handler-enforced to `kind='component'`
  (links carry no per-kind FK).
- **Write** via `put(kind='component', id=<parent>, contains=<child>,
  qty=<int ≥ 0>, ref_designator=<opt>)`, mirroring `_put_made_of`. Quantity +
  reference designator live in the link `meta` (`add_link(merge_meta=True)`, so
  re-put updates in place). **`qty=0` removes** the edge (add/update/remove
  through one param, with an explicit echo); **`qty` omitted on an existing edge
  preserves** its current qty (no destructive default-to-1); a new edge defaults
  to qty 1.
- **Cycle guard** — `add_link` guards only a direct self-loop, so a store-level
  BFS ancestor walk (`links_for(direction='in', relation='contains')`, bounded
  by a visited-set) rejects a transitive cycle at write time (BadInput). The
  graph is a DAG by construction.
- **Reads** — `view='tree'` (nested DFS render with qty/ref); `view='bom'`
  (flatten to leaves, qty multiplied down each path and summed per distinct
  leaf, plus rollup totals for `unit_cost`/`mass` with an `N of M leaves`
  coverage note); `view='bom', spec=S` (the **consistency query**: each leaf
  annotated with its current value of S, plus a uniformity summary — uniform vs
  MIXED with counts vs not-recorded). A component with no `contains` children is
  automatically a **leaf** — the PCB-leaf boundary (ADR 0071): a PCBA is one
  line item, the rollup never descends into the `pcb`/`part` subsystem.

Key stances:

- **Representative-value rule** — a `(component, spec)` may hold many values
  (`unit_cost` especially, via `as_of` + price-break `conditions`). One store
  authority `component_current_spec_value` resolves the single "current" value
  (`ORDER BY as_of DESC NULLS LAST, created_at DESC LIMIT 1`); both the rollup
  and the consistency annotation route through it exclusively. Price-break-aware
  costing (`conditions.qty_break`) is deliberately **out of scope** — v1 uses
  the latest single `unit_cost`.
- **Assembly-ness is a graph property**, not a spec or category (no
  `is_assembly` flag, no `assembly` category).
- **`spec` is a declared `get()` param** (not extras-only), mirroring how
  `search()` promotes its kind-specific filters so strict-schema MCP clients
  don't strip them — with a wire-level test through the tool layer.

## Deferred (follow-ons, each blocked-by this)

- **Comparator / violator query** — an explicit pass/fail against a target
  (`spec='grade' min=8.8` → boolean verdict + violator list), on the same tree
  walk. v1 ships the uniformity summary (answers the categorical "all X?" case).
- **Price-break-aware costing** — qty-break-tiered `unit_cost` selection and
  **quantity-unit reconciliation** (mixed per-each vs per-metre children); ties
  into the `calc`/units path.
- **Optional-part modelling** — a genuine "included at qty 0" line item (v1's
  `qty=0` means remove).
- Pre-existing `component` follow-ons unchanged: effective-property inheritance,
  `realized_by → part`, laminate layers, category taxonomy.

## Consequences

- One new relation pair, no new tables; no existing kind/worker/route behavior
  changes. Forward-only migration.
- The BOM/consistency reads assume the representative-value rule above; any
  future "current value" need across the codebase should reuse
  `component_current_spec_value` rather than re-deriving a tie-break.

## Relation to prior ADRs

Builds directly on ADR 0071 (the `component` kind) and inherits its PCB-leaf
boundary. `contains` (structural) and `made-of` (substance, 0071) are the two
orthogonal composition axes; a component is *made of* a material and *contains*
sub-components.
