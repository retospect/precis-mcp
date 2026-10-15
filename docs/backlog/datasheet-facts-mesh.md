---
status: draft
title: a pulled datasheet's ratings and key specs land as page-cited measures rows on the part ref
pillar: memory-graph
prio: normal
model: sonnet
blocked-by: measures-substrate
---

# datasheet facts in the mesh — remainder

Shipped 2026-10-14 (worktree ds-facts): the pin table, required externals
and conditional (strap/address) tables as one structured block per
package under `refs.meta.datasheet_facts` (`src/precis/pcb/datasheet_facts.py`),
the `datasheet_facts` extraction job (enqueued next to `datasheet_pull`
at put time), and the `part:<C#>~<pin>` link selector. The gr458878
pad-map check and pcb-datasheet-review slices 5-7 read
`part_facts(store, lcsc)`.

## Remaining scope

- **Ratings and key specs** as `measures` rows with `subject_ref_id` =
  the part ref, measurand = a taxon node, value normalised to the
  measurand's canonical unit, printed literal + unit as provenance,
  conditions (`Tj=25 °C`) in `conditions`, evidence anchor on the
  datasheet's page chunk. The extraction job's prompt doesn't emit them;
  this adds a measures-writing stage to `datasheet_facts` (or a sibling
  job) plus the measurand taxa (supply voltage abs-max/recommended, …).
- AC leftover: the part's fisheye shows its datasheet, its specs and its
  boards (the facts block already rides the part ref's meta — verify the
  eye renders it readably).

## Decisions carried from the landed slices

- Subject is the lazy part ref; pin provenance is the page-anchored pin
  table on the part ref, not one finding per pin (knowledge-mesh-4).
- Facts are trusted by default; an unclear/contradictory read is a low
  `confidence`, not a refusal (Reto 2026-10-13).
- **[open, risk]** Marker's table recognition on pinout and
  electrical-characteristics tables — a bad read gives wrong pins; the
  pad-map check doubles as the extraction's first test. Unverified on
  prod data yet.
