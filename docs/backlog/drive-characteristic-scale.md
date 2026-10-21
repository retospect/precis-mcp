---
status: draft
pillar: 3d-design
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

## Decisions (Reto 2026-10-21)

1. A forward-only migration adds nullable
   `refs.characteristic_scale_m double precision`; no `refs.meta` key.
2. The owning handler stamps it at write (`precis_se`, `structure`, `pcb`),
   the way `pcb` stamps `meta['last_route']`; no sweep job.
3. Metres always, rendered with `utils/units.py`.
4. The migration backfills the ~1,127 design rows (1120 `structure` + 5
   `se` + 2 `pcb`); verify the `structure` Å→m conversion first.
5. `component` stays null.

Buildable now.

## Acceptance

- A design ref written after this ships carries a scale value, in metres.
- A Drive row displays it, formatted with SI prefixes, as a badge alongside
  the step-3 badges.
- `sort=scale` orders by it, nulls last.
- No per-row computation is added: the value is read with the row.
- **No filter facet.** Revisit only after the column has been visible long
  enough to say whether filtering is wanted.
