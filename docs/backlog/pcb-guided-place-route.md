---
status: draft
title: pcb place+route — remaining open items (layer count as a user call)
prio: normal
pillar: 3d-design
---

# pcb place+route — remaining open items

The sketch-as-canonical place+route engine, the JLCPCB order op, the
route-state consolidation and `op=` idempotency all shipped; truth lives in
the `src/precis/pcb/__init__.py` docstring, `src/precis/pcb/route_state.py`,
`src/precis/pcb/jlc_order.py` and `git log`. Engine-level open work is in
[`pcb-engine-plan.md`](./pcb-engine-plan.md) (cost-neutral `ROTATE` /
`SIDE_FLIP` moves: `src/precis/pcb/optimize.py` module note and
engine-plan §1). Only the item below is unfiled elsewhere.

## Layer count is a user call, not a hard 4

`handlers/pcb.py::PcbHandler` (the `op='place'`/`op='route'` enqueue path,
which reads `pcb_session.load_snapshot`) refuses any stackup whose length is
not 4 with a `next=` hint ("out of v1 scope"). Failing loudly is right; the
scope is too narrow — 2-layer is much cheaper and often the correct board.

Spec: the machine derives the layer-count *floor* (minimum feasible count
from footprint escape demand — `src/precis/pcb/escape.py` — and routability);
the user picks the count at or above it; a pick below the floor is refused
*with the reason* (which part's escape demand forces the extra layer), never
silently promoted. 2-layer boards route power like signals (no plane layers),
so the plane/`net_plane_layers` machinery and the realizer's layer
assumptions need a no-plane path. The guard stays until the floor derivation
exists.
