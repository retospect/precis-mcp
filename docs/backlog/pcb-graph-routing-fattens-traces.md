---
status: draft
title: "Graph routing fattens traces by net property in place of flood fill"
prio: normal
pillar: 3d-design
---

# Graph routing fattens traces by net property in place of flood fill

Reto 2026-10-23 (memory me507871, superseding his 2026-10-17 "each layer
should be filled" ruling — gr495395 closed): "We change to graph routing
and we just fatten the traces according to their properties. So no fill,
we just fill with traces. (no floodfil.)"

The first half landed in `g4-pours-off`: `DEFAULT_STACKUP` declares no
`fill`/`power_regions`, so a fresh board pours nothing; pours stay
opt-in per board (`op='plane_net'`, a stackup `plane_net`/`fill` key, a
preset, an import's rows). This item is the second half: the copper a
flood pour used to supply now has to come from traces sized by net
property inside the (vector) router.

Filed survey-only per the g4-pours-off brief — do NOT build under this
item without a further ruling.

## What already exists (surveyed 2026-10-23)

- **Current → width.** `precis.pcb.rules.resolve_track_rules` derives
  `track_width_mm` from the net's `est_current_a` via IPC-2221
  (`ipc2221_track_width_mm`), with per-segment (`seg_current_a`) and
  per-pin (`pin_current_a`) resolution (gr497904);
  `via_count_for_current` sizes via count the same way. `realize` hands
  the resolved width to `vroute.Request.width_mm` — so "fatten by
  current" exists today, one width per net.
- **Impedance/edge-rate as DATA.** `pcb_nets.impedance_ohm` /
  `edge_rate_v_per_ns` (migration 0171) reach the IR
  (`PcbIR.net_impedance_ohm`, `net_edge_rate_v_per_ns`) and the cost
  model's annotation resolution (`cost.py::_annotation` →
  `objectives.annotation_for`, with `function_hint` fallback). They
  steer soft cost terms, not copper geometry.
- **Return path as a soft cost.** `objectives` marks power/ground
  connections with `return_net` + `low_impedance`; `cost.py`'s
  `loop_inductance` term charges pin-to-return separation — a v1
  placeholder (`return_net=net_id`; no real PWR/GND pairing table yet).
  `ir.reference_layer` renders the nearest poured layer in
  `view='stackup'` — informational, now `—` on a pour-less board.

## What is missing

- **Impedance → width.** Nothing derives trace width from
  `impedance_ohm` — there is no controlled-impedance model (needs
  dielectric geometry the stackup entries do not carry).
- **Return path → geometry.** `loop_inductance` prices a placement;
  nothing widens or co-routes a return trace. On a pour-less board GND
  is just another routed net — wide only when `current` says so.
- **Per-edge fattening.** `vroute.Request.width_mm` is one width per
  routed segment run — no tapering, necking, shielding or
  return-carrying traces yet; "fatten the traces according to their
  properties" needs a width-per-edge (or per-region) mechanism.
- **Open question.** `ratsnest.PLANE_CLASSES` still excludes
  gnd/power-class nets from the crossing metric although on a pour-less
  board they route like every other net — the exclusion's "they pour"
  premise is gone. Revisit when this item lands; changing it moves
  placement outcomes everywhere.
