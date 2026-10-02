---
status: draft
title: a pulled datasheet's ratings, key specs, package and pin table land as page-cited facts on the part ref
pillar: memory-graph
prio: normal
model: sonnet
blocked-by: measures-substrate
---

# datasheet facts in the mesh

## Motivation / why

Reto, 2026-10-02 (ewod-pcb-2 option 1, knowledge-mesh-4/6): a pulled
datasheet joins the mesh as extracted facts, not a PDF in storage:
absolute-max and recommended ratings, key electrical specs, package and
the pin table, each citing its datasheet page and linked to the part.
The pull (fetch, ingest, `datasheet-of`) is ewod-pcb's
`pcb-datasheet-autopull.md`. The pin table is the payoff: it can be
checked against the footprint's pad map, which nothing checks today
(gr458878).

## In scope

1. **Subject.** The part ref (shipped, linkable-parts slice 1); the datasheet links
   `datasheet-of` → part.
2. **Ratings and key specs** as `measures` rows with `subject_ref_id` =
   the part ref, measurand = a taxon node, the value normalised to the
   measurand's canonical unit (`measures-substrate.md` in-scope 4), the
   printed literal and unit as provenance, conditions (e.g. `Tj=25 °C`)
   in `conditions`, and the evidence anchor on the datasheet's page
   chunk.
3. **Pin table**: one structured block per package on the part ref (pin
   number, name, function, type, page anchor), read by the gr458878
   pad-map check.
4. **Extraction**: one LLM read of the datasheet's table chunks in a
   worker lane after ingest (about one BIG-tier call per datasheet).

## Explicitly NOT in scope

- The fetch and ingest (ewod-pcb).
- A finding per fact (rejected in knowledge-mesh-4 option 3: it floods
  the claim graph).
- Non-PDF parametrics (Octopart/Nexar), a later source.

## Acceptance criteria

1. A pulled datasheet for one part yields measures rows whose values are
   in canonical units and whose anchors open the right page.
2. The pin table for the part's package is stored and the gr458878 check
   compares it with the footprint's pad map, reporting mismatches.
3. The part's fisheye shows its datasheet, its specs and its boards.

## Target + blast radius

- worker lane for extraction; `measures` writes; part ref meta
- `handlers/datasheet.py`, the pcb pad-map check (gr458878)

## Open questions / decisions log

- **[decided 2026-10-02, Reto ewod-pcb-2]** Content: ratings, key specs,
  package, pin table, page-cited, linked to the part.
- **[decided 2026-10-02, Reto knowledge-mesh-6]** Subject is a lazy part
  ref; facts are `measures` rows, so this builds after
  `measures-substrate.md`.
- **[open, risk]** Marker's table recognition on pinout and
  electrical-characteristics tables (`docs/design/pcb-0042-
  implementation.md` §Slice 3). A bad read gives wrong pins, so the
  pad-map check doubles as the extraction's first test.
