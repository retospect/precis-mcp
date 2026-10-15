---
status: draft
title: Import records outer-layer pour coverage as pour-carried nets
prio: high
pillar: 3d-design
---

# Import records outer-layer pour coverage as pour-carried nets

## Motivation / why

gr486958. The epro importer stores a `POUR` zone's authored outline in
`meta.epro_pours` and turns an INNER layer carrying one net's pour into a
`plane` — but an OUTER pour is always dismissed as "local fill", even when
it covers the whole board. taz15 is the proof: `B.Cu` carries a GND pour
and `F.Cu` a 3V3 pour over essentially the whole outline. The router
therefore tries to route GND/3V3 as traces, and the taz15-blank baseline
works around it by hand (`op='plane_net'` twice).

## In scope

- `derive_stackup` measures each copper layer's pour coverage per net
  (zone polygon union ∩ board outline ÷ outline area). One net covering
  ≥ `_POUR_CARRIES_BOARD` of the outline is **pour-carried** — the
  fixtures justify the 0.5 cut (committed fixture's zone is the outline
  verbatim ~100%; taz15's outer zones are full-board pours; a genuine
  local fill is a fraction).
- A pour-carried net lands as `plane_net` when the layer is otherwise a
  plane (the existing inner single-net rule), otherwise as a `pour_nets`
  list on a still-`signal` entry — the "mixed" case is several such nets
  on one layer. New stackup key normalised by `validate_stackup`.
- A layer with exactly ONE pour-carried net writes a `pcb_planes` row at
  import — the same write `op='plane_net'` makes, so place/route/realize/
  `view='planes'`/content-hash all honour it with no second rule. Several
  qualifying nets on one layer stay recorded only: one sheet can't pour
  two nets, and guessing would silently connect copper (the existing
  inner-multi-net doctrine).
- `op='stackup'` applies a single `pour_nets` entry through the same
  `pcb_assign_plane` write `plane_net` gets — the key is never decorative.
- Small local pours change nothing: no key, no row, same warning.

## Explicitly NOT in scope

- Region/split pours: `plane_pours` floods the layer minus blockers — it
  cannot reproduce a zone's shaped extent, and a multi-net layer is left
  routable rather than re-poured.
- No schema change: `pour_nets` is a stackup JSONB key; `pcb_planes`
  already exists.

## Acceptance criteria

- taz15 import records `B.Cu`→GND and `F.Cu`→`3V3` as pour-carried nets
  (stackup `pour_nets` + `pcb_planes` rows); a small-pour board sets none.
- `content_hash` is byte-identical for boards without pours (append-only
  key; `tests/test_pcb_idempotency.py` guards).
- The router skips a pour-carried net's traces exactly like a plane net's
  (`promote_plane` via the authored `pcb_planes` row).

## Target + blast radius

`ingest/pcb_epro.py` (`derive_stackup`, `import_epro`, `report_copper`),
`pcb/ir.py` (`STACKUP_LAYER_KEYS`, `validate_stackup`),
`handlers/pcb.py::_op_stackup`, `precis-pcb-route-help` skill. Sibling
trees pcb-stackup/cap-model/place-groups/ds-review touch the package —
the seam kept small: no router-code edits, the `pcb_planes` row is the
existing mechanism.
