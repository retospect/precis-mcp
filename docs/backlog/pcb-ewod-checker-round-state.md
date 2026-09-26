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

## No longer uncommitted — both landed 2026-09-25

The two sibling worktrees this file used to warn about are **in `main`**. The
warning above them ("most likely to be lost") is discharged; do not go looking
for those trees.

| commit | fix |
|---|---|
| `3c8db49a` | `drc.check_via_pad_keepout` polygon-aware (gr346004) + `ratsnest` side-aware via bias (gr449579) + generator anchor diagnostics |
| `5b91abe1` | rect/obround pads use their real outline in `connectivity` and `check_outline_containment` (gr450064, gr449709 — both now `STATUS:done`) |

So **all four** checker fixes are on `main`, not one of three. What remains
is that none of them is DEPLOYED.

The ratsnest fix is **no longer inert**: `ir.from_graph` now decomposes each
net with `ratsnest.mst_edges` (renamed from `_mst_edges`, made public for
exactly this second caller), passing the `bottom` set built from
`padplace.is_bottom_instance`. That is td450119 (a), and it also closes the
"ships dead" half. Segment decomposition is therefore a side-aware spanning
tree, not a star from the net's first member.

## The baseline, recorded so the re-measure stays checkable

DRC run `b13770b5`, finished **2026-09-24 15:31:49Z**, against deployed
`8.34.0@1ae63c57` and the pre-ruling-10/11 fabric. **132 errors / 162
warnings**:

| rule | severity | count |
|---|---|---|
| annular_ring | warn | 60 |
| trace_width | warn | 54 |
| clearance | error | 49 |
| clearance | warn | 48 |
| connectivity | error | 43 |
| unrouted | error | 30 |
| silk_missing | error | 7 |
| via_pad_keepout | error | 2 |
| synthesized_footprint | error | 1 |

The two `via_pad_keepout` errors are the pair gr346004 proved false, so they
are the cheapest single confirmation that a deploy took effect. The
prediction on `connectivity` (43, expected to drop hardest) is the one that
tests gr449483 comment 2's root-cause story.

**Caveat, because it is no longer a clean experiment:** td450118 re-applies
the generator (2.25 mm pitch, 250 V) on the same board, so the next measured
tally moves for TWO reasons at once — new checkers and new geometry. Isolating
them afterwards means re-running the checkers offline against the OLD fabric,
not reading it off the tally.

## Sequence to the honest tally

1. ~~Qland the two sibling worktrees.~~ Done — `3c8db49a`, `5b91abe1`.
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

- ~~**gr450064**~~ — CLOSED `STATUS:done` 2026-09-25. `_pad_poly` synthesises
  the outline for rect/obround from `(x, y, w, h, rotation)`, so the polygon
  path is the normal path and the disk a degenerate fallback (`5b91abe1`).
- ~~**gr449709**~~ — CLOSED `STATUS:done` 2026-09-25 (`5b91abe1`). NOTE the
  round-7 re-measure it asked for is NOT done: whether the 25-31 rigid-group
  `outline_containment` errors were inflated by the circumscribed disc is
  still unmeasured, and that question now lives in
  `pcb-review-round7-0903.md` under "Large rigid group strands off-board".
- **gr449483** (triaged, fix shipped) — do not close until step 4 above has run;
  its own text commits to re-measuring the 43 connectivity errors.
- ~~**gr346004**~~ — fix landed in `3c8db49a`.
- **gr347037** stays PARKED; its own proposed remedy is untested.

## Deferred deliberately

**Do not wire connectivity into the `realized` predicate yet.** It was built and
reverted during this investigation: it made `route-status` and DRC agree only by
making both wrong the same way, regressing
`test_ring_sink_route_op_realizes_more_escape_nets_with_polygon_touch` from
29 to 25. Correct order is fix -> re-measure -> then wire. The wiring belongs in
the route job (`workers/job_types/pcb_route.py`), not in `realize.py` — that
module's docstring forbids the producer calling its own checker.

## td450118 — DONE 2026-09-25, and what it changed on prod

The `ewod_pad_array` generator was re-applied on `ewod-dogfood-2` through the
session MCP (build `8.35.1@4db6836b`, which carries rulings 2/3/10/11).

Changed: `pitch` 2.0 → **2.25**, `drive_voltage_v` **250** added, and the
explicit `hv_separation: 0.09` **removed** so it derives. It now derives to
**0.4** (IPC-2221B B4, external coated, the 101–300 V band) and `min_pitch`
to **2.2335**, which 2.25 clears. Everything else was replayed verbatim from
the stored `canonical_params`.

**One stored param could not be replayed, and this is the part worth
remembering.** `sink_grid.per_tiles: 8` was removed forward-only by ruling 2
(`4164fd50`); `_parse_sink_grid` refuses it *by name*, on the grounds that a
spatial block count cannot map onto a channel count. There is therefore no
faithful translation. Resolved by omitting `channels_per_sink` entirely so it
defaults to `len(channel_pins)` = 64, giving `ceil(54 / 64)` = **one** sink —
after first confirming prod had exactly one sink instance, so the topology is
reproduced rather than redesigned. Naming did move from spatial to chain
index: `ARR1_SINK_0_0` → `ARR1_SINK_0`, `ARR1_serial_0_0` → `ARR1_serial_0`.

Verified before writing by running `generators.expand()` locally on the exact
param dict, A/B against a replay of the stored params. Verified after by
counting `pcb_fixed_copper` on board 2: **108 track + 54 via**, exactly what
the dry run predicted and up from 54 + 54. That doubling *is* ruling 11's
radial B.Cu breakout stubs arriving — the clearest single proof the fabric
data had been stale. Array extent grew 16.2 → 18.2 mm square, comfortably
inside the 50×40 mm outline.

### What the re-apply cost, and the defect it exposed — gr451046

Re-applying the generator **silently orphaned every externally-authored
connection to a net the generator owns**. `pcb_netconns` has no `retired_at`;
a connection is live exactly while its instance row and its net row are live.
The re-apply retired net_ids 122/123/124 and minted fresh rows under the same
names, so `J_SERIAL` pin 1, `J_SERIAL` pin 2 and `POGO1` pin TOP were left
pointing into the graveyard. The board's top-plate terminal and serial header
were electrically disconnected by a parameter change.

It is silent twice over: the put echo says `+0 net(s)`, and `view='route-status'`
then reports the wrecked nets as `realized (dangling net (<2 members) —
nothing to route)`. **The damage is reported in the realized column.** Repaired
on prod by re-authoring the three connections; all three are back to fanout 2.
The defect itself is unfixed — see gr451046, which also records a smaller
sibling: the `_toc` summary counts `pcb_routes` rows for RETIRED nets, so it
printed "60 failed, 59 realized" (119 rows) on a 62-net board while the proper
view said 32/30.

**Anyone re-applying a generator on a live design must check the fanout of
every net the generator declares, before and after.**

### Ruling 10/11 bought two nets, not the escape class

Measured twice, both in-process on `8.35.1@4db6836b`. Job **451039** (seed 7)
ran while the three nets above were still severed, so the number that counts
is job **451047** (seed 11), after the repair:

| run | realized | failed | `no_path` | `congestion` | dangling |
|---|---|---|---|---|---|
| baseline, 2.0 mm | 32 | 30 | 23 | 7 | 0 |
| 451039, 2.25 mm, severed | 32 | 30 | 22 | 8 | 3 |
| 451047, 2.25 mm, repaired | 34 | 28 | 21 | 7 | 0 |

So 2.25 mm pitch is worth **+2 nets out of 54**. The hypothesis that the
plaza's 0.54 mm via pitch was the binding constraint on the escape, and that
raising it would clear the `no_path` class, is **not supported**. Do not spend
another round on pitch.

Routed copper beside those counts, because a realized tally alone is
worthless: 46 track + 8 via. That is small because the fixed fabric
(108 track + 54 via) does most of the work and the router only bridges the
remainder — not because nothing routed.

**Next lead, not a conclusion.** The failures look gap-capacity-bound rather
than pitch-bound: 0.093 mm gap with the whole escape field competing. That
points at spec slice 9 (per-net clearance in the maze grid, because
`realize._realize_maze` dilates every net by ONE max-across-classes
clearance) rather than at more geometry. gr347037 stays parked until someone
measures that directly.

## Decisions parked with Reto

- **td450118** — re-apply the `ewod_pad_array` generator on prod? Rulings 10/11
  (radial B.Cu breakout stubs, 2.25 mm pitch) have never reached this board;
  the code is current but the fabric DATA is stale. Retires and recreates fabric
  rows. Recommendation: yes, but *after* the first re-measure, so a geometry
  change is not confounded with the checker fixes.
- ~~**td450119**~~ — RULED and BUILT 2026-09-25 as option **(b)**, the
  side-aware hub, after option (a) was built, measured, and reverted.

  **Option (a), the via-aware MST, does not work today — this is the finding
  worth keeping.** It was implemented by delegating to `ratsnest`'s own Prim
  tree keyed by pin id, which is the right factoring and gave exactly the
  predicted result on the reported net. It then **regressed the ESP32-C3
  reference board to 2–4 `connectivity` DRC errors at every seed**, against a
  standing acceptance criterion of zero, and took the fab-render test with
  it. Cause: the router depends on the star shape more than its own
  docstrings admit. Every segment of a net shares the hub pad, so the trunk
  necessarily runs past it and each branch has already-routed copper to
  attach to — `maze.OccupancyGrid.route`'s multi-source start and `realize`'s
  `attached` handling both lean on that. A chain removes the guarantee.
  **Making the MST viable is a router change, not a netlist change**, and
  belongs to its own round.

  What shipped instead: `_net_tree_edges` still emits a star, but roots it on
  the member sitting on the MAJORITY board side, so the cross-side count is
  `len(minority)` instead of "however many members disagree with whoever was
  written first". On the reported net that is one crossing for every member
  order, was two. A net with no side split is byte-identical to before. The
  residual gap against (a): a net with 3 top and 3 bottom members still pays
  3 crossings where a tree would pay 1.

  The other half of td450119 — the `bottom` set that was shipping dead — is
  now wired at `handlers/pcb.py`'s `view='ratsnest'/'crossings'/'feasibility'`
  path. It was **not** wired into `place.py`: `scripts/coderef callers`
  reports `place.autoplace` has zero call sites, exactly as
  `pcb-engine-plan.md` predicted when it deleted `view='route'`. Wiring dead
  code would have manufactured the appearance of coverage.

  **One-time consequence, flagged rather than migrated.** A persisted
  `pin_side` override is keyed by `session.segment_key`, the segment's two
  endpoint pins sorted. Moving a net's hub changes those pairs, so an
  override recorded against the old hub's spoke is orphaned.
  `apply_route_overrides` already defines this case — it skips a key that no
  longer exists and lets the optimizer re-decide, by design and without an
  error — so nothing breaks, but a human's pinned side choice on a
  side-straddling net with 3+ members can be silently dropped once. Every
  EWOD electrode net is two-member and so unaffected.

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
