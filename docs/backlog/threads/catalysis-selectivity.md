---
roster: active
pillar: 3d-design
effort: high
design-review: yes
---
# catalysis-selectivity

## Resume

- **Next:** a thread session is being brought up (2026-10-26). catpath v0.41.0 (catpath main 8e4b119; local suite 1201 passed, 3 skipped) runs ledger → CHE electrochemical layer (v0.40 ShiftProvider; PCET count ledger-driven, gr515697; U_L sign follows reaction direction so oxidation is covered, gr516530) → `kin.solve` with NEB→BEP rated sources → steady state, TOF, X_RC, paired-draw ratio band, per-route U_L. Open work: (1) real-potential validation on spark, UMA backend only (MACE stopped on Reto's OK 2026-10-26, it starved spark's memory): 17.5 h in, on B3 (full-stack ammonia/Ru), test 3 of 7; C (co_oxidation) and A×3 (orr_pt111) to come, no ETA; B1 and B2 failed on both backends, cause unknown until pytest prints details at session end; zero validation passes so far. (2) gr517976 (test_draws) on catpath branch `fix/gr517976-draws-tests`: test 1 rewritten and passing (cross-checked against an NEB-only solve); test 2 scan shows rho=0.5 narrows the band (width ratio 0.82), no `draws.paired` bug; next multi-seed test 2, full suite, land, close.
- **Blocked by:** item 25 (no re-runs, incl. the NH₃ neb-tier re-run of pw455722's candidate): Reto, 2026-10-26, "lift 25 after validation" — lifts once the 7-test UMA validation on spark is green; holds until then. Item 23 (no Pd-hydride NEB pilot, Reto 2026-10-02) unchanged, and Reto 2026-10-29 keeps it held until SPSO slice 8 has run; its original lift condition was "the improved catpath": item-19 corrections (shipped in 0.24.0) plus the surface-Pourbaix work, whose optimizer build is unverified. td470248 (BEEF-vdW single points on the PdH geometries) is done. Pending: B1/B2 diagnosis, ordered by Reto 2026-10-26 and run separately off spark by the catpath coordinator.
- **Unblocks:** A ranking against a complete competing network.
- **Acceptance:** NO→NH₃ answered over a U/pH window on the complete network ([Status](#thread-context)); validation passes on real-potential cases; validation green lifts item 25; no re-run before. Engine-version checks must match the released engine.
- **Worktree:** `catalysis-selectivity`
- **Builds:** Unknown; use the owning item's slice estimate.
- **Detail:** [Ranked work](#do-next) · [Horizon](#horizon) · [Coordination map](INDEX.md).

## Thread context

Current declared activity: the `roster:` front matter above; dated allocation decisions below are historical.

**Status:** ends when a pathway answers "does the move toward the target
win at every fork, thermodynamically and kinetically, and over which
potential and pH window" for NO→NH₃, on a network that contains every
known route and competitor, and the explorer shows each step as one
chemical change with what entered and left. Today (Reto, 2026-10-02, from
pw455722) the screen runs a minimal network that cannot reach NH₃
associatively, the objective is span at one U, and the diagram draws
H-supply as a dashed sawtooth; order is network first (every number
downstream is measured against it), the diagram in parallel (it is how
Reto reads the results), then the objective, then where barriers run.
**Last reviewed:** 2026-10-02 (hold lifted). R1 like-with-like ranking
(per-measure network-basis stamp, stale competitor-margin demotion, scoped
restale pass, status-view marker) and the diagram fold/annotation web half
landed after design review §14 + fixes F1–F3 (R1 briefly backed out, then
re-landed on Reto's ruling `catalysis-selectivity-15`: a correctness fix,
not a criterion change). Margins compare only on equal engine version too
(Reto, `catalysis-selectivity-24`; the correction-set id joins once catpath
records one). On hold (Reto, items 25 and 23, 2026-10-02): the legacy
backfill, the qu164903 promotion pause, every re-run and the Pd-hydride
NEB pilot. All of it waits for the improved catpath (the item-19
corrections plus the surface-Pourbaix work). The backfill is re-filed
when that is close, with its dry run redone against it. The last dry run
(design note §21) would have labelled 196 of 215 selectivity margins
older-network, changing no ranking: margins are not qu164903 objectives.
The pause would only have stopped the re-runs, so it is no longer needed.
The PBE H-flight check (pair plus undoped control, run on spark) agrees
with MACE: next to Ta the subsurface H goes downhill to the surface at the
midpoint and the end. The Ta effect at the end is −0.29 eV in both methods
(design note §22f). Reto accepted it (`catalysis-selectivity-27`): the
emptied-site rule stands for when the pilot resumes, and every hold stays.
A spin-polarised point at the Ta end keeps no moment and matches the
spin-paired energy (§22i), so the table stands.
**Settled history (folded 2026-10-26; the Resume block above is current,
the dated detail is in `git log` and the owning items):**

- **Engine and pin.** Catpath 0.24.0 added NO+NO coupling, NH₂OH scission,
  template-level `network_digest` and the item-19 gas (NH₃, NO, N₂O,
  NH₂OH) and Pd H* +0.25 eV corrections, ON by default and recorded under
  `results.corrections`; precis has no correction code of its own, so
  nothing is applied twice. The pin has moved on since (see Resume); the
  rule stands that margins compare only on equal engine version
  (`frontier.same_network_basis`). Open: fold `results.corrections` into
  that basis as item 24's "correction-set id" —
  `backlog/pathway-correction-basis.md` (spec plus strict-xfail tests).
- **Restale.** `frontier.apply_network_demotion` takes the pinned basis
  (`compute._pinned_network_basis`: pin/env version plus the quest's
  resolved neb template); any stamped key on an older engine demotes to
  provisional, and the restale pass redispatches it at its own tier,
  cap-bounded and deduped by the in-flight pathway check (gr510764,
  `gf-questbasis`). That redispatch is a re-run: it is under item 25's
  hold until validation is green.
- **Kinetics guard (gr502251, `e0e59d776`).** The aggregate's
  microkinetics stage caps Monte Carlo samples to 60% of its wall budget
  and refuses below 100 samples; the 900-second child cap is unchanged.
- **Pd-hydride.** The stage-3 precis fixes landed (`c9c52c95b`:
  `struct_relax` keeps the relaxed lattice, explicit `fmax`; H-loaded-slab
  preflight is test-pinned). Stages 1–2 and the top-10 re-run are unbuilt;
  the NEB pilot stays held (item 23). The BEEF-vdW points (td470248) are
  done.
- **Surface-Pourbaix (SPSO).** Wave A (catpath 0.25.0 coverage scan and
  `che_affine` export; slices 2, 4, 5), wave B (anchors rung, staircase
  panel, window/Shapley/HER objectives) and wave C slice 7 are built; the
  item `backlog/surface-pourbaix-staircase-optimizer.md` owns the status.
  The paper-date collision is its §Rulings 2026-10-13 and `td462597`.
- **Operating point.** qu164903 (NO→NH₃ selectivity): −0.3 V_RHE, window
  −0.2 to −0.4 V, pH 7 (logbook decision entry 626; both source SIs state
  V vs RHE).
- **Live, not yet seen working.** The diagram arrows (item 22): pw455722
  drawn from prod shows 12 angled H⁺+e⁻ arrows and no dashed overlay, but
  Reto has not looked at it in the browser. The pourbaix_bulk note clamp
  and the pymatgen#4709 citation (b41b70d03) wait for a recorded
  Materials Project job ("MP key live: <call> → <result>" is still
  unrecorded; `PRECIS_MP_API_KEY` is in the overlay).
- **Waits on others.** 13 of the 15 papers the CNT item waits on (Parked).

**Worktree:** `catalysis-selectivity`
**Allocation decision (historical):** yes — Reto, 2026-10-02 ("high up").

## Do next

1. **NH₃ network completeness — neb-tier re-run** (spec closed 2026-10-21;
   the engine work shipped). The screen measured NH₃ selectivity against
   the wrong competitors until N–O cleavage after hydrogenation, N₂ and
   N₂O were in the network; screening stays parked by decision (R2), and
   the like-with-like network-basis ranking has shipped. Catpath 0.24.0 (NO+NO coupling, NH₂OH scission,
   template-level `network_digest`, gas + Pd H* corrections ON) is
   locked (2026-10-07). Left: the neb-tier re-run of pw455722's candidate
   and the trap/poison re-check in qu164903's logbook. Held by item 25,
   which lifts when catpath's 7-test real-potential validation (UMA, on
   spark) is green (Reto, 2026-10-26: "lift 25 after validation"). Prepare
   the plan now; launch nothing until the validation is green. Bound
   (Reto, 2026-10-29): owned GPU lanes count as $0 cash for the $25 rule;
   the stated bound is wall time, ≤1.5 GPU-h per seed (5400 s cap).
   The hold was prose only. The restale pass (gr510764, on prod since
   2026-10-24) can re-run stale stamps under it, so Reto ruled
   2026-10-29 to pause it on qu164903 (`fidelity_restale_cap=0`) until
   the lift. Not yet set: that key is outside the quest edit allowlist
   (gr537835), so it is a direct `refs.meta` update Reto runs; whether
   the tier ladder is on cannot be read over the MCP (gr537834). Seed
   jobs on spark were running for this quest on 2026-10-29 and cannot be
   attributed to restale. At the lift, restore the cap (default 2) in the
   same step as the first re-run.
2. **backlog/pd-hydride-substrate.md** — Reto, 2026-10-02: under cathodic
   operation Pd is β-PdH, and qu164903's 232 candidates all ran on bare
   Pd(111). Stages 0–2 done. The pilot pair is built
   (`scratch/pdh-pilot/`), and its NEB runs are held until the
   surface-Pourbaix optimizer (Horizon 3) is built, because the bare-surface
   construction may change once the resting state at the operating point is
   known (Reto, item 23). The PBE check of the H flight from Ta agrees with
   MACE, so the emptied-site rule stands (§22f; Reto accepted, item 27). Precis-side fixes (struct_relax
   lattice write-back, preflight on H-loaded slabs) can start.
3. **backlog/catpath-desorption-link-kind.md** (moved here from chemistry
   Horizon 2) — independent of 1, parallel. The step-annotations spec
   closed 2026-10-21: the web half (fold + label inference) shipped, and
   catpath `ledger.py::link_fields` now emits `added`/`removed`/`link_type`
   (precis pins 0.32.0), so re-check this item against that before building. Reto's look at pw455722
   (`catalysis-selectivity-22`) asked for two changes; both are now done:
   - the grey dashed line was the tier-ladder ghost overlay, which a neb
     sibling triggered on a screening page; it is removed;
   - the +H⁺+e⁻ feed is now a short arrow angled in from the upper right,
     landing on the level it feeds, with one grey label per shared column.

4. **src/precis/workers/job_types/pourbaix_bulk.py** →
   **backlog/pourbaix-quest-gate.md** — Reto asked for it next after the
   network (2026-10-02); independent of 1–3. A candidate whose bulk
   dissolves across the operating window cannot be the catalyst, so it is
   ruled out before selectivity is spent on it (qu202468 (Convert NO to
   fertilizer nitrogen with non-palladium solid-state catalysts)). The
   job (A) is on main (e167f4859, landed by the orchestrator because it
   changes deploy roles); its note clamp and the pymatgen#4709 citation
   followed in round 3.
   - **Key:** `PRECIS_MP_API_KEY` is in the overlay. Run one prod job
     through the Materials Project path and record it here as "MP key
     live: <call> → <result>".
   - The gate (B): its first slice (operating conditions, dispatch,
     harvest/stamp; no rule-out yet) and the R15 contribs fix are on main
     since 2026-10-07 (folded in from the Codex branch). The one
     production proof job runs after a deploy carries them; job 468292
     (died on the contribs client) stays as the before-record. Rule-out
     and lift landed 2026-10-21 (`ruled-out:pourbaix-dissolved` on a
     windowed dissolved-everywhere host verdict, lifted on a clean
     re-verdict); the leached proposal, leaderboard flags and tick prose
     are the next slice. qu202468's
     operating point is set: −0.2 V, pH 7, window −0.4…0 V, pH 7–10.
5. **backlog/pathway-selectivity-u-ph-window.md** — the objective Reto
   named; blocked by 1 and 2 because a window over an incomplete network
   or the wrong substrate is the wrong window. Post-processing only, so cheap once 1 lands. Also
   owns a screening criterion that ranks: R2 (2026-10-02) measured that
   thermo-only margins do not.
6. **backlog/pathway-kinetics-promotion-gate.md** — where barriers are
   spent; needs 5's `U_sel` to pick the decisive edges.
7. **backlog/crowded-surface-experiment.md** — Reto's
   crowded-surface ask; ranked last, after the catpath 0.30.0 re-run
   (spectator mode, NH2/NOH template fix), because the dilute-vs-1 ML table
   it starts from is uncertain until then.

## Horizon

1. **backlog/neb-barriers-in-the-catpath-pipeline.md** — owned by
   chemistry (its Horizon 1); Do-next 6 decides where NEB runs, this
   item makes NEB run at all. Kinetic margins wait on it.
2. **backlog/pathway-conditions-effects-report.md** — chemistry's report
   for qu164903; consumes the window scalars from Do-next 5 instead of
   U_L / U_opt alone.
3. **backlog/surface-pourbaix-staircase-optimizer.md** (Reto, 2026-10-02
   file only; lifted 2026-10-07, build plan approved 2026-10-13,
   slices 1–7 built; slice 8, Fe-doped Pd end to end, has a contract that
   passed design review on 2026-10-30 and awaits a second vet) extends the Pourbaix work
   (Do-next 4, Parts A and B) into a surface validity map, a linked
   reaction staircase, and a Shapley-attribution field that tells the
   slab search where and why a candidate loses its window.
   - Ranked last because it needs Parts A and B landed, the corrected
     references (catalysis-selectivity-19), and Do-next 5's window
     objective, which it generalises.
   - The proof-of-concept paper (Fe-doped Pd) belongs to
     qu459585 (Open science that ships: papers written from the
     graph), not this thread.
4. **backlog/encapsulated-metal-candidate-space.md** (Reto, 2026-10-02:
   **file only**) supplies the optimizer's candidate space: carbon-encapsulated
   metal families F1–F4, a carbon/cap Pourbaix layer, and
   host/containment constraints in the Shapley mask.
   - It is blocked by Horizon 3's first slices.
   - Its NO→NH₃ demo feeds qu164903 directly: F3/F4 sites become
     candidates on the same reaction and references.
   - Its F4 single-atom crater is the first slice.
   - It reuses `precis_surface` + `hexfold.smooth` for the cage
     generator, and leaves hexfold-toolkit's and nanobuds-paper's items
     alone. F2 is flagged as touching the nanobuds paper.
5. **backlog/electrochem-kinetics-beyond-che.md** (Reto, 2026-10-16:
   pile, needs design) — β·U and surface-charge terms in catpath kinetics,
   ported from the CatMAP/CatmapInterface.jl stack, plus a CatMAP parity
   export. Ranked last: Do-next 5–6 define the window and the decisive
   edges it would make U-dependent, and Horizon 1 supplies the barriers.
5a. **backlog/kinetics-engine-v2.md** (revised 2026-10-19 per Reto's
   review; open: Q4 `adsorbate_mode`, Q6 ladder vs rung, Q9 σ-shift,
   and the Zacros academic licence Reto submits) — the kinetic core
   Horizon 5 shifts: split input/output partial-pressure environment
   block with p_total and a coverage/selectivity objective, one
   free-energy ledger with detailed balance by construction, BEP as a
   rated source with staged correlated uncertainty, site-balance MKM,
   rejection-free KMC (no diffusion in v1) with MCAMC as the primary
   accelerator, TPD validation and `tpd_corrected` offsets, a
   validation manifest with re-validate. Ranked with Horizon 5 because
   it owns the environment block and the ΔG/TS provider seam that
   Horizon 5 and the electrochemical-interface spec plug into; slices
   1–4 and 10 need no KMC and no U. Slice 1 (environment block, refuse,
   Q/K, reversibility, basis fold) shipped on catpath v0.32.0; slice 2
   (VacuumProvider + rate-builder seam, core checks, `kinetics_v2`
   record, `adsorbate_mode=hindered` + derived corrections stamp) is on
   catpath v0.33.0 (1398d07, one commit — squashed per review; engine
   committed and tagged locally, NOT pushed pending the engine review).
   Review round 2 fixed: the equilibrium-flux check now runs in log
   space (pw455722's boundary equilibrium at ~1e29 bar is where it
   earns its keep, and a check that cannot run is reported, never
   stamped passed); barrier_identity verifies the rate-path-stored
   energies against the rate-implied barriers, so a clipped reverse
   barrier fails naming the step; the v2 record is a results_json
   SIBLING of the v1 block (byte-compatible). Precis glue lifts the
   sibling `kinetics_v2` key onto the pathway meta at review-ready.
   The slice-9 literature dataset (D11 TPD/adsorption reference) is
   landed at `tests/fixtures/kinetics/` (`tpd_reference.json` +
   provenance snapshot + README). Slice 5 (lattice KMC core behind the
   pluggable `KmcBackend` seam, `zacros` writer/reader, `kmc_points` in
   the sweep with `correlation_effect`) is catpath v0.36.0; slice 6
   (per-site MCAMC `bkl`/`mcamc_s2`/`mcamc_bfs` behind the pluggable
   accelerator seam, channel-scaling fallback, `kmc.compare`; the
   `Acceleration` record gates KMC trust in `_kinetics_scalars`) is
   catpath v0.37.0; slice 10 (validation manifest, `autocatpath_revalidate`,
   `view='manifest'`) is catpath v0.38.0; slice 9 (TPD validation,
   `tpd_corrected` offsets, `view='tpd'`, the TPD revalidation leg) is
   catpath v0.39.0. Its CO/Pd(111) beta = 2 and 5 K/s rows are derived
   (provisional, Reto to confirm — spec §Decisions); pa186444's
   heating-rate series is plotted only, so it cannot replace them.
   Slices 7 (waits on a ruling) and 8 remain.
5b. **backlog/catpath-repro-bundle.md** (Reto, 2026-10-19: draft) — one
   Zenodo-publishable zip per finished compound (seeds, lock, weights
   hashes, outputs, offline viewer, `verify.py`). Slices 1-2 need nothing
   else; slice 3's tolerance contract is `autocatpath.manifest` (catpath
   v0.38.0: `ValidationManifest.tolerances`, `compare`). Ranked after 5a
   because it consumes the validation manifest.
6. **backlog/electrochem-mlip-ladder.md** (filed 2026-10-19 from Ringe's
   talk, draft) — the σ side of Horizon 5: `a_i = ∂ΔE_i/∂σ` from three
   GC-DFT points (tier 0), a σ-conditioned fine-tuned MACE (tier 1),
   explicit double-layer MD (tier 2), one `Ω(U, pH, cation)` contract for
   states and TS. Ranked with Horizon 5 because it supplies the energies
   that item and the KINETICS spec consume; its slices 1–2 are pure
   modules shippable behind `response: off`.
7. **backlog/quest-batch-coverage-scan.md** — a quest-tier op to
   scan a named list of candidates × models; convenience over the documented
   per-candidate put recipe, so it ranks below the experiments that use it.

## Parked

- **Gas references under MACE-MP-0 misprice the N molecules.** Measured
  2026-10-02 (design note §17):
  - NO→NH₃ U_eq shifted +0.13 V;
  - NH₃/NH₂OH overstabilised and N₂ understabilised against experiment
    (not one ½H₂ error);
  - H on Pd 0.20–0.28 eV too stable against ½H₂ vs PBE. The error varies
    by metal (Cu −0.22, Pt +0.10), so a μ_H shift is a Pd-only correction.
    That H* error biases `selectivity_margin` on the 112 of 213 candidates
    whose worst branch point is a supply edge (A1).

  **Decided, Reto 2026-10-02 (`catalysis-selectivity-19`): both levers.**
  - Per-molecule gas corrections fitted to experiment, keyed by (backend,
    model): NH₃ +0.794, NO +0.150, N₂O +0.549, NH₂OH +0.999 eV for
    mace/medium, with the reservoirs as anchors.
  - A Pd-only H* shift (+0.25 eV, PBE anchor), keyed by (backend, model,
    host metal).
  - Both are recorded in every result.

  Briefs delivered to the catpath session (window 21) on 2026-10-02:
  - `scratch/catsel-catpath-brief-corrections.md`;
  - the NH₂OH `thermo.GASES` fix (`scratch/catsel-catpath-brief-href.md`),
    delivered by the review session.

  Release is out (0.24.0 has both on by default): an offline re-score of qu164903 in precis. It must
  reproduce the 299 validated replay rows with both levers off before it
  writes. qu164903's tier promotion is paused until then (Reto runs the
  meta write; the undo is in design note §19).

- **backlog/cnt-channel-staged-catalysis.md** (dormant, Reto 2026-10-03):
  a carbon channel whose arc-and-rib interior holds each intermediate of a
  staged reaction in one pose. Blocked on hexfold building a three-arc
  cross-section with ribs; no quest until then. 18 papers listed. Two
  read passes (8 fetched papers, 2026-10-03) minted 20 hubs and stubbed 5
  primaries the reviews cite. Both are listed in the item. The next pass
  waits on 13 unfetched papers (KcsA, CPS and 3 of the 5 stubs). Hilario
  2016 and Anderson 1991 are held as of 2026-10-04. Its concrete NO→NH₃
  instance and design toolchain moved to the nanoreactor thread
  (2026-10-04).

- **Three dormant quests owned by this thread** (Reto 2026-10-03, td460284
  for the two NO arms; qu207188 added the same day via review session).
  All three are tagged `thread:catalysis-selectivity` and carry a logbook note;
  keep them `STATUS:dormant`.
  - qu202468 (Convert NO to fertilizer nitrogen with non-palladium
    solid-state catalysts; Cu-foam electrochemical arm first).
  - qu202469 (Convert NO to fertilizer nitrogen via biocatalysis;
    literature-tracking arm, no compute lane).
  - qu207188 (Fertilizer from air and sunlight alone: a cheap catalyst of
    common elements making ammonia).

  Open decision, not yet taken: whether any of them folds into the NO→NH₃ work
  of qu164903 (NO→NH₃ selectivity).

## No action needed

- (none yet)

## Seam

- **chemistry** owns the catpath engine's health and output contract;
  this thread changes the network content and adds link fields. Engine
  edits here bump the catpath version through chemistry's
  `catpath-wheel-version-reuse.md` path, never a second wheel.
- **plugin-split** owns `pathway-presentation-shared-module.md`; if it
  lands before Do-next 3, the diagram change goes into it.
- `pathway-viewer-ux-batch.md` item 3 (where H₂O leaves) is subsumed by
  Do-next 3; the rest of that item stays with chemistry.
- **roadmap-quest** owns promotion dispatch; Do-next 6 changes the
  promotion rule; re-dispatch and infra retries now keep each candidate's
  own rung (`quest/compute.py::_redispatch_tier`, `_retry_tier`).
