---
status: draft
title: a derived characteristic-scale value for design refs — sort key first, filter only if earned
prio: normal
---

# Characteristic scale for design refs

Step 5 of the Drive front-door work. **Blocked on Reto's ruling**, because
the only honest implementations need either a forward-only migration or a
new `refs.meta` key convention, and migrations are sealed once they land.

## Why this exists

Reto's original framing: "maybe it would be nice to filter by order of
magnitude (nm, µm, m selector)". The instinct is sound — the `se` corpus
genuinely spans `boxel-3nm` to a ~1.2 m unicycle, about nine orders of
magnitude in one kind — but the case for a *filter* is not yet made:

- Live prod design rows: `structure` 1120, `component` 12, `se` 5, `figure`
  4, `pcb` 2, `material` 2. An order-of-magnitude selector would partition
  ~25 non-`structure` rows.
- The collision Reto actually described — "I seek a unicycle, it's drowned
  out" — was **not** a scale collision. Measured: `title ILIKE '%unicycle%'`
  returns 14 `gripe` rows against 2 `se`. The noise was commentary *about*
  the design, and the root cause was scope (`_DEFAULT_SOURCE_KINDS`
  excluded every design kind), fixed in `9a4a9d32`.

So: build the value, show it, and only promote it to a filter if looking at
it proves the filter is wanted. A facet added before the data is visible is
how a taxonomy grows buckets nobody uses.

## What exists today

Nothing stores scale.

- `precis_se/validate.py::_characteristic_length()` computes a design's
  extent **on read**. Never persisted.
- `se`'s L0–L5 abstraction levels are IR fidelity tiers (graph → solids →
  metrics → fabrication), **not** length. Do not reuse them for this.
- `structure` is Å-native, `pcb` is mm-native. Units are per-kind
  conventions, not a declared field.
- `precis/utils/units.py` formats quantities with SI prefixes — a renderer,
  not a store.

## The constraint that shapes the design

`/drive` renders 100 rows per page (`_PAGE_SIZE`) and calls every presenter
method per row. **`_characteristic_length()` must never be called per row.**
The step-3 presenters held this line — every badge is either free off
already-loaded `ref.meta` or one batched query per kind per page. Whatever
this item does must be readable in that same batched shape, which in
practice means: precomputed at write, not derived at read.

## Open questions — these are the ruling

1. **Where does the value live?** A real nullable column
   (`characteristic_scale_m double precision`) is sortable and indexable but
   costs a forward-only migration and touches a hot table. A `refs.meta` key
   avoids the migration but sorts badly and is invisible to the query
   planner. Recommendation: real column, because the whole point is sorting.
2. **Who populates it?** Cleanest is the owning handler at write time, the
   way `pcb` already stamps `meta['last_route']` in place. That means
   touching `precis_se`, `structure`, and `pcb` handlers rather than adding
   a sweep job.
3. **One unit, or per-kind units?** Recommendation: store metres always,
   render with `utils/units.py`. Mixed units in one column is how the
   "3 nm vs 1.2 m" comparison stops working.
4. **Backfill.** 1120 `structure` + 5 `se` + 2 `pcb` rows. Small enough to
   backfill in the migration, but `structure` extents need the Å→m
   conversion to be right first.
5. **Does `component` participate?** Its titles already carry dimensions
   ("6204-2RS deep groove ball bearing (20x47x14mm)") but parsing a title
   for a physical quantity is a bad idea. Recommendation: no — leave
   `component` null rather than guess.

## Acceptance

- A design ref written after this ships carries a scale value, in metres.
- A Drive row displays it, formatted with SI prefixes, as a badge alongside
  the step-3 badges.
- `sort=scale` orders by it, nulls last.
- No per-row computation is added: the value is read with the row.
- **No filter facet.** Revisit only after the column has been visible long
  enough to say whether filtering is wanted.
