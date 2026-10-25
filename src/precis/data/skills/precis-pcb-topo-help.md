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
The default joint place+route engine is [[precis-pcb-route-help]]; this
skill is only the topo opt-in and its readback view. Opt-in until a
bench shows it beats the default.

## Enqueue a topo-place run

```python
put(kind="pcb", id="sensor-node", args={"op": "place", "placer": "topo"})
put(kind="pcb", id="sensor-node", args={"op": "place", "placer": "topo", "iters": 50000, "seed": 3})
```

- `iters` is the topo placer's eval budget (default 20000, vs the
  anneal's step count); `seed` is the RNG seed, as on the anneal.
- The result lands through the SAME pre-route DRC gate and rip-up as the
  anneal — a worse placement refuses the write, `force=True` overrides.
  Locks (`fixed`, `max_travel_mm`) and the fixed-copper rip-up rules of
  `op='place'` apply unchanged — [[precis-pcb-route-help]] for both.

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

## Iterate locally — `scripts/pcb-loop`

The dev loop runs the whole pipeline off a local `.epro2` on this
worktree's dev DB — no prod, no MCP, no deploy:

```bash
scripts/pcb-loop                          # board.epro2 → /tmp/pcb-topo/loop/<n>/
scripts/pcb-loop --skip-route --seed 3 --board path/to/board.epro2 --out DIR
scripts/pcb-loop --force --anneal-route   # topo pose committed despite the
                                          # placement-DRC gate, then legalized
                                          # by the joint anneal before routing
```

One run: import → `placer='topo'` place job → route-only
(`place=False`) job → DRC → `placement.svg`/`sketch.svg`/`board.svg` +
`summary.md` + `metrics.json` (per-stage wall times). The bench side
(`scripts/pcb-topo-bench`) measures topo vs the anneal against the
default-switch bar on the standing boards.

## See also

- [[precis-pcb-route-help]] — the default anneal's `op='place'`/
  `op='route'` surface, the shared pre-route DRC gate and the rip-up
  loop.
- [[precis-pcb-help]] — author the netlist a topo-place run acts on.
