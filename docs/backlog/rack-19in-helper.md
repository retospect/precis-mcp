---
status: idea
title: 19-inch rack helper — rack units, panels, mounting-hole patterns and rack-mount enclosures from a few parameters
pillar: 3d-design
prio: low
---

# 19-inch rack helper

Reto, 2026-10-01 (pillar review): "potentially, a 19in rack helper".

## Wanted

- A rack described by height in rack units and depth, producing the
  rails or frame (EIA-310 / IEC 60297: 1U = 1.75 in = 44.45 mm, 19 in
  panel width, the repeating three-hole-per-U mounting pattern — cite
  the standard text for every dimension the helper uses).
- **Panels**: a blank or cut-out front panel of N U, with mounting
  slots on the standard pattern, ready for laser or CNC export.
- **Rack-mount enclosures and shelves**: a box of N U and given depth
  that carries a board (PCB designs from the ewod-pcb / pcb-platform
  work are the first contents) with its front panel cut-outs placed
  from the board's connectors.
- A check that a placed item fits: U count, depth, and rail clearance.

## Relation

Builds on `tslot-profile-library.md` when the frame is T-slot extrusion
rather than bought rails. Owner: se-machine-design thread, Horizon.
