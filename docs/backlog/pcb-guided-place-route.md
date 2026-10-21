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

The multi-iteration place-route loop and batch shove
([pcb-unified-place-route](pcb-unified-place-route.md),
[pcb-vector-router](pcb-vector-router.md) §Decisions 15–16 in the vector slices)
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
- **Deploy binaries:** `deploy/roles/precis_eda` is in-repo and
  unapplied. The pinned Freerouting 1.9.0 is coupled to
  `src/precis/pcb/route.py::_cmd`'s 1.x batch CLI (2.x reworked it; don't
  bump without rewriting `_cmd`); the jar's sha256 pin is blank; the DSN
  emits a via referencing an undefined padstack, so check that on the first
  real-jar run.
