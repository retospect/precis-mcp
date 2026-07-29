# 0070 — The `material` kind: a CRC-handbook materials-properties store with per-value sources

- **Status**: accepted (2026-07-29) · **built + verified** (this commit;
  migration `0092_material_kind.sql`). Graduated from
  `docs/proposals/materials-handbook-kind.md` (passed an ADR-0048 `ready`
  review; the proposal is deleted per the proposals lifecycle — this ADR is
  its durable record).
- **Deciders**: Reto + agent

## Context

We need a place to record engineering material properties as we learn them,
each value carrying a **validatable source**, so downstream simulators (and
humans) can trust *and check* the numbers they consume. The consumers are
open-ended and "various" — thermal, CFD, optical, structural, electrochemical
— so no fixed property set survives contact.

The data is genuinely tidy/long, like the CRC Handbook: `(material, property,
value, source)`, grown one measurement at a time. It is **not** a fixed wide
row per material — different materials expose different properties, the same
property takes different values under different conditions and from different
sources, and the point is to attach provenance to each *number*, not to a
material as a whole.

## Decision

A new DB-backed, slug-id kind `material` (handle `ma`; `is_numeric=False`),
reusing the `refs` table for the entity, with a **star schema** (migration
`0092`):

- **Material entity** — a `refs` row (`kind='material'`); `meta` holds
  aliases + `material_class`.
- **`material_properties`** — a **typed, growable registry**, not a frozen
  enum. Each property declares `canonical_unit`, `dimension`, `value_type`
  (`quantity | ratio | categorical | boolean | text`), optional
  `allowed_values` / `standard_ref`, and a `status` (`core | proposed`). The
  anti-junk-drawer discipline is *dimensional typing*, not enumeration: new
  simulators mint `proposed` properties at runtime (declaring unit +
  dimension) with no code change; a curated `core` tier is the stable
  contract and is promotable. Seeded with 17 `core` + 2 `proposed`
  properties.
- **`material_values`** — the fact table, one row per measurement:
  `value_num` (in the property's canonical unit) or `value_text`/`value_bool`
  by value-type; `conditions` (JSONB — sampling the same (material, property)
  at many conditions *is* how a curve is stored, for free); `maturity`
  (`commercial | lab | speculative`, a property of the *measurement*, not the
  material); and **per-value provenance** — `source_ref_id` (a
  `paper`/`datasheet` ref) + `source_chunk` (grounding to the exact source
  span, reusing the `citation` pattern) or a bare `source_url`, with `as_of`
  for cost.

Key stances:

- **Multiple values per (material, property) is a feature** — the handbook
  shows the spread; no canonical number is picked at write time.
- **v1 is canonical-units-only** — no unit conversion, no `units=` read param,
  no `pint`. Temperatures store canonical **Kelvin** (absolute scale,
  future-proof). The registry still records each property's canonical unit +
  dimension so the conversion follow-on has what it needs.
- **Provenance integrity is enforced**: a `chunk=` must belong to the same ref
  as `source=` (else rejected); a chunk-level `source=` records its chunk
  rather than silently degrading to ref granularity.

## Deferred (follow-ons, each blocked-by this)

Split out to keep v1 a clean, shippable store (see `OPEN-ITEMS.md`):

1. **Unit-conversion layer** — a `pint`-backed `utils/units.py` helper,
   convert-on-write, and a `units=` read param (the "serve every simulator in
   its own units" layer).
2. **Off-sample estimate / fitting layer** — interpolation, published-model
   evaluation (a `model` value-type + `model_spec`), and an `estimate=` flag,
   with a trust-ordered read (model → bracketing points → labeled in-range
   interpolation; no silently-invented fit).
3. **A general `part`/`component` kind** — bolts/hoses/pipes/beams/electronics/
   adhesives/laminates as one kind with **category as growable typed data**
   (not a kind-per-category), linked `made_of → material`. Distinct from, and
   layered above, the existing JLCPCB-native `part` catalog kind.

## Consequences

- Two additive tables + a new kind; no existing kind/worker/route behavior
  changes. Forward-only migration; no `refs` per-kind FK, so the
  `material_ref_id → kind='material'` constraint is handler-enforced.
- Known v1 trims (fast-follows, not defects): runtime `proposed`-mint is
  **numeric-only** (categoricals seed via migration); no write path for
  `value_low`/`value_high` (point values only — uncertainty ranges deferred).

## Relation to `part`

Three distinct tiers, deliberately not merged: `material` (bulk substance,
intensive properties, cost-per-mass — this ADR); the existing JLCPCB `part`
catalog (electronic components, bulk-imported SKUs); and a future general
`component` kind (discrete procurable parts, per-unit cost, `made_of →
material`). A part is *made of* a material; it inherits intensive properties
through the link and records only its extensive facts (geometry, ratings,
MPN, cost).
