---
status: draft
title: "pcb: meta-components — reusable schematic blocks and pre-routed layout bundles"
prio: high
model: opus
---

# pcb meta-blocks (design reuse)

Design session 2026-09-10 (Reto + agent), zippy-booping-sky worktree.
Reto: "pre-routed little bundles for design reuse (USB-C + power
negotiation, buck converter, Teensy, motor-driver H-bridge). Reduces
token use through reuse. Reuse at a schematic block level and at a
layout routed level? And where do we keep the blocks?"

Coordinate with the se/cad `functional-block-library-and-assembly-
states.md` / `blocktree-library-build-plan.md` — same library-of-
reusable-intent pattern, different substrate; don't fork vocabulary
gratuitously.

## Two levels, built in this order

**1. Schematic blocks (netlist macros) — first.** A block =
components + internal nets + declared **ports** (boundary nets:
VBUS, GND, SDA…) + measures + notes + source provenance. Stamp op:
`put(op='instantiate_block', block=<ref>, prefix='PD1',
bind={port: design_net})` — refdes prefixed, internal nets
namespaced, ports bound. Value:
- Correctness reuse: PD resistors / buck feedback / H-bridge
  protection designed and adversarially validated ONCE; provenance
  (pin-fact chunk refs, `pcb-design-source-provenance.md`) travels
  with the stamp.
- Checklist carry-over: block-interior verdicts keyed to the block
  content hash hold wherever that hash is stamped unmodified; only
  integration checks (port bindings, rail budgets) run per design.
- Tokens, both directions: one stamp op replaces ~dozens of rows on
  write; views render stampings collapsed ("PD1 = usb-c-pd@r3,
  12 parts, 3 ports") unless descended.

**2. Layout blocks (pre-routed bundles) — second.** Block
additionally carries relative placement + routed copper. Stamping
places a rigid super-instance (union courtyard; anneal moves/rotates
it as a unit); the router imports interior copper as pre-existing
segments and routes only the port escapes. **Rule envelope is a hard
gate**: a block declares the stackup/layer-count/clearance rules its
copper was routed under; stamping into a mismatched design REFUSES
honestly (the se/cad kernel-unit posture — never scale silently).
Target members: buck converter (loop area), USB-C connector + PD,
crystal island, H-bridge power stage.

## Where blocks live

**A block IS a pcb design** with `meta.role='block'` + a ports
declaration. No new tables; DRC, netlist views, SVG render, and the
checklist all work on blocks for free — a block is developed and
verified with the same tools as a board (a block can carry its own
checklist assignment). Library = prod DB, discovered by tag
(`role:block`) / search. A git-shipped curated standard library only
if/when a stable core earns it (same shipped/local split as
checklist-kind — deferred, not designed now).

**Stamping copies rows** — the design owns its copy; block evolution
never mutates shipped boards. Each stamp records provenance
`{block_ref, rev/content-hash}` in instance/net meta, so "which
designs stamp an outdated buck block" is one query (same reverse-
query pattern as app-note provenance), and checklist staleness can
flag outdated stampings without forcing updates.

## Open questions

- Port typing: do ports carry the pin-tag vocabulary (power/gnd/
  data/analog) so `bind=` can be sanity-checked at stamp time?
  (Cheap, probably yes — reuses the pcb_pins tags.)
- Parameterization: v1 blocks are literal (a 5V/3A buck is one
  block; a 12V variant is another). Parametric blocks (R-value
  formulas) are explicitly NOT v1.
- Nested blocks (block stamps block): defer; single level v1.

## Explicitly not in scope

Parametric/generated blocks; nested stamping; cross-kind unification
with the se blocktree library (coordinate naming only); automatic
block extraction from existing designs.
