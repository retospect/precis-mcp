---
status: draft
title: "Footprint-driven outline modification: card-edge interfaces + edge cutouts (mid-mount connectors)"
prio: medium
---

# Card-edge interfaces & footprint cutouts integrated into the board shape

User request (2026-09-02, round-5 follow-on): some footprints carry
"card edge interfaces" (gold fingers) and/or a cutout that modifies
the pcb from square — the footprint must be able to reshape the board
outline. Find a part and test it out.

## Measured reality (2026-09-02, live EasyEDA fetches)

Two real candidate parts fetched through `pcb/easyeda.py::fetch_component`:

- **C2765186** "TYPE-C 16PIN 2MD(073)" — mid-mount USB-C (needs a
  board-edge notch). Footprint carries 16 PADs, 2 NPTH `HOLE`
  primitives (locating pegs), silk TRACKs, lead/shell SOLIDREGIONs on
  layers 100/99. **The cutout itself is not drawn anywhere** —
  datasheet-only for this part.
- **C963213** "GT-USB-7025" — carries a U-shaped cutout as three
  SOLIDREGIONs, but on layer **12 (Document)**, not 10 (BoardOutLine).

So: real footprints encode cutouts inconsistently (Document layer, or
not at all); neither candidate uses the dedicated BoardOutLine layer.
Consequences for the design:

1. **An authored contract is primary, and NO path interprets a
   drawing** (user, 2026-09-02: "llm interpreting technical drawing is
   difficult at best"). Three geometry sources, in trust order:
   (a) authored numeric `outline_ops` — a human reads the datasheet's
   *stated dimensions* ("notch 8.94 × 4.6 mm") and types one JSON op;
   (b) footprint BoardOutLine-layer (10) vectors — exact machine
   coordinates, no interpretation, only a classification decision
   (which this layer's semantics already answer); (c) Document/
   Mechanical-layer (12/15) extraction — v2 only, and never silently:
   an extracted op lands as `proposed`, is excluded from gerbers/DRC
   until confirmed, and confirmation is the existing visual loop
   (render the board, look at the notch) plus mechanical sanity checks
   (cutout must intersect the authored ring boundary; must abut the
   part's own courtyard; must fit within the part bbox + margin).
   The LLM at most *proposes*; render + checks + human peek dispose.
   The user can supply footprints; they plug into (a)/(b) directly.
2. Intake currently drops ALL of it: `parse_component` keeps only
   PADs (+TRACK as a courtyard bbox hint) and discards
   SOLIDREGION/HOLE/ARC. NPTH locating holes (present on C2765186)
   are dropped too — a mid-mount part cannot even be placed correctly
   today, independent of the outline question.
3. Found while testing: `parse_component` hard-rejects current-format
   official footprints (docType moved to `head`, became a string) —
   gr293451, fix independently of this item.

## Design sketch

**Footprint model** (`catalog.py`/`footprint.py` canonical dict):
- `outline_ops`: list of `{op: "subtract", polygon: [[x,y],...]}` in
  footprint frame (v1: subtract only — notches/slots). Transformed by
  instance pose at realization.
- `npth_holes`: `{x, y, drill}` list (locating pegs; feeds the
  existing mounting-hole/NPTH machinery per instance).
- Pads gain optional `edge_finger: true` (gold fingers): mask-free,
  flush to a board edge, usually mirrored top/bottom pairs.

**Placement**: an instance with `outline_ops` or `edge_finger` pads is
**edge-locked** — legal poses put the designated footprint edge on an
outline segment (position along the edge + which edge become the free
variables; rotation snaps to the edge normal). New legality term +
move class in the anneal, same shape as the existing side/rotate moves.

**Outline realization — ONE derivation site.** Derived outline =
authored ring ⊖ union of placed instances' transformed `outline_ops`.
One function (ir-level) computes it; placer legality, router keepouts,
DRC `outline_containment`/`silk_edge_clearance`, pour clipping, and
gerber edge export ALL consume it. The recurring defect generator in
this build is one rule in two drifting sites — do not let any consumer
keep reading the authored ring directly.

**DRC**: edge-finger pads are exempt from copper-to-edge clearance
along their designated edge (that is their function); everything else
gets the derived outline. New check: an `outline_ops` instance whose
cutout does not intersect the authored ring boundary is a placement
error (a notch floating mid-board), not silently legal.

**Out of scope v1**: finger bevel/chamfer (fab note), castellations,
outline *additions* (op:"union" tabs), and ALL Document/Mechanical-
layer extraction (the source-(c) proposed/confirmed machinery above —
v1 ships with authored numeric ops + layer-10 vectors only).

## Test plan ("find a part and test it out")

- Unit fixture: current-format C2765186 response trimmed into
  `tests/fixtures/pcb/` (also serves gr293451's regression).
- Board fixture: small 2-layer board + the mid-mount USB-C with its
  datasheet cutout authored as `outline_ops` (8.94 × ~4.6 mm notch)
  + its 2 NPTH pegs + a few passives. Assert: derived outline is
  non-convex with the notch at the placed pose; no other instance or
  routed copper inside the notch; gerber outline emits the derived
  polygon; fab SVG renders the notch (visual check).
- Card-edge fixture: hand-authored gold-finger part (e.g. 2×6 fingers,
  PCIe-style key notch via `outline_ops`), asserting mask openings and
  edge-clearance exemption on the finger edge.

## Open questions (user input welcome)

- v1 part source: user-provided footprints (they offered) vs. the two
  fetched candidates — both work under the authored contract.
- Should edge-locking be a hard placement constraint (v1: yes,
  simplest) or a soft cost term?
