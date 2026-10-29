---
id: precis-pcb-planes-help
family: pcb
title: precis — PCB planes, pours and the stackup (plane nets, plane_assignment, view='planes'/'stackup')
summary: planes on a pcb design — the default 'main' board's 4-layer rigid FR-4 stackup (F.Cu/In1.Cu/In2.Cu/B.Cu), the no-flood-fill ruling (copper from routed traces or authored pours only), authoring a pour with op='plane_net'/op='stackup', assigning a net to a layer with Board.plane_assignment vs authoring a whole-layer pour feature with Board.plane, and reading plane state back with view='planes'/view='stackup'. Covers inner planes, plane nets, stackup presets.
answers:
  - how do I assign a net to an inner plane layer?
  - what is the difference between Board.plane_assignment and Board.plane?
  - how do I author a whole-layer pour or change the stackup?
  - how do I read which nets are plane-served?
  - which nets count as plane nets?
applies-to: put/get (kind='pcb'); Board.plane_assignment/plane in workspace scripts
status: active
tags: [design]
kinds: [pcb]
---

# precis-pcb-planes-help — plane nets, pours, stackup

Two different things share the word "plane": an **assignment** (a net is
served by — returns through — a layer; it creates no copper) and a
**pour** (an authored whole-layer copper feature tied to a net).
Assignments ride the batch's top-level `planes` rows and
`Board.plane_assignment`; pours are features from `op='plane_net'` or
`Board.plane`.

## The default board and its stackup

Every design gets a default **board** (`pcb_boards`, name `'main'`, the
4-layer rigid FR-4 stackup `F.Cu`/`In1.Cu`/`In2.Cu`/`B.Cu`) on first
`put` — the netlist≠board hedge for future multi-board work; v1 is one
board per design.

The 2026-10-23 ruling is **no flood fill by default**: copper area comes
from routed traces or authored pours. A board that wants a pour authors
one with `put(args={'op':'plane_net', ...})`,
`put(args={'op':'stackup', ...})`, or
`args={'op':'stackup','preset':'4l-two-sided'}` for the hybrid that pours
every layer. On a board whose front is spoken for, opening an inner
signal layer is usually the difference between routing and not.

## Assign a net to a layer — `plane_assignment` vs `plane`

For offline batch-to-script conversion, `scripts/pcb-batch-to-script`
retains top-level `planes` through `b.plane_assignment(net, 'F.Cu')`.
These rows assign nets to routing layers; they create no copper. The
existing `b.plane(layer_index, net=net)` call authors a whole-layer pour
feature and is separate from this assignment API.

## Plane nets

`gnd`/`ground`/`power`/`pwr`/`plane` class names mark the nets that pour
or return through vias rather than point-to-point airwires. The
`view='crossings'` metric excludes them by class; they stay fully in the
netlist.

## Read plane state — `view='planes'`, `view='stackup'`

```python
get(
    kind="pcb", id="s", view="planes"
)  # authored plane assignments (op='plane_net', or an import's pour-carried
#  net — a board-covering source fill lands the same row) — which nets are
#  plane-served
get(
    kind="pcb", id="s", view="stackup"
)  # every layer in order: role, plane net, routable, the unroutable nets
```

Related: [[precis-pcb-help]] (author and read the design),
[[precis-pcb-check-help]] (the check surface),
[[precis-net-class-help]] (name + classify nets).
