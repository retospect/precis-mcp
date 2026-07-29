# 0071 — The `component` kind: a general procurable-part store (category-as-data, made_of → material)

- **Status**: accepted (2026-07-29) · **built + verified** (this commit;
  migration `0093_component_kind.sql`). Graduated from
  `docs/proposals/component-kind.md` (passed an ADR-0048 `ready` review — 2
  blockers + 1 advisory, all resolved; the proposal is deleted per the
  proposals lifecycle, this ADR is its durable record).
- **Deciders**: Reto + agent

## Context

`material` (ADR 0070) is a CRC handbook of **bulk substances** and their
*intensive* properties (density, modulus, cost-per-mass). But a design is
built from **discrete procurable things** — bolts, hoses, pipes, beams,
gaskets, bearings, adhesives, electronic components — which carry *extensive*
facts a material does not (a manufacturer part number, a per-unit cost, an
overall length, a pressure rating) and are **made of** a material.

A `part` kind already exists, but it is the **JLCPCB/LCSC catalog**:
ingest-only, addressed by C-number, populated by the `parts_refresh` worker
from the jlcparts dump — SKU reference data for PCB assembly, not a
`put`-driven engineering store, and unable to hold a hose or a beam. We want
the general store that sits *above* it, deliberately **not merged** (see 0070
"Relation to `part`", now realized here).

The consumer set is open-ended and the categories "various" — same lesson as
`material`: no fixed per-category spec set survives contact, so both the
category vocabulary and the per-category spec vocabulary must be **growable
typed data**, not a kind-per-category and not a frozen enum.

## Decision

A new DB-backed, slug-id kind **`component`** (handle `cp`;
`is_numeric=False`), reusing `refs` for the entity and mirroring `material`'s
star schema (migration `0093`), plus a **category dimension**:

- **Component entity** — a `refs` row (`kind='component'`). `title` = name;
  `meta` = `{category, mpn, manufacturer, sku, uom, package, aliases, notes}`.
  `category` is **required** and handler-enforced against the registry; `uom`
  is the unit the component is counted/priced in (`each | m | kg | L | …`).
- **`component_categories`** — a growable typed **category registry**
  (`core | proposed`), flat in v1. Seeded with 10 `core` categories
  (fastener, hose, pipe, profile, electronic, adhesive, seal, bearing,
  fitting, laminate). An unknown `category=` mints a `proposed` category,
  never silently `core`.
- **`component_specs`** — the `material_properties` shape **plus a nullable
  `category_id`**: non-null = the spec belongs to that category; **NULL =
  universal** (`mass`, `unit_cost`, `length_overall`). A value write for
  `spec=S` on a component in category `C` is accepted only if
  `S.category_id IS NULL OR S.category_id = C` (handler-enforced
  applicability). An unknown numeric spec mints a `proposed` spec **scoped to
  the writing component's category**, so a proposed hose spec never leaks into
  fasteners.
- **`component_spec_values`** — the `material_values` fact table verbatim
  (`component_ref_id` handler-enforced to `kind='component'`, `spec_id` FK;
  value-by-type columns, `input_unit` reserved-NULL, `conditions`, `maturity`,
  `method`, per-value provenance `source_ref_id/source_chunk/source_url`,
  `as_of`). Same two btree indexes. **Per-unit cost is just the universal
  `unit_cost` spec** (canonical `USD`, `as_of`, `conditions={qty_break}`) — no
  special-cased cost column.
- **`made-of` link** — the one composition edge in v1: `component → material`
  via the `links` table, with a new `made-of`/`used-in` relation pair
  registered in the migration **and** in the `Relation` Literal +
  `_INVERSE_RELATIONS` in `store/types.py` (else mypy reds the gate). Edge
  `meta` may carry `{fraction, role}`.

Key stances:

- **Two composition axes are orthogonal.** `made-of → material` is *substance*
  composition ("this bolt is steel"); the deferred `contains → component` BOM
  edge is *structural* composition ("this gearbox contains these parts").
  **Assembly/BOM-ness is a graph property, not a spec or a category** — a
  component is a leaf "part" with no `contains` children and an "assembly"
  with them; nothing flags it on the entity, spec, or category. Category stays
  pure taxonomy.
- **v1 is canonical-units-only**, inheriting `material`'s discipline
  (`input_unit` reserved-NULL so the shared unit-conversion follow-on covers
  both kinds at once).
- **Provenance integrity is enforced** identically to `material` (chunk must
  belong to the same ref as source; chunk-level source records its chunk).
- **Bundled fix**: `as_of` — a store column + `material_value_insert` param
  since 0092 but never forwarded from `MaterialHandler.put` (dead column) — is
  now a declared `tools/core.py` `put` param wired through **both** the
  `component` and `material` handlers.

## Deferred (follow-ons, each blocked-by this)

Split out to keep v1 a clean, shippable store (see `OPEN-ITEMS.md`):

1. **BOM / assemblies** — a `contains → component` edge with quantity + a
   recursive cost/mass rollup over the assembly tree.
2. **Laminate layer structure** — ordered layers (thickness/orientation) and
   effective-property homogenization (v1 admits a `laminate` *category* but
   not the structured stack).
3. **Effective-property inheritance** — computing a component's intensive
   properties from its `made-of` material at read time (v1 records the edge,
   does not walk it).
4. **`realized_by → part` binding** — auto-linking a component to a concrete
   JLCPCB C-number + live price/stock; structured tiered price ladders land
   here.
5. **Category taxonomy tree** — parent/child categories with inherited spec
   sets (v1 is flat).
6. **Unit conversion / off-sample estimation** — shared with `material`'s
   deferrals.
7. **Runtime categorical/uncertainty writes** — runtime spec-mint is
   numeric/boolean/text-only (categoricals seed via migration); `value_low/
   value_high` columns exist without a write path yet.

## Consequences

- Three additive tables + one new relation pair; no existing
  kind/worker/route behavior changes. Forward-only migration; the
  `component_ref_id → kind='component'` constraint is handler-enforced (refs
  carries no per-kind FK).
- Incidental: the planner skill-index cap (`_SKILL_INDEX_MAX`) was bumped
  120→160 — the active-summary skill corpus had already silently exceeded 120
  (138 skills), so adding `precis-component-help` would otherwise have evicted
  another skill from the truncated index.

## Relation to `material` and `part`

Three distinct tiers, deliberately not merged: `material` (bulk substance,
intensive properties, cost-per-mass — ADR 0070); **`component`** (discrete
procurable/engineered item, extensive facts, per-unit cost, `made-of →
material` — this ADR); and the JLCPCB `part` catalog (bulk-imported
electronic SKUs, ingest-only, C-number). A `component` is *made of* a
`material`; the deferred `realized_by → part` edge will bind a generic
component to a concrete purchasable catalog SKU.
