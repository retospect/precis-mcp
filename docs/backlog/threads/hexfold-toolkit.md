# hexfold toolkit

**Status:** ends when hexfold composites join, catalogue and validate
themselves end to end with trusted measured rows, the `spec.md` §28 roadmap
reaches its three named test pieces (the box, the rotary ratchet valve and
the T-handle bearing), and the se + hexfold paper (td344088) reports it.
Today the join op and the environment catalogue are **verified working
against prod** — the 2026-09-30 dogfood's two alarming findings were
artefacts of a stale MCP process and are refuted, and the damage that
process did is now detected (`composite_part_stolen`) — so the order below
is: make the execution environment trustworthy, then diagnosable, then make
measured rows trustworthy.
**Last reviewed:** 2026-09-30 (re-ranked after gr457995 and gr457996 were
refuted, then again when the integrity check shipped and gr458061 moved to
the `session-mcp-shared-server` thread; pillar review same day added the
T-handle bearing test piece, four dogfood-correctness gripes, and the
instrumentation-leg horizon item — note at the bottom)
**Worktree:** `hexfold-toolkit` (live work is currently in `hexa`)

## Do next

1. **gr458061** — HANDED OFF 2026-09-30 to the `session-mcp-shared-server`
   thread; the fix is in `src/precis/`, not this surface, and two trees are
   live there. It splits in two, and **this thread's precondition is both
   halves**: **gr457361** (that thread's Do-next 2) makes the status
   surface truthful about which sha a session is served, and
   `backlog/mcp-staleness-title-roundtrip-guards.md` item 2 (its Do-next 2)
   restarts a server whose mounted checkout has advanced. Truthful
   reporting alone would tell me a dogfood was run against stale modules
   without stopping it from happening, so gr458061 stays open until both
   land. Ranked 1 as a precondition, not as work: until then, every item
   below that is verified by dogfooding is verified provisionally, and the
   re-verification is this thread's job.
2. **backlog/se-join-observability.md**, **slice 1** (`view='report'`) —
   a join's findings live only in the minted structure's meta and there is
   no `view='catalogue'` despite §25.3 specifying one. The dogfood spent
   six SQL queries and a container exec on "which row governed this
   seam?", and never asked the question that mattered because asking was
   expensive. `status: ready` and both open questions decided 2026-09-30:
   three slices in the one file, shipped in order, and `view='report'`
   lives on `se` addressed by block. Slice 1 ships alone and is the
   unblocker; slice 3 (the join dry-run) goes last, when there is a
   reading surface to prove it wrote nothing with. The top item that is
   actually work in this tree.
3. **gr456641 + gr457997** — one root cause: `EnvKey` records no
   measurement extent, so the seam radius and the armchair leak threshold
   (2.9° against zigzag's 0.025°) are both tube-length artefacts keyed as
   rim-type properties. Do them together. Precondition for
   `trust_measured`, which is the entire point of the catalogue.
4. **gr346966** — stick-rung seam-adjacent angles relax to 82–93° on every
   cap fuse. Independent of everything above, and it caps how far any
   stick-rung number can be believed — including 3's re-measurements and
   the valve's Q4 clearance stub, which is explicitly gated on it.
5. **gr456213** — join silently re-parents a block already a child of
   another composite, leaving the first composite's tree incomplete and
   claiming the same rim twice. Live correctness bug, not the stale-process
   artefact gr457995/gr457996 turned out to be — worth checking whether it
   is the shape `composite_part_stolen` was built to catch.
6. **gr454650** — a len=1 armchair tube reports 20 dangling
   atoms per rim instead of 10 (both rims share the one period); the fuse
   to a cap then fails on a spec that is correct. Related: gr454563,
   gr454488.
7. **gr454563** — the parser silently accepts unknown primitive keywords
   (`length=` instead of `len=`), so a typo surfaces as an unrelated
   `fit.unsolvable`/`port.mismatch` error pointing away from the defect.
   Cheap parse-time fix; cost a naive-agent dogfood 2 of 19 calls.
8. **gr454488** — five residuals from the 2026-09-28 dogfood: every
   `generate` block trips `mode_binding_mismatch` because generate never
   sets mode; sheet rim port direction is centroid noise; the persisted
   build record drops geometry findings the check-mode echo has; "dry-run"
   wording survives past its rename; generator-declared measures claim
   `origin=user`. Five independent one-line fixes, bundled because one
   dogfood found all five.

## Horizon

1. **`spec.md` §28.3 armchair lids** — the flat-lid family is done for
   zigzag `(6k,0)`; the armchair half is open. Delivers caps for
   armchair-rim parts, and the valve rotor is a lid pair, so the valve
   test piece waits on it.
2. **`spec.md` §28.3 `opening(port=)` + `junction(3)`** — solve a host hole
   from a target rim, then opening + fuse as a tee. Delivers the tilted
   pill (`geom.join.angle`) and the first branching topology.
3. **`spec.md` §28.3 elbow + closure → genus-1 torus, then genus-N** — the
   first real `registry.closure` test; delivers the junction/tube algebra
   the periodic cell and schwarzite nets reuse.
4. **`spec.md` §28.3 box + valve test pieces** — the ~4 nm pillbox and the
   radius-changing shell with a two-lid rotor, each as separate blocks with
   a revolute joint. The acceptance artefacts for the whole discrete half;
   waits on 1.
5. **backlog/hexfold-t-handle-bearing.md** — the third test piece (Reto,
   2026-09-30), alongside the box and the valve.
6. **backlog/hexfold-seam-type-catalogue.md** — the seam-motif rows the
   catalogue's third row type exists for. Waits on Do-next 3, since a
   motif measured at one extent has the same defect the radius had.
7. **`spec.md` §28.8 valve tool set** — clearance field → pocket extractor
   → attachment-site enumerator → complementarity scorer → bond-energy
   audit → drag-vs-torque. Delivers the valve's design surface; its Q4
   clearance stub is gated on Do-next 4.
8. **rotary-ratchet-valve.md Q2** — scrubber cadence per poison species,
   decided by instrumenting the first lining, so it waits on 7.
9. **backlog/hexfold-sp3-seam.md + backlog/hexfold-sp3-isolation-band.md**
   (§28.7) — three- and four-sheet joins at an atom, and the valve's sp³
   isolation loops. Sequenced here by choice, not blocked: §28 lists it as
   unblocked since step 2 landed. `JOINERS` is already keyed on a lattice
   *pair* for it.
10. **backlog/hexfold-instrumentation-leg.md** — the instrumented first
    lining rotary-ratchet-valve.md Q2 (item 8) needs to decide scrubber
    cadence; sequenced right after the seam it instruments.
11. **`spec.md` §28.5–28.6 smooth solve + direction field** — discrete-mesh
    smooth solve, curvature bound, bent collar; delivers the tapered
    (collar-driven) shell the discrete washer step stands in for.
12. **td344088** — the se + hexfold paper. The thread's end state; it
    reports the above rather than waiting on all of it.

## Parked

- **backlog/se-op-handler-test-fixture.md** — was ranked for gr457996,
  which is refuted, so its motivating case is gone. The prepare/finish
  seam is still untested; unparks the next time a phase bug is suspected,
  with a real instance to write the first test on.
- **backlog/se-join-unknown-op-in-web-proposal.md** — `status: ready`, but
  viewer-surface work; unparks when someone is in `precis_web`.

## No action needed

- **gr457995**, **gr457996** — both REFUTED 2026-09-30. The
  `join.part_addressed` guard and the catalogue seed are both correct; a
  16-hour-stale server process produced both symptoms. Kept as the worked
  example behind Do-next 1.
- **gr456201** — ruled: regeneration is the remedy, no new write path.
- **gr456202** — not a bug; `rim_word` takes `abs(turn)` by design.
- **gr456203**, **gr456212** — auto-diagnosed as already fixed. Verify
  against main and close; no code work.
- **backlog/se-composite-integrity.md** — SHIPPED 2026-09-30 as
  `composite_part_stolen` (error) on `view='validate'`. The prod sweep it
  called for found **1 violating row in all of prod**, the deliberately
  corrupt `hexfold-catalogue-dogfood` (ref 457890); result recorded on
  gr458061. Kept here one review cycle because the sweep's first run
  returned a confident 0 — it joined `refs.handle` instead of
  `ref_identifiers` where `id_kind='cite_key'`, and matched nothing.
  Re-verified against the deployed code after the 2026-09-30 gate: the
  SQL oracle still returns that one row, and `view='validate'` on
  `hexfold-catalogue-dogfood` reports `1 error(s)` — the
  `composite_part_stolen` finding, naming both composites.

<!-- Re-rank note, per the README: the first version of this list ranked a
join-side corruption bug at 1 and a test fixture at 5. Both rested on
findings a 16-hour-stale MCP process had manufactured. The lesson that
changed the order is that an unverifiable execution environment outranks
the defects you think you found in it. -->
