---
status: ready
title: EWOD checker round — resume state, what shipped, what is still uncommitted
prio: high
model: opus
---

# EWOD checker round — resume state

Written 2026-09-25. **This is a resume pointer, not a spec.** Delete it once
the DRC re-measure has run and its outcome is recorded on the gripes.

The round's goal is one thing: **an honest DRC error count on prod design
`ewod-dogfood-2`.** Every item below is either in service of that or a defect
found while pursuing it.

## The governing finding

Three checks were measuring pads with a circle where the real outline was
available, and the resulting false verdicts inverted three of this session's
conclusions. The class, the citations, and the structural remedy live in
`pcb-lazy-netlist-and-checks.md` section 1g-bis — read that before touching
any check. Short version: an approximation is sound only in the direction its
error is signed, and a check's verdict is a threshold, so a conservative-looking
simplification becomes a false positive or a false negative purely by which way
it rounded.

**Operating rule for this round: before fixing anything a check reported,
confirm the check measured the real shape. Re-measure first.**

## Shipped

- `aa861102` on `origin/main` — `connectivity.net_islands` threads real polygon
  outlines through `_touch_gap` (gr449483); two regression tests; the pcb
  check-surface specs; the EWOD controller/HV item.
  **UNGATED** (`/qland`) — no ruff/mypy/pytest ran on the merge. Passed
  10222/28 locally pre-ship. Debt settles at the next full gate.

## Uncommitted, and this is the thing most likely to be lost

Two sibling worktrees hold pcb fixes that exist **only as dirty trees**:

| worktree | fix | state |
|---|---|---|
| `agent-adb6745ad1932338d` | `drc.check_via_pad_keepout` polygon-aware (gr346004) | 91 + 26 tests pass, mypy clean on 2131 files |
| `agent-a8e7f220489155494` | `ratsnest._mst_edges` side-aware via bias (gr449579) | 20 tests pass, `MST_VIA_BIAS_MM = 20.0` |

The ratsnest fix is **INERT** — its `bottom` parameter defaults to `None` and
no caller passes it. It needs wiring into `place.py` and `handlers/pcb.py` or
it ships dead.

**Qland both before the `/go`.** If they are not in, the re-measure reflects one
checker fix of three and the resulting number will be wrong in a way that is
hard to detect from the number alone.

## Sequence to the honest tally

1. Qland the two sibling worktrees.
2. `/go` — full gate + deploy of the gated sha. A deploy is required, not just
   a merge: 103 commits on `main` were undeployed as of 2026-09-25.
3. Re-route **with a varied seed**. `op='route'` is idempotent per
   `(design, op, content-hash)`, and the hash covers netlist/placement state
   but NOT the code version, so an unchanged design silently returns the prior
   job's result even across a deploy.
4. Re-measure DRC. Prediction, recorded so it is checkable: the 132-error tally
   roughly halves and `connectivity` drops hardest. If it does not, the
   root-cause story in gr449483 comment 2 is wrong.
5. Only then decide what is actually broken.

Note that `op='route'` does **not** re-run the generator — see td450118.

## Open gripes in this class

- **gr450064** (new, open) — rect/obround pads still get the inscribed disk;
  `_pad_poly` returns `None` for them by design. Measured against the shipped
  fix: a 2.0 x 1.0 mm rect pad with a track landing on its short edge gives
  `net_islands -> [('N1', 2)]`. Masked only because the maze router targets pad
  CENTRES. Fix: synthesise the outline for rect/obround from
  `(x, y, w, h, rotation)`.
- **gr449709** (open) — `check_outline_containment`, same circumscribed disc as
  gr346004. May be inflating round-7's 25-31 rigid-group errors. Not yet
  re-measured.
- **gr449483** (triaged, fix shipped) — do not close until step 4 above has run;
  its own text commits to re-measuring the 43 connectivity errors.
- **gr346004** (fix uncommitted) — see the worktree table.
- **gr347037** stays PARKED; its own proposed remedy is untested.

## Deferred deliberately

**Do not wire connectivity into the `realized` predicate yet.** It was built and
reverted during this investigation: it made `route-status` and DRC agree only by
making both wrong the same way, regressing
`test_ring_sink_route_op_realizes_more_escape_nets_with_polygon_touch` from
29 to 25. Correct order is fix -> re-measure -> then wire. The wiring belongs in
the route job (`workers/job_types/pcb_route.py`), not in `realize.py` — that
module's docstring forbids the producer calling its own checker.

## Decisions parked with Reto

- **td450118** — re-apply the `ewod_pad_array` generator on prod? Rulings 10/11
  (radial B.Cu breakout stubs, 2.25 mm pitch) have never reached this board;
  the code is current but the fabric DATA is stale. Retires and recreates fabric
  rows. Recommendation: yes, but *after* the first re-measure, so a geometry
  change is not confounded with the checker fixes.
- **td450119** — `ir.py::from_graph` star decomposition: via-aware MST
  (recommended) vs cheaper side-aware hub selection. This is the real cause of
  the "2 vias where 1 would do" on J_INSTR -> U_TEMP -> R_bleed; the topology is
  order-dependent on the net's first member. **Not** `ratsnest._mst_edges`,
  which feeds placement, not copper — gr449579 was filed against the wrong
  module.

## Housekeeping

- Several pcb gripes carry a comment claiming they were "folded into
  docs/backlog/pcb-0042-implementation.md". That file exists (60 lines) and
  mentions **none** of gr449483 / gr449579 / gr346004 / gr449709 — verified
  2026-09-25. The claim is false. Corrected on gr449483; **the siblings are
  suspected but not individually verified** — check before relying on it, and
  do not propagate the count.
- The real owning spec is `pcb-lazy-netlist-and-checks.md`.
- Slices 5-9 of that spec (pin model, supply/client roles, resolved netlist
  snapshot, `view='check'`, per-net clearance) are unstarted.
- `pcb-argue-with-design.md` is still `status: ready` / high prio as a click-UI
  item. It should be rewritten to the handle grammar
  (`pcb:ewod-dogfood-2~comp234/pin4/courtyard`, view-independent, gettable as a
  ref, findings carry handles as participants) or killed.
- HV board blocker: `realize._realize_maze` builds the maze grid with ONE
  clearance = max across all classes, so a 0.4 mm HV class inflates every net
  and makes the 0.099 mm fabric unroutable. Needs per-net dilation (spec
  slice 9).
- HV507 has 12 non-channel NAMES across 13 pads and `_real_pin_offsets` is
  first-wins, so one pad stays invisible even after declare-all-pads. Needs a
  ruling.
