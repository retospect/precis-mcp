---
id: precis-pcb-topo-help
family: pcb
title: precis — the phase-0 topological placer (placer='topo', view='topo')
summary: the opt-in topological placer for pcb placement — op='place' with placer='topo' anneals an incremental straight-airwire proxy (crossings + length) with side flips and solver-side pin assignment, writes its run files on the job's meta.topo_run, and view='topo' reads them back. Covers topo-place, pair map, elite archive, crossing count, run diffing.
answers:
  - how do I run the topological placer on a pcb board?
  - what does placer='topo' do differently from the default anneal?
  - how do I read a topo-place run's summary, pair map or elite archive?
  - how do I diff two topo-place runs on the same board?
applies-to: put(kind='pcb', args={'op': 'place', 'placer': 'topo'}); get(kind='pcb', view='topo')
status: active
tags: [design, troubleshooting]
kinds: [pcb, job]
---

# precis-pcb-topo-help — the phase-0 topological placer

`placer='topo'` on `op='place'` swaps the default anneal for the phase-0
topological placer (`precis.pcb.topoplace`) — an anneal over the
incremental straight-airwire proxy (crossings + length) with side flips
among the moves, plus solver-side pin assignment for `any_of` slots.
Topological routing is the only router: the old vector router was
deleted 2026-10-25 (tag `vroute-final`) and `op='route'` refuses. Place
and route are ONE job when you opt in — `route=True` on `placer='topo'`
commits the pose, then routes it in-process (mesh → wires → per-piece
layers → tracks/vias, power rails widened where the board's DRC allows)
and writes the `pcb_copper` + `pcb_routes` rows in one transaction.
Without `route` the job places only and leaves stored copper untouched.
The route stage is best-effort and early: it is known to fail on some
boards (a raised mesh conflict on nano, clearance errors on esp32c3).
A raise fails the job — `meta.topo_run.route.error` carries the error,
never "succeeded, 0 copper" — so check `view='drc'` and
`view='route-status'` before trusting the copper it wrote. This skill covers the topo placer opt-in and its
readback view; the placer stays opt-in until a bench shows it beats
the default.

## Enqueue a topo-place run

```python
put(kind="pcb", id="sensor-node", args={"op": "place", "placer": "topo"})
put(kind="pcb", id="sensor-node", args={"op": "place", "placer": "topo", "route": True})
put(kind="pcb", id="sensor-node", args={"op": "place", "placer": "topo", "route": True, "iters": 50000, "seed": 3})
```

- `route=True` runs the in-job route stage after the pose commits;
  absent means place only — stored copper is never touched.
- `iters` is the topo placer's eval budget (default 20000, vs the
  anneal's step count); `seed` is the RNG seed, as on the anneal.
- The result lands through the SAME placement-DRC gate and rip-up as the
  anneal — a worse placement refuses the write, `force=True` overrides.
  Locks (`fixed`, `max_travel_mm`) and the fixed-copper rip-up rules of
  `op='place'` apply unchanged — [[precis-pcb-help]] for both.

## Read back a topo-place run — `view='topo'`

A `placer='topo'` job stores its run files on the job ref's
`meta.topo_run` (manifest, elites, pair map, pin assignment, summary,
placement SVG). The view drills into them — it never runs a solve:

```python
get(kind="pcb", id="sensor-node", view="topo")                          # latest run's summary
get(kind="pcb", id="sensor-node", view="topo", args={"part": "pairs"})  # pair map, worst first
get(kind="pcb", id="sensor-node", view="topo", args={"part": "elites"}) # elite archive table
get(kind="pcb", id="sensor-node", view="topo", args={"part": "svg"})    # placement figure
get(kind="pcb", id="sensor-node", view="topo", args={"run": <job ref_id>, "against": <job ref_id>})
```

`run=` picks a stored run (a `pcb_place` job ref_id, or `'latest'` —
the default); `against=` diffs the selected run's best pose and
crossing count against another run's. A board with no topo runs says
so and names the enqueue.

When `route=True` ran, the route half's counts ride the same job meta
under `topo_run.route` (absent when the stage never ran) — nets,
realized/failed, tracks, vias, unresolved crossings, unreachable nets,
widened rails — and the job summary prints a `topo-route:` line. Per-net status (realized / sketched /
failed with the legible note) lands on `view='route-status'`; a net
left unrouted or an unresolved same-layer crossing is written
`failed` with the reason, never silently.

## Iterate locally — `scripts/pcb-loop`

The dev loop runs the whole pipeline off a local `.epro2` on this
worktree's dev DB — no prod, no MCP, no deploy:

```bash
scripts/pcb-loop                          # board.epro2 → /tmp/pcb-topo/loop/<n>/
scripts/pcb-loop --seed 3 --board path/to/board.epro2 --out DIR
scripts/pcb-loop --force                  # topo pose committed despite the
                                          # placement-DRC gate
```

One run: import → `placer='topo'` place+route (one job — the pose is
committed, then routed in-process and its copper written) → DRC →
`placement.svg`/`topo-placement.svg`/`sketch.svg`/`board.svg` +
`summary.md` + `metrics.json` (per-stage wall times, route counts off
the job's `meta.topo_run.route`, unrouted nets and the realized via
count). The bench side (`scripts/pcb-topo-bench`) measures the topo
job's routed output on the standing boards — the anneal+vroute column
is retired (`op='route'` refuses), so the table is topo numbers and
their DRC-by-rule breakdown.

## See also

- [[precis-pcb-help]] — author the netlist a topo-place run acts on; the
  `op='place'` surface, the shared placement-DRC gate and the rip-up
  rules.
