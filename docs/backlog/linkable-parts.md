---
status: draft
title: catalog parts become lazy refs on first use; design-internal items stay addressed through their design
pillar: memory-graph
prio: high
model: sonnet
---

# linkable parts — a part ref on first use, items through their design

## Motivation / why

Reto, 2026-10-02 (knowledge-mesh-4 and its addendum): "all parts and
items and designs should be able to be part of the mesh"; ruled option 1
of knowledge-mesh-6 ("this is what we do"). Every design kind is already
a ref and linkable: se, pcb, cad, structure, make, component, material,
rxn, pathway, route, protein, estimate, figure, checklist. What is not
linkable:

- **Catalog parts.** `kind='part'` is a read-only lookup over the
  `parts` table (LCSC/JLCPCB, ~100–300k rows, replaced daily by an atomic
  staging-table swap, `store/_pcb_ops.py`). It has a reserved handle code
  (`pn`) and 0 refs; `link(target='part:C25804')` fails with "resolves to
  no live ref". `handlers/datasheet.py` stores `part_lcsc` in meta for
  that reason.
- **Items inside designs.** `pcb_instances.part_lcsc` is plain text; se
  elements are addressed by name inside their design
  (`precis_se/handles.py`: "never per-row").

## In scope

1. **Lazy part refs.** The first reference to a catalog row mints a
   chunkless `part` ref with identity `ref_identifiers('lcsc', '<C-no>')`,
   the paper-stub pattern (`upsert_stub_paper`). Minting happens in
   `parse_link_target` for `part:<C-no>`, at pcb placement, at datasheet
   pull and on a BOM line. Nothing foreign-keys into `parts`, so the
   daily swap cannot orphan a ref; a ref whose row has left the catalog
   stays and says so.
2. **`get(kind='part')`** keeps reading the live catalog row and appends
   the ref's links as rings (the eye's link ring).
3. **`component realized-by part`.** New relation pair `realized-by` /
   `realizes` (the edge ADR 0071 deferred). Components stay for authored,
   non-catalog items.
4. **Board parts.** Placing a part writes `pcb contains part` with
   `refdes` and quantity in `links.meta`; a board's fisheye lists its
   parts and a part's fisheye lists its boards.
5. **Backfill.** Mint part refs and `contains` edges for the parts on the
   existing prod boards (2 today).
6. **Runtime doc.** `precis-part-help` (or the pcb skill) says a part is
   linkable and how; `precis-relations` gains `realized-by`.

## Explicitly NOT in scope

- Eager refs for the whole catalog.
- A ref per design-internal item (per pcb instance, per se element).
  A link to one is the design ref plus the item's name.
- Datasheet facts as nodes: `datasheet-facts-mesh.md`, which builds on
  this item and on `measures-substrate.md`.
- Changing the catalog refresh.

## Acceptance criteria

1. `link(kind='memory', id=M, target='part:C25804', rel='related-to')`
   succeeds, minting exactly one part ref; a second link to the same
   C-number reuses it.
2. A catalog refresh (staging swap) leaves every part ref and edge
   intact, and `get(kind='part', id='C25804')` still shows the live row.
3. Placing a part on a board writes one `pcb contains part` edge with the
   refdes; the board's and the part's `fisheye+1hop` each list the other.
4. A component linked `realized-by` a part shows it in both rings.
5. The backfill on prod reports refs minted and edges written, and is
   idempotent.

## Target + blast radius

- `src/precis/handlers/part.py`, `src/precis/handlers/_link_target.py`
- `src/precis/handlers/pcb.py` placement path, `pcb/export.py` (BOM)
- `src/precis/handlers/datasheet.py` (link instead of `part_lcsc` meta)
- one forward-only migration seeding `realized-by` / `realizes`
  (goes to the orchestrator's gate, not a qland)
- skills: part/pcb help, `precis-relations`

## Open questions / decisions log

- **[decided 2026-10-02, Reto knowledge-mesh-6]** Option 1: lazy part
  refs, design-internal items addressed through their design.
- **[open, non-blocking]** Whether `contains` (acyclic since migration
  0180) is the right board→part relation, or a dedicated `uses-part`.
  Default `contains`: a board is an assembly of its parts.
