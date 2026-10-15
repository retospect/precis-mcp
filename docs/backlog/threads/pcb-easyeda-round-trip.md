# pcb EasyEDA round trip

## Resume

- **Pillar:** 3d-design
- **Next:** Reto opened the regenerated taz15 `.epro2` in EasyEDA Pro on 2026-10-14: clean import, banner lifted. Pours from an already-poured source now open filled via `POURED` passthrough; precis-authored pours stay unfilled until rebuild copper. Catalog identity, local-first footprints and pin semantics landed (a7863eda3); pcb-datasheet-review slice 1 shipped. Still open inside 2f: rule AREAS, mask openings, teardrops (no record shapes). Router work stays with ewod-pcb. Full 2026-10-14 log: [Thread context](#thread-context).
- **Blocked by:** Nothing for 2d/2e. Reopened 2026-10-07 by the coordinator (Reto's order: route/place first, then EasyEDA). Reto's 2026-10-14 open verified the file (banner lifted); the pour-fill gap it surfaced is now carried via `POURED` passthrough. Shared router/realizer files belong to [ewod-pcb](ewod-pcb.md#resume); sequence, do not merge.
- **Unblocks:** A corrected board that routes and round-trips.
- **Acceptance:** Use [pcb-router-fails-at-real-board-size](../pcb-router-fails-at-real-board-size.md) and [latest handoff](#thread-context); compare against the latest 67/89 result and alignment/DRC findings, not the initial 8/89.
- **Worktree:** `pcb-easyeda-round-trip`
- **Builds:** Not estimated here; use the owning item's current slice estimate.
- **Detail:** [Ranked work](#do-next) · [Horizon](#horizon) · [Coordination map](INDEX.md).

## Thread context

**2026-10-14 resume log (moved from Resume):** Reto verified the regenerated `epro.local/taz15-roundtrip.epro2` in EasyEDA Pro on 2026-10-14 — import clean, parts and board content all present — so the UNVERIFIED banner is lifted (export response now says Verified). His one gap: pours opened unfilled, because Pro displays the computed fill from a `POURED` record's `pourFill` cache (10-mil units — the trap), which we now carry through `meta.epro_pours` and re-emit verbatim, pairing `['POURED', <pour id>]`; a board the source had already poured now opens filled, and a precis-authored pour still opens unfilled until rebuild copper. First open attempt 2026-10-14: import reported success, but all schematics were absent (expected — the writer emits no SCH/SCH_PAGE/DEVICE/SYMBOL docs by design; the export response now says so) and the PCB doc looped loading on two record-shape bugs, both fixed: 13x `startsWith` on missing LAYER color/transparency fields (the writer now emits Pro's full 93-record LAYER palette per doc, verbatim) and `NaN is not permitted` in addText (all 679 catalog ATTRs carried null x/y — every text slot is now numeric, hidden attrs parked on the component origin). The same pass brought every emitted record to Pro's field shape (DOCHEAD user/client, PAD/VIA/LINE/POLY/POUR/FILL/COMPONENT/STRING field sets, footprint-doc board-state block + Name/Designator attrs), replaced the invented NET record with the source's own stale-PAD_NET mechanism for memberless nets, and added a shape-parity test (a Pro-shape spec in CI + a per-(docType,type) diff vs the source when taz15 is present). Catalog-identity slice landed 2026-10-14 (Reto "fix it" + the DRY/family-datasheet amendments): the import now carries each component's LCSC C-number (`Supplier Part` → `part_lcsc`), value→`label`, MPN/manufacturer/supplier into `meta.part`; local footprints resolve FIRST everywhere (the join that used to force `part_lcsc IS NULL` is gone), a local copy identical to the cached catalog footprint is dropped AT import (no deferred sweep; holes always keep the row; `meta.local_fp_lcsc`/`local_fp_diff` records `match`/`differ`/`uncached` + why); the `datasheet_pull` trigger fires on imported C-numbers and `datasheets.link_known` links a sheet the store already has — one ref, one `datasheet-of` link per C-number. Taz15 proof now diffs 85 C-numbers (exceptions exactly JP1/JP2/C13–C17/C19/C20/C21/C24 — none on the source), value/mpn/mfr/supplier verbatim per part, and pin semantics (62 typed pins, 14 nc) verbatim — same-day pcb-datasheet-review **slice 1 SHIPPED in full** (marked in that item): symbol `Pin Type`→`etype`, schematic `NO_CONNECT`→`nc`, sch `link`→`part_meta.link`, part lookup mfr+MPN→C-number→unique parametric, TOC `fixed` column, and the `+3V3`/`3V3` split verified as the SOURCE's own topology (real two-member `+3V3`: C17.2+U9.VCC), not an importer bug. Pin semantics round-trip through a private `Pin Types` JSON ATTR (written files carry no symbol docs). 2f landed 2026-10-14 (per-distinct-footprint docs, NPTH, polygon pours, rules write-back) plus the rule-table import (V1.15's table is **19 RULE records and ZERO RULE_SELECTORs**, on every one of its 6 PCB documents — the earlier "16 + 168" was a guess) plus the structural round-trip test: import → `view='epro'` → re-import on the real board diffs **96 parts / 396 pads / 386 connections / 749 copper segments / 4 pours / 19 rules / 16 outline verts / 5 holes all within 0.5 um** (worst pad 0.3 um, worst elsewhere 0.000), and the committed tiny fixture runs the same diff in CI. Slice 2c (660a2eccf) round-trips the asymbendtest fixture to 0.001 um (td468296 closed); 2d/2e landed the same day (a5534de1c); all three ride round 6's deploy (bb244e093). Still open inside 2f's set: rule AREAS (no EasyEDA record shape observed — an undecoded RULE warns it "binds nothing"), mask openings (warned drop, no record shape), teardrops (derived geometry — Pro regenerates them on repour; precis does not author them). The same session runs ewod-pcb first while Reto's 2026-10-07 routing/placement/pin-swap investigation is open (ordered by Reto via chat-interface, ahead of EasyEDA); resume here when that note lands. Do next 1 (the router on the real board) is now ewod-pcb's router question too — measure there, do not re-plan it here.

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
unrouted. Dogfooding it found a courtyard bug (courtyards from synthesized pad
sizes; fixed by ewod-pcb, verified on prod: silk_missing 96 → 3 real ones). The SATA connector's slot holes (gr461213) import as
slots now (pad `slot`, Excellon G85, slot-aware annular ring); re-import
the board to clear its 4 false annular-ring errors.
Reto's 2026-10-02 look found the CN1/CN2 standoff rings around no hole
and C27–29 with pad-strip courtyards: the reader dropped footprint FILLs
on the multi layer (KiCad maps them to Edge.Cuts) and `--update` never
refreshed a stored footprint. Both fixed; the board got 15 refreshed
footprints and 8 footprint holes (CN1/2/5/6 Ø6.4, U32/U33 SATA pegs
Ø1.6). Footprint holes now live on the footprint (migration 0197) and follow
the part, optimizer included; `--update` retires the legacy part-owned
`mounting_hole` features and warns about measures still naming them. Worse, found the same day:
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
`import-epro --update` + `op='rip'` per net). On heater-base-test Reto
lifted this 2026-10-02 (review-queue `pcb-easyeda-round-trip-4`): it is a
test piece, the anneal may move its 103 unlocked parts, and it gets
routed on prod as is. Import locks only the parts the source locked
(`--freeze` locks all). A same-day freeze-all default was reverted: it
contradicted his 09-30 "only freeze when needed"
(`pcb-freeze-mechanicals-and-parts.md`). Per Reto's ruling on review-queue
`pcb-easyeda-round-trip-5`, the import report lists likely alignment-critical
parts without locking them, and the author pins one with the `align` measure
(within one board; hard align snaps after the anneal; precis-measures-help).
The cross-design half (a part aligned to another design's feature) rides
pcb-se-binding.
Collides with ewod-pcb on generator/DRC/realizer files:
sequence, do not merge.
**Reopened 2026-10-07** (coordinator, on Reto's "route/place first, then
EasyEDA import/export"); owner is the `pcb` session that also runs
ewod-pcb. First slice after reopen: export 2c (copper), landed the same
day. **Parked 2026-10-03 (TIER 3, Reto's priority review):** start no new
slice until the organizer reopens the thread. Last landed: 044b67bff
(round 4): the route dedup key carries `pcb_route.CODE_VERSION`, and the
sketch SVG restores pin swaps first. Round 3 (403b08026, measure-sort
crash) is live in 929107f32; its dogfood is job 464286 below. Waiting,
not owned by this session: review-queue `pcb-easyeda-round-trip-2`
(Reto opens the look-at in Pro). On reopen, resume at Do next 1.
**Round-2 dogfood (2026-10-03, prod at 63301c5c):** job 462706 finished
on round-1 code: 70/89 realized, 19 failed, 0 stripped by the post-route
gate, 398 vias (unfrozen: the anneal moved parts). That anneal moved the
standoffs CN1/CN2/CN5/CN6 1.0–5.4 mm off their own Ø6.4 holes (features
47–50 then did not follow the part; the footprint holes now do). Four hard `align` measures (CN→its hole,
offset 0) are redundant once `--update` retires 47–50; they were on the board and read VIOLATED 1.019/3.104/5.358/4.559,
as expected before a re-place. The follow-up `put(op='route', seed=1)`
crashed in `session.content_hash`: two measures tied up to their dict
operands raise TypeError in `sorted` (any board with two same-shaped
measures). Fixed by sorting rows by their JSON form (main 403b08026);
this changes the dedup key once for boards with two or more measures.
Re-route job 464286 (enqueued via `scripts/prod-precis`, ran on
63301c5c): failed as ruled, 67/89 realized, 22 failed, 0 stripped, 373
vias. Standoffs: CN1/CN5 `snapped` onto their holes (ok, 0 mm); CN2/CN6
stay VIOLATED at 1.98/3.835 mm. The anneal's hard pull lost to
congestion, and the snap only closes residuals under 0.5 mm. New: one
pre-route DRC error, U4/10 (BTN1) pad touching C6/2 (VCC) on B.Cu
(0 mm < 0.09 mm), in the anneal's output placement; not yet diagnosed.
For a part whose position a fixed datum fully determines, the anneal
should not search at all: place it on the datum and freeze it. Added
to Horizon 9; not started while parked.
**Last reviewed:** 2026-10-02 (negotiated congestion landed DARK
825e451aa; the real-board number still waits on review-queue
`pcb-easyeda-round-trip-1`. Export slice 2b's writer landed after design
review, VERIFIED by Reto 2026-10-14 — review-queue
`pcb-easyeda-round-trip-2` closed.)
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
   best). Negotiated congestion landed DARK (825e451aa) and was measured
   2026-10-02 on the prod-row dump (Reto approved): **55 → 56/89 at
   2.5× the time**, so it stays off. The current baseline is 55, not 65:
   the board now carries its real holes, and the post-route DRC gate
   strips 13 realized nets (method + tags in the item, step 13).
   Classified 2026-10-02: all 13 were router faults. 12 sat inside the
   0.45 mm NPTH clearance of the Ø6 holes, and same-net vias overlapped
   drills. Two router fixes (NPTH claim radius, net-blind via-to-via
   keep-out), accepted in review round 5, leave **zero nets stripped by
   the gate**. Routed count: 67/89 at seeds 1, 2 and 3 (frozen board,
   `scratch/pcb_route_offline.py` method, item step 14). With every part
   frozen the seed has nothing to vary, so the three runs agree by
   construction; they show determinism, not robustness. The remaining 22
   are all `unrouted`, i.e. congestion. Own-path via pairs inside one
   search are kept a keep-out apart by the router itself since
   2026-10-13 (`maze.py::OccupancyGrid._own_vias_apart`).
   **taz15 measured 2026-10-15 (builder taz15-loop, dev replay of the
   prod snapshots, branch `worktree-agent-ad15fd57082a9e156`):** the
   0.000 mm pad hits in jo484766 were the route job's OWN annealed
   placement, and prod's taz15 now carries jo489777's annealed poses
   (186 placement errors; the source poses live on `taz15-blank`) —
   gr489911/gr489913 (op='place' now refuses a placement worse than
   the stored one; pinning parts to their copper is part 2). At the source poses with every part frozen: the
   categorical `courtyard_overlap` error refused the route on 30 pairs
   the fab built (gr489912, fixed: graded on lands);
   `taz15` with its source copper then routed 146 → **150/151** after
   the plane fan-out stopped drilling drop vias for pins the pour or a
   source via already serves (gr489924, unlanded: branch
   `worktree-agent-ad15fd57082a9e156` 7731da53b); the last
   net is gr489175. `taz15-blank` from scratch: 138/151 as imported
   (GND/3V3 traced, no outer pours), 139/151 with GND→B.Cu and
   3V3→F.Cu declared — 8 of the rest are CN4's 0.5 mm-pitch escapes
   (gr489923), 2 drop-via sites (GND/PV+), 2 search budget. The
   source's 7 `via_via_keepout` errors still need `force=True` on
   `taz15` (gr489914; the doubled via is deduped at import).
2. ~~The real board's design-rule table is dropped at import~~ — **DONE
   2026-10-14 in `p1-epro`.** V1.15's table is 19 `RULE` records on every
   PCB document and ZERO `RULE_SELECTOR`s (the "16 + 168" guess was wrong).
   `extract_rules` maps `TRACK.stroDef` → `track_width_mm`, the widest
   `SAFE.safeSpacing` cell → `clearance_mm`, `RADIUS.defRadius/defInner`
   → via dims, into three net classes (`Default` 0.30 mm/0.152 mm,
   `Wide` 0.308 mm, `Xtra` 2.0 mm); per-net categories only — the
   singleton categories (`OTHER`, `CREEPAGE`, `SOLDER`, …) are
   board-global and map to nothing. No selectors ⇒ the DEFAULT group
   lands on all 151 nets. Every record is preserved verbatim in
   `meta.epro_rules` and the writer emits it back byte-identical;
   `RULE_SELECTOR` records would ride the same passthrough, applied to
   nothing, with a warning (their body shape was never observed).
   `backlog/pcb-keepout-does-not-bind.md` stays a real gap (the ftype
   binds nothing) but this board has no keepout to test it with; it
   stays in Horizon.
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
   Reto opened it in Pro on 2026-10-14 — import clean, parts and
   board present; banner dropped. His one gap (pours unfilled) is fixed
   by emitting the source's cached `POURED` fill verbatim. **2c (copper) BUILT
   2026-10-07:** every track segment as `LINE`/`ARC`, every via as a
   through-hole `VIA` (blind/buried flattened WITH a per-via warning),
   nets declared before use, pours still warned (2d). Round trip through
   the reader: 0.5 um on the synthetic model; Reto's asymbendtest fixture
   (17 netted arcs on two layers, 2 bottom parts) comes back with worst
   copper error 0.001 um and worst bottom-pad error 0.048 um
   (`test_real_board_copper_and_bottom_pads_round_trip`, env-gated).
   **2d (pours) BUILT 2026-10-07, rectangle zones only** (the one shape
   Pro has been seen to write; a polygon pour is dropped with a warning
   until a Pro-written polygon-pour fixture exists — asked of Reto in
   the item). **2e (silk + editable designators) BUILT 2026-10-07**:
   `SilkPlacement` x/y/angle → `_fab_model["silk_labels"]` → the
   Designator ATTR anchored where the silk pass drew it, strokes as POLY,
   refdes strokes suppressed. **2f BUILT 2026-10-14 in `p1-epro`** — the
   subset the spec ranked first: one `FOOTPRINT` document per DISTINCT
   authored pad/hole geometry (96 parts → 51 docs on taz15, content-keyed
   uuid/name), real rotations in `COMPONENT.angle` (bottom = rot+180),
   NPTH as free `PAD`s and footprint `FILL` circles, free-form pours as
   `"L"` vertex-list zones (Pro 3.2.206's own shape — the ask for a
   polygon-pour fixture answered itself, V1.15 carries four), plane
   layers with no authored pour get an outline-sized zone so the plane
   net survives, and `meta.epro_rules`/`epro_board`/`epro_pours`
   write-back verbatim. Dropped by design (each warned or justified):
   rule areas (no record shape ever seen — undecoded RULE warns
   "binds nothing"), mask openings (same — a guessed record is worse
   than a warned drop), teardrops (derived fill, Pro regenerates on
   repour), source silk art (regenerated — 16 source records vs 139
   POLYs + 96 designators written), the two plated free pads (no
   precis free-pad model — pcb-free-pad-model.md), schematic docs and
   DEVICE records (deliberate — no "update PCB from schematic" trap;
   pin names fall back to pad numbers on re-import). **The 0.5 um
   round-trip test (Horizon 6) landed with it**: import → `view='epro'`
   → re-import → structural diff on the real board is all-ok at worst
   0.3 um (pads) / 0.000 um (parts, copper, pours, outline), and the
   same diff runs on the committed tiny fixture in CI. Next: Reto
   opened `epro.local/taz15-roundtrip.epro2` in Pro 2026-10-14 —
   VERIFIED (import clean). The earlier LAYER/ATTR parse crashes were
   fixed first; the one remaining gap (unfilled pours) is now covered by
   `POURED` fill passthrough, tested in the structural diff.
5. **backlog/pcb-export-dir-confinement.md** — the four file exports
   write to any agent-supplied `dir`. Cheap and mechanical, and it closes
   a write-anywhere path on the shared server, so it ranks above the
   design work in Horizon. Filed from the orchestrator's round-1 review.
6. **backlog/pcb-realize-silk-version-stamp.md** — nothing versions
   realized copper, so a router change is invisible on stored boards.
   Needed before any router change goes default-on (negotiation's R3).
   The router half is this thread's; the silk half is ewod-pcb's.

## Horizon

Import, re-route, export is one arc; split at import/re-route vs
export/fab if this file outgrows itself.

1. **td458069** — a NETTED arc and an asymmetric bottom-side part. Reto
   supplied arc-y.epro2 on 2026-09-30, which closed the ARC record itself;
   what remains is verifying handedness on the way OUT, since its arcs
   carry no net and nothing yet exercises the mirror outbound. Gates 2c and
   2f only; blocks nothing in import.

4. ~~backlog/pcb-epro-export.md slice 2f~~ — landed 2026-10-14 in
   `p1-epro` except the pieces with no observed record shape (rule areas,
   mask openings → warned drops) and teardrops (Pro regenerates on
   repour). Retracts into Do next 4's Pro-open acceptance.
6. ~~backlog/pcb-epro-export.md round-trip test~~ — landed 2026-10-14:
   `test_taz15_import_export_reimport_structural` (gitignored-file-gated)
   + the committed-fixture twin in `test_pcb_epro_export.py`. Still the
   right regression: it diffs structure end to end at 0.5 um.
7. **backlog/pcb-engine-plan.md** §5 — 2-layer and n-layer stackups; enqueue
   refuses len(stackup) != 4 while the processor already handles 2. Waits on
   nothing; removes the wall a colleague's 2-layer .epro2 hits at import.
8. **gr451356 residue** — `edge_mate` (instance attribute, `pcb/edge_mate.py`)
   is authored per instance; shipped: placer snap + `edge_mating` DRC in
   `view='drc'`, the place and route jobs' pre-route check
   (`session.placement_drc_findings`) and the judged-mutation gate (moving
   an edge-mated part off its edge is refused; newly authoring one on a
   mid-board part is shown, not refused). Left: the `.epro2` import never
   sets it (a re-imported USB-C loses its constraint); a small package
   table (USB/card-edge/barrel) to default `face` from the footprint name.
9. **backlog/pcb-freeze-mechanicals-and-parts.md** — Reto, 2026-09-30:
   "All should be able to be frozen — nuts, holes etc for sure. But also
   parts ... But only actually freeze when needed."
   `pcb_instances.fixed` is real; `pcb_features.fixed` is unconstrained
   text nothing reads, so a frozen hole is not frozen. Invisible today
   because nothing moves a feature either — it becomes real the moment
   anything does, and it is already the wrong answer to "did my freeze
   take?". Waits on nothing. Live evidence, 2026-10-03 (job 464286): a
   hard `align` from standoff to hole held for 2 of 4 standoffs on
   heater-base-test. Footprint holes now follow their part, so the
   hole-side fix shipped; what remains is a part aligned to a fixed
   datum (not a hole) being placed there and frozen before the anneal.
10. **backlog/pcb-free-pad-model.md** — Reto, 2026-09-30: "just to
    measure. we should add a free pad model." His board's 6 plated free
    pads on GND are probe points with no precis model, so the import drops
    them and their net loses those connections every time. Waits on
    nothing; overlaps the inert `ftype='testpoint'`, which the item says
    to resolve rather than duplicate.
11. **backlog/pcb-datasheet-review.md** — provenance folded in there
    (in-scope 12); **slice 1 SHIPPED here 2026-10-14** (LCSC C-number →
    `part_lcsc`, value → label, MPN/mfr/supplier/link → `meta.part`,
    `Pin Type` → pin `etype`, `NO_CONNECT` → `nc`, local-first footprint
    join, datasheet pulls + known-sheet links, mfr+MPN → C → parametric
    lookup, TOC `fixed` column; the +3V3/3V3 split verified as the
    source's own topology). Still open there: slices 2+ (op='pins',
    margin rules, fact shapes, pcb_review). Datasheet-chunk citations
    behind each pin/net spec the re-route trusts.
12. **backlog/pcb-flexboard.md** — waits on 6; the flex half of "all of them".
13. **backlog/pcb-guided-place-route.md** slice 9 (JLCPCB ordering) — the
    workflow's endpoint; gated on a human granting Components/PCB scope in
    the JLCPCB Open API console.
14. **backlog/pcb-keepout-does-not-bind.md** — `ftype='keepout'` binds
    nothing in placer, router or DRC. Demoted from Do next 2026-10-02:
    the real board's "keepout" records look like its rule table (Do next
    2). The placer half belongs to ewod-pcb's obstacle-set item; this
    thread would own router + DRC + import. Waits on Do next 2's reading
    of the RULE bodies.

## No action needed

- Spike items S2–S12 and R1 in backlog/pcb-epro-export.md — resolved
  2026-09-29 against the real board; R2 and R3 stay open inside items 4 and
  2.
