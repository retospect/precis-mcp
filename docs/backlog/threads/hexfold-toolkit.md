# hexfold toolkit

## Resume

- **Pillar:** 3d-design
- **Next:** Reto's fin scenes (2026-10-07, via chat-interface, after the Y): `hexfold_scene` feature `fin-sp3-z` is built — an `(n,n)` tube with a 4-row-pair strip grafted along one axial zigzag chain by one radial sp3 bond per period (§11.1 `bond`, not the refused unequal k3 seam), outward or inward. Prod ops after the deploy: outward `tube:[10,20]` and inward `tube:[18,20]` (rows 4 needs at least (16,16); the tip keeps 2 Å from the axis), then the /se URLs to Reto. Local measurements: outward 963 atoms, inward 1,603, no clashes, sp3 host angles 87–124° at the pinned registration. Then the gr459567 family's authored bud face on a flat sheet (W3 pair bar shipped 2026-10-07), then gr459928. Y variant A ran on prod 2026-10-07 12:15Z (round 5, fc8ad08f7): `se:hexfold-dogfood-y-a` block `y` UID 212, 5,790 atoms, 87 seam eight-cycles, 0 clashes, 696 ring-angle WARNs, matching the dev-DB build; URL handed to Reto, td471803 done. Native stored-target acceptance on legacy r5 recorded on gr470905 (default reads honest-unavailable, override and null boundaries as specified); a receipt-backed positive read still needs a post-R17 sphere/cylinder generation. Contract/evidence: [catalogue R17 variant A](../hexfold-seam-type-catalogue.md#r17-variant-a-public-scene-contract--2026-10-07).
- **Blocked by:** Three-plane Y cannot use the revolution-target receipt; surface_deviation stays unavailable. Legacy r5 remains unavailable without receipt. Unequal variant B needs seam-row measurement. Larger-pillar cost gate is a separate next slice; scientific/hero/model holds unchanged.
- **Unblocks:** Trusted geometry for [nanobuds-paper](nanobuds-paper.md#resume).
- **Acceptance:** Use [hexfold-ideal-surface-then-tile](../hexfold-ideal-surface-then-tile.md) and the verification steps in [Do next](#do-next); check current deployment and worktree state before acting.
- **Worktree:** window9 thread01a108d5-fdb3-7b91-a50b-c3bc5532aa56; work/hexfold/y-junction-a, codex-hexfold/.scratch/y-junction-a. The source branch, dirty pillar notes and all earlier scratch/branches remain untouched.
- **Builds:** Not estimated here; use the owning item's current slice estimate.
- **Detail:** [Ranked work](#do-next) · [Horizon](#horizon) · [Coordination map](INDEX.md).

## Default behaviours (rulings that every build applies)

- **Open edges are H-terminated at the end of every hexfold-family
  build** (Reto 2026-10-07, via chat-interface; shipped in
  `_block_from_net`): after the relax and the geometry judgement, every
  carbon short of its valence gets an H at 1.09 Å along its missing bond;
  the block records `terminated: {element, mode, count, hosts}` and the
  structure carries the tag `terminated:h`, so se reports, renders and a
  DFT handoff know the edges are capped and why. The findings still
  describe the carbon net. Opt-out per op: `terminate: "none"`;
  `terminate: "ports-open"` leaves join ports' rim atoms bare for a later
  fuse. The scene grammar (Do-next 1a) inherits it as its cross-cutting
  rule.

## Thread context

**Status:** ends when hexfold composites join, catalogue and validate
themselves end to end with trusted measured rows, the `spec.md` §28 roadmap
reaches its three named test pieces (the box, the rotary ratchet valve and
the T-handle bearing), and the se + hexfold paper (td344088) reports it.
Today the join op and the environment catalogue are **verified working
against prod** — the 2026-09-30 dogfood's two alarming findings were
artefacts of a stale MCP process and are refuted, and the damage that
process did is now detected (`composite_part_stolen`). The execution
environment is now trustworthy too (every session on the shared HTTP
server since 09-29), so the order below is: make joins diagnosable, then
make measured rows trustworthy.
**Last reviewed:** 2026-10-01 (09-30 re-ranks: gr457995/gr457996 refuted;
integrity check shipped and gr458061 moved to the
`session-mcp-shared-server` thread; pillar review added the T-handle
bearing, four gripes and the instrumentation leg; 22:30Z dogfood round
shipped gr458713, confirmed gr454650 harder, closed gr454563. 10-01
round: gr456213 **closed** — it was fixed on 09-29 and I had ranked it 2
off a stale auto-diagnosis, see "No action needed"; gr459058 and gr459057
filed from that round, then both fixed in the 01:18Z round, leaving only
gr459058’s cascade-vs-refuse ruling at 3. 02:16Z round: gr454650
**closed and my escalation of it retracted** — already fixed, with
regression tests I never looked for; the one real defect under it was a
ring-less net crashing the stick pass, also fixed. 11:00Z: gr456203 and
gr456212 verified and closed (the latter needed its element half
written); gr458061 removed as a met precondition — the transport move it
waited on happened 09-29 — note at the bottom)
**Worktree:** `hexfold-toolkit` (live work is currently in `hexa`)

## Detailed handoff

- State at 2026-10-04 07:21Z (round 6):
  - On prod: cycle 1 round tops (ebe4aedb1, 58bb0a6ee, 727728cc9).
  - On main, not yet deployed: the per-scene-op budget wording and hexfold
    0.3.1 (0adb12e7f, bbd013d99).
  - Landed for round 6: 2cf091466, cycle 2 step 1 (the per-n plan table),
    and f30684f8c, the scene-relax guard. No job or agent is running, and
    no code is unlanded.
  - Spread-pentagon rows are scoped at about 2 cycles; under MACE they
    pay. hexfold-toolkit-6 confirms Reto's (b).
  - Open, in this order:
    1. the rest of cycle 2;
    2. the spread build's pre-check (MACE on the other spread rows and on
       washer k4 L2 / k5 L3);
    3. the spread build itself;
    4. then the buds.
  - Not on any branch: the bud-in-scene fix (lead: lopsided seam bonds,
    under Do next 1, S4).
  - Gap found in the 2cf091466 dry run: the table makes planning free,
    but the old sphere ceilings also bounded the scene relax. A scene op
    with default spheres at n=12 and n=24 took 476 s locally (0 s planning);
    one tabled n=12 sphere scene takes 54 s and one n=24 scene takes
    356 s, so n ≤ 12 stays. Fixed in the same round: a
    scene-relax guard in `hexfold_scene._normalize` allows one `sphere`
    per scene op, n ≤ 12, whether tabled or not. Raising n there needs a
    timed scene (hexfold-corner/scene_cost.py).
  - Next step: the round-6 prod dogfood, after the guard deploys.
    - Ops are in hexfold-corner/dogfood_r6.json; the local dry run is
      dogfood_r6_local.py.
    - Mint design `hexfold-dogfood-r6` with `scripts/prod-precis` and read
      it back with `view='block'`.
    - Pass:
      1. `ball12` (default sphere, n=12, sheet 30×30) reports
         `planned: table` with k=4 L=3 R 10.07 and all five bars met, in
         about 55 s (round 4: ~95 s, which included planning).
      2. `pill12_tabled_lid` (lid, `top_fillet` 4.69) reports
         `planned: table`, dome_rows 3, in about 15 s.
      3. A second put with the file's `refusal_check` params (spheres at
         n=12 and n=24 in one scene op) is refused before planning by the
         guard: "at most 1 top: 'sphere' per scene op".
    - Write one Resume line, then mark the round.
    - Then the θp band and the R_min refusal.
- Round 4 dogfood (prod 727728cc9, 2026-10-04 04:43Z): design
  `hexfold-dogfood-r4` → **pass**, with the warnings as designed. The put
  took 143 s and holds two generates:
  - `ball12`, a (12,0) sphere top: 2406 atoms, rings {5:12, 7:12}.
  - `pill12r`, two rounded lids: 2652 atoms, rings {5:12, 7:12}.
  - Both reports are ok with 0 ERROR, and both are stored as hexfold 0.3.1.
  - `scene.top.relaxed_shape` fires on every top, with the stick-model and
    trial-tube wording: sphere 0.92 Å p95 / ~0.9 Å at the pole; lids
    0.59 Å / ~0.6 Å.
  - `geom.summary` shows the new totals: ball12 589 angles past tolerance,
    pill12r 119.
  - Gap: the 16-candidate budget is per `generate` op. One put with two
    scene ops ran 143 s, past the ~2 min the refusal message promises. The
    per-n plan table (cycle 2, first) removes it.
- Round 3 dogfood (prod 929107f32, 2026-10-03 20:38Z): design
  `hexfold-dogfood-r3`, pill12 through `hexfold_scene` via
  scripts/prod-precis → **pass**, 55 s. The `view='block'` readback shows
  `relax=tethered ks={t:4, q:6, p:6}`, the `scene.top.joint` WARN on t,
  and the stored `scene` and `plan`. The report is ok, with no
  `scene.bar`. 2898 atoms and a 1.05 Å minimum pair, the same as the
  local S4b run. The only clashes are WARNs at the t ball neck
  (gr464391). Reto's ball-top nanobud question is review-queue
  hexfold-toolkit-3.
- Round 2 dogfood (prod 63301c5c): design `hexfold-dogfood-r2` through
  scripts/prod-precis → **pass**. `sw` is a true Stone–Wales rotation on
  a stored build (rings {5:2, 7:2}, atom count equal to pristine, bond
  rms 0.006 Å). The stored record carries the geometry findings:
  `view='block'` on `pillar_foot` shows "report: NOT ok — 51
  finding(s)" with 4 ERROR `geom.clash` at 0.54–0.97 Å. Gap filed:
  `view='validate'` says 0 errors and `view='drc'` none on the same
  design (gr464342).

## Do next

1. **backlog/hexfold-ideal-surface-then-tile.md — ideal smooth surface
   first, then tile it** (Reto 2026-10-03, via nanobuds-paper-25; the
   nanobuds hero dr173020 waits on it).
   - The ask: author the ideal surface (flat sheet, a fillet of chosen
     radius into the tube, a chosen radius into the ball or cap), then
     tile that fixed surface. The tiling must not bend it.
   - Why today's drum looks co-optimised:
     - its catenoids and table-snapped fillet radius come from the tiler's
       needs, not from authored radii;
     - its weak relax tether (0.01) leaves atoms a mean 0.7 Å off the
       target.
   - Stages, each through a design note. Distance is counted in dev
     cycles, not dates (Reto, 2026-10-03, hexfold-toolkit-2). One dev
     cycle = build, design note, orchestrator verdict, qland.
     - S1, surface spec + deviation metric, measured on hero5 and
       `hexa-smooth-drum-v2`: **done**, 0 cycles left.
     - S2, Gauss–Bonnet defect rows: **done at the 3+3 level**, 0 cycles
       left. The per-annulus form is the next rung below.
     - S3, tile and pin one feature: the tether and the planner
       `plan_foot` are built (see the backlog item's acceptance). The
       pillar (6,0) R = 3 and the pill (24,0) R = 5/8 are inside the
       bars; (18,0) is an open row. **Done**, 0 cycles left; the (18,0)
       re-seed probe is one probe, off the hero path.
     - **S4, the hero scene: in progress, 1 dev cycle plus 1 render round
       trip out.**
       - S4a, **built**: `plan_scene` tiles several authored feet on one
         sheet in one tethered relax. The hero with a (12,0) R = 8 pill
         meets the S4 bars (numbers in the backlog item's acceptance).
         Next: hand it to nanobuds-paper as relaxed coordinates, since the
         tether is not in the `.hx` text and figs/render_hero.py's
         untethered build cannot reproduce it. A prod `hexfold` generate
         still relaxes untethered until S4b.
       - The (24,0) pill variant waits on gr464358 (the k=10 frustum is
         seeded onto the sheet). Hole cells that hit gr464341's fuse
         phase are refused, so a layout change may need a cell moved.
       - Reto picked pill12. The pillar's fused (6,0)→C60 neck opens under
         MACE-MP and GFN2-xTB (gr464391), so it can only be shown as a
         stick model.
       - The bonded alternative passes both relaxers as an isolated
         pillar: a (12,0) tube with a lid and the C60 as a sidewall [2+2]
         bud (scripts in hexfold-corner/s4_lid_check.py).
       - Inside the tethered scene it fails: the multi-pass tethered relax
         leaves about 6 close pairs around the bud, and four scratch fixes
         did not pass.
       - Buds on tethered tubes: root cause found 2026-10-03. The
         `_tethered_relax` settle test judges only authored atoms, so it
         stops at about pass 8 with the bud still pulled in; close pairs
         reach 0 by pass 16 with the tether on. Stopping the relax only
         once the close pairs stop falling (pass 14, 0 pairs) is not
         enough. The bud–tube seam still opens: MACE 2.58/3.55 Å, xTB
         1.65/2.14 Å (hexfold-corner/rc2_*, 2026-10-03). Lead: the scene
         starts the two seam bonds lopsided (1.418/1.551 Å), while the
         isolated pass started them at 1.427/1.428 Å. Cycle 3 stays a
         full cycle. Reto's hero choice is nanobuds-paper item 31.
     - **Next, spec-driven shapes** (Reto, review-queue hexfold-toolkit-3
       and -4; design note and orchestrator verdict "Spec-driven shapes"
       in reviews/hexfold-toolkit*.md, 2026-10-03 20:53–20:55Z). Order:
       - cycle 1, **built**: `top: "sphere"` (`top_R`, `top_fillet`) and a
         rounded `top: "lid"` (`top_fillet` up to r) in `hexfold_scene`, via
         `plan_top` (reviews: "Cycle 1" 21:09Z, 21:15Z, 21:46Z):
         - a washer route (tube → `cap(6k,0) - hex(n/6-1)` → `(6k,0)` bulge
           → `cap(6k,0)` lid) held to the authored meridian; k in [r+3, r+5],
           L in 1..3, five bars per candidate, the relaxed (tether-off) p95
           measured per candidate and WARN `scene.top.relaxed_shape` over
           0.5 Å; WARN `scene.top.R_mismatch`;
         - `geom.summary` counts every corner past tolerance
           (`bond_n_over`, `angle_n_over`); hexfold geometry reports from
           before hexfold 0.3.1 list at most 10 entries per code and carry
           no `n_over` totals (0.3.1 marks it; the golden provenance pin
           moved with the version only);
         - cost ceilings until the per-n plan table in the next cycle
           lifts them: one `top: "sphere"` per call (~95 s), a sphere only
           to n=12 (n=24 plans ~100 s alone), and 16 candidate builds per
           call (sphere 9, each distinct `(n, top_fillet)` rounded lid 4);
         - on prod since round 4 (`hexfold-dogfood-r4`); the n=12 washer ball
           relaxes ~0.9 Å flatter (stick), ~2 Å (MACE/xTB), so the
           relaxed-shape WARN fires;
       - cycle 2, step 1 **built**: the per-n plan table
         (`hexfold_top_plans.json` beside `authored_foot.py`). It holds:
         - spheres with the default fillet at n = 12/18/24/30/36;
         - lids with `top_fillet` = r rounded down to 0.01 Å at n = 12/18/24;
         - all keys meeting the bars, with hexfold 0.3.1 recorded.

         Tabled tops build nothing and are stored as `planned: table`; the
         candidate budget counts only live tops. The scene-relax guard (one
         sphere per scene op, n ≤ 12) applies to every sphere. A slow test re-plans lid12 and
         sphere12 live. To regenerate: `uv run --with numba python -m
         precis_se.atomic.generators.authored_foot`, ~10 min.
       - cycle 2, rest:
         the θp finding plus its band and the R_min refusal on the ball, and
         the MACE/xTB verdict as product evidence;
       - next: spread-pentagon rows on the dome, so a ball stays round after
         relax (Reto 2026-10-04, hexfold-toolkit-5 option b: ahead of the
         buds). Scoped 2026-10-04, about 2 cycles. Under MACE the 6+3+3
         graded top moves 0.42 Å RMS against the washer's 0.81, and its pole
         drops 0.72 Å against 2.01. The stick relax called them a tie, so
         the chooser's roundness number must come from MACE (offline, in the
         plan table). Reviews 07:04Z and 07:07Z, hexfold-corner/sp_*;
         hexfold-toolkit-6 confirms (b). The orchestrator's conditions
         (review.md, "Spread tops under MACE"):
         - before the build, MACE the other five clean spread rows and the
           washer k4 L2 and k5 L3;
         - prefilter on the five bars only, with every passing candidate
           MACE'd offline into the table;
         - fmax 0.01 for tabled numbers, with the model named;
         - if a washer key's chosen row changes on regeneration, bump the
           hexfold version and say so in the skill;
         - one xTB cross-check at the build's start;
       - then buds on tethered tubes;
       - then spec v0, Reto's CAD grammar (`plane`, `hole_tube`,
         `ball_on`/`lid_on`, `round` defaulting to min(1.5 × the θp-derived
         R_min, the room the neighbouring pieces leave), with the chosen R
         written into the stored spec,
         `nest`), compiled golden-neutral from `hexfold_scene` params;
         bounds as `add_measure` bands. This is the nanobud to spec.
       - then (2 cycles) the coaxial two-shell bearing. Its queue item carries the
         commensurate-friction sentence. The gap is the centre-line radius
         difference.
       - The render round trip is nanobuds-paper's build plus Reto's
         eyeball. Each change he asks for that stays inside the bars is
         one more hero build, with no new code.
       - S4b, **built**: the se generator `hexfold_scene` (params:
         `sheet` and `features`, plus optional `extra` and `k_tether`)
         mints tethered scenes, about 35 s, synchronous.
         - It reports `scene.bar` and `scene.top.joint` WARNs, also in the
           provenance; the stored record keeps `scene` and `plan`.
         - `plan_foot` rows with ERROR findings no longer meet, so the
           (24,0) R=8 k=10 cell now misses (gr464358).
         - `hexfold` output is unchanged, pinned by the golden test.
         - Next: a prod dogfood of the pill12 scene after the round
           deploys, read back with `view='block'`.
       - Round-2 prod dogfood (2026-10-03 13:53Z, design
         `hexfold-dogfood-r2`, hexfold 0.3.0) shows why this comes first.
       - `sw` and gr462144's sheet_sw build ok on prod:
         - rings {5:2, 7:2};
         - atom count equal to pristine;
         - sheet_sw bond rms 0.006 Å, no seed overlap and no clash.
         - gr462144 is closed.
       - The pillar 3+3 foot through the deployed generator (untethered
         stick) is NOT ok:
         - `geom.clash` ERRORs at 0.54–0.97 Å;
         - angles down to 59°.
         - This is the same crumple as the S3 tether-off rows, and the
           clash check catches it;
     - next rung after S4: per-annulus Gauss–Bonnet rows (2+2+2 / 1×6:
       irregular hole + tube-wall surgery, gr459928), the route to
       R ≳ 12 Å. At least 2 dev cycles (the grammar, then the rows), not
       yet scoped.

1a. **backlog/hexfold-scene-grammar-from-primitives.md — scene grammar
   from primitives** (Reto ruled YES 2026-10-07, via chat-interface;
   status draft, readiness vet owed). Ranked right behind the hero path
   because it is the frame every further "next variant" lands in: tube,
   sheet, seam/graft line, strip and top as primitives, each with its own
   tether, one composition rule, one planner and one judgement; today's
   foot features, the Y and the fin become parameter sets, stored scenes
   render byte-identically. Slice 0 first: a tethered-relax op over `.hx`
   text or a join plus a tether list, so a scene is askable the same day
   without a deploy. Cross-cutting default: H-termination of every open
   edge at the end of every build with the `terminated:h` tag
   (`terminate: "none" | "ports-open"` opts out) — shipping first in
   `_block_from_net` for the hexfold family. Acceptance: Y-A, both fin
   scenes and the 120° seam tube regenerate from parameter sets within
   the bars, plus one new scene with zero code change.

2. **gr459567 family: overlaps the clash check now reports.** The bud
   placement shipped on 2026-10-02 with `geom.clash`. Every [2+2], [9-6]
   and [8-7] C60 now seeds outside its host, and the `geom.clash` bands
   are ERROR under 1.0 Å and WARN up to 1.8 Å. The orchestrator's C1
   verdict (2026-10-02 14:55Z, `reviews/hexfold-toolkit.review.md`) sets
   the order:
   - **Round 2 deployed 2026-10-03 13:49Z** (63301c5c). It carries the C3
     disclination seed 7a746e5cc and the C4 flat-host bud face
     (`build._flat_bud_sides`, outer rim by largest mean radius,
     `place.face_conflict` for a washer whose rims disagree).
     - Open: nanobuds-paper regenerates prod `hexa-nanobud-pillar`
       (structure 459564, stored as a peapod) and reports the
       before/after z table. I confirm it when it arrives.
     - gr462144 (`sheet_sw` stacked seed) is verified on prod and closed.
   - **Seed tier: built 2026-10-02.** `geom.seed_overlap` is an ERROR
     for seed pairs under 0.7 Å, and `geom.summary` gains
     `seed_clash_count`/`seed_clash_min`. Exactly the four seed-wrong
     examples raise it. The bud necks seed at 1.44–1.78 Å; stick is what
     squeezes them.
   - **Next, the seeds**, each measurable now by its seed numbers:
     - **Sheets with authored ring defects: the disclination seed shipped
       (7a746e5cc).** No seed pair under 0.7 Å and no stick clash ERROR on
       any defected sheet; numbers in `reviews/hexfold-toolkit.md` (C3
       result).
       - **K = 0 clusters, 57 (one dislocation): shipped**
         (`build._volterra`, K0 note + verdict 2026-10-03). The cut is
         read off the cluster seed's turn boundary, J is a truncated
         least-squares fit, and one Laplacian solve spreads it. The far
         field is 1.40–1.44 Å and the snapped circuit reads a = 2.46 Å.
         At plateau the worst bond is no worse than C3: 0.0426 vs 0.0450
         Å on g57.
       - **`geom.seed_short_bond` (WARN, bonded seed pairs under 1.0 Å):
         shipped.** It fires on two examples: `sheet_pill_bump` (6 bonds,
         min 0.49 Å; feed into gr459812) and `tube_ring_closure` (0.00 Å,
         the closure fixture). `sheet_sw` (0.72 Å) is clean since the bond
         rotation.
       - **SW: `sw` is a bond rotation now** (K0 SW ruling 2026-10-03;
         `Patch.rotate_bond`, `GLYPHS` construction table). The old wedge
         footprint was a 5577 dislocation dipole (pentagons adjacent, net
         b = a), removed. Measured on sw30: circuit 0, seed far field
         1.420–1.422 Å, plateau worst 0.028 Å. Reto ruled 2026-10-03
         (hexfold-toolkit-1): it ships in round 2. The
         stacked-seed ERROR test's only fixture is now a flat-seeded
         `57@(4,4,A):1`; if the 57 seed is ever de-stacked too, find it
         another.
       - **Later:** compare the planar relaxed 5-7 core (fix seed, 0.042
         Å worst bond) with the buckled one (flat seed, 0.029) by MACE
         energy once the science lane is back. If buckled wins, the seed
         tier should offer an out-of-plane core perturbation as an
         option.
     - **gr462074** `tube_ring_closure`: seed 212 pairs, 0.00 Å. **Not a
       placement bug.** Two straight rigid tubes cannot close a ring, so
       the joint cycle solve stacks `b` exactly on `a` (centroid distance
       0.0). The example is a `registry.closure` fixture, and its ERROR
       is the correct report. The example comment and
       `test_tube_ring_closure_gets_a_seam_cycle_finding_and_improves`'s
       docstring now say so. Close gr462074 on that reading when the MCP
       is back (it was down at 20:00Z).
     - **gr462075** `flanged_doughnut`: every seed pair under 0.7 Å
       (22, min 0.15 Å) lies among the 24 atoms of the `outer` k=3 seam.
       Seam atoms are seeded at their rim neighbours' mean (the gr347187
       fix), and the three rims they average over (`top.in`,
       `bottom.in`, `flange.hole`) are placed up to 11.45 Å apart by the
       known part-graph cycle residual
       (`test_example_seeds_have_no_long_crossing_bonds`' 11.7 bound). So
       the root is that residual, not the seam seeding: re-seeding the
       seam would only hide it. That is a `place_graph` item and needs a
       design note of its own.
     - **gr462075** `flanged_doughnut` (+`_oh`): seed 89, 0.15 Å.
     - **gr459812** `sheet_pill_bump`: seed 66, 0.49 Å. `capped_tube` and
       `capped_tube_da_neck` seed clean (≥ 1.47 Å) and only stick
       crushes them, so they belong to the spring check below, not here.
       A curved rim seeding its seam mirrored on the *fuse* path is the
       same symptom the menu path had. Measure whether the cause is the
       same; if it is, note that a tube cannot simply be reflected,
       because it is chiral.
   - **Then the springs, before any `K_REP` change.** Where the seed is
     fine and stick compresses the junction (buds, DA/DB necks, capped
     tubes), angle deviations stay at 47–61°. Check the rest angle and
     rest length stick gives the attach atoms and their first
     neighbours. A 120° rest angle on a 4-coordinate [2+2]/junction atom
     would squeeze its neighbours together exactly as observed. If that
     is the cause, fix it there and leave the repulsion constant alone.
     Item 8 (gr346966: cap-seam angles relaxing to 82–93°) is probably
     the same question at a different junction; check both together.
   - **Only then `K_REP`.** Measured for the C1 review: at 1.0 the bud
     minima rise to 1.58–1.75 Å, clean specs are unchanged, but strain
     moves into bonds and `sheet_sw` regresses. Adopting it needs a
     relaxer version stamped on stored results and the catalogue's
     measured rows regenerated in the same commit. Sweep the non-bud
     prod structures before it lands. Until then
     `test_nanobud_menu_seed_has_no_stick_clash` holds [9-6]/[8-7] to
     1.0 Å, not 1.8 (W2, partial).
   - **Per-element-pair clash bar (W3): shipped 2026-10-07.**
     `Profile.clash_pair_A` (default H–H 1.5 Å) overrides `clash_A` for
     a listed pair; `geom.clash` names the elements and the bar it
     answered to. Re-measured before the change: armchair-bay H–H 1.872 Å
     on `tube(8,8)`, 1.888 Å on `tube(10,5)`, 2.59 Å on `tube(10,0)`,
     2.46 Å on `sheet(6,6)`; C–H at least 2.52 Å; no carbon clash count
     changes on the examples.
   - **Which face a bond seeds on, on a flat sheet.**
     `build._surface_normal` cannot get a sign from the instance centroid
     on a flat instance, so it falls back to largest-component-positive.
     Every bond-verb attachment to a sheet therefore seeds on the same
     face. That is fine for one bud. A design that wants buds on both
     faces, or a sheet whose outside is fixed by the assembly, has no way
     to say so. The fix is an authored face, or reading it from the
     assembly. From the round-2 review.
   - **Not testable today:** the non-C60 refusal (`place.mirror_refused`)
     and the second-host check (`place.inward`) have no test. The grammar
     has only one fullerene, C60, and one menu per bud, so no spec reaches
     either branch.
   - The 2026-10-02 baseline before the fix (`check(geometry=True)`,
     clash count / min Å): capped_tube 38/0.91 · capped_tube_da_neck
     51/0.92 · flanged_doughnut(+_oh) 591/0.27 · nanobud_22 89/0.85 ·
     nanobud_87 17/1.22 · nanobud_96 13/1.47 · nanobud_da_neck 12/1.16 ·
     nanobud_db_neck 9/1.05 · sheet_bud_22 76/0.82 · sheet_pill_bump
     32/0.90 · sheet_sw 3/1.65 · tube_ring_closure 180/0.49. After the fix
     only the bud lines change: nanobud_22 0, sheet_bud_22 0, nanobud_87
     14/1.19 and nanobud_96 12/1.53, and all four balls now sit outside.
3. **gr459928 — graded bends, re-ranked behind the gr459567 family on 2026-10-02**
   (C2 verdict). The far-rims-pinned relax put the graded corner one ring
   row ahead of the baseline, which is the resolution of the measurement:

   | run | convex turn arc | intermediate rows |
   |---|---|---|
   | one seam | 4.5 Å | 3 |
   | graded 3+3, k=3 | 5.6 Å | 4 |
   | misphased k=0 (+3 5-7 pairs) | 7.4 Å | 6 |

   The misphased control spreads the turn more than the designed one, so
   turn width follows defect count and placement. Don't build tube-wall
   surgery on this. The decisive test is the 3-row (2+2+2) corner, which
   needs the irregular hole. When it exists, also report the arc length
   over which the slope covers the middle 80% of the total turn (it does
   not depend on where one row lands relative to a threshold).
   Background. Reto, 2026-10-01: the drum should have
   "no 1-ring-90-degree turns". Each bend should step through
   progressively steeper rings: sheet, slight slope, steeper, tube, then
   flare out a little, more, a lot.
   - The constraint: a fuse puts the full rim-turning mismatch (6 defects)
     into one seam ring. A bend is graded only by defects inside a patch.
     Today those work as one defect on a sheet and nothing else; the gripe
     has the probes.
   - Smallest unblock: Volterra surgery on tube walls, with the ray leaving
     axially through an end rim. A bend is then a tube instance carrying
     C3 orbits (0→60→90°) or single defects (34/48/60/70/80/90°), and
     the seam mints only the remainder.
   - Reto, 2026-10-01: we want two capabilities, and they are distinct.
     (a) Authoring, this item: the author places each defect explicitly,
     on any patch (sheet, tube wall, cap). This is exact and reproducible,
     and it is what a hand-designed graded bend uses.
     (b) Solving, Horizon 12 and Do-next 1: the author gives a smooth target shape, and
     §22's budget and distribution places the defects. (b) emits (a)'s
     defect lists, so (a) is also (b)'s output format and test oracle.
     Do (a) first.
   - Reto, 2026-10-01: concretely, "2 heptagons in this ring, 2 more in
     the next, and 2 more in the next" instead of 6 in one.
   - Built 2026-10-01 for the sheet-to-tube foot
     (tests/hexfold/test_graded_bend.py). Each row is a flat lid with a
     centred p-wedge cut and a centred hole, i.e. a cone frustum with rims
     6-p and -(6-p), so each seam mints only the step between neighbours.
     3+3 (`+ 3@`) is clean. 2+2+2 (`+ square@`, then `+ 2@`) has clean
     inner seams. Its flat-to-p=2 seam carries two extra 5-7 pairs,
     because a regular hex hole has 6 corners and the frustum has 4, and
     only 2 line up. Fix: an irregular hole with sides 3,3,6,3,3,6, which
     the grammar does not have yet. Relaxed with Tersoff, the bend spreads
     from r=5-7 Å (one row) to r=5-9 Å (3+3) and r=5-10 Å (2+2+2). Total
     strain energy is not lower (25 / 32 / 51 eV interior), partly because
     there is more curved area.
   - Engine fixes that unblocked it:
     - `Patch._frame_coords` merged A/B wedge copies for odd dirs, so any
       heptagon corrupted the sheet.
     - `hex(r)` holes now centre on a disclination core.
     - Flat lids accept authored defects.
     - Sheet and lid `b_expected` count the authored wedges.
   - Built topologically 2026-10-02, **unrelaxed**: the convex corner
     3+3, a flat washer's outer rim onto a p=3 frustum's hole, frustum
     rim onto the wider tube (`CORNER_33` in the test file). The tests
     prove a defect census and Euler closure, not a corner.
   - **Relaxed 2026-10-02, the result is borderline.** Method, per the
     orchestrator's C2 verdict:
     - neck and wall 7 periods long, only the far rims pinned;
     - Tersoff-1988 C;
     - slope between successive ring rows from their mean (r, z);
     - "convex turn" = from the flattest row after the neck turn to the
       first row at ≥ 80°.

     Results, by arc length and number of intermediate rows:
     - one-seam baseline: 4.5 Å, 3 rows;
     - graded 3+3 (k=3): 5.6 Å, 4 rows;
     - misphased k=0: 7.4 Å, 6 rows, with three extra 5-7 pairs.

     So grading buys one row. The k=3 washer never flattens (11.6° at
     its flattest), and turn width tracks defect count as much as design.
     Probes and plot are in
     `~/.claude/projects/-Users-reto-precis-mcp/hexfold-corner/`
     (`corner3.py`, `corner_profile.png`). The earlier 26°/42° figures
     came from whole-instance fits and are retracted. Recommendation,
     awaiting the verdict: don't rank tube-wall surgery on this. The
     decisive test is the 3-row (2+2+2) corner, which needs the irregular
     hole; move this item behind the gr459567 family. Clean only at
     k ≡ 3 mod 4 on the washer seam; any other phase adds three 5-7
     pairs. The flare at the tube top is the foot construction again. A
     seam's phase decides whether corners line up, so the k ranking
     (`build._k_cost`, used by `join.rank_k` and `hexfold options`) now
     breaks the max-ring tie on defect charge `Σ|6−n|`; before, `options`
     interleaved clean and dirty phases by index. The foot's k=0 was
     clean only because 0 ≡ 0 mod 3. Prod sweep 2026-10-02: 7 join
     composites, 2 resolved via `k='fit'` (456187, 456190), neither
     re-phases (every alternative ties on the new cost too); replay uses
     the recorded integer k regardless.
   - Still open:
     - The irregular hole. Phase cannot replace it: 6 and 4 corners share
       only 2 at every k (swept, 24 phases).
     - Tube-wall surgery, which is still unbuilt.
     - Then the full graded drum on prod.
   - Probe scripts are not in the repo (/tmp/hexa-bud/gradfoot.py,
     ports.py, seams.py, meridian.py, f3.hx, f33.hx).
4. **gr459602 + gr459568 + gr459571** — the agent cannot read what it
   built. The stats need the tier of the coordinates they were measured
   on: `structure-geometry-tier-visible` (Reto, 2026-10-01) makes that tier
   visible in the viewer. Reto asked for mean/extreme C–C bond lengths per build
   (gr459602); a structure's default `get` is an 80 KB atom table with no
   summary and its probe views disagree on argument names (gr459568); the
   check echo is two-thirds per-bond INFO (gr459571). gr459602/gr459568
   live in the `structure` kind, outside this thread's files; they rank
   here because hexfold builds are where they bite.
   - Reto asked again on 2026-10-01: "can we see bond and angle strain
     yet?" The ask has two parts. First, numbers: bond-length and
     bond-angle deviation, plus POAV θp, with mean, p95 and max. Second,
     the se 3D viewer colours each atom by its strain, as a toggle next
     to the tier badge.
5. **backlog/se-join-observability.md**, **slice 1** (`view='report'`) —
   a join's findings live only in the minted structure's meta and there is
   no `view='catalogue'` despite §25.3 specifying one. The dogfood spent
   six SQL queries and a container exec on "which row governed this
   seam?", and never asked the question that mattered because asking was
   expensive. `status: ready` and both open questions decided 2026-09-30:
   three slices in the one file, shipped in order, and `view='report'`
   lives on `se` addressed by block. Slice 1 ships alone and is the
   unblocker; slice 3 (the join dry-run) goes last, when there is a
   reading surface to prove it wrote nothing with.
6. **backlog/se-join-observability.md slices 2 and 3** — `view='catalogue'`
   (SPEC §25.3) then the join dry-run, after slice 1. Slice 3
   goes last by the file's own decision: a dry-run needs a reading
   surface to prove it wrote nothing with.
7. **gr459058, remaining half** — Reto ruled 2026-10-01 (recorded on the
   gripe): a design retire **cascades** to the structures its blocks
   minted, except structures promoted to building-block status, and
   (no ruling needed) except any structure another live design still
   binds. Blocked on one more call: the promotion mechanism — a tag
   (structure has no `tag()` today), folder placement, or a component
   row. Fix site: `persist.retire_design` + the se `delete` message.
   **td458221** closes with it.
8. **gr456641 + gr457997** — one root cause: `EnvKey` records no
   measurement extent, so the seam radius and the armchair leak threshold
   (2.9° against zigzag's 0.025°) are both tube-length artefacts keyed as
   rim-type properties. Do them together. Precondition for
   `trust_measured`, which is the entire point of the catalogue.
   - Reto ruled 2026-10-01 (td458117) that gr456641 belongs to this
     thread, not the auto-fix lane. Job 457204 timed out, and the branch
     it reported pushing never existed (gr458326).
   - Contained until then: `DbCatalogueStore` withholds measured rows
     unless `trust_measured=True`, and nothing sets it. Leave it off.
   - First slice, half shipped (7bf54004b, a stranded fix_gripe branch):
     a non-monotone `max_disp` downgrades the row to
     `coverage="unstable"` (`catalogue._STABILITY_RISE_FACTOR`, test
     `test_measure_environment_flags_far_rim_runaway_unstable`). Still
     open: asserting convergence. The `join.Relaxer` contract returns
     coordinates only; the stick relaxer discards its `max_force` and has
     no tolerance, and the geo relaxer's `trace.converged` is checked only
     by the test adapter (`tests/test_hexfold_seam_decay.py::_geo_relaxer`).
     Threading it means changing the `Relaxer` return type, so it is a
     design call, not a one-liner.
   - Second, larger slice: the `EnvKey` extent field, shared with
     gr457997.
9. **gr346966** — stick-rung seam-adjacent angles relax to 82–93° on every
   cap fuse. Independent of everything above, and it caps how far any
   stick-rung number can be believed — including 7’s re-measurements and
   the valve's Q4 clearance stub, which is explicitly gated on it.

## Horizon

1. **backlog/hexfold-fullerene-from-sphere.md — standard fullerenes from a
   sphere** (Reto 2026-10-03). A diameter in, the canonical cage out:
   C60-Ih, C70-D5h, C80-Ih and the other named IPR isomers, via the
   Fowler–Manolopoulos spiral, with spirals taken from the Atlas. Hero
   generation stops hand-building balls. It is independent of Do-next 1
   and can be picked up between its review gates.

2. **`spec.md` §28.3 armchair lids** — the flat-lid family is done for
   zigzag `(6k,0)`; the armchair half is open. Delivers caps for
   armchair-rim parts, and the valve rotor is a lid pair, so the valve
   test piece waits on it.
3. **`spec.md` §28.3 `opening(port=)` + `junction(3)`** — solve a host hole
   from a target rim, then opening + fuse as a tee. Delivers the tilted
   pill (`geom.join.angle`) and the first branching topology. Also what a
   sheet-with-a-hole needs before a non-zigzag tube can protrude from it:
   the `hex(k)` ↔ `(6,0)` route already works (`examples/pillar.hx`,
   `{7:6}`, no collar), but an armchair or chiral end is a mixed rim and
   cannot enter a C6 hole without this. Reto 2026-10-01 asked for a ~3 nm
   fullerene ball with a hole fused onto a 1 nm tube: that is `junction(1)`
   or a Goldberg `fullerene(N)` minus a patch, and neither exists
   (`fullerene` is C60-only). se `hexa-nanobud-drum` is the stand-in — a
   2.9 nm washer-capped drum, flat-ended.
4. **`spec.md` §28.3 elbow + closure → genus-1 torus, then genus-N** — the
   first real `registry.closure` test; delivers the junction/tube algebra
   the periodic cell and schwarzite nets reuse.
5. **`spec.md` §28.3 box + valve test pieces** — the ~4 nm pillbox and the
   radius-changing shell with a two-lid rotor, each as separate blocks with
   a revolute joint. The acceptance artefacts for the whole discrete half;
   waits on 1.
   The box test piece (`spec.md` §28 step 3, `src/hexfold/spec.md`) has no
   backlog file; it is ranked here, and the file is filed when it starts.
6. **backlog/hexfold-t-handle-bearing.md** — the third test piece (Reto,
   2026-09-30), alongside the box and the valve.
7. **backlog/hexfold-seam-type-catalogue.md** — the seam-motif rows the
   catalogue's third row type exists for. R17 variant A exposes the private
   equal120 joiner through an exclusive scene feature. Replay after source
   review/integration: `{"op":"generate","generator":"hexfold_scene",
   "name":"y","params":{"sheet":[30,30],"k_tether":1.0,"features":[
   {"name":"y","type":"k3-sp2-120-z","dihedrals_deg":[120,120,120]}]}}`.
   Three open sheets, authored joint pinned, remaining sheets tethered;
   no three-plane surface receipt. Prod run/viewer belongs to orchestrator.
   Other rows retain their earlier measurement prerequisite. Generate-as-job
   transfers to a local agent; this fleet owner does not start it.
   Variant B (tube wall plus sheet, unequal 180/90/90 seam) still needs the
   seam-row measurement before it is buildable.
8. **`spec.md` §28.8 valve tool set** (checklist in
   backlog/precis-surface-kernel.md) — clearance field → pocket extractor
   → attachment-site enumerator → complementarity scorer → bond-energy
   audit → drag-vs-torque. Delivers the valve's design surface; its Q4
   clearance stub is gated on Do-next 9.
9. **rotary-ratchet-valve.md Q2** — scrubber cadence per poison species,
   decided by instrumenting the first lining, so it waits on 7.
10. **backlog/hexfold-sp3-seam.md + backlog/hexfold-sp3-isolation-band.md**
   (§28.7) — three- and four-sheet joins at an atom, and the valve's sp³
   isolation loops. Sequenced here by choice, not blocked: §28 lists it as
   unblocked since step 2 landed. `JOINERS` is already keyed on a lattice
   *pair* for it.
11. **backlog/hexfold-instrumentation-leg.md** — the instrumented first
    lining rotary-ratchet-valve.md Q2 (item 9) needs to decide scrubber
    cadence; sequenced right after the seam it instruments.
12. **`spec.md` §28.5–28.6 smooth solve + direction field** — owned by
    **backlog/precis-surface-kernel.md** (its checklist carries §28.5,
    §28.6 and §28.8); this thread ranks them, that item holds the
    spec. Discrete-mesh smooth solve, curvature bound, bent collar;
    delivers the tapered (collar-driven) shell the discrete washer step
    stands in for. This is the "solving" capability of Do-next 3's split:
    a smooth profile in (sheet → catenoid foot → tube → flare → drum →
    rounded lid), and distributed defects out. It emits authored-defect
    lists (gr459928). The smooth-drum slice there (Reto 2026-10-01) has
    its meridian (`precis_surface.revolution`, table radii from
    `hexfold.radii`), row fit (`precis_surface.rowfit`), viewer overlay
    and carbon wrapper (se generator `smooth_drum`) landed; prod has se
    `hexa-smooth-drum-v2`. Next after it:
    **backlog/smooth-drum-engineered-mode.md** (Reto 2026-10-02): an exact
    nanotube stalk with rotationally symmetric collars, beside today's
    organic loft. Do-next 1 (ideal surface first) is the first concrete
    slice of this item, with authored radii in place of catenoids and
    table fillets.
13. **backlog/global-structure-search-slices.md** — variable-composition
    (`add` ranges) and surrogate warm-start from a prior AGOX database;
    structure-kind search work homed here by Reto's pillar-2 ruling
    (2026-10-01). Waits on nothing; sequenced after the test pieces by
    choice.
14. **backlog/structure-kind-demotion.md** — `ready`; se is the origin of
    atoms (Reto, 2026-09-14), structure becomes an import filter with no
    direct agent access. Staged, independent of the §28 pieces; ranked
    here because hexfold builds are where the structure-kind gripes
    (Do-next 4) bite.
15. **backlog/hexfold-nanoreactor-tube.md** — T2 of the
    nanoreactor thread (Reto 2026-10-04): tube between two sheets,
    inner Y-junction attachment rails, H-inlet pores, site list. Waits on
    the k = 3 seams, the same blocker as the three-arc cross-section.
16. **td344088** — the se + hexfold paper. The thread's end state; it
    reports the above rather than waiting on all of it.

## Owned quests, kept dormant

Reto, 2026-10-03 (td460284): this thread owns three molecular-motor quests
because they feed the CNT channel and motor work. Keep them **dormant**; do
not tick, wake or re-scope them without his word. On prod they are tagged
`thread:hexfold-toolkit`, and `STATUS:dormant` is unchanged.

- **qu347482** (propagate a state along a chain of molecular units under
  optical control) — 130 logbook entries; the cyanide-bridged Fe chain
  series stops at the tetramer (max_disp growing with chain length).
- **qu347483** (gang many molecular motors so their strokes and forces add).
- **qu347484** (join molecular units to each other and to what they act on,
  so motion transmits).

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
  example behind gr458061 (below).
- **gr458061** — this thread's precondition, **met; removed from Do-next
  2026-10-01.** It needed both a truthful served-sha surface (gr457361,
  done) and an end to per-session stdio servers that could serve stale
  modules. The second was td458385, and it turns out to have been done on
  **2026-09-29 around 17:00** — every session's config has pointed at the
  shared `http://127.0.0.1:8765/mcp` since then, and on 10-01 the shared
  server became the only MCP container. So this file's claim that "this
  tree is still on stdio, its dogfood provisional" was wrong when written:
  I inferred the transport from the stale-process episode instead of
  reading the config. Dogfood results from 09-30 onward ran against the
  shared server and are not provisional on transport grounds. The
  question about it I had queued for Reto was moot.
- **gr456201** — ruled: regeneration is the remedy, no new write path.
  Closed on prod 2026-09-30 with that ruling as its resolution; it had
  been left open under this heading — the drift that verifying each label
  against the code, not the prose, is meant to stop.
- **gr456202** — not a bug; `rim_word` takes `abs(turn)` by design.
  Closed on prod 2026-09-30, same as above.
- **gr456203** — CLOSED 2026-10-01 after verifying, not on its label:
  `_leak_finding` names the breached measure(s) and prints each value
  against its own threshold, and
  `test_seam_leak_names_the_breached_measure_and_its_threshold` pins it.
- **gr456212** — CLOSED 2026-10-01, and the "auto-diagnosed as already
  fixed" label this file carried was half wrong. The sigma half was done
  (`seam.sigma`); the element half never landed — nothing in the join
  path compared the two rims' elements. Shipped `seam.element` (WARN,
  matching `seam.sigma`, because `JOINERS` is keyed on a lattice pair so
  heterojunctions can exist later; promoting it to ERROR is a product
  call). The load-bearing test is the negative: two carbon rims draw
  nothing, since every ordinary join passes through it.
- **gr454650** — CLOSED 2026-10-01, **and my 09-30 "confirmed, worse than
  filed" comment on it is retracted.** It was already fixed by
  `build._split_degenerate_tube_rims`, with four regression tests in
  `tests/hexfold/test_len1_rims.py` that all pass on main — I escalated
  it to Do-next 3 without ever checking for existing coverage. The
  "five cyclobutanes" reading was wrong too: SPEC puts
  `ring.size.unusual` *outside* `[4, 8]`, so a four-ring in a seam is
  inside the library's accepted band by design, and that test file pins
  `seam.rings {4:5, 6:5}` as the intended result (every face in `[4, 8]`,
  `sum == 10`, one per fused bond pair). `rings={}` for the bare `len=1`
  armchair tube is likewise consistent — every atom on a rim, no face
  closed, `chi=0 rims=2 residual 0` is what an open band reports.
  Whether hexfold's `[4, 8]` band *should* admit cyclobutanes in an sp²
  seam is a spec question needing a citation, not a defect; not filed as
  one. **The real residue** was a different bug and is fixed: see
  `gr454650`'s own comment and the stick-geometry entry below.
- **ring-less nets and the stick pass** — FIXED 2026-10-01 in the same
  round. `tube(5,5,len=1)` with geometry on raised `IndexError` from
  `stick.py`'s `springs[:, 0]`, because `_angle_springs` returns `[]` for
  a net with no rings and `np.array([])` is shape `(0,)` against a
  documented `(K,3)` contract. One-line `.reshape(-1, 3)`; tests in
  `tests/hexfold/test_len1_geometry_crash.py` assert the report comes
  back *and* that `geom.summary` is present, so a future "fix" that skips
  the geometry pass instead of running it fails the test. It surfaced as
  "hexfold internal error while compiling the spec" on a legal spec.
- **gr459057** — FIXED 2026-10-01. `generate` then `join` in one ops list
  can never work (the mint is deferred until the list validates) and the
  error now says so and names the remedy. Note for a later pass: the
  same-batch case is **not** detected precisely, because
  `finish_generate` sets `bound_kind` and `bound` together, so a block
  generated in the same list is indistinguishable from a bare one at join
  time; the message names the deferral as the likely cause and keeps a
  correct fallback clause. Threading the pending-generate set through
  would allow a precise, separate error.
- **gr459058, reporting half** — FIXED 2026-10-01: a retire now prints
  the structures it left live and the `delete(kind='structure', …)` call
  for each. One test pins the leak itself, so a later cascade fix has to
  update the message in the same change rather than quietly making it a
  lie. Reto ruled cascade on 2026-10-01; building it is Do-next 7.
- **gr454488** — CLOSED 2026-10-02. All eight residuals were fixed on
  2026-09-28 by 1a5475438 (in prod), with regression tests in
  `tests/test_se_hexfold_dogfood2.py`; this file had it ranked at 7 for
  four days without checking.
- **gr456213** — CLOSED 2026-10-01, and the closure is a correction of
  this file. It was fixed on 2026-09-29 by Reto's own ruling that a part
  may not belong to two composites: `prepare_join` refuses via
  `_addressed_part_redirect` as `join.part_addressed`, before either side
  is rebuilt, and names the owning composite's already-exposed port to
  use instead. Verified live on prod — a join addressing a recorded part
  was refused with exactly that message. The unconditional
  `node.parent` assignment the auto-diagnosis pointed at still exists,
  but every endpoint reaching it has cleared the gate, so its old parent
  is either `None` or an ordinary non-composite layout parent, reported
  as a `join.reparented` INFO.
  **Why this file had it at Do-next 2:** I took comment 1's auto-diagnosis
  at face value. That comment describes pre-fix code and was never re-run;
  comment 3 had reset the row to open for an unrelated reason (a false
  "branch pushed" claim, gr458326). So the ranking rested on a stale
  report — the same failure this thread's re-rank note is about, committed
  by the person who wrote the note. The method that caught it was running
  the scenario against prod instead of reading the diagnosis. **Follow-on:**
  ref 457890's corruption is therefore historical damage from the
  stale-process window, not evidence of a live write path, and current
  code cannot reproduce it. Also: the refuse-vs-reconcile question I was
  about to put to Reto was already answered by him on 09-29 — refuse.
- **gr454563** — REFUTED 2026-09-30 by the same round: the parser now
  raises `5:13: unknown parameter 'length' for tube — known: hand, len,
  m, n`, which is the remedy the gripe asked for. Closed. Whoever fixed
  it never linked it, which is why it sat ranked for two days.
- **`hx-sheet-tube-trial`** — not a defect. Reto read it in the viewer as
  a sheet plus a blob; its four-line spec fuses tube to cap and never
  joins the sheet to anything, so two loose components in one se block is
  a faithful render of what was asked for. What it *did* surface is
  gr458713, since shipped as `net.components`.

<!-- Re-rank note, per the README: the first version of this list ranked a
join-side corruption bug at 1 and a test fixture at 5. Both rested on
findings a 16-hour-stale MCP process had manufactured. The lesson that
changed the order is that an unverifiable execution environment outranks
the defects you think you found in it. The 2026-09-30 evening re-rank added
the second half of that lesson: three separate items here — the stolen
part, the two-component net, and (on a sibling thread, same day) a
basepair check with zero callers — were all cases where the code was
willing to report success without having asked the question. Prefer the
item that makes a wrong answer impossible over the one that makes a wrong
answer visible, and prefer both over re-measuring.

The 10-01 rounds added the symmetric lesson, learned the expensive way at
ranks 2 and 3 of this very list. gr456213 sat at 2 because I trusted an
auto-diagnosis of code that had since been fixed; gr454650 sat at 3
because I "confirmed" a defect against prod without checking whether a
test file already asserted the behaviour was intended — it did, and the
chemistry objection I built on top of it contradicted the spec's own
stated tolerance. Both were caught, but only after they had steered the
ranking and, in the first case, been relayed to another session as fact.
So: a gripe's status and its diagnosis are two different claims, and
neither is evidence about current code. Run the scenario, and read the
tests around it, before ranking anything — and when a dogfood "finds"
something in a mature area, the first hypothesis should be that it is
already known. Of nine items probed across the 10-01 rounds, three were
already fixed and one was intended behaviour. -->
