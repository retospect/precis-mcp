---
status: draft
title: Flat-pack furniture generator — a plywood box with shelves, joinery from sheet thickness, nested onto one sheet for laser or Maslow CNC
pillar: 3d-design
prio: normal
---

# Flat-pack furniture generator — box + shelves from one plywood sheet

Reto, 2026-10-03: "make a basic box with shelves from plywood of some
thickness, and make the pattern fit on a sheet to be laser cut or cut with
a Maslow CNC machine." Filed by the orchestrator as a specced draft; the
owning thread (se-machine-design) argues the open questions below before it
goes `ready`.

## Motivation / why

The se kinds realise machines as solids and the organic-print route
realises them as printed fields (`structural-solution-space.md` §Slice 4
bridge). Neither produces the third common way a maker builds a thing: a
set of flat parts cut from one sheet and slotted together. A box with
shelves is the smallest useful member of that family — it exercises every
piece the family needs (part outlines from a 3-D intent, joinery derived
from the sheet thickness, kerf, nesting onto a stock sheet, a cut file a
machine accepts) and nothing else.

## In scope

1. **An se generator `flatpack_box(W, H, D, t, shelves=[...])`** in
   `precis_se.atomic.generators` (beside `authored_foot` and the other
   generators): outer box (two sides, top, bottom, back) plus N shelves at
   given heights, all parts `t` thick, `t` taken from the declared plywood
   (6 / 9 / 12 / 18 mm nominal; actual thickness is a parameter because
   plywood runs under nominal).
2. **Joinery derived from `t`, not drawn by hand.** Default: through-tabs
   on sides/top/bottom with matching slots (finger joints), shelves in
   dadoes or captive tabs; tab length = `t`; a `fit` parameter adds
   clearance per joint (laser: ~0.1 mm; Maslow: ~0.3–0.5 mm) and a `kerf`
   parameter offsets every cut path by half the kerf. Dog-bone/T-bone
   relief on inside corners when the cutter is round (CNC), none for the
   laser.
3. **Nesting onto a stock sheet** — the part set placed on one rectangle
   (laser bed e.g. 600 × 400 mm; Maslow full sheet 2440 × 1220 mm, 4 × 8 ft)
   with a `spacing` ≥ cutter diameter; refuse with the shortfall if the
   parts do not fit; a rectangular-packing heuristic is enough (parts are
   rectangles with tabs).
4. **Cut file export**: SVG (laser) and DXF (Maslow / any CNC), units mm,
   one layer per operation (through-cut, dado/pocket), path direction and
   order set so the small inner cuts come before the outline.
5. **Checks** (hard tier, se DRC style): every slot has a mating tab of the
   same width; no part is thinner than `2·t` in any direction; shelf span
   vs plywood deflection under a declared shelf load (simple beam, E for
   plywood ≈ 8–10 GPa) warns past 3 mm sag; sheet utilisation reported.
6. **The 3-D assembly is a real se design**: the realised box is a solid
   the viewer shows and `view='drc'` checks, so a generated flat-pack is
   also a machine-design input (a cabinet for a mechanism, an enclosure).

## Explicitly NOT in scope

- Doors, drawers, hinges, hardware, finger pulls (follow-ons once the box
  family exists).
- Toolpaths / G-code. Export stops at the cut file; the machine's CAM
  (LightBurn, Maslow's web control) takes it from there.
- Non-rectangular parts, curved shelves, living hinges, mitres.
- Multi-sheet layouts (refuse and say how much does not fit).
- Grain direction optimisation (a flag for later; plywood is near-isotropic
  in-plane).

## Acceptance criteria

- `flatpack_box(W=800, H=1000, D=300, t=12, shelves=[300, 650])` realises
  a solid; `view='drc'` reports no slot without a mating tab; parts list
  names 7 parts with dimensions.
- The nested SVG for a 600 × 400 mm laser bed either fits all parts with
  the given spacing or refuses naming the shortfall in mm²; the DXF for a
  2440 × 1220 mm sheet fits the example box with ≥ 40 % utilisation
  reported.
- Changing `t` from 12 to 18 changes every tab/slot/dado dimension and
  nothing is hand-edited; changing `kerf` from 0 to 0.2 moves every path
  by 0.1 mm outward on the part side (test by area difference).
- A dog-bone appears on every inside corner when `cutter_d > 0`, none when
  `cutter_d = 0` (laser).
- One physical check before `ready` → shipped: cut the example at toy
  scale (W=200, t=3 mm, laser) and confirm the tabs seat; record the
  `fit` that worked in the generator's docstring.

## Target + blast radius

`src/precis_se/atomic/generators/` (new module), se export (`view='export'`
gains `format='svg-cut'` / `dxf-cut'` or a sibling verb), se DRC (new
check family), skill `precis-se-help` (generator row) + a new
`precis-se-flatpack-help`. No migrations, no worker changes. Risk: the
nesting heuristic is the only non-trivial algorithm; rectangle packing is
fine for a box.

## Open questions / decisions log

- Which plywood thickness and which machine first — Reto's own laser bed
  size and whether the Maslow is the real target (sheet 2440 × 1220 mm
  assumed).
- Joinery default: finger joints throughout, or tabs on the carcass and
  dadoes for shelves (dadoes need a pocket operation the laser cannot do —
  then the laser variant uses captive tabs through the sides).
- Whether the generator lives in se (3-D first, pattern derived) — proposed
  here — or starts 2-D only. Proposed: se, because the 3-D solid is what
  makes it compose with the rest of machine design.
