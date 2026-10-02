# pcb EasyEDA round trip

**Status:** ends when a colleague's EasyEDA Pro board round-trips through
precis (import, re-route to a correct spec, export Pro opens and edits,
order) with every loss warned rather than silent. **Reto's real
140-component 4-layer board now IMPORTS** — slice 1b landed 2026-09-30, so
`precis pcb import-epro` writes components, footprints with real pads and
semantic pin names, the netlist, the outline and the mounting holes, and
derives the stackup from the board's own LAYER records.
**Re-routing is partly fixed.** The router realized 8 of 89 nets with no
reported failure (2026-09-30); main now reaches 65/89 in ~100 s
(2026-10-01 — `pcb-router-fails-at-real-board-size.md`). Re-routing
to a corrected spec is the reason for importing, so the remaining 29 nets
are the thread's central gap.
Slice 1c is complete as of 2026-10-01: the copper measurement report and
`--update` (re-import applying moves and new parts, reporting the rest).
The whole write path (export) is unbuilt.
**On prod since 2026-10-02:** the real board is imported as pcb
`heater-base-test`, APWR/BPWR annotated (td458070 closed), all 89 nets
unrouted. Dogfooding it found gr460567 (courtyards from synthesized pad
sizes; fixed by ewod-pcb, verified on prod: silk_missing 96 → 3 real ones;
closed). DRC still shows 4 annular-ring errors on the SATA connector's
slot holes (imported as round, with a warning; no slot model yet —
gr461213, Horizon 14).
Reto's 2026-10-02 look found the CN1/CN2 standoff rings around no hole
and C27–29 with pad-strip courtyards: the reader dropped footprint FILLs
on the multi layer (KiCad maps them to Edge.Cuts) and `--update` never
refreshed a stored footprint. Both fixed; the board got 15 refreshed
footprints and 8 footprint holes (CN1/2/5/6 Ø6.4, U32/U33 SATA pegs
Ø1.6). A footprint hole is a `mounting_hole` feature (`geom.part`), so it
does not follow a moved part — cutouts inside footprints need a real
model before anything re-places those parts. Worse, found the same day:
the importer had written every mounting hole as `geom: {x, y, dia_mm}`,
a shape no reader takes, so the board's 24 Ø6 holes never reached the
drill file, DRC or router keep-outs. Fixed: position on the feature row,
`geom.diameter`. Prod rows rewritten in place (drills 126 → 158). A part's own
hole is exempt from `courtyard_hole` in DRC and the placer
(`MountingHole.part`, ewod-pcb). Both verified on prod data 2026-10-02
with main's code: DRC has no `courtyard_hole` errors and
`view='congestion'` reads STALE. `view='congestion'` now marks
a `last_route` digest STALE after `op='rip'`.
**Trap:** `op='route'` runs the place anneal first and MOVES every
unfrozen part; it re-placed 103 parts on this board once (restored with
`import-epro --update` + `op='rip'` per net). Measure routing with the
env-gated local real-board test, never `op='route'` on Reto's board.
Collides with ewod-pcb on generator/DRC/realizer files:
sequence, do not merge.
gr457053 is closed: a re-`put` now patches `net_class`/`est_current_a`/
`width_mm`/`note` onto an existing net alongside the 0171 spec columns, so
the annotation step can correct a net's current, not just its voltage.
**Last reviewed:** 2026-10-02 (negotiated congestion landed DARK
825e451aa; the real-board number still waits on review-queue
`pcb-easyeda-round-trip-1`. Export slice 2b's writer landed after design
review, banner "UNVERIFIED" until Reto opens one in Pro — review-queue
`pcb-easyeda-round-trip-2`.)
**Worktree:** `pcb-easyeda-round-trip`

## Do next

1. **backlog/pcb-router-fails-at-real-board-size.md** — this thread OWNS
   the fix (agreed with ewod-pcb 2026-10-01; avoid-list in the item).
   Measured
   2026-09-30 at Reto's request: the router realizes **8 of 89 nets** on
   his real board and the job reports zero failures. Re-routing to a
   corrected spec is the whole reason for importing, so this blocks the
   thread's goal rather than a slice of it. Reto's order (2026-10-01) was
   diagnosis → 1c → fix; all three are on main.
   **Fix state 2026-10-01:** grid pitch capped at clearance×⅔, windowed
   A* with a tightened inner loop, and `search_budget` as its own unrouted
   reason: the frozen board realizes **60/89 in 1096 s** (was 8/89). The
   rest is 13+3 congestion and 10 no_path. Reto ruled (td460164): any
   unrouted net fails the job, numba is fine, and fixed copper through a
   foreign pad refuses the route — all three on main. With numba it takes
   ~100 s, the keep-out is a true disk (it was an L1 diamond, short on
   diagonals), and a pad no longer reads as walled in by the CONTESTED
   sliver between it and a fine-pitch neighbour (the 11 `no_path` nets):
   **65/89**. A history-cost PathFinder term was tried and LOST (62/89 at
   best). Left: 19 congestion + 3 `search_budget`. Negotiated congestion
   is built and lands DARK (item step 13: off by default, opt-in
   `negotiate=N`). NEXT = the real-board number off vs on, which needs the
   `.epro2` back or Reto's OK to run on a prod-row dump
   (review-queue `pcb-easyeda-round-trip-1`).
2. **The real board's design-rule table is dropped at import** — the 184
   records once read as keepouts are 16 `RULE` + 168 `RULE_SELECTOR`,
   which in Pro are most likely the design-rule table and its per-net
   assignments (168 ≈ 2 × 89 nets), not keepout areas (re-read
   2026-10-02; the bodies went with the `.epro2`, so this is unverified).
   If so, re-routing loses the board's own per-net clearance/width, which
   matters more for "re-route to a correct spec" than keepouts do. Needs
   the `.epro2` back (review-queue `pcb-easyeda-round-trip-1`, option 2)
   to read the bodies, then: map rules onto `net_class`/`width_mm`, and
   any genuine rule AREA onto `ftype='keepout'`. The import warning now
   says the rules are lost rather than calling them keepouts.
   `backlog/pcb-keepout-does-not-bind.md` stays a real gap (the ftype
   binds nothing) but no longer has evidence of a keepout on this board;
   it moves to Horizon behind this.
3. **backlog/pcb-missing-constraint-classes.md** §E-1 router half —
   realize/maze draw to per-net clearance and cannot express a pairwise
   term, so the router lays copper view='drc' only flags afterwards.
   Re-routing to a corrected spec is the reason for importing; outranks the
   export half for that reason, not cost. Touches realize/maze, which
   ewod-pcb's generator depends on.
4. **backlog/pcb-epro-export.md** — slice 2b is BUILT (`epro_write.py`,
   `view='epro'`); its acceptance is Reto opening the look-at file in Pro
   (review-queue `pcb-easyeda-round-trip-2`: an asymmetric part at four
   angles on both sides, against `lookat-expected.svg`). Until that passes,
   every later export slice rests on an unverified premise, and the view's
   UNVERIFIED banner stays. On pass: drop the banner and the
   description's "not yet opened" in the same commit.

## Horizon

Import, re-route, export is one arc; split at import/re-route vs
export/fab if this file outgrows itself.

1. **td458069** — a NETTED arc and an asymmetric bottom-side part. Reto
   supplied arc-y.epro2 on 2026-09-30, which closed the ARC record itself;
   what remains is verifying handedness on the way OUT, since its arcs
   carry no net and nothing yet exercises the mirror outbound. Gates 2c and
   2f only; blocks nothing in import.

2. **backlog/pcb-epro-export.md** slice 2c (copper) — waits on 2b and
   td458069's arc; the first export a colleague can inspect, traces
   clicking through to the right net.
3. **backlog/pcb-epro-export.md** slice 2d (pours) — waits on 2c; planes Pro
   re-pours on open, the difference between readable and editable.
4. **backlog/pcb-epro-export.md** slice 2e (silk + editable designators) —
   waits on 2d and SilkPlacement gaining x/y/angle; retypeable refdes text.
4. **backlog/pcb-epro-export.md** slice 2f (deferred set) — waits on 2b–2e;
   each of footprint documents, rule areas, mask openings, NPTH, teardrops
   either round-trips or becomes a warned drop.
6. **backlog/pcb-epro-export.md** round-trip test (0.5 um geometry equality)
   — waits on both halves; the only regression that catches EasyEDA moving
   the format.
7. **backlog/pcb-engine-plan.md** §5 — 2-layer and n-layer stackups; enqueue
   refuses len(stackup) != 4 while the processor already handles 2. Waits on
   nothing; removes the wall a colleague's 2-layer .epro2 hits at import.
8. **gr451356** — a side-insertion connector (USB, card edge) must sit on
   the board edge, and nothing in the imported part/footprint model
   records that; an imported board that re-routes could relocate one to
   an unbuildable position with no warning. Waits on nothing; cheap DRC
   half is buildable independent of the placer term.
9. **backlog/pcb-freeze-mechanicals-and-parts.md** — Reto, 2026-09-30:
   "All should be able to be frozen — nuts, holes etc for sure. But also
   parts ... But only actually freeze when needed."
   `pcb_instances.fixed` is real; `pcb_features.fixed` is unconstrained
   text nothing reads, so a frozen hole is not frozen. Invisible today
   because nothing moves a feature either — it becomes real the moment
   anything does, and it is already the wrong answer to "did my freeze
   take?". Waits on nothing.
10. **backlog/pcb-free-pad-model.md** — Reto, 2026-09-30: "just to
    measure. we should add a free pad model." His board's 6 plated free
    pads on GND are probe points with no precis model, so the import drops
    them and their net loses those connections every time. Waits on
    nothing; overlaps the inert `ftype='testpoint'`, which the item says
    to resolve rather than duplicate.
11. **backlog/pcb-design-source-provenance.md** — waits on the annotation
    step; datasheet-chunk citations behind each pin/net spec the re-route
    trusts.
12. **backlog/pcb-flexboard.md** — waits on 6; the flex half of "all of them".
13. **backlog/pcb-guided-place-route.md** slice 9 (JLCPCB ordering) — the
    workflow's endpoint; gated on a human granting Components/PCB scope in
    the JLCPCB Open API console.
14. **gr461213** — slot holes: precis carries one drill diameter, so an
    imported slot drills round at its long axis (Reto's SATA pads 23/24)
    and DRC flags a ring the real slot has. Waits on nothing; fab-correctness
    blocker before ordering that board.
15. **backlog/pcb-keepout-does-not-bind.md** — `ftype='keepout'` binds
    nothing in placer, router or DRC. Demoted from Do next 2026-10-02:
    the real board's "keepout" records look like its rule table (Do next
    2). The placer half belongs to ewod-pcb's obstacle-set item; this
    thread would own router + DRC + import. Waits on Do next 2's reading
    of the RULE bodies.

## No action needed

- Spike items S2–S12 and R1 in backlog/pcb-epro-export.md — resolved
  2026-09-29 against the real board; R2 and R3 stay open inside items 4 and
  2.
