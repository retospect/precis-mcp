---
pillar: 3d-design
status: draft
title: "pcb: the router realizes 65 of 89 nets on a real 140-component board (was 8, reported as success)"
prio: high
---

# pcb: the router does not work at real board size

## Motivation / why

Measured 2026-09-30 on Reto's real board (`heaterBaseTest.epro2`, 200 × 75
mm, 140 components, 4 layers), imported by `precis pcb import-epro` and
routed in-process via `pcb_route._dispatch` at `iters=200`:

```
140 components, 89 nets: 105.7s
status: {'failed': 81, 'realized': 8}
   54 net(s): failed: no_path, same-layer-crossing
   27 net(s): failed: no_path
job failures reported: 0
```

**8 of 89 nets realized — 9%.** Every other net is `no_path`, two thirds of
them additionally flagged `same-layer-crossing`.

This matters more than its size suggests. The entire
`pcb-easyeda-round-trip` thread exists so Reto can **re-route his board to
a corrected spec** — importing it is only the way in. A router that
realizes 9% of a board of this density cannot do that job, and every
downstream slice (the annotation pass, the §E-1 pairwise-clearance work,
the export a colleague opens) assumes a routed board on the far side.

Until now this was an explicitly open question rather than a known defect:
`pcb-epro-import.md`'s decisions log recorded "no route has been attempted
at this size, so whether the router is usable on it at all is unmeasured."
Reto asked for the measurement directly ("it's routed already, but yea do
the needful"). This is the answer.

**The second defect is the reporting.** `ctx.failures` is EMPTY: the job
completed, wrote a summary, and reported no failure while marking 81 of 89
nets `failed` in `pcb_route_status`. A caller that checks the job outcome —
which is what an autonomous pass or `route_complete` would do — sees a
successful route of a board that is 91% unrouted. A pass that cannot route
the board should say so as a failure, not only as 81 rows someone has to
go and count.

## In scope

- **Find out why `no_path` dominates.** The three candidates, in the order
  they are cheap to rule out: (a) `iters=200` is simply far too low for 89
  nets — the existing worker tests use 500 iterations for a *two-pin* net,
  so the budget may be off by orders of magnitude and this is not a
  routability finding at all; (b) the maze router is not using the inner
  layers, which `same-layer-crossing` on 54 nets points at directly, and
  which would make this a via/layer-transition problem rather than a
  congestion one; (c) genuine congestion at this density. Establish which
  before changing anything — the fix for each is unrelated to the others.
- **Make an unroutable pass report as one.** A route job that leaves a
  configurable fraction of nets unrealized must record a failure with the
  count, so the job outcome and the route status cannot disagree.
- **A repeatable measurement.** `tests/test_pcb_epro_import.py::
  test_a_real_board_routes_at_all` is env-gated (`PRECIS_EPRO_FIXTURE`) and
  `slow`-marked; it prints the status histogram and the top failure notes
  and asserts nothing about quality. Keep it that way — a quality
  assertion at this size is a claim about one anneal seed.

## Explicitly NOT in scope

- Routing quality: length, via count, crosstalk, matched lengths. This item
  is about whether a net gets routed at all.
- The §E-1 pairwise-clearance term (`pcb-missing-constraint-classes.md`).
  It makes the router draw to a *stricter* rule, which cannot help a board
  that already cannot find a path, and folding the two would make it
  impossible to tell which change moved the number.
- Placement. Measured 2026-10-01: freezing every part leaves the count at
  8/89, so placement is not the variable (see Diagnosis).
- Rewriting the router. Establishing the cause may well show the budget or
  the layer usage is wrong, which is a much smaller change.

## Acceptance criteria

1. The dominant cause of `no_path` on this board is identified and
   written down with the measurement that shows it — specifically, whether
   raising `iters` alone materially changes the realized count.
2. A route pass that fails most of its nets records a job failure naming
   the count; a test asserts the job outcome and the route status agree.
3. The realized fraction on the real board is materially better, or the
   item is closed with a written statement of what density precis' router
   does handle — an honest ceiling is a usable answer, a silent 9% is not.

## Target + blast radius

`src/precis/workers/job_types/pcb_route.py` (the pass, the iteration
budget, the failure reporting) · `src/precis/pcb/maze.py` and
`realize.py` (path search, layer transitions) ·
`src/precis/workers/auto_check_evaluators.py` (`route_complete`) ·
`tests/workers/test_pcb_route.py` · `tests/test_pcb_epro_import.py` (the
measurement harness).

## ⚠ The 8/89 number is NOT a pure router measurement (found 2026-10-01)

`pcb_route` runs the **JOINT place+sketch anneal** — placement, layer
assignment, side flips, plane role and pin swap — and only then realizes
(the job module's own docstring says so). On the imported board only the
parts EasyEDA had locked are frozen: **37 of 140**. So the trial let the
anneal re-place 103 of Reto's parts before routing, and 8/89 measures
"re-place then route", not "route the board as he laid it out".

Two consequences:

- **The diagnosis must separate the two.** Planned harness (extend the
  env-gated, `slow` `test_a_real_board_routes_at_all` into a parametrized
  run, each config profiled with cProfile): (1) as imported, `iters=200` —
  the baseline; (2) **every instance frozen** (`fixed='both'`), `iters=200`
  — the router alone on his placement, which is the question that matters;
  (3) every instance frozen, `iters=2000` — whether budget alone moves the
  count. Per run record: realized count, copper rows PER LAYER (tests the
  `same-layer-crossing` → inner-layers-unused hypothesis), how many
  instances moved (verifies the freeze held), wall time, and the top
  cumulative-time functions. The env passthrough allowlist in
  `scripts/test` carries only `PRECIS_EPRO_FIXTURE` of ours, so configs are
  test parameters, not env vars.
- **A workflow hazard independent of the score.** "Re-route my board"
  through `op='route'` silently RE-PLACES every unlocked part. Reto's
  2026-09-30 ruling was that parts are freezable and frozen only when
  needed — which makes moving them legitimate in general, but not as an
  unannounced side effect of a request to route. Either the route op needs
  a routing-only mode that holds placement, or the import/route surface
  must say plainly that routing will move parts. Decide once the diagnosis
  shows whether a frozen-placement route is even viable.

## Diagnosis (measured 2026-10-01) — the GRID, not congestion or budget

`test_a_real_board_routes_at_all`, four configs, same board, seed 1:

| config | parts moved | realized | wall |
|---|---|---|---|
| as imported, iters=200 | 54 | 8/89 | 165 s |
| all frozen, iters=200 | 0 | 8/89 | 138 s |
| all frozen, iters=2000 | 0 | 8/89 (identical copper) | 269 s |
| all frozen, iters=200, grid 2000 cells/axis | 0 | **62/89** | 2068 s |

- **Cause: `maze.grid_for`'s `target_cells_per_axis=400`.** The pitch is
  `span / 400`, so on a 200 mm board the routing cell is 0.5 mm (the ~50 mm
  reference board gets 0.125 mm). At 0.5 mm, neighbouring fine-pitch pads
  plus clearance dilation cover every cell around a pin, so the `no_path`
  probe — which clears every OTHER net's copper and routes at near-zero
  width — still fails. Raising the cap to 2000 (0.1 mm) moves 8 → 62 and
  turns the failure mix from 81 `no_path` into **21 `congestion` + 5
  `no_path`**: the remainder is now genuine routing contention, which is
  what rip-up/retry (or negotiated congestion, PathFinder) is for.
- Placement (frozen vs not) and anneal budget (200 vs 2000) change nothing.
- `same-layer-crossing` is a placement-time chord note attached to nets the
  maze left without copper, NOT evidence the inner layers are unused. Both
  inner layers are planes on this board (as in the source), so this is a
  2-signal-layer route by design.
- **Cost of the fine grid: 2068 s.** 1850 s of it is `maze.route` (5412
  calls over 12 rip-up passes; pure-Python A*: 286 M `heuristic`, 217 M
  `heappop`, 490 M `passable`). A uniform fine grid is the wrong shape —
  the fix wants fine cells only where pads are dense (pitch derived from
  the finest pad pitch locally, coarse elsewhere), or a vectorized/compiled
  A*, or both. Separately the anneal at iters=2000 calls
  `cost.hardened_penalty` 281 M times (anneal overhead, not routing).
- **Also wrong:** an A* that exhausts `max_expansions` returns `None`, and
  the unrouted-reason probe reports that as `no_path` ("walled in") — a
  budget exhaustion and a topological wall are indistinguishable today.

**Two more handicaps, found by slice 1c's copper report (2026-10-01) — the
four runs above carry both, so re-measure before judging the fix:**

- **Half the routing layers were missing.** The author routed 81 track runs
  on 50 nets across In1.Cu/In2.Cu, which also carry the V5SATA/V12SATA
  pours. The importer stacked both as pure planes, so precis routed on 2
  layers where the author used 4. FIXED in the import: such a layer is now
  `role='plane'` + `routable: True` (`derive_stackup`). Whether the router
  and the plane pour actually coexist on one layer at this scale is
  unmeasured.
- **precis asks for more clearance than the author used.** 50 nets run
  0.102 mm gaps (above the 0.09 mm fab minimum) where precis' default rule
  is 0.150 mm. The author's default track is 0.254 mm against precis'
  0.150, and the author's via is 0.61/0.305 mm against 0.75/0.25. A
  re-route to precis' defaults is a different, harder board than the
  source; the spec correction comes before judging the router.

**Re-measured 2026-10-01 after the import fix** (inner layers now
`routable`): still **8/89**, frozen or not, default grid; only 5 tracks
landed on In1/In2. So the missing layers were not what held the count
down; the grid is. Profile at the default grid: `_pad_keepout_mask` was
32 of `maze.route`'s 43 s (rebuilt every call), and `_diagnose_all`
rebuilt its pads-only probe grid per segment (36 of 85 s of realize).

**Fix in progress (uncommitted, this thread's worktree):**
1. DONE — `OccupancyGrid._pad_keepout_mask` cached per via radius,
   invalidated when a pad is stamped.
2. DONE — `maze.route` sets `last_route_exhausted`; `_diagnose_unrouted`
   reports a new `'search_budget'` kind instead of `no_path`/`width` when
   a probe gave up; the probe grid is built once per realize.
3. DONE — `route` searches a window (endpoint bbox + max(5 mm, ½ span))
   first, masks computed over the window only, and falls back to the full
   grid only when the window search ran dry (not when it exhausted).
4. DONE — fine grid (0.1 mm) with 1–3 on the real board: 66/89 realized
   in 1653 s (was 62/89 in 2068 s); 20 `congestion`, 3 `no_path`, no
   `search_budget`. `_route_in`'s own loop was ~700 s of that, so:
5. DONE — pitch rule: `grid_for(max_pitch_mm=)`, capped at
   `MAX_CELLS_PER_AXIS`; it only bites when span/400 is coarser, so small
   boards are unchanged. First rule, (narrowest track + clearance) / 2 =
   0.15 mm, routed only 20/89 in 483 s (44 `no_path`): `maze` adds one
   cell of slack to every keep-out, so the cell itself is clearance an
   escape must squeeze past. Now `clearance * 2/3` (0.1 mm here).
   EWOD counts re-measured and sent to ewod-pcb 2026-10-01: island 16/54
   (12 before polygon touch), dogfood 17/54 escapes.
6. DONE — `_route_in`'s inner loop rewritten with local bindings and
   `bytes` masks; same costs and tie-breaks.
7. DONE — real board at the rule pitch with 5–6, default settings: 60/89
   in 1096 s (13 `congestion`, 10 `no_path`, 3 congestion + crossing).
   The A* loop is still ~450 s of self time. NEXT: what remains is mostly
   congestion, which wants negotiated congestion (rip-up and reroute,
   PathFinder), not a finer grid.
8. DONE — AC2, per Reto's ruling below: ANY failed net fails the job
   (`non-convergence`), after copper, statuses and summary are written.
   EWOD tests drain with `_drain_one_job(unrouted_ok=True)`.
9. DONE — numba (now a core dependency), measured on the real board
   (frozen, iters=200), realized count unchanged at 60/89 throughout:
   `maze._astar_kernel` (A* loop compiled; same step order, costs and
   `(f, cell)` heap order) 1096 → 296 s; `OccupancyGrid.chord_is_free`
   (path straightening's 9.6 M `disk_is_free` calls) and keep-outs
   checked on demand per cell (`_disk_hits_foreign`, memoised in the
   kernel) instead of dilating a window that is most of the board →
   **123 s**. ewod-pcb then made `optimize.risk` incremental (e4ba56fc):
   **96 s** at iters=200 and 95 s at iters=2000, still 60/89. What is
   left is ~60 s of search (`_route_in`) and ~14 s of copper stamping.
   Found on the way: the keep-out dilation was documented as Chebyshev
   but built an L1 diamond, reaching only r/√2 diagonally — a wide
   track's clearance came up short on diagonals. Now a Euclidean disk
   (`test_the_keep_out_is_a_euclidean_disk_including_at_the_grid_edge`).
   Realized count on the real board unchanged; the failure mix moved by
   one net (14 congestion, 11 no_path, 2 congestion + crossing).
10. DONE — pre-route gate (`pcb_route._fixed_copper_collisions`): fixed
   copper with an error-severity clearance against another net's pad
   refuses the job (`input`) before the anneal, and again after it
   (`non-convergence`) — Reto, ewod-dogfood-6. The ring-sink island test
   is strict-xfail until ewod-pcb's placer legalization lands.
11. DONE — the 11 `no_path` nets were a modelling defect, not geometry.
   Two 0.5 mm-pitch neighbours' clearance keep-outs overlap in a sliver
   that `_stamp_pads` stamps CONTESTED; it sat two cells from the pad
   centre, inside the endpoint's keep-out disk, so the net's own pad read
   as walled in (U1, U17–U28: D4_RESET, VCC, the SCL_O_*/LED I2C nets).
   An endpoint now ignores CONTESTED (`maze._disk_hits_net`); every other
   cell does not, and another net's copper still blocks. Real board:
   **65/89 in ~100 s** (17 congestion, 2 congestion + crossing, 3
   `search_budget`, no `no_path`). The pass kept is now the one with the
   fewest failed NETS (Reto's ruling counts nets), then segments — same
   65 here.
12. TRIED, LOST — a PathFinder history cost on top of re-ordering:
   after each pass, surcharge the corridor each lost-race connection
   takes on a board with nothing routed. 49/89 at one grid step per cell,
   62/89 at a quarter step, against 65/89 without, and slower (129–228
   s). Those corridors cross the pad-escape regions every net needs, so
   everyone detours and the board fills faster. Not shipped. Real
   negotiated congestion lets nets SHARE cells while they negotiate
   (present + history cost on overuse, legality only at the end), which
   the hard-ownership grid cannot express — that is a new occupancy model
   (per-cell usage counts + rip-up by net), not a cost term.
13. BUILT, DARK (off by default) — negotiated congestion
   (`maze.Negotiation`, `realize._negotiate`). It runs only when the
   re-ordering passes leave a net unrouted, and only when asked for
   (`op='route'` `negotiate=N`, `RealizeConfig.negotiate_iterations`).
   Routed copper counts per-cell usage instead of owning cells; price
   rises per iteration (VPR schedule), contested cells gain history,
   whole conflicted nets are ripped and re-routed; 60 s wall-clock cap.
   The proposal commits onto a fresh hard grid through
   `OccupancyGrid.path_is_legal` (the search's own disks), verbatim
   (snapped as negotiated, never straightened, so branch attach points
   survive), and is kept only if it fails fewer nets. Fable review
   2026-10-02: accepted the method, required verbatim commit, default off
   and the time cap (all done). Landed dark 825e451aa.
   **Measured 2026-10-02, and it does not earn default-on: +1 net for
   2.5× the time.** Method: pcb 460559 (heater-base-test) prod rows dumped
   read-only 2026-10-02 10:55Z to
   `~/.claude/projects/-Users-reto-precis-mcp/scratch/pcb_boards_460559_458868.json`
   (`pcb_board_dump.py`); harness `scratch/pcb_route_offline.py` runs
   `pcb_route._dispatch` unmodified against a fake store (layers, pitch,
   fab caps and net classes as the job derives them from the rows), every
   instance frozen (`--freeze`), seed 1, iters 200, code c23ef0aa9 on a Mac
   host. Off (`--negotiate 0`): **55/89** realized, realize 79 s. On
   (`--negotiate 30`, 60 s budget): **56/89**, realize 197 s. Same harness,
   one run each.
   **The baseline is 55, not 65.** The 65/89 above came from the
   env-gated `.epro2` test before 2026-10-02's import fixes. Since then
   the board's 24 Ø6 mounting holes and 8 footprint holes reach the router
   as keep-outs, and the post-route DRC gate strips nets. Off-run failure
   tags: 18 `unrouted` alone, 13 `drc` (with `same-layer-crossing`, which
   is the placement IR's straight-line crossing from
   `pcb_route._residual_crossings`, not routed copper), 3 unrouted with
   `same-layer-crossing`. **The 13 `drc` nets are the next thing to
   diagnose**: the router realized them, and the gate stripped them. Either
   the router lays copper the DRC refuses (a legality mismatch between
   maze disks and DRC geometry, e.g. oblique pads, which DRC may see as
   their unrotated rectangle), or the gate is wrong. Either way it is
   cheaper than more congestion work.

Literature, for the fix owner: grid-maze routers are known to degrade on
fine-pitch parts on large boards because cell size couples to board size;
the industry answer is shape-based/gridless routing (Specctra; Freerouting's
"expansion rooms" = A* over a rectangle decomposition of free space), and
for real congestion, PathFinder negotiated-congestion (McMurchie & Ebeling
1995). Not yet imported into precis — import before citing in a spec.

## Open questions / decisions log

- **Decided (Reto, 2026-10-01, td460164):** a route with unrouted nets
  FAILS — "it's no good if the wires are not there. You may show it to me
  but it is failed." numba for a compiled A*: "Yes that's fine, numba is
  cool." Fixed copper through another net's pad: "highly illegal and
  should prevent the router from starting."

- **Decided (Reto, 2026-10-01):** run the diagnosis now — "be reasonable
  but these are fast, it can run for a few minutes easily"; profiling and
  optimizing the router is wanted, now or soon.
- **Decided (Reto, 2026-10-01):** order is diagnosis → `pcb-epro-import`
  slice 1c → the router FIX. For the fix: "coordinate and pick one" — after
  the diagnosis, agree with the ewod-pcb session (which is editing
  `realize`/`maze`) which ONE session owns the fix, rather than both
  touching the router.

- **Decided (2026-10-01, agreed with the ewod-pcb session):** the
  `pcb-easyeda-round-trip` thread OWNS the maze/realize fix; ewod-pcb has
  nothing in `maze.py`/`realize.py`. Avoid its unlanded regions:
  `pcb/optimize.py`, `pcb/ir.py`, `pcb/session.py`, `pcb/export.py`,
  `handlers/pcb.py`, and the `build_ir` call sites in `pcb_place.py`/
  `pcb_route.py`. Couplings: (1) `optimize._FIXED_VIA_CLEARANCE_MM` is
  pinned equal to `RealizeConfig().clearance_mm` by
  `test_pcb_optimize.py::test_a_land_may_not_sit_on_an_authored_via_but_the_body_may_cover_one`
  — change both or neither; (2) the EWOD dogfood/island route tests assert
  realized-net COUNTS (e.g. `test_pcb_island_terminal_polygon.py::
  test_ring_sink_route_op_realizes_more_escape_nets_with_polygon_touch`)
  that a grid change will move — re-measure, don't treat as baselines.
  The 50x40 mm EWOD board already gets ~0.125 mm pitch; ewod-pcb wants the
  expansion-exhaustion reason split out too (its 15/55 mixes the two).
- **Measured (2026-09-30):** 8/89 realized, 105.7 s at `iters=200`; 54
  `no_path, same-layer-crossing` + 27 `no_path`; 0 job failures. Runtime
  varied 87–211 s across three runs on a loaded machine, so the wall clock
  here is indicative only — the realized COUNT was stable at 8.
- **Open (Reto):** does this outrank `pcb-epro-import.md` slice 1c? He
  chose 1c as next before this number existed. 1c (the copper measurement
  report) is what tells us which of the source board's widths and
  clearances precis' rules disagree with — arguably a prerequisite for
  judging whether the router is being asked for something impossible. But
  a 9% router blocks the thread's actual goal.
- **Open:** whether `iters` should scale with net count rather than being a
  caller-supplied constant, since a per-net budget is what the number
  actually means.
