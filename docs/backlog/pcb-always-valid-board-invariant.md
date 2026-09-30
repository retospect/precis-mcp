---
status: draft
title: every board mutation leaves a geometrically valid board, or is refused with the violated rule
prio: high
---

# every board mutation leaves a geometrically valid board, or is refused

> Revised 2026-09-30 after a design review against the code. The first
> draft proposed a three-state machine (S0 unrouted / S1 routing-and-
> shoving / S2 fully routed). **That framing is dead** — see "Why the
> state machine was wrong". What survives is the invariant itself and the
> delta-vs-state clause.

## Relationship to `pcb-placement-must-be-valid-before-routing.md`

Same defect, narrower scope — Reto's 2026-09-29 read of `pb345846`: plaza
vias inside `ARR1_SINK_0`'s lands, routing ran anyway. This item is its
generalization. If this is adopted, that one folds in and is deleted; if
not, that one still stands. Do not implement both.

## Motivation / why

`ewod-dogfood-4` was generated, placed, routed, and its job reported
`succeeded` with 25 of 63 nets written `status='failed'` and 64 DRC errors
— including three vias drilled into the sink's solder lands:

```
error  via_pad_keepout  via[ARR1_R1C3] @ (-0.042, -5.625) <-> ARR1_SINK_0/HVOUT7   -0.315
error  via_pad_keepout  via[ARR1_R4C3] @ (-0.042,  1.125) <-> ARR1_SINK_0/HVOUT15  -0.138
error  via_pad_keepout  via[ARR1_R7C3] @ (-0.042,  7.875) <-> ARR1_SINK_0/HVOUT24  -0.315
```

None of it was on record. `pcb_drc_findings` held zero rows until someone
asked for `view='drc'` out of band — `_render_drc` (`precis.handlers.pcb`)
is the **only** writer of that table, and `netlist_drc_clean` returns
`None` for "no run yet" rather than failing. The rule text was already
correct. DRC was simply never run.

## Validity is geometric. Routedness is progress.

The first draft's fatal flaw: `check_unrouted` (`precis.pcb.drc`) files
every non-realized net at `severity="error"`, and `run_geometric_drc`
includes it unconditionally. So "valid = the registry returns no errors"
makes **every unrouted board invalid by construction**, and the invariant
could never hold at the start of the pipeline.

The split this forces is the real content of the design:

- **Geometric validity** — the rules about where copper and pads physically
  are. This is what the invariant enforces, at all times.
- **Routedness** — how many nets are realized. Already tracked per net in
  `pcb_routes.status` and gated by `route_complete`. It is *progress*, not
  validity. An unrouted board is valid; an unmanufacturable one is not.

`check_unrouted` and `check_connectivity` therefore belong to the second
set and must not participate in the invariant.

## Why the state machine was wrong

The first draft's axis ("does routing exist yet") is the same axis the
registry already converts into an error, which is how it produced the
contradiction above. Once geometric validity is split from routedness:

- **S0/S1 collapse.** There is one rule set, always on. Nothing about it
  depends on whether copper exists yet — rules simply find nothing to
  complain about when there is nothing there.
- **S2 buys nothing.** "Fully routed" is a derived per-net fact, not a
  state with its own rules. Its only motivating example — pin swap plus
  atomic removal of the via it obsoletes — **is not a tool op today**: pin
  swaps are persisted solely by the route job's write-back
  (`pcb_pin_swaps_replace_derived` in `workers/job_types/pcb_route.py`),
  and there is no inline pin-swap op in `_INLINE_EDIT_OPS`.

**Ruling: drop S2.** A mutation on a routed board rips the affected nets
(per-net `pcb_rip_route` already exists in `store/_pcb_ops.py`) and
re-routes. Cost is one route job; inline edits are rare and the job is
idempotency-keyed.

## Validate the resulting state, not the delta

`_placement_is_legal` (`precis.pcb.optimize`) gates **moves**, never the
incumbent — it sets `d2[moving] = math.inf` and tests only proposals. A
part that already overlaps something is never rejected, so only a graded
cost term can push it out, and a graded term can lose (`U1` sat on a
mounting hole through a full anneal until `seed_placement` was changed).

Worse, and unstated in the first draft: **the placer sees no fixed copper
at all.** There is no fixed-copper input to `_placement_is_legal` or to the
IR it reads. So the sink-on-plaza-via case is invisible to both legality
and cost. Output validation is the only thing that could catch it, and
today output validation does not exist.

This clause is the most important one here and is unchanged by the review.

## Where the invariant lives — NOT only the handler

The first draft named `precis.handlers.pcb` (`_JOB_OPS` /
`_INLINE_EDIT_OPS`) as the boundary. That is wrong for the two ops that
matter: `_JOB_OPS` only **enqueues**. The worker then writes straight to
the store with no DRC anywhere in the job —
`pcb_planes_replace_derived`, `pcb_pin_swaps_replace_derived`,
`pcb_set_pose`, `pcb_routes_write`, `pcb_copper_replace` (all in
`workers/job_types/pcb_route.py`), and `pcb_set_pose` in
`pcb_place.py`. Grepping that module for `drc` hits only comments.

So the check must live **inside the job, between `realize()` and the
writes**. Two consequences:

1. Copper is written for every `rres.tracks` entry, including partial
   tracks of nets whose status lands `failed`. Per-net filtering needs a
   net→clean map that only an in-job DRC run can produce — the job holds an
   IR and a `RealizeResult`, not the dict model `_render_drc` builds from
   the store. `realize.to_gerber_model` is the existing IR→model converter,
   so this is feasible, but it is new plumbing.
2. Those writes are five separate transactions. A crash mid-sequence
   already leaves `realized` rows over the previous run's copper. "Refuse
   the commit entirely" requires folding them into one `store.tx()`.

### Mutation paths that bypass `_dispatch_op` entirely

An invariant that does not cover these is decorative:

- `put(kind='pcb', args={components|nets|features|...})` → `pcb_apply`.
  Inserts instances with authored x/y, retires and re-expands generators
  including their `pcb_fixed_copper` rows, rewrites mounting holes and
  outline. Moving a hole under a placed part invalidates a valid board.
- **`get(kind='pcb', view='route')` moves parts.** `_render_route` →
  `_place_and_store` → `pcb_set_placement`. A read verb with a write
  effect; worth fixing on its own merits.
- `op='footprint'` (in `_FOOTPRINT_OPS`, not `_INLINE_EDIT_OPS`) swaps pad
  geometry under an existing placement and existing copper.
- `op='class_rules'` changes the clearance/width thresholds existing copper
  is judged by, with no re-check.
- `op='move'` deliberately moves `fixed` instances and never touches
  copper. On `ARR1` (`fixed='both'`, authored plaza copper stored in the
  **absolute board frame**) it desynchronises every electrode pad from its
  own escape via.

Precedent to build on: `_op_stackup` already refuses to strand copper —
the one existing state-level refusal in the handler, and it chose **refuse**
over **rip**.

## Copper is not one category

The first draft's rule was "a pad may never be placed on a trace; copper
yields, pads do not". That is wrong for this item's own motivating case:
the three `via_pad_keepout` errors are sink pads against `ARR1`'s
**authored** plaza vias — `pcb_fixed_copper` rows, generator output, not
router copper. "Copper yields" there means ripping the array's escape
fabric. The right outcome is the sink moving, or a refusal.

Three categories, not two:

| | on conflict |
|---|---|
| **Pads** | never yield |
| **Authored / fixed copper** | never yields — it is design input |
| **Router copper** | yields: rip and re-route |

Cases where the original rule also misfires:

- Same-net pad-on-trace is not a violation at all —
  `clearance_pairs_indexed` skips `net_i == net_j`.
- A pad over its own net's pour is normal thermal relief; a foreign-net pad
  over a pour is resolved by the re-pour cutting an antipad (`_pour_planes`
  in `realize.py`), not by ripping.
- `check_via_pad_keepout` already exempts a same-net *fixed* via on its own
  body — a pad-on-via that is legal by policy.

`_claim_fixed_copper` (`realize.py`) implements "pads win" for router
copper on the maze grid, but that is a grid-claim ordering, not a
design-level rule, and it is silent on authored copper.

## Shoving does NOT remove the need for atomic multi-part edits

The first draft claimed it did. It doesn't: shoving resolves
part-vs-**copper** conflicts, but the motivating case (swap two parts) is
part-vs-**part**. `courtyard_overlap` is categorical and a part cannot
shove a part; move A into B's slot and it is simply refused, with no copper
to rip. On a dense board there may be no parking slot, so the swap needs a
joint move.

The annealer already does exactly this: `_placement_is_legal` takes
`proposals: Sequence[(inst, x, y)]` and validates them **together**,
because "SWAP moves two parts at once, a rigid-group TRANSLATE/ROTATE/SWAP
more". Rigid groups are the second case — a single member cannot be moved
legally alone.

So the minimal form is `op='move'` accepting a **list** of poses validated
as one resulting state. That is a transaction in all but name, and this
document should say so rather than claim shoving eliminates it.

## The rule set actually decidable before routing

The first draft's list was wrong in both directions. Decidable with pads
and fixed copper only: `synthesized_footprint`, `clearance` (pad-pad,
pad-fixed-copper), `annular_ring`, `npth_clearance`, `via_pad_keepout` and
`via_via_keepout` (fixed vias), `board_edge_clearance`,
`silk_edge_clearance`, `outline_containment`, `courtyard_overlap`,
`silk_missing`, `silk_printability`.

Also: **"phase tags" do not exist.** `run_geometric_drc` is a flat call
list, and `_render_drc` already runs the whole registry in a "pads-only"
mode. That mode, not a new tagging scheme, is the thing to build on.

### A gap this exposes: no courtyard-vs-hole rule

DRC has none. `check_npth_clearance` tests copper primitives against holes
and **only non-plated ones**. The dogfood solder nuts are `plated: True`,
so they are skipped outright. A part whose courtyard covers a mounting hole
but whose pads do not is DRC-clean.

So the placer's hole rule (in `_placement_is_legal`) and DRC **disagree**,
and the first draft's acceptance criterion — "a part seeded onto a mounting
hole is reported as invalid" — is not satisfiable against the current
registry. Either a courtyard-vs-hole rule gets added, or that criterion is
dropped. Adding it is the honest option: the placer already believes it.

## Prerequisite: `check_via_pad_keepout` is too slow to run per op

Measured on an 8×8 EWOD tile (55 pads / 55 fixed vias / 55 tracks):
`run_geometric_drc` **1.9 s**, of which `check_via_pad_keepout` is **1.9 s**
— `check_clearance` is 117 ms, `check_connectivity` 70 ms, everything else
under 5 ms. At 4 tiles (220 pads / 220 vias): **30 s**.

Cause: it is O(vias × pads) with no spatial index, and it rebuilds
`_copper_item_polygon(pad)` inside the per-via inner loop.
`clearance_pairs_indexed` in the same module already shows the fix (a
per-layer STRtree). Precompute the pad polygons and index them and a full
pass drops to roughly 0.3 s — cheap enough per tool-boundary op.

True incremental DRC is a restructure: the registry is a flat list of
`check_*(model, capability)` over a dict model with no item identity, no
changed-set input, and no shared spatial index. **It is not needed for v1.**
Fix this one rule first; scoping can wait.

## Explicitly NOT in scope

- Per-move DRC inside the annealer.
- The "next legal spot" suggester. (`_hole_blocking_slot` is circle-vs-
  circle and its own docstring forbids reuse as a legality test — it is a
  seed heuristic only, so the hint can borrow its shape but not its
  guarantees.)
- True incremental DRC.
- Changing any rule's threshold or text.

## Open decisions for Reto

1. **Authored copper on a `move`:** refuse the move, or translate the
   `pcb_fixed_copper` rows with their owning instance? Today it is neither,
   which is how `ARR1` can desync from its own plaza vias.
2. **Does `move` grow a multi-pose form**, or do we accept a
   parking-slot workflow and let swaps be a two-step dance that can dead-end
   on a dense board?
3. **`route` that cannot finish:** refuse the whole commit, or commit only
   the DRC-clean nets? Option (b) is more useful and needs the in-job DRC
   from "Where the invariant lives"; option (a) needs the five writes folded
   into one transaction. Both are real work.

## Acceptance

- A mutation that would produce a `via_pad_keepout` or `courtyard_overlap`
  violation is refused, naming the rule and the pair.
- Negative control: a legal mutation is NOT refused. Without it the refusal
  path can be vacuously "always refuse".
- An incumbent-invalid board is reported as invalid rather than silently
  accepted — the delta-vs-state clause. **Requires the courtyard-vs-hole
  rule to exist first** if the mounting-hole case is the fixture.
- `route` on a board it cannot fully realize does not leave stored copper
  with unreported DRC errors.
- `check_unrouted` does NOT cause a refusal — the geometric/routedness
  split has its own test, or the invariant is unshippable.

Owner anchors: `precis.pcb.drc` (`run_geometric_drc`, `check_unrouted`,
`check_via_pad_keepout`), `precis.pcb.optimize` (`_placement_is_legal`),
`precis.workers.job_types.pcb_route` / `pcb_place` (the real write path),
`precis.handlers.pcb` (`_dispatch_op` and the bypass paths listed above).
