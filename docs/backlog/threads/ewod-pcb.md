# ewod pcb

**Status:** ends when an EWOD board can be designed, placed, routed, DRC'd,
fabricated and driven as a testable system without a human pinning
geometry, on the way to backlog/pcb-global-codesign-north-star.md. Today
the dogfood board routes, but its measurements described an artifact (the
sink's pads were synthesized bounds; DRC never ran on what routing stored):
make the geometry real, make invalidity impossible to store, then
re-measure everything ranked off the old numbers. Shares generator, DRC
and realizer files with pcb-easyeda-round-trip: sequence, do not merge.
**Last reviewed:** 2026-09-30 (pillar review same day added five orphan
gripes and a cross-scale-single-assembly seam pointer; pruned gr346009,
soft-deleted)
**Worktree:** `ewod-pcb`

## Do next

1. **backlog/pcb-ewod-sink-pin-names-do-not-match-the-real-part.md** — 56 of
   59 sink pins silently fell back to synthesized bounds while the footprint
   row read `cached: yes`; every escape-yield and congestion number in this
   thread was measured against those bounds (ewod-dogfood-4: names only,
   21→38 realized on identical code). Nothing below can be trusted until it
   lands, so it outranks the corruption item.
2. **backlog/pcb-always-valid-board-invariant.md** — a board reached stored,
   routed, `succeeded` state with 64 DRC errors and zero pcb_drc_findings
   rows until asked out of band. Prevents persisting unmanufacturable copper
   at all. Folds in backlog/pcb-placement-must-be-valid-before-routing.md
   (same defect, narrower): implement one, not both. Under a design review
   that rewrote its content on 2026-09-30, not its rank.
3. **backlog/pcb-placer-obstacle-set-is-mounting-holes-only.md** — authored
   pcb_fixed_copper (plaza vias) is invisible to the placer; this is the
   mechanism behind 2's symptom (2 makes the state unstorable, this stops
   producing it). Needs a fixture with an authored via where an instance
   would land; ewod-dogfood-1 cannot exercise it.
4. **backlog/pcb-risk-is-a-max-so-any-money-term-is-a-free-tiebreaker.md** —
   risk() is a MAX over margin terms, so any MONEY term is a free tie-breaker
   against every non-maximal constraint (a $0.046 term overruled
   courtyard_overlap). Decides whether 3's fix can be graded or must be a
   hard gate.
5. **backlog/pcb-placer-starves-the-escape-corridor.md** — its acceptance
   criteria came off an invalid placement and are void; now a
   rewrite-against-a-new-fixture job that needs 1's real pad geometry.
6. **backlog/pcb-generator-version-is-a-manual-bump-with-no-tripwire.md** —
   op='route' never re-runs the generator, which is why pb345846 still
   permits F.Cu after the 09-27 fix. Now owns the live half of 7: the stale
   stored class IS this staleness, and without a tripwire the same
   staleness re-opens any generator fix.
7. **backlog/pcb-escape-layers-leak-fcu-between-net-class-and-realization.md**
   — **demoted 2026-09-30, its key evidence was contaminated.** The
   fresh-fixture failure it was ranked on was measured in a worktree
   carrying the uncommitted routing_area term; on main that fixture passes.
   Re-verified 2026-09-30 on top of the pad-orientation fix: with
   routing_area reverted the dogfood file is `8 passed`, with it applied the
   F.Cu escape assertion fails — so the F.Cu escape is attributable to the
   term's placement, not to pad geometry. What is left is the stale class on
   pb345846, which 6 owns, plus the narrower open question: is that escape a
   real leak at that placement, or a placement-sensitive assertion? Reads
   realize/maze, which pcb-easyeda-round-trip's router item also touches;
   this thread sequences behind theirs.

## Horizon

1. **routing_area cost term** (held on the local branch
   `wip/routing-area`, not on `main` — it was set aside and restored four
   times across 2026-09-30's ships, and a `/tmp` patch as its only copy is
   how that work gets lost; restore it with `git checkout wip/routing-area
   -- src/precis/pcb/cost.py src/precis/pcb/optimize.py
   tests/test_pcb_optimize.py`) —
   prices the board area a strand sweeps; the only thing holding the sink
   under the array. Waits on its own gate; a placer that needs no pinned
   sink. **The esp32c3 regression is GONE** — it was the pad-orientation
   divergence (`realize.pad_board_wh`, landed 2026-09-30), not the term:
   with that fixed, `test_pcb_reference_end_to_end.py` is 5 passed at every
   seed WITH the term applied. What remains against the term is the dogfood
   F.Cu escape assertion (item 7 above), re-measured on top of the fix. So
   the open decision is back to "gate now or wait", plus that one
   assertion.
2. **backlog/pcb-always-valid-board-invariant.md** implementation slices —
   waits on Do-next 2; the precondition for trusting any number below this
   line. Its cost prerequisite is gone: check_via_pad_keepout is indexed,
   so a full geometric DRC pass is 0.29 s on an 8x8 tile and 1.2 s at 16x16
   (was 1.4 s and 23 s), and check_clearance is now the pass's bottleneck —
   backlog/pcb-clearance-findings-name-no-pad.md carries both that figure
   and the observability gap the 09-30 investigation paid for.
   **gr458087 is stale, not a regression — reconciled 2026-09-30 by
   re-running the measurement.** Its 1.9 s/8x8 and 30 s/4-tile figures are
   the PRE-fix state; the STRtree fix it proposed is already in
   `check_via_pad_keepout`, whose own comment cites gr458087 and quotes
   those numbers as history. Re-measured on `main` today over the same
   `tests/test_pcb_ewod_generator_drc.py::_ewod_model` fixture:
   `via_pad_keepout` 20 ms of a 224 ms pass at 8x8 (55 pads / 55 vias),
   97 ms of 1279 ms at 16x16 (231 pads / 200 vias). So the rule is no
   longer the bottleneck at any size measured, `check_clearance` is, and
   the affordability prerequisite this item named is genuinely discharged.
   gr458087 wants closing: it was bounced back to `STATUS:open` by the
   false-push incident (gr458326), so nobody noticed the real fix had
   landed by another route. Closing it is a prod write and waits on Reto.
3. **backlog/pcb-guided-place-route.md** — the remaining engine slices;
   waits on 2 because each slice's acceptance is an "is the board still
   valid" claim.
4. **backlog/pcb-layer-preferred-direction.md** +
   **backlog/pcb-congestion-driven-spread.md** — the two escape-yield
   levers that are not defects; wait on Do-next 1 or they optimise against
   an artifact.
5. **backlog/pcb-missing-constraint-classes.md** +
   **backlog/pcb-footprint-pad-layer-unvalidated.md** — the HV constraint
   vocabulary (creepage at 250 V) and pad-layer validation; wait on 2, where
   a class becomes enforceable rather than advisory. Reading nearby:
   backlog/pcb-oblique-rotated-pad-is-an-axis-aligned-rect-in-the-model.md
   is the same "what shape is this pad" question at an oblique angle, and
   an .epro2 import is its likely first real source.
6. **backlog/pcb-tapeout-checklist-seed-items.md** — the pre-fab gate; waits
   on 5, a checklist over unenforceable constraints is theatre.
7. **gr451277** — three copper-routing inefficiencies on ewod-dogfood-2
   (a bottom-layer retrace that buys nothing, one plaza escape that
   crosses the whole field and comes back, a pin swap that lengthens
   instead of shortens); none violates DRC, so nothing has ever measured
   it but a human looking at the render. Same root gap as Do-next 5's
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
13. **backlog/pcb-ewod-multitile.md** — waits on 3 and 4; multitile
    multiplies whatever the escape corridor does.
14. **backlog/pcb-global-codesign-north-star.md** — the arc all of the
    above serves; re-read when ranking the next round.

## Parked

- **pb345846 regenerate** — destructive prod write; unparks on Reto's
  per-write go-ahead, and not before Do-next 1 lands and the escape-layer
  question (Do-next 7, with its live half in 6) is settled, or it is done
  twice. Named by item, not number: that rank has moved twice already.
- **U_TEMP part selection** — C32254 is a dual MOSFET placeholder; unparks
  when Reto picks a real LM75-class part (detail in item 1's file).
- **backlog/pcb-via-geometry-ignores-pad-side-and-pads.md** — unparks with
  Do-next 2, where via-vs-pad becomes an enforced rule rather than a
  reported one.

## No action needed

- **backlog/pcb-pre-place-route-blocks.md** — already landed; verify and
  delete.
- **gr346009** — soft-deleted, do not reopen; the count it exposed is
  Do-next 1's. (Tombstone restored 2026-09-30 after the review's relink
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
