# ewod pcb

**Status:** ends when an EWOD board can be designed, placed, routed, DRC'd,
fabricated and driven as a testable system without a human pinning
geometry, on the way to backlog/pcb-global-codesign-north-star.md. Today
the dogfood board routes, but its measurements described an artifact (the
sink's pads were synthesized bounds; DRC never ran on what routing stored):
make the geometry real, make invalidity impossible to store, then
re-measure everything ranked off the old numbers. Shares generator, DRC
and realizer files with pcb-easyeda-round-trip: sequence, do not merge.
**Last reviewed:** 2026-09-30
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
3. **gr458087** — drc.check_via_pad_keepout is O(vias × pads) and rebuilds
   the pad polygon per pair: 1.9 s per 8x8 tile, 30 s at 4 tiles, 100% of a
   full DRC pass. 2 wants geometric DRC at every mutation, unaffordable at
   that cost and fine at the ~0.3 s an STRtree gives; the pattern exists in
   the same module (clearance_pairs_indexed). Hard prerequisite for 2.
4. **backlog/pcb-placer-obstacle-set-is-mounting-holes-only.md** — authored
   pcb_fixed_copper (plaza vias) is invisible to the placer; this is the
   mechanism behind 2's symptom (2 makes the state unstorable, this stops
   producing it). Needs a fixture with an authored via where an instance
   would land; ewod-dogfood-1 cannot exercise it.
5. **backlog/pcb-risk-is-a-max-so-any-money-term-is-a-free-tiebreaker.md** —
   risk() is a MAX over margin terms, so any MONEY term is a free tie-breaker
   against every non-maximal constraint (a $0.046 term overruled
   courtyard_overlap). Decides whether 4's fix can be graded or must be a
   hard gate.
6. **backlog/pcb-escape-layers-leak-fcu-between-net-class-and-realization.md**
   — escapes realize on F.Cu against a derived net class of ["B.Cu"]; live on
   pb345846. Below 1–5 only because it is one rule rather than a class.
   Reads realize/maze, which pcb-easyeda-round-trip's router item also
   touches; this thread sequences behind theirs.
7. **backlog/pcb-placer-starves-the-escape-corridor.md** — its acceptance
   criteria came off an invalid placement and are void; now a
   rewrite-against-a-new-fixture job that needs 1's real pad geometry.
8. **backlog/pcb-generator-version-is-a-manual-bump-with-no-tripwire.md** —
   op='route' never re-runs the generator, which is why pb345846 still
   permits F.Cu after the 09-27 fix. Leverage for 6: without a tripwire the
   same staleness re-opens any generator fix.

## Horizon

1. **routing_area cost term** (uncommitted, worktree-EWOD) — prices the
   board area a strand sweeps; the only thing holding the sink under the
   array. Waits on its own gate; a placer that needs no pinned sink.
2. **backlog/pcb-always-valid-board-invariant.md** implementation slices —
   waits on gr458087 and on 1; the precondition for trusting any number
   below this line.
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
   a class becomes enforceable rather than advisory.
6. **backlog/pcb-tapeout-checklist-seed-items.md** — the pre-fab gate; waits
   on 5, a checklist over unenforceable constraints is theatre.
7. **backlog/ewod-controller-and-hv-supply.md** — Reto-side, procurement
   lane, parallel; a testable system rather than a bare PCB.
8. **backlog/ewod-synthesis-protocol.md** +
   **backlog/ewod-oil-constraint-grounding.md** — the wet side; wait on
   physical boards existing (the protocol also consumes se-nucleic-chain's
   make_steps).
9. **backlog/pcb-ewod-multitile.md** — waits on 3 and 4; multitile
   multiplies whatever the escape corridor does.
10. **backlog/pcb-global-codesign-north-star.md** — the arc all of the above
    serves; re-read when ranking the next round.

## Parked

- **pb345846 regenerate** — destructive prod write; unparks on Reto's
  per-write go-ahead, and not before 1 and 5 land or it is done twice.
- **U_TEMP part selection** — C32254 is a dual MOSFET placeholder; unparks
  when Reto picks a real LM75-class part (detail in item 1's file).
- **backlog/pcb-via-geometry-ignores-pad-side-and-pads.md** — unparks with
  2, where via-vs-pad becomes an enforced rule rather than a reported one.

## No action needed

- **gr346009** — closed by the footprints-view per-pin count fix; the count
  it exposed is item 1. Soft-deleted, do not reopen.
- **backlog/pcb-pre-place-route-blocks.md** — already landed; verify and
  delete.
