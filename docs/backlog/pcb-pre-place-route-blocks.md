---
status: draft
title: "pcb: pre-place-route blocks — generators emit real fixed copper (vias + traces), solved once per unit cell and tiled"
prio: high
model: opus
---

# pcb: pre-place-route blocks

The spec round `pcb-ewod-multitile.md` §"Cross-cutting: pre-place-route
blocks" reserved (Reto, 2026-09-13) and said must land **before slice 3
locks the generator output format**. Its four open questions are answered
here.

Ruling that opens it (Reto, 2026-09-15), on being asked whether the
generator should materialize plaza vias as fixed copper rather than
leaving them to the router:

> "Yes, fixed copper (ie vias and wires to the pads) seem right. There's a
> tight pattern we need to do that can be optimized, and it should be …
> there automatically, with all the spacing."

Three things in that, all load-bearing: **vias *and* the traces to the
pads** (not just the vias I asked about); the escape fabric is a **tight
repeating pattern worth optimizing once**; and it must be **automatic,
spacing included** — not an operator's routing chore.

## What this replaces: geometry that lies

The generator today already computes the whole escape fabric and emits it
as **footprint pad rows** — "every electrode square, every F.Cu neck stub,
and every plaza via as its own pad row" (`pcb/generators.py:18`). Pads are
the wrong primitive for a stub and a via, and the cost lands in three
places at once:

1. **The router can't see it.** One position per PIN, body first-wins, "so
   the plaza via itself is invisible to the router" (`generators.py:45`) —
   `op='route'` therefore does not route the designed B.Cu escape at all
   (**gr339236**; `pcb-ewod-multitile.md:23-32` criterion 1 UNMET).
2. **Fab export refuses, correctly.** `view='gerber'` rejects the board —
   "61 net(s) carry synthesized pad geometry". The refusal is right; the
   geometry is fake. It also means the unfabbable board cannot escape, so
   this is a blocked road, not a live hazard.
3. **DRC is unreadable.** Phantom pad copper is checked as copper. On
   `ewod-dogfood-1` that plus the layer collapse produced 196 findings of
   which the 65 clearance errors and the courtyard overlap were all one
   false-positive class.

Promoting stubs and vias from fake pads to **real `track` + `via` copper**
fixes all three by construction: the router sees real segments, the export
has real geometry to emit, and DRC checks the thing that will be
manufactured.

## The architectural crux — authored copper is an INPUT

`pcb_copper` cannot simply receive generator output. Its own contract
forbids it (`migrations/0138_pcb_boards_routes.sql:214`):

> 'DERIVED realized copper … regenerated wholesale (DELETE board''s rows +
> INSERT) per realize run, the same cascade discipline as
> chunks->embeddings. Never hand-edited, no retired_at — a realize run
> replaces, it does not soft-delete.'

And `GeneratorExpansion` has **no copper channel at all** — it carries
`components / nets / connections / footprints / features / net_classes`
and nothing else (`pcb/generators.py:253-263`).

So a generator writing into `pcb_copper` would have its fabric deleted by
the next realize run. **Decision: authored copper is an input, parallel to
`pcb_planes`** — which is the exact precedent in the same migration
("authored plane assignment per (board, layer, net)", `:227`), an authored
table that feeds derived geometry without living in it.

- New authored table **`pcb_fixed_copper`** (core migration — take the
  next free number at build time; 0163 is claimed by
  `pcb-argue-with-design.md`). Columns mirror `pcb_copper`'s geometry
  contract (`ctype` ∈ `track|via`, `layer`, `net_id`, `geom` jsonb, mm)
  plus provenance (`refdes` / generator identity + version) and
  `retired_at` — unlike `pcb_copper`, authored rows soft-delete, because
  a generator re-apply retires and re-inserts its own fabric the way
  `_pcb_apply` already diffs `canonical_params`.
- `GeneratorExpansion` gains a **`copper: list[dict]`** field; `_pcb_apply`
  routes it to `pcb_fixed_copper` scoped by generator identity, so a
  re-apply with changed params replaces exactly that generator's fabric
  and nothing else.
- **Realize replays it.** A realize run seeds `pcb_copper` from
  `pcb_fixed_copper` as pre-existing fixed segments, then routes only what
  remains. This is the meta-blocks spec's stated router posture already —
  "the router imports interior copper as pre-existing segments and routes
  only the port escapes" (`pcb-meta-blocks.md:44-46`) — so the two specs
  converge on one mechanism rather than forking.
- **Derived stays derived.** `pcb_copper`'s wholesale-regen contract is
  untouched; it simply has a second upstream. No migration edits a sealed
  file.

Relationship to `pcb-meta-blocks.md`: that spec is **library reuse** —
hand-designed blocks stored as designs and stamped by reference, and it
explicitly puts "parametric/generated blocks" out of scope
(`pcb-meta-blocks.md:79-83`). This spec is the **generated** half. They
share the fixed-copper-as-router-input mechanism above and should share
the rule-envelope gate below; they do not share storage.

## The unit cell — "a tight pattern that can be optimized"

The EWOD array's escape is one motif repeated: a **3×3 tile** of 8 driven
electrodes around a vacant centre plaza (`r % 3 == 1 and c % 3 == 1`,
`generators.py:559-567`). Solve that tile's interior once — 8 electrodes →
8 F.Cu neck stubs → 8 vias on the plaza's 45° slot ring → B.Cu fan to the
tile's local sink — and replay it per tile. Two consequences:

- **Optimization is affordable.** One cell is small enough to solve
  *well* (tightest legal spacing, matched stub lengths for uniform
  capacitive load, symmetric via ring) instead of well enough. Effort
  spent there multiplies by tile count.
- **Spacing is derived, not authored.** `_plaza_capacity` already models
  the real floor — 8 slots at 45° spacing around the plaza centre, HV
  separation, via land + annular ring (`generators.py:292-324`) — and
  pitch is already derived from it rather than chosen
  (`pcb-ewod-multitile.md:44-46`). The fabric emitter consumes that same
  function, so "with all the spacing" means *no new spacing vocabulary*:
  the existing capability floor becomes the generator of coordinates
  instead of only a validator of them.
- **Determinism is already there.** `_plaza_slot_point` / `_rim_via_point`
  are pure functions of grid indices and the array is `fixed='both'`, so
  the fabric is reproducible without a seed. This slice materializes
  positions that are already deterministic — it does not invent placement.

**Rule envelope is a hard gate** (adopted from `pcb-meta-blocks.md:46-49`):
the fabric records the stackup / layer count / clearance rules it was
solved under. Re-applying into a design whose rules moved **refuses
honestly** rather than silently keeping copper that is no longer legal.

## 9×9 — unblocked, and it is the natural shape

I previously told Reto 9×9 was held behind an odd-grid plaza rule. **That
was wrong** and the correction matters for sequencing: `r % 3 == 1` puts
plazas at {1,4,7} on a 9×9, i.e. exactly three interior tile centres per
axis — nine plazas, 72 driven electrodes, no truncation. 9×9 is the
canonical target the spec already names ("e.g. **9×9** tiled as 3×3
multitiles … the middle position empty",
`pcb-ewod-multitile.md:34-41`). The **8×8** dogfood is the awkward fit:
plazas land on the last row/column with truncated blocks. Resizing
`ewod-dogfood-1` to 9×9 needs no new lattice rule — do it with this
round's fabric, where the tiling is exact.

## Sequencing — corrected

The order I gave Reto earlier (synthesized-footprint signal → paste/drill
→ layer-aware DRC → 9×9) **inverts** under this ruling, for a concrete
reason:

- **gr341516 (layer-aware clearance/courtyard) becomes a PREREQUISITE, not
  a follow-on.** I had put it last to avoid masking the synthesized-
  footprint problem. This round *deletes* the synthesized geometry by
  construction, so there is nothing left to mask — and the fabric is
  genuinely multi-layer (F.Cu stubs, plaza vias, B.Cu fan, bottom-side
  sinks). Emitting real 4-layer copper while `rules.py::PAD_LAYER` still
  forces layer 0 would re-create the 65-false-positive flood on *real*
  copper, where it is far harder to dismiss. Layer-aware checking has to
  land first or the fabric cannot be verified.
- **gr341532 (real footprints for C639448 / C32254) stays a prerequisite.**
  The B.Cu fan terminates on the HV507 sink's pins; with `part_footprints`
  empty, `apply_real_pin_offsets` no-ops and `landpattern.offsets_for`
  falls back to `_quad(59, 0.65mm)` — a phantom perimeter. Routing real
  traces to phantom pins produces confidently wrong copper. Note there is
  no MCP verb for `store.part_footprint_put`, only `ensure_footprint`'s
  network pull.
- **gr341578 (`pads_for_ir` drops paste/mask/role/drill)** is independent
  and can land any time; it is what made `F_Paste` byte-identical to
  `F_Cu`. It matters more once copper is real, because the review surface
  is then load-bearing.

Resulting order: **gr341532 → gr341516 → slice 1 below → slice 2 → 9×9**,
with gr341578 anywhere before the fab-review step.

## Slices

**Slice 1 — the storage + emission seam (kind-wide, no EWOD behaviour
change).** `pcb_fixed_copper` migration; `GeneratorExpansion.copper`;
`_pcb_apply` routing scoped by generator identity; realize seeds
`pcb_copper` from it; DRC and `view='gerber'` read it as real copper.
Ships with **no generator emitting copper yet** — provable in isolation,
and it keeps the format-locking decision (which slice 3 waits on) separate
from the EWOD geometry work.

**Slice 2 — `ewod_pad_array` emits the fabric.** Stubs and plaza vias stop
being pad rows and become `track` + `via` rows; the per-tile B.Cu fan to
the local sink is emitted; the unit cell is solved once and replayed. The
capability ledger reports fabric per tile (emitted / refused / suppressed)
so an unroutable region is visible rather than silently unrouted — the
posture `pcb-ewod-multitile.md:225-240` already sets for escape capacity.
Suppression map (`reserve`) and merged pads must both subtract from the
fabric: a merged pad may never cover a plaza (it would short 8 nets),
which is an existing rule the emitter must now honour in copper, not just
in pad layout.

**Slice 3 — resize the dogfood to 9×9** and re-run the full path: apply →
DRC → `view='gerber'`.

## Acceptance criteria

1. `view='gerber'` on `ewod-dogfood-1` **stops refusing** — no "synthesized
   pad geometry" rejection, because there is none — and emits a loadable
   zip whose F.Cu/B.Cu/drill contents include the stubs, the plaza vias,
   and the B.Cu fan.
2. `pcb_copper` holds via rows for every plaza slot in use (today: zero)
   and the drill file contains those vias (today: mounting holes only).
3. A realize run **preserves** the authored fabric — run realize twice,
   fabric identical, only router-owned copper differs.
4. DRC on the fabric is clean array-internally, with layer-aware checking
   in place; no clearance finding is a cross-layer artifact.
5. Re-applying the generator with changed params retires exactly its own
   fabric rows; a second identical apply is a no-op (mirrors the existing
   `canonical_params` diff discipline).
6. Applying a fabric whose rule envelope mismatches the design **refuses
   with a named reason**; it never keeps illegal copper.
7. 9×9 applies with nine untruncated plazas and 72 driven electrodes.
8. gr339236 closes: `op='route'` routes the remaining nets against the
   fabric instead of ignoring an invisible via.

## Open questions this round still must answer

- **Does the fabric belong to the footprint or the board?** Emitting per
  generator-instance (above) is the assumption; the alternative is a
  reusable footprint-scoped fabric that every instance inherits. The
  per-instance choice is simpler and matches `_pcb_apply`'s existing
  identity diffing — but it duplicates rows per instance, which matters at
  9×9 × N cards.
- **DRC attribution**: does a finding inside the fabric report against the
  block/generator or the board? (`pcb-ewod-multitile.md:350-353` lists this
  as open; a generator-attributed finding is the more useful default,
  since the fix is a generator change, not a board edit.)
- **Nesting**: a thermal-zone block containing a heater serpentine plus a
  bottom-side sensor plus its plaza-slot claims is a block-in-block. Defer
  to single level for v1, consistent with `pcb-meta-blocks.md:77`.
- **Inner-layer fabric** (In1/In2) is out of scope for slice 2 — B.Cu-only
  escape, per the multitile spec's standalone posture
  (`pcb-ewod-multitile.md:238-240`). The heater's legs surfacing through
  reserved plaza slots is the first inner-layer consumer and waits.

## Not in scope

Router algorithm changes beyond accepting seeded fixed segments; the
library/stamping half (`pcb-meta-blocks.md`); parametric rule solving
(pitch derivation already exists); lateral droplet transfer across card
seams (ruled out permanently — coating break).

## Vet status

**Not yet vetted** — written directly rather than through the `ready`
agent, so the storage-shape claim (`pcb_fixed_copper` vs extending
`pcb_planes`' pattern), the migration number, and the realize-seam
assumption each want a read against current code before build.
