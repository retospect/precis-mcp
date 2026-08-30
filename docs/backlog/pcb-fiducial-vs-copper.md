---
status: draft
title: Fiducials need a no-pour ring, not a better hiding place
prio: high
---

# Fiducials need a no-pour ring, not a better hiding place

## Motivation / why

The 40mm reference render ships two `clearance 0.000mm` findings: a
fiducial pad (net `""`) sitting inside a GND pour, flooded with copper. A
fiducial buried under flood is not a fiducial — it is a copper dot the
assembly machine's camera cannot distinguish from the plane around it, so
the board loses its optical alignment reference while every gerber still
looks plausible to a human.

**The fix is an antipad, not relocation.** A fiducial MAY sit inside a
flood; that is normal practice. What it needs is a no-pour ring: the pour
must be cut back around the fiducial so the target reads as an isolated
copper dot on bare substrate. Making `build_fiducials` avoid pours instead
would push fiducials into the board's cramped margins for no reason and
would still fail on a board that is poured edge to edge.

**Why it is not caught anywhere:** fiducial synthesis happens at *render*
time (`handlers/pcb.py::_board_furniture`, reached from `_render_drc`,
`_render_gerber` and the fab SVG). Pour antipads are cut at *realize* time
(`pcb/planes.py::plane_pours`). Neither knows the other exists, so no pass
owns the interaction. `_board_furniture`'s own comment already states the
defect: a fiducial on a poured layer is flooded over and nothing downstream
reports it. It became visible only on 2026-08-30, when `_render_drc` began
folding furniture pads into the DRC model.

## In scope

1. **Cut an antipad for every fiducial on a poured layer.** The ring
   clearance is a fab figure, not a convention — source it from
   `capabilities.py` alongside the other pour clearances rather than
   minting a fresh constant.

2. **Decide which pass owns it.** Two shapes, and the choice is the whole
   item:
   - *Move fiducial synthesis earlier*, before `plane_pours`, so the pour
     cuts around it the same way it cuts around any other pad. Structurally
     right, and `_board_furniture`'s comment already names it. Cost:
     fiducial placement stops being render-time and starts affecting
     persisted geometry.
   - *Cut the ring at render time*, subtracting the fiducial's keep-out
     from the pour polygon as it is emitted. Cheaper and local, but adds a
     second place that modifies pour geometry — the two-call-sites defect
     this repo keeps paying for.

3. **The DRC finding must clear on its own**, not be exempted. The waiver
   in `tests/test_pcb_fab_render_all_layers.py::KNOWN_OPEN_DRC_ERRORS`
   drops `clearance` to 0 when this ships.

## Explicitly NOT in scope

- **Making `build_fiducials` pour-aware as an obstacle test.** That is the
  rejected alternative above, not a smaller version of this item.
- **The other waived findings on the same fixture** (`board_edge_clearance`
  10um, `connectivity` plane fragmentation) — different mechanisms,
  separate items.

## Acceptance criteria

- A fiducial placed inside a pour emits a pour polygon with a hole around
  it; the fiducial's copper is separated from the flood by at least the
  capability-sourced ring on every poured layer it appears on.
- Regression test at the `realize`/`_board_furniture` boundary: given a
  plane-promoted pour on the fiducial's layer, the fiducial's keep-out does
  not intersect the pour's filled area. Assert the HOLE exists — a test
  that only checks the DRC count passes equally well if fiducial synthesis
  is deleted.
- `clearance` count on the 40mm fixture goes 2 -> 0, and its entry is
  removed from `KNOWN_OPEN_DRC_ERRORS` in the same commit.
- The ring clearance is read from `capabilities.py`; changing the process
  row moves the hole.

## Target + blast radius

`src/precis/handlers/pcb.py::_board_furniture`, `src/precis/pcb/planes.py`
(`plane_pours`), `src/precis/pcb/silk.py::build_fiducials`,
`src/precis/pcb/capabilities.py`, `tests/test_pcb_fab_render_all_layers.py`.
Changes emitted gerber copper on any board with both planes and fiducials.

## Open questions

- **OPEN — which pass owns the cut** (in-scope item 2). This is the design
  decision; everything else follows from it.
- **OPEN — ring size.** Typical practice is a fiducial-diameter-sized bare
  ring (1mm dot, ~2mm clearance), but source the figure rather than adopt
  the folklore number.
