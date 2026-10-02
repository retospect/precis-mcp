---
status: draft
title: catalog parts become lazy refs on first use; design-internal items stay addressed through their design
pillar: memory-graph
prio: high
model: opus
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

Two independently shippable slices; neither needs a migration.

**Slice 1 — the part ref and the link door.**

1. **Identity.** A part ref is `refs(kind='part', slug='<C-no>')`, slug
   the upper-case LCSC number (so `insert_ref` writes the `cite_key` row
   `get_ref` resolves by), plus a second identifier row
   `ref_identifiers('lcsc', '<C-no>')`. Title `<mfr_part> — <description>`
   from the catalog row at mint time; `set_by` the calling actor.
   Chunkless, so the embed and summary cascade never sees it. The
   reserved handle `pn<ref_id>` becomes a live universal handle with the
   first ref (`KIND_CODES` already has it).
2. **One mint step, write doors only.** New
   `Store.ensure_part_ref(lcsc, *, set_by, conn=None) -> int`:
   find by the `lcsc` identifier, else mint; race rule as
   `upsert_stub_paper` (`ON CONFLICT DO NOTHING` on the identifier, then
   re-probe). It mints only for a C-number present in `parts` and raises
   `NotFound` otherwise; an existing ref is returned whether or not its
   row is still in the catalog. `parse_link_target` never mints (it also
   serves unlink and `like=`). The add-mode link doors
   (`apply_link_ops`, `NumericRefHandler.link`) call `ensure_part_ref`
   for a `part:<C-no>` target before parsing; unlink and read paths
   resolve only.
3. **`get(kind='part', id=<C-no>)`** reads the live catalog row as today
   and, when a ref exists, appends its link ring. A ref whose row has
   left the catalog renders the ref's title and links with one line
   "no longer in the catalog (as of the last refresh)", not `NotFound`.
   A C-number with neither row nor ref still raises `NotFound`.
4. **Datasheets, dual-write.** `DatasheetHandler.edit(part_lcsc=…)`
   keeps writing `meta.part_lcsc` (read by `nanopub/evidence.py`,
   `export/docx.py`, `export/latex.py`) and also writes
   `datasheet-of` → the part ref (relation seeded in 0054).
5. **`component realized-by part`** uses the existing `realized-by` /
   `realizes` pair (migration 0156, already the cad catalog-part sync's
   design → component edge). Only the part ref as a target is new.
6. **Runtime doc.** The part skill says a part is linkable, how it mints
   and the catalog-absent case; `precis-relations` gets one line under
   `realized-by` for component → part.

**Slice 2 — board edges and backfill.**

7. **Board parts.** At the end of `store/_pcb_ops.py::_pcb_apply` (the
   one write path; generators and `ingest/pcb_epro.py` drive it too),
   reconcile the board's `contains` edges to the set of distinct
   `pcb_components.part_lcsc` on the board, in the same transaction: one
   edge per (board, part), `links.meta = {refdes: [...], qty: n}`
   rewritten each time; a part no longer on the board loses its edge.
8. **Backfill.** A CLI verb (`precis pcb link-parts [--dry-run]`) that
   runs the same reconcile over every live board; idempotent; dry run
   first on prod.

## Explicitly NOT in scope

- Eager refs for the whole catalog.
- A ref per design-internal item (per pcb instance, per se element);
  a link to one is the design ref plus the item's name.
- Minting from the BOM export (`pcb/export.py::bom_csv` is a pure
  renderer; slice 2's edges already cover the BOM).
- Datasheet facts as nodes: `datasheet-facts-mesh.md`.
- Changing the catalog refresh, or removing `meta.part_lcsc`.

## Acceptance criteria

Slice 1:
1. `link(kind='memory', id=M, target='part:C25804', rel='related-to')`
   mints exactly one part ref; a second link to the same C-number reuses
   it; two concurrent first links converge on one ref.
2. `link(..., target='part:C99999999')` for a C-number not in `parts`
   raises `NotFound` and mints nothing; `link(..., mode='remove',
   target='part:C25804')` and `like='part:C25804'` never mint.
3. `get(kind='part', id='C25804')` shows the catalog row and, once
   linked, the link ring; after the row leaves the catalog (simulated
   staging swap) it shows the ref with the "no longer in the catalog"
   line instead of raising.
4. `edit(kind='datasheet', id=…, part_lcsc='C25804')` sets
   `meta.part_lcsc` and writes `datasheet-of` → the part ref; the
   nanopub/docx/latex readers are unchanged.
5. `link(kind='component', id=…, rel='realized-by', target='part:C25804')`
   shows in both refs' rings.
6. `pn<ref_id>` resolves as a universal handle.

Slice 2:
7. `_pcb_apply` adding two instances of C25804 (R1, R2) leaves one
   `pcb contains part` edge with `{refdes: ['R1','R2'], qty: 2}`;
   removing both deletes the edge; swapping R1's part moves it.
8. The board's and the part's `fisheye+1hop` each list the other (verify
   `contains` renders for `pcb`; add it to the ring registry if not).
9. `precis pcb link-parts --dry-run` on prod reports boards, refs to
   mint and edges to write; the real run is idempotent.

## Target + blast radius

- `src/precis/store/` (`ensure_part_ref`; `_pcb_ops.py::_pcb_apply`
  reconcile)
- `src/precis/handlers/_link_tag_ops.py`, `handlers/_numeric_ref.py`
  (add-mode mint call), `handlers/_link_target.py` (resolve only)
- `src/precis/handlers/part.py` (get with ref + ring),
  `handlers/datasheet.py` (dual-write)
- a `precis pcb link-parts` CLI verb
- skills: part help, `precis-relations`
- no migration

## Open questions / decisions log

- **[decided 2026-10-02, Reto knowledge-mesh-6]** Option 1: lazy part
  refs, design-internal items addressed through their design.
- **[decided 2026-10-02, after the ready vet]** The vet's seven blockers
  are resolved above: no migration (`realized-by` exists, 0156); minting
  in one write-door step, never in `parse_link_target`; identity = slug
  C-number + `lcsc` identifier; the hook is `_pcb_apply` over
  `pcb_components.part_lcsc`, with removal handled by reconcile; one
  edge per (board, part) with a refdes list; BOM dropped; datasheets
  dual-write. Split into slices 1 and 2; model opus.
- **[decided 2026-10-02, ready vet]** `contains` is the board → part
  relation: acyclic and transitive since 0180, no domain/range, and a
  part has no outgoing edges, so it cannot close a cycle.
- **[open, non-blocking]** A C-number not in the catalog refuses to mint
  (AC 2). Revisit if a hand-authored board needs a part the catalog has
  dropped; the alternative is minting from the board's own snapshot.
