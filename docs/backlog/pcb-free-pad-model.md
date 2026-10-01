---
pillar: 3d-design
status: draft
title: "pcb: a free pad — copper on a net belonging to no component"
prio: normal
---

# pcb: free pads have no model, so they are dropped

## Motivation / why

Reto, 2026-09-30, on the six plated free pads the `.epro2` import currently
discards: "just to measure. we should add a free pad model."

Every pad precis knows is a component's pad: `pcb_pins` hangs off
`pcb_components`, and `padplace.board_pads` derives board copper by placing
a footprint's pads at an instance's pose. There is no way to express a pad
that belongs to no part.

Real boards have them. The spike board (`heaterBaseTest.epro2`, 140
components) carries **30 free `PAD` records on the PCB document itself**: 24
non-plated 6.0 mm holes, which the importer correctly turns into
`mounting_hole` features, and **6 plated 10.0 mm pads on `GND`** — Reto's
measurement points, deliberately placed copper he probes.

Those six are dropped today, with a warning, because the alternatives are
worse: turning them into mounting holes would silently discard their net,
and inventing a synthetic one-pad component would put a part on the BOM
that nobody placed. So the net loses those connections on every import, and
the loss recurs on every re-import.

A measurement point is also a thing the router must not pave over and the
DRC must check clearance against — so "dropped with a warning" is not a
stable resting place even for a read-only workflow.

## In scope

- **A free-pad row**: placed copper with a shape, a layer, an optional
  drill, and a net, owned by the BOARD rather than by a component.
  Forward-only migration; never edit a sealed one.
- It reaches board copper through the same path a component's pad does, so
  DRC clearance, the router's obstacle set, and the fab/SVG exporters all
  see it without a second geometry source (the "one rule, N call sites,
  drifted" defect `padplace.py`'s own docstrings keep recording).
- **Addressable as a net member**, since its whole purpose is to be on a
  net — which means deciding what a connection to it looks like when there
  is no refdes to name.
- The `.epro2` import stops dropping them: a plated free pad imports, and
  the warning goes away because the thing it warned about no longer
  happens.
- A testpoint role, since that is what these are. `pcb_features` already
  accepts `ftype='testpoint'` and nothing reads it — decide whether a free
  pad subsumes that value or sits beside it, rather than leaving two
  half-models of the same idea.

## Explicitly NOT in scope

- Non-plated free pads. Those are mounting holes and already work.
- Probe-point *placement* (choosing where a testpoint should go), test
  coverage analysis, or bed-of-nails fixture output.
- Free copper that is not a pad — a standalone fill or an antenna keepout.
  Different shape, different consumers.

## Acceptance criteria

1. A free pad can be authored on a net with no component, survives a
   re-`put`, and appears in `view='drc'`'s clearance check against a
   neighbouring net — the check that proves it reached the obstacle set
   rather than just the database.
2. The router does not route through it, and reports the net as connected
   at that pad.
3. An `.epro2` import of the spike board imports all 6 plated free pads
   with their net, and emits no "plated free pad" warning.
4. It renders in `view='svg'` and lands in the fab output, at the position
   the source file gave it.

## Target + blast radius

`src/precis/migrations/` (new) · `src/precis/store/_pcb_ops.py` (the write
path + whichever read `board_pads` consumes) · `src/precis/pcb/padplace.py`
(board copper) · `src/precis/pcb/drc.py` · `src/precis/pcb/realize.py` /
`maze.py` (obstacle set) · `src/precis/pcb/gerber.py` + `export.py` ·
`src/precis/ingest/pcb_epro.py` + `src/precis/pcb/epro.py`
(`extract_mounting_holes` currently counts and drops them).

## Open questions / decisions log

- **Decided (Reto, 2026-09-30):** add a free-pad model; the spike board's
  six are measurement points.
- **Open:** how a connection names a free pad when there is no refdes.
  A synthetic stable identifier ("TP1") authored by the user is one answer;
  keying the connection on the pad row's own id is another and avoids
  inventing a namespace, at the cost of being unreadable in a netlist view.
- **Open:** whether this subsumes `ftype='testpoint'` (also inert today) or
  coexists with it. Two half-models of one idea is the outcome to avoid.
- **Open:** whether a free pad participates in escape routing / fanout at
  all, or is purely an obstacle plus a connection endpoint.
