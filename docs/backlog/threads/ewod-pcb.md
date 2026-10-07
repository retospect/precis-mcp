# ewod pcb

## Resume

- **Pillar:** 3d-design
- **Next:** Reto's 2026-10-07 brief (via chat-interface: "ewod-dogfood-6 looks cool but far from optimal. Discuss and investigate improved routing/placement/pinswap mechanisms"), ahead of EasyEDA: five measured steps on the replay fixture only (pin-swap effect, analytic channel capacity, routing-aware pin assignment prototype, structured escape planning, placement), delivered as a specced backlog item with numbered results for Reto to rule on, pointer at its rank here. [pcb-easyeda-round-trip](pcb-easyeda-round-trip.md) is reopened and its slice 2c (copper export) landed 2026-10-07; it resumes at 2d after the note. The layer lever is dogfooded on prod (Reto 2026-10-07: "two-layer escape is acceptable", EWOD has vias on every layer): ewod-dogfood-6 now carries stackup F.Cu signal / In1.Cu plane / In2.Cu signal / B.Cu signal and `ewod_ARR1_escape` rules `{"layers":["In2.Cu","B.Cu"],"clearance_mm":0.099}`; route job 472109 (`negotiate=10`) = 43/55 and job 472111 (`negotiate=100`, seed 0) = 45/55 with 29 vias, the replay numbers reproduced (Do next 4, dogfooded). The 10 residual failures sit in rows 5–7 and plateau at ~24 nets in conflict regardless of iterations, so the next router question is a third routable layer or escape-order/placement, not more negotiation. Job 472103 before the stackup fix was a silent no-op (In2.Cu was a plane; gripe 472108). Negotiated congestion on the B.Cu lock stays measured dead; arm B (job 470129) stays not-repeated. R14–R16 have shipped (prod 8.35.15), no renewed gate is pending; R13 preview native dogfood PASS is complete.
- **Blocked by:** Scientific/production0.22/service/NAS/node-role constraints stand. No provider/model/compute/manufacture/service or release work in this slice. Historical handoffs below remain historical and do not renew programme holds.
- **Unblocks:** Reproducible routing progress and trustworthy labels on the dogfood EWOD board.
- **Acceptance:** Preserve all reference/fab seed routing and DRC ratchets; verify affected seeds plus explicit EWOD coarse/fine experiment. No global finer-grid gain or deployment claim.
- **Worktree:** Source commands in isolated `pcb-r14-route-gate-repair` at frozen53e6cb255, branch `work/pcb/r14-route-gate-repair`; original `pcb-pad-retention`/`codex-pcb` branches/fixtures/scratch and pane registration retained. Owner `pcb`, thread `01a108d5-fdb2-7913-a2e2-ee7a08e58d40`, pane `%23` (window6).
- **Builds:** Not estimated here; use the owning slice estimate.
- **Detail:** fleet-state `inbox/pcb-r14-route-gate-repair-ready.md/.json`, then `inbox/pcb-snapshot-ready.md/.json`, `inbox/pcb-escape-diagnosis.md/.json`, then `inbox/pcb-escape-ready.md`. [Ranked work](#do-next) · [Horizon](#horizon) · [Coordination map](INDEX.md).

## Current source evidence

- **Current evidence:** R13 d33bf4b /8.35.12 verified/CLOSED by root; same-owner preview dogfood26 assertions PASS, exposed snapshots unchanged. R14 frozen53e6cb255 has no deployment; independent snapshot correction/escape source PASS did not establish whole-suite grid safety. gr467885 remains OPEN for its stated observable-proof remainder; 23 P1 checks remain historical PASS.
- **Routing:** Reto authorizes ewod-dogfood-N route/place/rebuild and public fixtures; non-dogfood boards remain protected. Exact ewod-dogfood-6 replay preserves all raw routing inputs and stored22 routed/33 failed/3 dangling. Actual-terminal/B.Cu probes classify all17 no_path labels as congestion. Half-grid experiment23/32/3, zero routed DRC errors, with12 gained/11 lost nets remains reproduced. Its global default regresses reference5/fab2/3/4; repair restores2/3 and route epoch4, default EWOD22/33/3. No thresholds/budgets/API changes; explicit fine experiment retained, global gain withdrawn. Diagnostic endpoint/layer parity remains separate.

2026-10-04 bounded reconciliation: R9 is CLOSED; four-host attestation is coordinator evidence, while this owner's single native status independently verifies the served SHA/version above. R7 and all deployment/route measurements below are historical. Fixture correction authorized using existing `ARR1_SINK_0` / C639448; BOM confirms queued acquisition. Part read is NotFound, lexical job search returns no entry; request/job/link/readback cannot be completed through returned hints. The stored route summary (22 routed, 33 failed, 3 dangling) is not a fresh routing or DRC result. No board mutations, new jobs, tests, deploy or provider probe. EasyEDA stays PARKED; pcb-platform inactive. Checkpoint: shared fleet `inbox/pcb-postdeploy-reconcile.md` + `.json`; FINISH and wait.

## Thread context

**Status:** ends when an EWOD board can be designed, placed, routed, DRC'd,
fabricated and driven as a testable system without a human pinning
geometry, on the way to backlog/pcb-global-codesign-north-star.md. Today
the dogfood board routes, but its measurements described an artifact (the
sink's pads were synthesized bounds; DRC never ran on what routing stored):
make the geometry real, make invalidity impossible to store, then
re-measure everything ranked off the old numbers. Shares generator, DRC
and realizer files with pcb-easyeda-round-trip: sequence, do not merge.
**Last reviewed:** 2026-10-02 (route-job post-route DRC gate landed; silk shared label spot shipped; dogfood-6
valid: 30/55 routed, 0 geometric DRC errors; resume at Do-next 1). 2026-10-01 (Pillar 2 review: pcb items from the unthreaded
sweep adopted — escape-and-driver-chain, floating-pour-island, stackup
orphan, checklist-kind, component-followons, argue-backport; round-7 and
pre-place-route-blocks items deleted as shipped; placer-sees-authored-vias, the gerber DRC
banner, the board-feature CPL/BOM rule and the feasibility layer-lock count
landed; the sink pin-name item landed (U_TEMP is the TMP112, C28927);
Do-next renumbered)
**Worktree:** `ewod-pcb`

## Detailed handoff (CLOSED 2026-10-03 ~22:00Z to save usage; unparked 15:38Z — Reto: "I also want the pcb/ewod thread to continue")

Everything is landed. Builds 3-4 are live (prod 929107f32). Build 5
(datasheet pull, 2b3c62811) and the docs (7f22e8128, 24872a348) are
marked in round 4 and wait on its deploy. **Waits on:** the round-4
deploy, for steps 2, 3 and 5 below. gr464537 (step 4) waits on nothing
and is the first build on reopen. The sheet-job adapter waits on
se-machine-design's input shape (Do-next).

1. **gr464240, round-3 dogfood done (job 464668, prod 929107f32):**
   22 of 55 routable nets realized (round 2: 20; before gr462607: 29),
   0 vias, best_at=0/708 (round 2: 17/699): the anneal never beat its
   start state. gr464237's fix helped by 2 nets, but it did not recover
   the 29. Still open; do step 2 before touching the cost model, so the
   v4 generator's effect is measured on its own.
2. **ewod-dogfood-6 re-put still owed** (ARR1 stored v3, code v4; the
   route reply carries the stale warning, confirmed live). Re-put its
   generators entry, route, report routed count before/after — after
   step 1, so the two effects are not confounded.
3. Build 4 is landed (round 3: 5f145c3a, 4d58cefc, 4def048c, 796fc1b4).
   Batch put, class_rules and op='footprint' are judged. Next is Do-next 2
   (datasheet pull, build 5). Order confirmed by Reto (ewod-pcb-3).
   After the deploy, dogfood: a put that shrinks the outline under routed
   copper rips the net and does not refuse.
4. gr464537: JLCPCB's getComponentInfos answers prod's parts_refresh with
   HTTP 500, so `parts` stays empty. Build 5's datasheet pulls resolve
   URLs through the same API and will mostly record `jlc_api_error`
   until this is fixed. Probe the first-page payload by hand (one
   API call), then fix it. This comes first after build 5 deploys.
5. **Judge cost (round-3 diff review, 2026-10-03).** `_judged_mutation`
   runs the validity DRC twice, plus pcb_load, _build_ir and a copper
   listing, on every judged put, class_rules and footprint call.
   Measured 0.9 s per DRC call on an 8-part fixture. Measure it on
   ewod-dogfood-6 during the post-deploy dogfood. Above about 3 s per
   put, cache the "before" report keyed on the board's content hash
   (the previous judged mutation's "after" is the next one's
   "before"). The contract (first put judged, rip at any severity,
   class_rules lists pad shortfalls without refusing) is written
   into precis-pcb-help.
6. gr464529: re-putting an outline feature appends a second outline.
   Small; decide replace-or-refuse when Do-next 2 allows.
Reto approved se-machine-design-7 option 1 (2026-10-03): the shared 2-D
sheet job lives under flat-pack; pcb only emits into it. This thread builds
the pcb model -> sheet-job adapter (edge-cut->cut, NPTH->drill,
silk->engrave-vector, gasket outline->drag-knife cut) after build 4, against
the input shape se-machine-design defines in its build 1; that thread
reviews it. Start nothing until that shape arrives.
Owed to pcb-easyeda-round-trip's version-stamp item if picked up from
here: a code-version input to `content_hash` (round-2 review finding 1).

## Do next

0. **backlog/pcb-silk-refdes-row-gets-no-shared-side.md**: Reto's own
   board (heater-base-test, 2026-10-02). Both asks are fixed: label spots
   are chosen in the board frame, and an aligned row or column of
   identical parts shares one spot. What is left is small: a courtyard
   break that did not reproduce on the real pads (re-check on the next
   render), and EasyEDA designator poses not imported. **New on prod
   2026-10-02 (dogfood after the round-1 deploy, route job 462600):**
   ewod-dogfood-6 reports `silk_missing` for ARR1_SINK_0's bottom refdes
   ("every candidate placement overlaps a pad, a via, or silk already
   committed"); it read 0 geometric errors before. **Root-caused
   2026-10-02:** not the silk commits (reproduced on code before both);
   the run moved the sink's courtyard to 1.385 mm from a bottom-side
   furniture rect, and `_board_furniture`'s `court_margin` (silk clearance
   + 1.0 = 1.375 mm) leaves less than the ~1.75 mm a below-box refdes needs.
   Fixed 2026-10-03: `silk.refdes_label_slot_mm` (text height + 2 ×
   clearance + inset) sets the furniture margin's floor.
1. **backlog/pcb-always-valid-board-invariant.md** — built 2026-10-02
   (all undeployed): the route job DRCs its own router copper and strips a
   violating net (`drc:<rule>`); `op='move'` on a generator member moves
   the whole group with its fixed copper and rips the router nets it
   strands; `ewod_pad_array` v4 emits its copper at the array anchor. The
   13 strips on pcb 460559 were router faults, fixed by
   pcb-easyeda-round-trip (0 strips after). Gripe 462607 fixed 2026-10-02: the anneal restores its best state
   (judged at the reporting schedule), so `cost_after <= cost_before`
   always; job summaries show `best_at=N/M`. It changes route output for
   the same seed. **Do not re-route prod boards for dogfood until it
   deploys** (orchestrator, 2026-10-02): until then every prod route job
   stores a placement worse than it found. Still owed: a job-level
   determinism test for `pcb_route` — **built 2026-10-03**, together with the
   fix for a second defect: the job restored the per-segment layer/side
   sketch BEFORE pin swaps, so every segment on a swapped pin restarted
   unlayered (the 1.08 cost gap between runs). Pin swaps now restore first,
   unmatched sketch entries are reported in the job summary, and a test
   holds run 2's `before` equal to run 1's `after`. The pose half of the move check is a
   delta now too (verdict 2026-10-02). The multi-pose `op='move'`
   (ruling 2) is built too. **Built 2026-10-03 (build 4):** batch `put` (`pcb_apply`) and
   `op='class_rules'` run in one judged transaction (`Store.pcb_judged_tx`,
   `PcbHandler._judged_mutation`): new or worse pad/fixed/courtyard findings
   refuse, router copper that now conflicts is ripped, class-requirement
   shortfalls between pads are listed as now visible. `op='footprint'` is built too
   (ruling ewod-pcb-4: stored always, colliding router copper ripped, pad/pose
   collisions listed as now visible, other designs using the part named);

   Dogfood after the deploy: re-route ewod-dogfood-6, expect 0 in the
   summary's "stripped by post-route DRC" count.
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
   ewod-pcb-2. SHIPPED and deployed (2b3c62811, in R16); the backlog file was deleted 2026-10-07; still unchecked: the first prod pull's chunk quality. Built: the `datasheet_pull` job (job_inproc),
   the put/EasyEDA-import trigger, `get(kind='part')` reasons. First on
   deploy: put a board in prod and read `get(kind='part')` for a C-number;
   JLC's component API is erroring (gr464537), so expect `jlc_api_error:500`
   until a datasheet_url exists in `parts`. What is left is in the backlog
   item.
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
   **Measured 2026-10-07 on the replay fixture** (`tests/fixtures/pcb/
   ewod-dogfood-6-replay-v1.json.gz`, hydrated as
   `tests/test_pcb_escape_replay._hydrate`; probes were untracked scratch
   tests, 4 min for the per-net run):
   - *Negotiated congestion never converges on the B.Cu lock.* 40
     iterations under four pressure schedules (pres growth 1.5/3.0, hist
     ×1/×5, pres start 0.5/2.0): 50–52 of 55 nets still in conflict after
     EVERY iteration; every conflicted net re-routes to a different path
     each iteration; no conflict is at an endpoint (so not the exempt-end
     sliver); contested cells cover the whole board, not one choke. The
     commit pass takes 10–14 of 53 proposals verbatim and scores 35–40
     failed, so the hard passes' 33 is kept. Job 470129's "no gain" was
     this, unreported — hence the report.
   - *The 33 are contention, not walls, but the single layer is full.*
     Each net alone (all other router nets ripped, `route_passes=1`):
     53/55 route; only R0C1 and R0C4 fail alone, labelled `congestion`
     (gripe 472026 — the label-honesty case this item already warned
     about). The 33 failed nets alone: 17 route. So no re-ordering or
     negotiation reaches far past 22 on B.Cu alone.
   - *Opening In2.Cu is the lever:* same placement, escape class layers
     `[In2.Cu, B.Cu]`: hard 40/55 (21 vias), negotiate=10 43/55 (26
     vias). The class/layer policy itself is item 6's question.
   **Dogfooded on prod 2026-10-07** (Reto: two-layer escape acceptable,
   EWOD has vias on every layer):
   - *The first class_rules route (job 472103) gained nothing.* In2.Cu
     was role `plane` in dogfood-6's stackup, so `_net_class_layers`
     narrowed the class back to B.Cu without a word (gripe 472108). Fixed
     by re-authoring the stackup (`op='stackup'`: In1.Cu plane without a
     `plane_net`, the board has no GND net; In2.Cu signal).
   - *Then the replay reproduced:* job 472109 `negotiate=10` → 43/55, 26
     vias; job 472111 `negotiate=100`, seed 0 → 45/55, 29 vias, 23 s,
     "46→24 net(s) in conflict, did not converge; 55 proposal(s), 25
     committed verbatim, result taken". Ninety more iterations bought two
     nets; the loop plateaus near 24 in conflict.
   - *What is left:* 10 nets — R2C4, R3C3, R3C5, R5C4, R5C7, R6C1, R6C4,
     R6C6, R6C7, R7C6 — mostly rows 5–7, the far side from the driver.
     Past In2.Cu the lever is a third routable layer, or the escape
     order/placement, not iterations. Every route put still warns that
     generator ARR1 is stored at version 3 against code version 4; not
     re-expanded, to keep the runs comparable (re-putting the generators
     entry redoes placement and routing).
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
   a class becomes enforceable rather than advisory. **gr458878** (filed on
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

- **oblique pads, remaining path** (2026-10-02). `pads_for_ir` now emits an
  oblique rect/obround land as its rotated polygon, but the footprint pads
  no pin claims (`_unclaimed_footprint_pads`, via
  `padplace.place_footprint_pads`) still enter the same model as an
  unrotated w×h with an `oblique_rot` flag. Same fix, same tests
  (tests/test_pcb_oblique_pad.py). Reachable only via an imported or
  authored oblique footprint.
- **gate: same-net via barrels under `via_via_keepout`** (2026-10-02,
  from the 460559 classification). The rule judges two via barrels as
  independent copper at `trace_spacing_mm` whatever their nets, so two GND
  vias whose barrels sat 0.048 mm apart (drills 0.55 mm edge to edge, fine)
  failed. Same-net copper cannot short, so exempting it while keeping the
  hole-to-hole check is probably right, but check a fab source on
  same-net slivers first. Low: the router no longer produces the pair
  (`register_via` spaces every via at grid clearance).

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
  errors.** 160 warnings, all copper at JLC minimums. Earlier history
  (the 116-error placement; jobs 460181/460302 accepting 0 of 3000 moves
  for lack of an outline) is in git log.
  **2026-10-07:** stackup re-authored to F.Cu signal / In1.Cu plane /
  In2.Cu signal / B.Cu signal and `ewod_ARR1_escape` opened to
  `[In2.Cu, B.Cu]` (clearance 0.099 mm). Latest route job 472111
  (`negotiate=100`, seed 0): 45 realized, 10 failed, 3 dangling, 29 vias,
  53 pin swaps, 0 pre-route DRC errors; STATUS failed = incomplete. Its
  10 failures are Do-next 4's fixture. Generator ARR1 is stored at
  version 3 against code version 4 (warned on every put, deliberately not
  re-expanded).

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
