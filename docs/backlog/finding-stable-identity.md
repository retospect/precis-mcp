---
status: ready
pillar: memory-graph
title: se findings carry the same stable finding_key as pcb DRC
prio: medium
model: sonnet
---

# `se` findings get the shared finding identity

Remainder of the finding-stable-identity build. The `pcb` half shipped
(`feat(pcb): findings carry stable ids; DRC reports new/still/gone against
the previous run`): `precis.pcb.drc.finding_key` /
`finding_identity` / `delta_against`, migration 0191 (`finding_key`,
`margin_mm`, `location`, `first_seen_at` on `pcb_drc_findings`;
`pcb_drc_runs` so a clean run is a recorded run), and `view='drc'`
rendering `new / still (worse) / gone` against the previous run. Reto,
2026-09-24: "I feel we want to make a gripe to make the se stuff also
provide such ids."

## What remains

`se`'s validator/DRC/clearance findings (`{severity, rule, subject,
detail}`, rendered by `precis_se/handler.py`'s report rendering) have no
key and are recomputed on every call; two runs can only be compared by
eye.

1. **Same contract, se's own participants.** A finding's identity is
   `(rule, canonicalised participants)` — for `se` the `subject` (block
   and neighbour of an interference, sorted so the pair keys the same
   either way), never a coordinate, a measured value or list position.
   Reuse the derivation shape `precis.pcb.drc.finding_key` uses (12 hex of
   SHA-1 over `rule|…|participant+participant`) rather than inventing a
   second one; if a shared home is wanted, lift the hash step out of
   `pcb.drc` into a small `precis` module both import — the pcb side keeps
   its per-rule participant extraction either way.
2. **An `id` column** on the `se` report tables (`view='drc'`,
   `view='clearance'`, validate).
3. **Delta without storage.** `se` recomputes; the cheap first slice is a
   caller-supplied previous set (`args.previous=[ids]` or the previous
   report's ids) diffed into new / still / gone, keeping `se` stateless.
   Persistence (an `se_findings` run table like `pcb_drc_runs`) only if
   that proves too fiddly for the loop it serves.

## Acceptance

- Two consecutive `se` checks on an unchanged tree produce identical id
  sets.
- A dimension change that alters an interference's depth without changing
  the blocks involved keeps the id (`still`), not `gone` + `new`.
- `se` and `pcb` ids are derived by the same rule shape and are documented
  once (precis-pcb-help has the pcb paragraph; the se skill gets its own).
