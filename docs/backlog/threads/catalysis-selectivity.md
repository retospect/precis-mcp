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
4. **backlog/pourbaix-bulk-verdict-job.md** →
   **backlog/pourbaix-quest-gate.md** — Reto asked for it next after the
   network (2026-10-02); independent of 1–3. A candidate whose bulk
   dissolves across the operating window cannot be the catalyst, so it is
   ruled out before selectivity is spent on it (qu202468). The job (A)
   is built and reviewed (design note §18). It goes to the orchestrator as
   branch `worktree-agent-af864454b1508ef28` (tip `224232762`) for round 2,
   not a qland, because it changes deploy roles. The orchestrator owns that
   branch now. After it lands: clamp the within-tol note's margin at 0 (a
   ΔG in (−1e-6, 0) prints "-0.000 eV/atom above"), and cite
   materialsproject/pymatgen#4709 at the `process_multientry` workaround
   so it can be deleted once upstream fixes it. The gate (B) waits on A
   landing, the MP key in the vault (Reto), and qu202468's operating point
   (set: −0.2 V, pH 7, window −0.4…0 V, pH 7–10).
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
   - The proof-of-concept paper (Fe-doped Pd, ~Nov 2026) belongs to
     qu459585, not this thread.
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
  cross-section with ribs; no quest until then. 18 papers listed; next
  is a read-for-question pass for design numbers once they fetch.

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
