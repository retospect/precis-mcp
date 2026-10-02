# ewod pcb

**Status:** ends when an EWOD board can be designed, placed, routed, DRC'd,
fabricated and driven as a testable system without a human pinning
geometry, on the way to backlog/pcb-global-codesign-north-star.md. Today
the dogfood board routes, but its measurements described an artifact (the
sink's pads were synthesized bounds; DRC never ran on what routing stored):
make the geometry real, make invalidity impossible to store, then
re-measure everything ranked off the old numbers. Shares generator, DRC
and realizer files with pcb-easyeda-round-trip: sequence, do not merge.
**Last reviewed:** 2026-10-02 (route-job post-route DRC gate landed; silk shared label spot shipped; dogfood-6
valid: 30/55 routed, 0 geometric DRC errors; resume at Do-next 1-2). 2026-10-01 (Pillar 2 review: pcb items from the unthreaded
sweep adopted — escape-and-driver-chain, floating-pour-island, stackup
orphan, checklist-kind, component-followons, argue-backport; round-7 and
pre-place-route-blocks items deleted as shipped; placer-sees-authored-vias, the gerber DRC
banner, the board-feature CPL/BOM rule and the feasibility layer-lock count
landed; the sink pin-name item landed (U_TEMP is the TMP112, C28927);
Do-next renumbered)
**Worktree:** `ewod-pcb`

## Do next

0. **backlog/pcb-silk-refdes-row-gets-no-shared-side.md**: Reto's own
   board (heater-base-test, 2026-10-02). Both asks are fixed: label spots
   are chosen in the board frame, and an aligned row or column of
   identical parts shares one spot. What is left is small: a courtyard
   break that did not reproduce on the real pads (re-check on the next
   render), and EasyEDA designator poses not imported.
1. **backlog/pcb-always-valid-board-invariant.md** — the route job now
   DRCs its own router copper before writing (2026-10-02, undeployed):
   a violating net is stripped and lands `failed` with `drc:<rule>`.
   Decision 3 was read off standing rulings; review item ewod-pcb-1 lets
   Reto overrule it and still holds decisions 1-2 (move carries authored
   copper; multi-pose move). After the deploy, re-route ewod-dogfood-6 and
   check the summary's "stripped by post-route DRC" count — expected 0,
   since the board already reads 0 geometric DRC errors.
   **Now `status: canonical`** (Reto, 2026-09-30: "ok make it canonical"), carrying his
   design consequence: *"If placement is always valid and routing is valid
   (but may be incomplete), we should never get a failure."* So legality is
   a hard gate on both stages and incompleteness is the only permitted
   failure mode. The file also records the answer to his follow-up — a
   netlist is still buildable before placement, because the IR's levels are
   progressive and an unplaced design has no geometry to violate. Folds in
   backlog/pcb-placement-must-be-valid-before-routing.md (same defect,
   narrower): implement one, not both.
2. **backlog/pcb-datasheet-autopull.md** — Reto's ask (2026-10-02): a
   part a board uses gets its datasheet pulled, ingested and linked
   `datasheet-of`, in a worker lane through `safe_fetch`. Prod has 0
   `parts` rows and 0 datasheets, so the pull resolves the URL from the
   C-number itself. Ranked above the escape items because three other
   threads wait on it (claims-and-evidence citations, knowledge-mesh's
   datasheet nodes, gr458878 pin provenance) and nothing here blocks it.
   The mesh half is knowledge-mesh's; its shape is in review item
   ewod-pcb-2.
3. **backlog/pcb-risk-is-a-max-so-any-money-term-is-a-free-tiebreaker.md** —
   **de-escalated by 1's ruling.** risk() is a MAX over margin terms, so any
   MONEY term is a free tie-breaker against every non-maximal constraint (a
   $0.046 term overruled courtyard_overlap). With legality moved out of the
   objective entirely this no longer gates anything manufacturability-facing;
   what survives is tuning clarity for the next person adding a term — and
   backlog/pcb-tightest-connected-part.md is the next item that will trip
   over it.
4. **dogfood-6's 25 failed escapes are not corridor starvation.**
   Measured 2026-10-02: `view='congestion'` reports 0 over-capacity gaps;
   the failures split 8 `congestion` / 17 `no_path`. So
   backlog/pcb-placer-starves-the-escape-corridor.md lost its only
   evidence (it came from the invalid placement) and is demoted to
   idea/low until a legal board shows an over-capacity gap. Also ruled out
   (2026-10-01): the escape layer lock. All 54 pins reach B.Cu through the
   authored plaza vias, so do not re-open the via-floor estimate either.
   What is left is a router question, and the router half belongs to
   pcb-easyeda-round-trip: hand them dogfood-6 as the fixture rather than
   diagnosing it here.
5. **backlog/pcb-escape-and-driver-chain.md** — `prio: high`; escape and
   driver-chain are general PCB primitives wearing EWOD names (the engine's
   only registered generator is `ewod_pad_array`). Sits beside 4: both are
   the escape corridor, this one is where the primitive lives. Reto
   2026-09-26/27: board = data, engine = general.
6. **backlog/pcb-escape-layers-leak-fcu-between-net-class-and-realization.md**
   — **demoted 2026-09-30, its key evidence was contaminated.** The
   fresh-fixture failure it was ranked on came from the then-unlanded
   routing_area term. That term landed 2026-10-01 (Reto's call) and on that
   main the dogfood file passes with it, so the F.Cu escape no longer
   reproduces. What is left is the stale class on pb345846 (fixed by
   re-putting its generators entry; responses now name a stale board, and
   tests/test_pcb_generator_version_tripwire.py pins each generator's output
   to its version), plus the item's negative-control acceptance. Reads
   realize/maze, which pcb-easyeda-round-trip's router item also touches;
   this thread sequences behind theirs.

## Horizon

0. **backlog/pcb-freerouting-view-replaces-without-legality.md** — the
   Freerouting `view='route'` re-place skips the legality gate that
   op=place/move now enforce; gate it or retire the view (product call).
1. **backlog/pcb-always-valid-board-invariant.md** implementation slices —
   no longer waits on anything: the placer fix it queued behind landed
   2026-10-01. Still the precondition for trusting any number below this
   line. Its cost prerequisite is gone: check_via_pad_keepout is indexed,
   so a full geometric DRC pass is 0.29 s on an 8x8 tile and 1.2 s at 16x16
   (was 1.4 s and 23 s), and check_clearance is now the pass's bottleneck
   (195 ms of 287 ms at 8x8). Its findings name a pad by part/pin and give
   the nearest points since 2026-10-01, so `pad[ARR1_R7C0]` no longer reads
   as an electrode when it is the driver's land.
   **gr458087 was stale, not a regression — reconciled 2026-09-30 by
   re-running the measurement.** Its 1.9 s/8x8 and 30 s/4-tile figures are
   the PRE-fix state; the STRtree fix it proposed is already in
   `check_via_pad_keepout`, whose own comment cites gr458087 and quotes
   those numbers as history. Re-measured on `main` today over the same
   `tests/test_pcb_ewod_generator_drc.py::_ewod_model` fixture:
   `via_pad_keepout` 20 ms of a 224 ms pass at 8x8 (55 pads / 55 vias),
   97 ms of 1279 ms at 16x16 (231 pads / 200 vias). So the rule is no
   longer the bottleneck at any size measured, `check_clearance` is, and
   the affordability prerequisite this item named is genuinely discharged.
   It had been bounced back to `STATUS:open` by the
   false-push incident (gr458326), so nobody noticed the real fix had
   landed by another route. **Closed: it reads `STATUS:done` on prod
   (checked 2026-10-01).** Reading nearby:
   **backlog/pcb-placer-obstacle-set-is-mounting-holes-only.md** — what
   is left of the placer's obstacle set after authored VIAS became
   obstacles on 2026-10-01: authored tracks and pours are still
   invisible, and the seed is still blind (the anneal walks a part off a
   via and reports it when it could not, but `pcb_route` surfaces that
   nowhere). Belongs with the invariant because refusing to route an
   illegal placement is what closes it.
2. **backlog/pcb-guided-place-route.md** — the remaining engine slices;
   waits on 1 because each slice's acceptance is an "is the board still
   valid" claim.
3. **backlog/pcb-layer-preferred-direction.md** +
   **backlog/pcb-congestion-driven-spread.md** — the two escape-yield
   levers that are not defects. The sink's pads have been real since
   2026-10-01; re-measure escape yield before tuning either, because every
   number they were ranked on was taken against synthesized bounds.
4. **backlog/pcb-missing-constraint-classes.md** +
   **backlog/pcb-footprint-pad-layer-unvalidated.md** — the HV constraint
   vocabulary (creepage at 250 V) and pad-layer validation; wait on 1, where
   a class becomes enforceable rather than advisory. Reading nearby:
   backlog/pcb-oblique-rotated-pad-is-an-axis-aligned-rect-in-the-model.md
   is the same "what shape is this pad" question at an oblique angle, and
   an .epro2 import is its likely first real source. **gr458878** (filed on
   Reto's word 2026-09-30) is the pin-level datasheet-provenance gap: a
   datasheet links to a PART, but no pin selector exists so no pin fact can
   cite the page it came from — and the payoff is validation, since the
   footprint and the datasheet pin table are two independent statements of
   pad->function that we compare nowhere. Owned by pcb-platform, sequenced
   behind pcb-component-model. That gripe also records the blocker found
   while checking it: the prod `parts` catalog is EMPTY (0 rows), so no
   part can be SEARCHED for, only confirmed by C-number.
5. **backlog/checklist-kind.md** — `ready/high`; the argued, invalidating
   ledger the seed items below instantiate (first instance: pcb
   pre-tapeout). Ranked beside its seed companion, not above 5: a ledger
   over unenforceable constraints is theatre either way.
6. **backlog/pcb-tapeout-checklist-seed-items.md** — the pre-fab gate; waits
   on 4 and 5, a checklist over unenforceable constraints is theatre. Its
   `drc-clean` item is half-served: `view='gerber'` now runs DRC and leads
   its response with a `DRC FAILED` block (Reto, 2026-10-01: banner, not
   refusal — he wants the bundle of a broken board to debug from). The
   banner informs and does not gate; the item records what a refusal
   with an override would add if a red bundle is ever uploaded anyway.
7. **gr451277** — three copper-routing inefficiencies on ewod-dogfood-2
   (a bottom-layer retrace that buys nothing, one plaza escape that
   crosses the whole field and comes back, a pin swap that lengthens
   instead of shortens); none violates DRC, so nothing has ever measured
   it but a human looking at the render. Same root gap as Do-next 4's
   escape corridor — no signal scores total copper length against the
   achievable minimum.
8. **backlog/ewod-controller-and-hv-supply.md** — Reto-side, procurement
   lane, parallel; a testable system rather than a bare PCB.
9. **backlog/ewod-synthesis-protocol.md** +
   **backlog/ewod-oil-constraint-grounding.md** — the wet side; wait on
   physical boards existing (the protocol also consumes se-nucleic-chain's
   make_steps).
10. **gr451662** — EWOD stack assembly needs non-fab mechanical 2D layers
    (ITO top sheet, spacer adhesive, alignment holes, via-plaza covers),
    generalising the pcb 2D layer system the way solder paste already
    does; the hard part is stencil bridges that keep the adhesive sheet
    one connected component. Reto-architected 2026-09-26; waits on nothing
    but is large. Interacts with gr414481.
11. **gr414481** — EWOD boards need a second max-extent check for parylene
    coating, separate from the fab/manufacturing size cap; feeds gr451662's
    adhesive-layer extent.
12. **gr338660** — the ewod-oil route-platform-constraint screen has no
    reagent-economy axis (distinct-reagent count vs reservoir budget,
    reaction-type diversity, longest unpurified run); waits on 9
    (ewod-oil-constraint-grounding), the file this screen lives beside.
13. **backlog/pcb-ewod-multitile.md** — waits on 2 and 3; multitile
    multiplies whatever the escape corridor does.
14. **backlog/pcb-floating-pour-island.md** (`prio: medium`) +
    **backlog/pcb-stackup-edit-orphans-a-class-layer-lock.md** (`low`) —
    two ways a stored board is silently invalid: a pour island with none of
    its net's copper, a stackup edit that strands a net-class layer lock
    until route time. Wait on 1, where "stored board is valid" becomes a
    gate.
15. **backlog/component-followons.md** — `idea`; comparator/violator query,
    price-break costing on the shipped component kind. Waits on nothing; no
    pcb item depends on it.
16. **backlog/pcb-argue-backport-se.md** — `blocked-by`
    pcb-argue-with-design (se-machine-design Do-next 5, not ranked here);
    low, back-ports the argue box to se.
17. **backlog/pcb-global-codesign-north-star.md** — the arc all of the
    above serves; re-read when ranking the next round.

## Parked

- **pb345846 regenerate** — destructive prod write; unparks on Reto's
  per-write go-ahead, and not before the escape-layer question
  (backlog/pcb-escape-layers-leak-fcu-between-net-class-and-realization.md)
  is settled, or it is done twice. The regenerate itself is now a plain
  re-put of its generators entry.
- **backlog/pcb-via-geometry-ignores-pad-side-and-pads.md** — unparks with
  Do-next 1, where via-vs-pad becomes an enforced rule rather than a
  reported one.

- **round-7 residue (2026-09-03 review, item deleted)** — a large authored
  rigid group (~12x18 mm, TQFP-32 + crystal circuit) seeds straddling the
  outline and containment pressure cannot walk it back (fix direction:
  containment-aware group seeding); not re-verified since. `pcb` slugs skip
  `mint_slug` (`handlers/pcb.py::PcbHandler.put` takes `str(id).strip()`);
  the two XSS sinks it enabled are fixed, charset sanitising is
  defence-in-depth only. Unparks on a reproduction / a pcb-id audit.

## Boards on prod

- **`ewod-dogfood-6` is the only live design, and it is now a valid
  board.** 2026-10-02 (Reto: "whatever size please do it"): authored a
  32 mm square outline centred on the array (corner radius 1 mm), which
  fits the 19 x 23.5 mm driver underneath in any rotation. Re-place job
  461063 (build 7242d4c9): 0 pre-route DRC errors, no land on a plaza via.
  Route job 461064: 30 realized, 25 failed, 3 dangling, 49 pin swaps;
  STATUS failed means incomplete, which the always-valid invariant allows.
  DRC run 55023e04: 51 errors = the 25 unrouted nets twice (unrouted +
  connectivity) + 1 `silk_missing` (ARR1_SINK_0's bottom refdes has no
  spot clear of the 55 plaza vias: a real board finding). **Zero geometric
  errors.** 160 warnings, all copper at JLC minimums. Its 25 failures are
  Do-next 4's fixture. Earlier history (the 116-error placement; jobs
  460181/460302 accepting 0 of 3000 moves for lack of an outline) is in
  git log.
- **dogfood-1 through dogfood-5 are RETIRED** (Reto, 2026-09-30: "retire all
  the junk dogfood"). Every one of them is measured against something now
  known wrong: 1/2 had pinned sinks, 1/2/3 predate the real sink pin names,
  4 predates the pad-orientation fix, 5 came off the stale MCP build. **Do
  not cite a number off any of them** — that includes dogfood-4's 38
  realized / 25 failed, which is the figure most likely to be quoted back.

## No action needed

- **gr346009** — soft-deleted, do not reopen; the count it exposed was
  the sink's misnamed pins, fixed 2026-10-01, and a put or DRC now names
  any pin that matches no pad on a cached footprint. (Tombstone restored 2026-09-30 after the review's relink
  pass pruned it as "closed".)
- **The "maze occupancy guarantee leak"** — there was no leak. The router
  cleared its own claim by 0.1703 mm on the copper that fired the finding;
  the model and the grid disagreed about the pad's ORIENTATION. Fixed by
  `realize.pad_board_wh` 2026-09-30, item deleted. The guarantee's
  argument (`BASELINE_DRC_ERRORS = 0`, "find the leak") held up: it is
  what made the measurement worth taking instead of tuning the cost term.

## Seam

`backlog/cross-scale-single-assembly.md` (owned by se-machine-design) is
where the EWOD cartridge stack — gr451662's ITO/adhesive/alignment
layers, the physical assembly above the PCB — becomes part of one
cross-scale design rather than a bare PCB; not ranked here.
