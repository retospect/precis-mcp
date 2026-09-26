---
status: draft
title: EWOD round 9 — drive the array from OUTSIDE it, so escape is a fan-out and not a search
prio: high
---

# EWOD round 9 — drive the array from OUTSIDE it

## Motivation / why

Round 8 shipped a board where 34 of 62 nets realize. Four measured
experiments this round (probes 1-4, `tests/test_pcb_*_probe.py` at the
time of writing, deleted on ship) say the remaining 28 are not a tuning
problem:

| lever | escapes realized (of 54) |
| --- | --- |
| baseline | 13 |
| electrode pitch 2.0 -> 2.25mm | 15 (measured in prod, round 8) |
| maze clearance 0.15 -> 0.05mm (below ANY fab minimum) | 16 |
| per-net layer lock lifted | 15 |
| **generator's own escape fabric not claimed as an obstacle** | **22** |

> **READ THIS BEFORE TRUSTING THE TABLE.** Every row except the pitch one
> was measured on `tests/test_pcb_ewod_dogfood.py`'s fixture, whose HV507
> stand-in is `_grid_footprint(cols=9)` -- a SOLID 8x9 grid, 1.0mm pitch,
> 0.5mm pads, so ~31 pads are INTERIOR. At the 0.15mm the grid dilates by,
> those interior pads are unreachable by construction. Prod's real
> C639448 is a PQFP-80: **80 pads, ZERO interior**, 17x23mm (queried
> against prod 2026-09-26). The fixture manufactures a wall prod does not
> have, which is why it sits at 13/54 while prod sits at 34/62. Re-measure
> against a ring-shaped stand-in (item 0) before designing to these
> numbers.

Three dimensional levers move the number ~5% each. Removing the fabric
from the obstacle set moves it 70%. And the verdict on the failures is
`no_path`, which `_diagnose_unrouted` only returns after a probe grid
with *every other net's copper cleared away* fails at near-zero width:
"walled in, not merely too tight". The wall is not made of gap. It is
made of the fabric's own copper.

**The fabric exists because the driver sits UNDER the array.** That one
placement decision causes all of it: electrodes must drop through vias to
reach a driver on B.Cu; the vias and their stubs are 108 fixed tracks +
54 fixed vias of B.Cu congestion; `_claim_fixed_copper` claims each row
with its own net as owner, so every escape's stub blocks the other 53;
and the sink's pads overlap the array, which is gr451052's nine
keepout errors.

Move the driver off the array and none of that is needed. On B.Cu, with
no electrodes above competing for it, 64 vias in a grid fan out radially
to a driver on the perimeter. That is a planar problem with room to
spare: escaping the innermost ring of an 8x8 needs 3 traces between
adjacent perimeter vias; at 2.25mm pitch with a 0.6mm via land that gap
is ~1.65mm, and at 0.1mm trace + 0.09mm clearance it fits ~8.

The code already says this is the intended topology. The coating check's
own polarity argument states "outline margin, connectors and the sink
grid all sit outside it" (`generators.py`, `_coating_verdict`) while
`sink_grid` places the sink underneath.

## In scope

0. **Fix the fixture first, then re-measure.** Give the HV507 stand-in a
   PERIPHERAL pad ring (80 pads, no interior), matching the real
   C639448's shape, and re-run probes 1/2/4. Everything below is designed
   against the re-measured ranking, not the table above. This is a test
   change only and gates the rest of the round.

1. **A driver placement that does not overlap the array.** The generator
   grows a keep-clear region for the electrode field and places the sink
   (or sinks) outside it. Prefer: driver on the perimeter, rotation left
   FREE so the placer can use it.
2. **Unpin what must move.** Today every instance carries `fixed='both'`,
   so `optimize`'s existing `ROTATE` move (90 degree steps) and its
   existing `crossings` cost term are both dead code on this board. The
   generator should pin only what genuinely must be pinned (the electrode
   array itself) and leave the driver's rotation, at minimum, free.
3. ~~Escape as fan-out, drop the stub fabric.~~ **DELETED after review.**
   The B.Cu breakout stub is not there to reach the sink -- it is there
   to get the escape's start terminal OUT of its own 8-via plaza ring.
   Dropping it reinstates the "walled in even routed alone" failure
   recorded at `realize.py:1813-1826`, and the stub already points
   radially outward (`_breakout_far_point`). Keep it. Note also that the
   vias are not a 64-point grid: they sit 8-per-plaza in 9 clusters, with
   ~4.4mm of open B.Cu between clusters, so the fan-out that matters is
   cluster-to-cluster.
4. **Pad set sourced from the footprint** (gr451276). A pad is physical;
   it occupies space and nothing may route through it whether or not a
   net names it. Add a footprint-sourced, pad-number-keyed pad set to the
   IR, claimed on the occupancy grid and checked by DRC, SEPARATE from
   pin identity.

   **Corrected premise** (the gripe and this item's first draft both had
   it wrong): the design declares **59** pins, not 69 -- 69 was the
   FIXTURE footprint's `pin_map`. And the missing-pad gap is **SVG-only**:
   the fab SVG renders via `_drc_pads` -> `pads_for_ir` (IR-driven), but
   the gerber zip goes through `board_pads`, which emits every footprint
   pad. So the manufactured artifact is complete and the VIEWER is not.
   The serious half is neither: the ROUTER's obstacle set is
   `pads_for_ir` too, so copper can be routed through a pad that the
   gerber does contain -- a real short, not a cosmetic gap.

   Known break sites for a separate pad set: `realize.py` router pads
   (:1287), `_stamp_pads` (:1838), `pads_for_ir` (:5600), `_pad_blockers`
   (:2315), `_fixed_copper_connectivity_model` (:4846 -- a second
   same-named pad becomes a net island and mints new connectivity errors
   unless it is net-less), and the courtyard hull built from IR pins
   (`ir.py::instance_courtyard_polygon`, feeding `courtyard_overlap` and
   `_courtyard_body_mask`).
5. **Pre-route DRC gate** (gr451274a) — refuse to route a board that is
   already unmanufacturable. Cheap once (4) lands, and meaningless
   before it.

## Explicitly NOT in scope

- Promoting footprint pads to IR **pins**. Pin ids are load-bearing for
  pin swaps, net indexing, and `pin_to_net`-by-name; adding ~20 pins per
  driver surfaces in all three. Pads get their own set.
- Touching the star decomposition or the occupancy grid's per-cell
  ownership. The router depends on the star (a true MST reddened the
  ESP32-C3 acceptance board at every seed).
- The `max()` clearance collapse. Two real defects live there — the 0.15
  literal in `RealizeConfig` (`realize.py:180`, applied at `:1337`)
  outranks every computed per-net rule, and the collapse discards the
  per-net values. Separate gripe, not this round — BUT the "worth ~3
  nets" estimate came from the broken fixture and is probably low: with a
  ring footprint, 0.15 is what closes the QFP's own ~0.8mm pin gaps.
  Re-judge after item 0.
- Placement illegality as a placer cost term (gr451274b/c). On this board
  the colliding part is placed AND pinned by the generator, so the fix is
  in the generator, not the optimizer. `optimize` already has
  `courtyard_overlap`.

## Open questions / decisions log

- **Fresh board vs. amend `ewod-dogfood-2`?** Leaning fresh: the topology
  change is large enough that a diff against the old board is not
  informative, and a new vehicle can carry the controller/USB/power the
  old one lacks. Board size is free to change.
- **Controller + USB + power on-board.** Wanted. Needs part picks.
- **Edge-insertion connectors have no representation.** A side-insertion
  connector MUST sit on the board edge with a specific outward
  orientation, and nothing in the part/footprint model records that, so
  neither placement nor DRC can honour or check it. Filed separately;
  this round should not invent it ad hoc.
- **Rotation is safe for the fabric, but NOT free elsewhere.** Every
  fixed-copper row is positioned from `layout`, never from the sink's
  pose, so an unpinned sink cannot invalidate its own fabric. The traps
  are: `PinSwapGroup.offsets` are computed once from `pin_point` BEFORE
  the anneal and read raw, so a `ROTATE` leaves swap scoring on stale
  offsets; and `pcb_route` writes the anneal's pose back via
  `pcb_set_pose`, so an unpinned sink MOVES on every `op='route'`.
  Decide whether rotation is unpinned for a one-off solve or permanently.
- **Channel assignment must face the array.** `pinswap.
  propose_radial_assignment` assumes the far ends sit "inside or around
  that ring"; with the driver outside, the far side of a QFP gets chords
  through its own package. The generator likely has to bias channel
  assignment to the array-facing sides.
- **HV-vs-control steering.** `cost.py` is already net-class aware
  (`ir.net_class[net_id]` feeds coupling/thermal) but `cost.py`'s own
  note says per-class rules are not fully wired into the IR yet. Decide
  whether rotation + `crossings` alone gets the tangle acceptable before
  adding a class-separation term.

## Acceptance criteria

- The driver's courtyard does not intersect the electrode field: zero
  `via_pad_keepout` and zero courtyard findings between the array and its
  driver (gr451052 closes).
- Every footprint pad on every placed instance appears in the router's
  obstacle set, in DRC's pad set, and in the fab render. Pinned by a test
  that gives a part more footprint pads than the netlist declares.
- Escape yield is reported against the CORRECTED pad set. Expect the
  baseline to DROP first: 18 pads were being routed through illegally,
  and a lower honest number is the right outcome.
- `tests/test_pcb_reference_end_to_end.py` and
  `tests/test_pcb_fab_render_all_layers.py` green (other shards — run by
  name).

## Probe recipe (how the table above was measured; regenerate after item 0)

The probes were throwaway test files, deleted rather than shipped -- they
always pass, they are measurements not contracts, and each costs ~3
minutes. Regeneration is mechanical. Each one imports `_seed`,
`_drain_one_job` and the `pcb` fixture from
`tests/test_pcb_ewod_dogfood.py`, drives `pcb.put(id=slug, args={"op":
"route", "seed": 1})`, drains the in-process job, then reads
`store.pcb_route_status(ref.id)` and counts nets named `ARR1_R*`. The
lever is a `monkeypatch` on `precis.pcb.realize`:

- **clearance**: wrap `_resolve_track_rules` to `dataclasses.replace(r,
  clearance_mm=min(r.clearance_mm, X))` AND wrap `RealizeConfig` with a
  factory that defaults `clearance_mm=X`. Both are needed -- the config
  default is a floor, and any net with no class override falls through to
  the fab HOUSE tier (0.15), so patching either alone changes nothing.
- **layer lock**: replace `_net_class_layers` with
  `lambda ir, n, config, signal_layers: (list(signal_layers), None)`.
- **fabric self-block**: replace `_claim_fixed_copper` with a no-op.
  Island terminals are built separately, so the escapes keep the plaza
  vias they terminate on -- the arms differ in what BLOCKS, not in what
  is reachable.

Write results to a JSONL file under the worktree root, NOT `/tmp`:
`scripts/test` runs in a container and only the worktree is mounted.

## Target + blast radius

`pcb/generators.py` (`_expand_ewod_pad_array`, `_parse_sink_grid`),
`pcb/ir.py` (pad set), `pcb/session.py` (`apply_real_pin_offsets`),
`pcb/realize.py` (`pads_for_ir`, `_claim_fixed_copper`), `pcb/drc.py`,
`handlers/pcb.py` (`view='drc'`, `board_pads`).

---

# Absorbed 2026-09-26

## EWOD checker round — resume state

_Grouped 2026-09-26; was `pcb-ewod-checker-round-state`, status ready, prio high._

**Absorbed 2026-09-26: round 9 supersedes its per-net-clearance lead; its three delete-gates (confounded connectivity re-measure, 2.25 mm negative verdict, driver collision) now live here. Slices 5-9 pointer belongs to pcb-lazy-netlist-and-checks.**

Written 2026-09-25, still live 2026-09-26. **This is a resume pointer, not a
spec.**

**Do not delete it on the old criterion.** It used to say "delete once the DRC
re-measure has run and its outcome is recorded on the gripes". The re-measure
HAS run (`a55ae1728cea44a9`, recorded on gr449483) — but the round did not end
there, and three things below outlived it:

1. the re-measure is **confounded** (checkers and geometry moved together), so
   the connectivity question it was built to settle is still open;
2. the 2.25 mm pitch verdict is **negative** and worth not re-deriving;
3. the array now collides with its own driver chip — an open layout call.

Delete this file when those three are resolved, not before. The round's goal
was one thing — **an honest DRC error count on prod `ewod-dogfood-2`** — and
every item below is either in service of that or a defect found while
pursuing it.

### The governing finding

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

### Shipped

- `aa861102` on `origin/main` — `connectivity.net_islands` threads real polygon
  outlines through `_touch_gap` (gr449483); two regression tests; the pcb
  check-surface specs; the EWOD controller/HV item.
  **UNGATED** (`/qland`) — no ruff/mypy/pytest ran on the merge. Passed
  10222/28 locally pre-ship. Debt settles at the next full gate.

### No longer uncommitted — both landed 2026-09-25

The two sibling worktrees this file used to warn about are **in `main`**. The
warning above them ("most likely to be lost") is discharged; do not go looking
for those trees.

| commit | fix |
|---|---|
| `3c8db49a` | `drc.check_via_pad_keepout` polygon-aware (gr346004) + `ratsnest` side-aware via bias (gr449579) + generator anchor diagnostics |
| `5b91abe1` | rect/obround pads use their real outline in `connectivity` and `check_outline_containment` (gr450064, gr449709 — both now `STATUS:done`) |

So **all four** checker fixes are on `main`, not one of three. THREE of
them are also deployed: the deploy-state marker reads `4db6836b`, written
2026-09-25 13:41:15Z. Only `5b91abe1` (rect/obround outlines) post-dates it,
and that one is in the `0a4cf18d` ship. The "103 commits undeployed" figure
this file used to carry is long stale.

The ratsnest fix is **no longer inert**: its `bottom` set is now built from
`padplace.is_bottom_instance` and passed at `handlers/pcb.py`'s ratsnest view.
Segment decomposition itself stays a STAR — see the td450119 entry below for
why the spanning tree was reverted.

### The baseline, recorded so the re-measure stays checkable

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

### Sequence to the honest tally

1. ~~Qland the two sibling worktrees.~~ Done — `3c8db49a`, `5b91abe1`.
2. ~~Deploy.~~ **DONE 2026-09-26 05:19Z — cluster on `81ccb10a`, MCP install
   on `eee5edf4`.** The note this step used to carry was half wrong and the
   correction matters: yes, `op='route'` and `view='drc'` run
   `executor='job_inproc'` on the MCP's build rather than the cluster's — but
   that does NOT mean a re-measure can skip the deploy, because the MCP
   install is itself refreshed BY the deploy. A bare `/mcp` reconnect leaves
   it pinned. Check `get(kind='skill', id='precis-status')`'s `git_sha`
   before trusting any measurement.
3. Re-route **with a varied seed**. `op='route'` is idempotent per
   `(design, op, content-hash)`, and the hash covers netlist/placement state
   but NOT the code version, so an unchanged design silently returns the prior
   job's result even across a deploy.
4. Re-measure DRC. ~~Prediction: the tally roughly halves and `connectivity`
   drops hardest.~~ **RUN 2026-09-25 23:46:09Z, `a55ae1728cea44a9`: 132 err /
   162 warn -> 90 err / 169 warn.** The tally fell by a third, not half, and
   `connectivity` did NOT drop hardest (-16, against clearance's -31 errors
   and -47 warnings). That does not refute gr449483 comment 2, because the
   geometry moved in the same step; settling it needs the checkers re-run
   against the OLD fabric offline.
5. Only then decide what is actually broken.

Note that `op='route'` does **not** re-run the generator — see td450118.

### Open gripes in this class

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

### Deferred deliberately

**Do not wire connectivity into the `realized` predicate yet.** It was built and
reverted during this investigation: it made `route-status` and DRC agree only by
making both wrong the same way, regressing
`test_ring_sink_route_op_realizes_more_escape_nets_with_polygon_touch` from
29 to 25. Correct order is fix -> re-measure -> then wire. The wiring belongs in
the route job (`workers/job_types/pcb_route.py`), not in `realize.py` — that
module's docstring forbids the producer calling its own checker.

### td450118 — closed 2026-09-25, and what it changed on prod

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

#### What the re-apply cost, and the defect it exposed — gr451046

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

**gr451046 is now CLOSED — both halves fixed 2026-09-26.** `9bc27528`: the
net-retire UPDATE skips any net still carrying a connection from a LIVE
instance, so a net the generator does not solely own survives and the
re-expansion attaches to it by name. `a10708e9`: the `_toc` summary drives from
live nets and left-joins routes, exactly as `pcb_route_status` does — it used
to count raw `pcb_routes` rows, disagreeing with `view='route-status'` in BOTH
directions (119 rows on a 62-net board, and live routeless nets omitted). Both
were verified in the FAILURE direction, guard removed until the tests went red,
then restored.

**Still true for OLD boards:** any design whose generator was re-applied before
those landed may already be severed. Only `ewod-dogfood-2` was repaired.
Symptom to look for: a net reading `realized (dangling net (<2 members))`.

#### Ruling 10/11 bought two nets, not the escape class

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

### The array now collides with its own driver chip — a LAYOUT call (gr451052)

The fresh DRC run after td450118 shows nine `via_pad_keepout` errors, up from
two. They are **real**, and the two-hour detour spent proving they were not is
the more useful record.

What they report: a plaza escape via belonging to one electrode sits
**0.008–0.225 mm from the HV507 driver's own solder land for an unrelated
channel**, against a 0.090 mm minimum. That is the solder-wicking risk the rule
exists to catch. It is new, caused by td450118 growing the array (pitch
2.0 → 2.25) over a driver footprint whose real pin positions were never checked
against escape-via placement.

**The misreading, recorded because it is a trap the whole board is shaped
like.** The findings name the pad by NET, and on this board a net name is not a
location: every one of the 54 escape nets has two members in two different
places — the electrode on F.Cu and a driver channel pin on B.Cu. So
`pad[ARR1_R3C4]` was read as "the R3C4 electrode", 10 mm away, and the findings
were declared arithmetically impossible. All nine name **B.Cu** pads, and no
electrode body is ever on B.Cu. Verified twice: the structured `pad_layer`
column (9 of 9) and the live netlist (each net exactly two members, ARR1/RxCy
plus ARR1_SINK_0/HVOUTn).

**The fix that was one step away would have done real harm.** Loosening the
check, or filtering findings whose named net sits far from the via's own net,
would have deleted a genuine fab defect AND very likely re-opened the two
ARR1_RESV false positives `3c8de49a` fixed — same code path.

Two separable outcomes:

1. **Shipped:** the finding now names `<refdes>/<pin> (net <net>)` and carries
   `pad_refdes`/`pad_pin` in its `objects`, so this cannot be misread the same
   way twice. `pads_for_ir` already put both on every pad, so it was free.
   Nothing else in the checker, `_copper_item_polygon`, `pads_for_ir` or the
   generator needed changing — a replay of the array's own generator through
   `expand → build_ir → pads_for_ir → check` produces zero findings.
2. **Reto's call, not made here:** move the sink, reassign channels so a via's
   neighbours are its own net, or teach the generator's via placement to
   respect the sink's real footprint.

**Generalisable:** any check reporting by net name alone on a multi-member net
invites this misreading, as does any other generator wiring a real catalog
part's real pins onto per-cell net names.

### Post-deploy dogfood, 2026-09-26 06:38Z — fixes confirmed live

Cluster deployed to `81ccb10a` at 05:19Z (1h26m, dragged by castor memory
exhaustion — gr450103). The session MCP install then swapped to `eee5edf4`,
which contains `98889951` and so every pcb fix.

**Record this, it cost an hour:** the MCP install DOES refresh with a deploy.
Earlier restarts left it pinned at `4db6836b` only because nothing had been
deployed. A bare `/mcp` reconnect does not move it; the deploy does. Since
`op='route'` and `view='drc'` use `executor='job_inproc'`, the MCP's build —
not the cluster's — is what gates whether a re-apply is safe.

DRC run `469661a7`: **90 err / 169 warn, identical to `a55ae1728` rule for
rule.** That is the correct result, not a null one — no geometry changed, and
the fixes that landed were about how findings are worded and counted. A moved
tally would have meant something was wrong.

Routed copper beside it, since a DRC count alone is meaningless: `pcb_copper`
46 track + 8 via; `pcb_fixed_copper` 108 track + 54 via **live** (a first read
of 162/108 was counting retired rows — the same filter mistake this board
keeps generating).

Confirmed live on the deployed build:

1. **gr451052 identity** — all nine keepout findings now read
   `via clears ARR1_SINK_0/HVOUT44 (net ARR1_R3C4) by -0.225mm`, with
   `pad_refdes`/`pad_pin` in `objects`. The driver collision is legible
   instead of reading as two electrodes 10 mm apart. (Channel numbers moved —
   R3C4 is HVOUT44, was HVOUT24 — because ruling 2's chain-order assignment
   reassigned them at the re-apply.)
2. **gr451046 headline** — the TOC prints `## route status: 28 failed, 34
   realized` = 62, matching the live net count and `view='route-status'`. It
   printed 119 before.
3. The three severed nets read fanout 2 and plain `realized`.

**A trap found while doing this:** passing `args={'rule': 'via_pad_keepout'}`
to `view='drc'` is SWALLOWED — it returns the unfiltered report rather than
an error, and that report is ~25 KB. Read findings from `pcb_drc_findings`
instead. Note each `view='drc'` call mints a NEW run.

### Decisions parked with Reto

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

### Housekeeping

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

### Round 9 supersedes the "per-net clearance" lead (2026-09-26)

**Next round is specced in `pcb-ewod-perimeter-driver-round.md`. Read it
before picking anything up from this file.**

Four measured probes retired the lead recorded just above. Per-net
dilation (slice 9) buys ~3 nets, not a round: forcing the maze clearance
from 0.15 mm down to an unmanufacturable 0.05 mm moved escapes 13->16 of
54. Pitch moved it 2, lifting the layer lock moved it 2. What moved it 9
was not claiming the generator's own escape fabric as an obstacle.

**But the fixture lies.** All of those were measured on
`test_pcb_ewod_dogfood.py`, whose HV507 stand-in is `_grid_footprint(
cols=9)` -- a SOLID grid with ~31 interior pads that are unreachable by
construction at any clearance the grid dilates by. Prod's real C639448 is
a PQFP-80: 80 pads, ZERO interior (verified against prod 2026-09-26).
That is why the fixture sits at 13/54 and prod at 34/62. Fix the fixture
to a peripheral ring and re-measure before designing to any of it -- the
regeneration recipe is in the round-9 item.

The real direction: the driver sits UNDER the array (`sink_grid`), which
is what forces the via fabric, the B.Cu congestion AND gr451052's
collision. The generator's own coating check already asserts the opposite
topology ("connectors and the sink grid all sit outside it"). Move the
driver off the array.
