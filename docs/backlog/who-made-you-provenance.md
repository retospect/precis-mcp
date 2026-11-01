---
status: draft
title: "pcb provenance: ask a part who made it, who moved it, who routed it"
pillar: 3d-design
prio: normal
---

# pcb provenance — who made you, who moved you, who routed you

Reto 2026-11-01: "Are we adding the 'stack trace' pointing to the
generating code as meta to all components (and all the moves too), so we
can inquire a resistor (who made you, who moved you) and maybe even
router."

## What exists

- The board-script builder already records where each thing was made:
  `precis_geom.pcb` stamps `Part.site` and `Pin.site` (`file:line` of the
  script line, `_script_site`), connection sites (`Board._conn_sites`)
  and a trace leaf id (`Part.maker`, `precis_geom.trace`).
- `to_batch()` drops all of it: a batch component carries refdes, label,
  part, pins, pose and pin declarations only, so the stored design cannot
  say which script line made R23.
- Moves: the place job writes poses with a run record per job
  (`run_record`), but no instance carries which job and which stage
  (anneal, settle, legalize, shove, label repair, route-stage shove) last
  moved it, or by how much.
- Copper: `pcb_copper` rows carry net and geometry, not the route pass,
  the realiser stage (wire, kink, fanout, pour) or the job.

## Plan

1. **Made by.** `to_batch()` emits `meta.src = {"site": "board.py:368",
   "maker": <trace id>}` per component, net and measure; the store keeps
   it in instance/net/measure meta; `op='move'` and the web editor stamp
   `src = {"site": "op:move", "by": <session>}`.
2. **Moved by.** Every pose write appends `{job, stage, code, dx, dy,
   drot, at}` to a bounded per-instance move log (`code` is the
   `module::function` that produced the pose: `topoplace/legalize.py::
   legalize_pose`, `topoplace/shove.py::shove_pose`, …).
3. **Routed by.** Copper rows get `meta.made_by = {job, pass, stage,
   code}` (route pass number, realiser stage: wire / kink / fanout /
   pour).
4. **Ask.** `get(kind='pcb', id=…, view='who', args={'refdes': 'R23'})`
   prints the creation site, the move log, and the copper on its pads
   with its `made_by`.

## Acceptance criteria

- A put batch from a board script answers "who made R23" with the script
  `file:line`.
- After a topo place+route job, `view='who'` lists each stage that moved
  the part, with the job id and displacement.
- Every committed copper row names its job, pass and realiser stage.

## Explicitly NOT in scope

Full Python stack traces in the DB (one `file:line` per record, plus the
trace id for the rest); provenance for authored fixed copper beyond its
import source.

## Open questions / decisions log

- Move-log bound per instance (default proposal: last 20 entries).
