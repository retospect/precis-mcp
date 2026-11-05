---
roster: active
pillar: 3d-design
effort: high
design-review: yes
---
# catalysis-selectivity

## Resume

- **Next:** a thread session is being brought up (2026-10-26). catpath v0.41.0 (catpath main 8e4b119; local suite 1201 passed, 3 skipped) runs ledger → CHE electrochemical layer → `kin.solve` with NEB→BEP rated sources → steady state, TOF, X_RC, paired-draw ratio band, per-route U_L. Open work: (1) real-potential validation on spark, UMA backend only: on B3 (full-stack ammonia/Ru), test 3 of 7; C and A×3 to come, no ETA; B1 and B2 failed on both backends, cause unknown; zero validation passes so far. (2) gr517976 (test_draws) on catpath branch `fix/gr517976-draws-tests`: test 1 passing; next multi-seed test 2, full suite, land, close. (3) SPSO slice 8: Part A landed (6a81453eb); Part B stage 1 scans queued, detail under Settled history.
- **Blocked by:** item 25 (no re-runs, incl. the NH₃ neb-tier re-run of pw455722's candidate): Reto, 2026-10-26, "lift 25 after validation" — lifts once the 7-test UMA validation on spark is green; holds until then. Two scoped exceptions for SPSO slice 8 (Reto 2026-10-30 and 2026-10-31; scope and ids under Settled history). The restale pause (`fidelity_restale_cap` 0 on qu164903) stays. Item 23 (no Pd-hydride NEB pilot, Reto 2026-10-02) stays held until SPSO slice 8 has run (Reto 2026-10-29). td470248 is done. Pending: B1/B2 diagnosis, run separately off spark by the catpath coordinator. Open (2026-11-05): the global lift versus a scoped verify-tier exception for st549459 (Horizon 10), Reto's call.
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

- **Item 25 lifts for SPSO slice 8 (Reto, 2026-10-30 and 2026-10-31).**
  First, one run: subject A's pathway (st202238, neb tier, seed 0;
  pw541657, minted 2026-10-30). Second, via the coordinator: the stage-1
  `surface_coverage_scan` jobs on baseline st203306 (jo543903), subject A
  st202238 (jo543904) and subject B st463208 (jo543910), pinned to castor
  (the first mints, jo543898 and jo543899, failed in 0.1 s on a node without
  autocatpath, gr543909). Stage-0 pathways: baseline pw538937 (minted
  2026-10-30, origin accepted as-is), A pw541657, B pw517889. The overnight
  pathways pw542513, pw542519 and pw542537 stay out of the study until
  they are explained. Scope: those runs only; no other re-run. Item 23's
  original lift condition was "the improved catpath": the item-19 corrections
  (0.24.0) plus the surface-Pourbaix work.
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
  cap-bounded and deduped by the in-flight pathway check
  (`gf-questbasis`). That redispatch is a re-run: it is under item 25's
  hold until validation is green.
- **Kinetics guard (`e0e59d776`).** The aggregate's
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
  The paper-date collision is ruled (Reto, 2026-10-30): November is
  `td459589` (draft:validation-paper, "Reproducing published catalysis
  results with autocatpath", cases A–D); the SPSO paper (`td462597`) has no
  month yet.
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
   The hold was prose only. The restale pass (on prod since
   2026-10-24) can re-run stale stamps under it, so Reto ruled
   2026-10-29 to pause it on qu164903 (`fidelity_restale_cap=0`) until
   the lift. Set: cap 0 read back on 2026-11-03 (`view='meta'`); the key
   is in the quest edit allowlist, so the lift is
   `edit(kind='quest', id=164903, meta={'fidelity_restale_cap': 2})`.
   The ladder is on (`fidelity_levels: true`, `view='raw'`), so cap 0
   holds every automatic re-mint: restale (2 per tick), promote
   screening→neb (2), neb→verify (3) and the repair re-dispatches. Seed
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
8. **gr543776 items 3–13 — keep-everything retention, remaining slices.**
   Items 1–2 landed (745e225c5, bddd19fd2): NEB band geometries,
   `images_energy` profiles and TS geometries ride the job artifact store
   (`precis.store.job_artifacts`, NAS mount — the ruled storage backend,
   Reto 2026-11-01 reconfirmed 2026-11-02, never `refs.meta`), and a
   killed/failed seed's finished relaxes are harvested from its scratch
   `checkpoints/`. Remaining drops, in the gripe's order (sizes for ~40
   atoms, 3 seeds, 20 steps, 30 states):
   - (3) Step retry: `narrow_partial` keeps the retried step and discards
     the rest of the full-network run, ~100 kB.
   - (4) Hidden intermediate (`_split_and_stamp`): `mres.atoms`, the newly
     found minimum, and both segment bands.
   - (5) Discarded NEB attempts: multistart, denser-band retries,
     auto-retry bands.
   - (6) Reseat (`_relax_state_bound`, `_adsorption_barrier`): desorbed and
     failed geometries and the adsorption band; heights survive only in
     warning text.
   - (7) KMC `sweep_points`: replica TOFs, `block_tof`, `channel_counts`,
     `coverage_ci`, ~10 kB per point.
   - (8) Relax trajectories, ~5 MB.
   - (9) Sweep timeout: only the error string survives; finished points
     lost.
   - (10) TPD: T/rate curves, ~5 kB per adsorbate.
   - (11) MC draws: 500 log-TOFs — cheap to redo, rank last.
   - (12) `poison:` geometries always dropped (`runner.run_seed_partial`),
     ~4 kB each.
   - (13) `model_index>0` units keep no structures, ~100 kB per seed,
     including the geometry vib ran on.
   Solver warnings are also discarded at `kmc.py` and `mkm.run_sweep`
   (`warnings=[]`); vib modes and the Hessian are covered by gr543723
   (closed).

## Horizon

1. **backlog/neb-barriers-in-the-catpath-pipeline.md** — owned by
   chemistry (its Horizon 1); Do-next 6 decides where NEB runs, this
   item makes NEB run at all. Kinetic margins wait on it.
2. **backlog/pathway-conditions-effects-report.md** — chemistry's report
   for qu164903; consumes the window scalars from Do-next 5 instead of
   U_L / U_opt alone.
3. **backlog/surface-pourbaix-staircase-optimizer.md** (Reto, 2026-10-02
   file only; lifted 2026-10-07, build plan approved 2026-10-13,
   slices 1–7 built; slice 8, Fe-doped Pd end to end, has a contract (v4) that
   passed design review on 2026-10-30 and the second vet's fixes, split into
   Part A, library code, built and landed 2026-10-30, and Part B, the prod
   run) extends the Pourbaix work
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
5a. **backlog/kinetics-engine-v2.md** — transferred to graph memory on
   2026-11-05 (Reto's ruling me568168): the spec is the root me568528
   with its section, ruling and gotcha nodes; open work is the plan todo
   td568706 under the kinetics programme root td568263; the file is a
   stub. State, read from the graph rather than restated here: engine
   slices 1–6 and 8–10 are built (catpath v0.32.0 to v0.40.0); slice 7
   (lateral interactions and diffusion) is the open slice, ordered after
   the first licensed Zacros run on validation case C (ruling me568384);
   Q4, Q6 and Q9 were ruled 2026-10-21; the Zacros academic licence key
   is in hand (me568261); the derived CO/Pd(111) TPD rows were kept by
   Reto on 2026-10-24. Holds 25 and 23 are the ruling nodes me568380 and
   me568381; the recompute policy is me568382; the transfer campaign node
   is me568399.
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
8. **backlog/spin-polarised-fe-dft-check.md** (Reto 2026-10-29, review item
   -30; filed 2026-10-30) — one spin-polarised DFT point on an Fe anchor of
   slice 8's subject A; the slice-8 table carries "spin check: pending" until
   it lands. Needs a single-point mode, a `dft` params pass-through and
   per-species initial magmoms in the GPAW path, and a measured wall time
   before any run.
9. **backlog/pathway-explorer-controls.md** (Reto 2026-11-03, from
   pw549461; draft, prio high) — the explorer's two control boxes
   (environment on the Pourbaix slider bank, visual filters), per-step
   kinetics on the diagram, BEP humps, one environment record, the compute
   cart, the left box as a tab strip (Pourbaix, kinetic map, rung lines,
   sensitivity, per-slab Pareto). Ten slice gripes (cluster
   `pathway-explorer-controls`, gr559345–gr559353 and the two filed
   later that day) plus gr559354 (catpath transient θ(t)) and gr559355.
   Reto's six decisions are recorded in the file (2026-11-03). Ranked
   here, not in Do next: slices 1–4 are stored-data display Reto reads
   results through, and nothing in them touches the engine. Slice 6
   (gr559351, the environment box) is in build on worktree `envbox`
   (td567073) as of 2026-11-05.
10. **backlog/slab-environment-optimizer.md** (Reto 2026-11-03; draft,
   prio high) — the inner loop of Horizon 3 widened to the whole
   environment and run by the quest per slab: three goals as Pareto
   objectives with masks, a derivative table per axis, a three-layer
   search (closed-form U/pH, stencil steps in T/p, multi-objective
   sampler over the box), `env_best`/`env_pareto`/`env_next` stamped on
   the candidate, `env_next` minted through the compute cart under the
   quest's caps. Kinetics gain a U axis only with kinetics v2 slice 8.
   First map on the Ge slab (jo560022, 2026-11-03, spark, 60 s): st549459
   rests as NO+H* at 0.78 ML at −0.30 V_RHE, H* at 1.00 ML below −0.533 V,
   one model, so the Ge barely moves the clean-Pd map. pw549461's
   clean-slab staircase now reads "coads mismatch": the mask M(e) is 0 for
   that pathway at the operating point. The run that would fix it is a
   verify-tier re-run on st549459 (co-adsorbed template), a pathway mint
   under item 25. Reto has not ruled between the global lift and a scoped
   exception for that run (2026-11-05).

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
