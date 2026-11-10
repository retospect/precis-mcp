---
status: idea
title: pcb place+route residuals — layer count as a user call; ADR 0042 web/phase leftovers
prio: normal
pillar: 3d-design
---

# pcb place+route residuals

The two open items carried over from `pcb-guided-place-route.md`, retired
2026-10-25 (gr515107: `vroute`/`pcb_route` deleted, the topological router is
the only router) and deleted here; git history holds the spec. Everything
else that file described is shipped — truth lives in the
`src/precis/pcb/__init__.py` docstring, `src/precis/pcb/route_state.py`,
`src/precis/pcb/jlc_order.py` and `git log`; engine-level open work is in
[`pcb-engine-plan.md`](./pcb-engine-plan.md).

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

The multi-iteration place-route loop and batch shove
([pcb-unified-place-route](pcb-unified-place-route.md),
pcb-vector-router (deleted; tag vroute-final) §Decisions 15–16 in the vector slices)
take the layer count as given: when they find the stackup is the limit,
they report it the way this item refuses a pick below the floor, and never
add a layer themselves.

## From pcb-0042-implementation (ADR 0042 slices 8–9, deploy binaries)

- **Web (slice 8):** the board page has a schematic view and browse tab,
  but no ratsnest SVG (airwires, crossings, measuring tapes) and no BOM
  table with probe/DRC results; `src/precis_web/routes/pcb.py` has
  neither. Done when a board renders its ratsnest and BOM in the web UI.
- **Design-session phase machine (slice 9):** a pcb design as an `LLM:*`
  project whose phases (intent, architecture, netlist, part selection,
  placement, route, export) are gated by `auto_check` evaluators
  (`netlist_drc_clean`, `all_parts_selected`, `placement_legal`,
  `route_complete`), with back-edges route→placement and any→netlist.
  Some evaluators exist in `workers/auto_check_evaluators/`; the phase
  machine does not.
- **Deploy binaries:** none. `src/precis/pcb/route.py` and the
  `precis_eda` deploy role are deleted (Freerouting drop, Reto 2026-10-25).
