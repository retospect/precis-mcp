# catalysis-selectivity

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
spin-paired energy (§22i), so the table stands. Catpath
0.23.0 (Part B) runs in Reto's
`catpath` tmux window, and he sends the release sha for
`uv lock -P autocatpath`. Pourbaix job A: rulings recorded, build next.
**Round 2 dogfood (deployed 63301c5c, 2026-10-03):**
- Diagram (item 22): pw455722, rendered from prod with the deployed code
  and drawn by the test harness, has 12 angled H⁺+e⁻ arrows, each on its
  hydrogenation product, and no ghost or dashed overlay. Reto has not yet
  looked at it in the browser.
- Engine-version guard (item 24): `precis quest status 164903` runs
  clean and marks no margin older-network. That is expected: all three
  prod network stamps are catpath 0.22.0. The guard has nothing to act on
  until a 0.23.0 margin lands; check it then.
**Round 3 (deployed 929107f3, 2026-10-03):** the pourbaix_bulk note
clamp and the pymatgen#4709 citation (b41b70d03) are live. They have not
been dogfooded: the only prod path through them is a live pourbaix_bulk
job, and that waits on Reto's MP key. The first MP-key job checks them.
The rest of round 3 here was docs: the CNT read passes and the item-17
ruling.
**Round 4 (deployed 727728cc, 2026-10-04):** this thread landed only a
test fix (81dff3684, no prod surface) and docs. On prod, the SI
auto-ingestion shipped in this round brought in the SIs of pa5303
(pa465134) and pa166889 (pa465698). Both state V vs RHE, so item 17 is
resolved. qu164903's operating point (−0.3 V_RHE, window −0.2 to −0.4 V,
pH 7) is now in its logbook as decision entry 626. That changes no
compute; the holds stay.

**Worktree:** `catalysis-selectivity`
**Active:** yes — Reto, 2026-10-02 ("high up").

## Do next

1. **backlog/pathway-nh3-network-completeness.md** — the screen measures
   NH₃ selectivity against the wrong competitors until N–O cleavage after
   hydrogenation, N₂ and N₂O are in the network; every ranking on
   qu164903 inherits the error. Slice 1 done (parked is incomplete);
   screening stays parked (R2), and the like-with-like network-basis
   ranking has shipped. Left: catpath 0.23.0 (NO+NO coupling, NH₂OH
   scission, template-level `network_digest`) → `uv lock -P autocatpath`
   → the neb-tier re-run of pw455722's candidate (steps 5–6).
2. **backlog/pd-hydride-substrate.md** — Reto, 2026-10-02: under cathodic
   operation Pd is β-PdH, and qu164903's 232 candidates all ran on bare
   Pd(111). Stages 0–2 done. The pilot pair is built
   (`scratch/pdh-pilot/`), and its NEB runs are held until the
   surface-Pourbaix optimizer (Horizon 3) is built, because the bare-surface
   construction may change once the resting state at the operating point is
   known (Reto, item 23). The PBE check of the H flight from Ta agrees with
   MACE, so the emptied-site rule stands (§22f; Reto accepted, item 27). Precis-side fixes (struct_relax
   lattice write-back, preflight on H-loaded slabs) can start.
3. **backlog/pathway-diagram-step-annotations.md** +
   **backlog/catpath-desorption-link-kind.md** (its first slice, moved
   here from chemistry Horizon 2) — independent of 1, parallel. The web
   half (fold + label inference) has shipped. Reto's look at pw455722
   (`catalysis-selectivity-22`) asked for two changes; both are now done:
   - the grey dashed line was the tier-ladder ghost overlay, which a neb
     sibling triggered on a screening page; it is removed;
   - the +H⁺+e⁻ feed is now a short arrow angled in from the upper right,
     landing on the level it feeds, with one grey label per shared column.

   Left: the catpath `added`/`removed`/`kind` link fields.
4. **src/precis/workers/job_types/pourbaix_bulk.py** →
   **backlog/pourbaix-quest-gate.md** — Reto asked for it next after the
   network (2026-10-02); independent of 1–3. A candidate whose bulk
   dissolves across the operating window cannot be the catalyst, so it is
   ruled out before selectivity is spent on it (qu202468 (Convert NO to
   fertilizer nitrogen with non-palladium solid-state catalysts)). The
   job (A) is on main (e167f4859, landed by the orchestrator because it
   changes deploy roles); its note clamp and the pymatgen#4709 citation
   followed in round 3.
   - **Waits on Reto: key.** Reto adds `PRECIS_MP_API_KEY` to the secret
     store himself (release-2-2); do not add or ask for it. After he says
     it is set, run one prod job through the Materials Project path and
     record it here as "MP key live: <call> → <result>".
   - The gate (B) waits on the same key. qu202468's operating point is
     set: −0.2 V, pH 7, window −0.4…0 V, pH 7–10.
5. **backlog/pathway-selectivity-u-ph-window.md** — the objective Reto
   named; blocked by 1 and 2 because a window over an incomplete network
   or the wrong substrate is the wrong window. Post-processing only, so cheap once 1 lands. Also
   owns a screening criterion that ranks: R2 (2026-10-02) measured that
   thermo-only margins do not.
6. **backlog/pathway-kinetics-promotion-gate.md** — where barriers are
   spent; needs 5's `U_sel` to pick the decisive edges.

## Horizon

1. **backlog/neb-barriers-in-the-catpath-pipeline.md** — owned by
   chemistry (its Horizon 1); Do-next 6 decides where NEB runs, this
   item makes NEB run at all. Kinetic margins wait on it.
2. **backlog/pathway-conditions-effects-report.md** — chemistry's report
   for qu164903; consumes the window scalars from Do-next 5 instead of
   U_L / U_opt alone.
3. **backlog/surface-pourbaix-staircase-optimizer.md** (Reto,
   2026-10-02: **file only, do not build**) extends the Pourbaix work
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

  After that release: an offline re-score of qu164903 in precis. It must
  reproduce the 299 validated replay rows with both levers off before it
  writes. qu164903's tier promotion is paused until then (Reto runs the
  meta write; the undo is in design note §19).

- **backlog/cnt-channel-staged-catalysis.md** (dormant, Reto 2026-10-03):
  a carbon channel whose arc-and-rib interior holds each intermediate of a
  staged reaction in one pose. Blocked on hexfold building a three-arc
  cross-section with ribs; no quest until then. 18 papers listed. Two
  read passes (8 fetched papers, 2026-10-03) minted 20 hubs and stubbed 5
  primaries the reviews cite. Both are listed in the item. The next pass
  waits on the 10 unfetched papers (KcsA, CPS) and the 5 new stubs.

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
