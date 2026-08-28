---
status: draft
title: The gerber export tail is unwired — no design can currently produce manufacturing output
prio: high
model: opus
---

# Fab output is unwired

Found 2026-08-28 while building the SVG renderer, which needed the same
model the gerber writer consumes and discovered there is no producer for it.

## What is actually true

**`export_fab` and `zip_fab` have ZERO callers in `src/`.** The only
mention of `realize.to_gerber_model` outside its own module is a docstring
reference in `drc.py`. Verified by grep across the whole tree.

`pcb/gerber.py` is complete and well tested — X2 headers, aperture dedup,
true G02/G03 arcs, G36/G37 pours, Excellon PTH/NPTH split, zip bundling —
but **only against the synthetic `_MODEL` fixture in
`tests/test_pcb_gerber.py`**. Nothing in production ever calls it.

The handler's fab artifacts are `bom|cpl|netlist|dsn|mechanical`. Its own
guidance says to import the `.ses` into KiCad and run `kicad-cli` for
gerbers — i.e. the Freerouting/DSN route, which `handlers/pcb.py`'s own
docstring calls "the rented, demoted escape" path. So the *intended*
primary path (our realizer → our gerbers) has an unwired final step.

This also explains an earlier finding that looked isolated:
`to_gerber_model` having no production callers was not a quirk, it is the
whole export tail being disconnected.

## Three gaps between a design and a manufacturable board

1. **Nothing wires realized copper → gerber model → `export_fab`.** Needs a
   `view='gerber'` (or fab-bundle export) that assembles the model from
   `pcb_copper_list` + outline + pads + silk and returns the zip.
2. **No instance-placed pad geometry.** Footprint pads are parsed
   (`easyeda.py` emits `"pads"`) and stored, but **nothing rotates and
   translates them into board coordinates**. `export.py`'s DSN writer only
   emits placeholder-pitch or footprint-centroid *pin offsets*, never true
   pad geometry. A gerber with tracks and no pads is not a board — it is
   unsolderable and unmanufacturable.
3. **No silkscreen persistence at all** — there is no silkscreen table.
   This is also the missing foundation under the entire labelling design in
   `pcb-pinout-view-and-connector-intake.md`: text primitive, stroke font,
   label placement as a move class all assume somewhere to persist silk.

## Why this ranks above most other open items

`pcb-usb-c-pd-nano-testboard.md` cannot be built without (1) and (2). The
board can be placed, routed, DRC'd and rendered, and still not produce
output a fab will accept. This is the difference between a demo and a
board.

It is also the sharpest instance of this build's recurring pattern: a
component that is correct, tested, and **structurally unreachable in
production**, where green tests say nothing about whether the feature
works. Same shape as the via DRC rules that could never fire and the flat
track width in a function with no callers.

> **Consider for the paper.** "Tested but unreachable" as a distinct defect
> class — three independent instances in one subsystem — is worth naming.
> Test coverage measured against *reachable* code would have flagged all
> three. See `pcb-paper-benchmark-selection.md` §CONSIDER FOR THE PAPER.
