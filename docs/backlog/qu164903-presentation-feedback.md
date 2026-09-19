---
status: draft
title: qu164903 presentation feedback — Pourbaix diagram, pareto axes + pH slider, structure→pathway navigation, profile legend + TS provenance
prio: high
---

# qu164903 presentation feedback (Reto, 2026-09-18)

The tracked list. Each item gets a decision line (level / owner / blocked-by)
once discussed; items that become independently shippable split off into
their own backlog files with `blocked-by` back here.

| # | feedback (verbatim intent) | surface | status |
|---|---|---|---|
| 1 | "we want to make a Pourbaix diagram — how? for what level? discuss" | new view (pathway/structure/quest) | DECIDED b+c → `surface-pourbaix-view.md` |
| 2 | Legend for the chemistry pathway: chemistry vs electrochemistry steps — dashed vs fixed lines? | pathway profile viewer | discussing |
| 3 | "note somewhere on the page how transition-state energy was calculated" | pathway detail page | discussing |
| 4 | From the compound page (`/structure/<slug>`) get to the pathway and to the Pourbaix — tabs on the same page? UX discussion | structure detail / pathway detail | discussing |
| 5 | Pareto front: another energy axis; be specific what the axes are — detailed text | quest page pareto | DECIDED: `barrier` stays, per-axis definitions under the plot (in flight) |
| 6 | Clicking a reaction on the pareto front shows that thing's details (pathway, …) — solved by the tabbed landing spot if tabs are done well | quest page pareto → detail | discussing |
| 7 | pH slider on the pareto front — do we need to rerun? justify why this can be dynamic | quest page pareto | discussing |
| 8 | Default axes of the pareto plot (all three) considered more carefully | quest page pareto | DECIDED: x U_L_abs · y log_tof · colour barrier, via rubric reorder (Reto's SQL below) |

## Working notes (verified 2026-09-18 against this tree + /Users/reto/catpath)

**What exists today (anchors).**
- Pathway page CHE lever: `templates/refs/pathway_detail.html.j2` `#pw-u-lever`
  — U slider vs RHE (−1.5…0.5 V), a **pH input already exists** but only feeds
  the `sheFromRhe()` readout (`U_SHE = U_RHE − 0.0592·pH`); the diagram never
  re-renders on pH because `analysis.py::at_potential` is pH-free by
  construction. Engine twins: `autocatpath/electrochem.py::u_rhe_to_she`,
  `decoupled_ph_shift` (non-PCET proton step; unused by any qu164903 pathway).
- Chemical vs PCET drawing: chemical steps = solid hump trace with a barrier,
  PCET supply edges = dashed connectors, no barrier; the legend
  (`buildPathLegend`) lists *paths*, not line styles — nothing explains the
  dash.
- TS provenance: the Methods body chunk (bottom of the page, `## Methods`)
  says "climbing-image NEB (5 images, converged to 0.15 eV/A)", model, seeds,
  thermo tier. It is 1500 lines below the profile.
- Structure page (`templates/structure/detail.html.j2`, `routes/structure.py::
  _quest_context`): 3D viewer + relax runs + a quest-context panel listing
  the candidate's pathways (`meta.candidate_ref` lookup) — 1 click to a
  pathway. Pareto point → `/refs/structure/<id>` (the candidate slab), not
  the pathway.
- Pareto defaults (`quest/frontier.py::plot_axes_for`): first two declared
  rubric objectives → for qu164903 `span_at_Uopt` × `U_L_abs`, colour z =
  third objective `energy`. `_AXIS_LABELS` has short labels only, no
  description text. `energy` = the candidate slab's **relaxed total energy
  (eV)** — an absolute total energy, not comparable across compositions.
- Coverage scan (`autocatpath/coverage.py`): γ_ads(θ) = [G(slab+nA) − G(slab)
  − n·μ_A]/area at (T, p) — single species per termination, no lateral
  interactions, **no U input**; precis never runs it (`runner.py::run_kinetics`
  `mari=None`).

**Item 1 — Pourbaix, levels.**
(a) solution-species N Pourbaix (NO₃⁻ … NH₄⁺/NH₃) from tabulated ΔG_f
(pymatgen `PourbaixDiagram`, Persson 2012 PRB 85 235438) — zero compute, says
nothing about the catalyst; a context panel at most.
(b) **surface Pourbaix of the candidate slab** (Hansen, Rossmeisl, Nørskov
PCCP 2008 10 3722): lowest-G termination among clean / H*(θ) / OH* / O* / NO*
/ N* / NH_x* (+ a few pairs) in (U, pH). Per termination
ΔG(U,pH) = ΔG(0,0) − n_e·eU_SHE − n_H⁺·kT·ln10·pH; for PCET n_e = n_H⁺ so vs
U_RHE it is lines in U only. Data = 10–30 relaxations per slab; the engine's
coverage scan produces exactly the γ(θ) half, the CHE shift is closed-form on
top ⇒ level (b) = effects-report **Phase 3.1** (`autocatpath_coverage` job +
`view='coverage'`) + a renderer. Mixed terminations (H*+NO*) need engine v2.
(c) operating-point overlay: the pathway's U_L / U_opt as vertical lines on
(b) — the coverage-consistency check ("is the slab clean at U_L?"). This is
the level that answers the effects report's coverage question.
Home: the **structure (candidate) page** — a Pourbaix is a property of the
slab, not of one pathway. NO3RR data source for sanity: Liu, Richards, Singh,
Goldsmith ACS Catal 2019 9 7052.

**Item 7 — pH slider: no rerun.** Every qu164903 step is PCET; on the RHE
scale pH cancels exactly (CHE). A pH control can only (i) relabel U to SHE,
(ii) shift the product reference by NH₃/NH₄⁺ speciation (pKa 9.25 — closed
form, last desorption step only), (iii) shift a decoupled proton step
(`decoupled_ph_shift`, none exist). A rerun is needed only for beyond-CHE
physics (field / cation / grand-canonical). Page wording: "All steps are
coupled proton-electron transfers, so free energies on the RHE scale are
pH-invariant by construction (CHE). pH re-expresses U on the SHE scale
(U_SHE = U_RHE − 0.0592·pH at 298.15 K); it does not move the points." The
more useful pareto control is a **U slider**: span at U per candidate is
closed-form client-side if each candidate's node (G, n_H) list ships with the
page; log_tof at U needs a microkinetic re-solve (server-side, no new DFT).

**Items 5/8 — axes.** Drop `energy` as an objective (composition bias:
selects heavier dopant loadings, not better catalysts). Proposal: x =
`U_L_abs` (limiting potential, V vs RHE, min), y = `barrier` (rate-limiting
TS, eV, min; kinetic, U-independent) or `log_tof` (max), z colour = `P_side`
(min) once bumblebee makes it non-null, else `barrier`. Replacement "energy"
worth adding later: dopant formation/segregation energy vs clean slab + bulk
references (stability), needs `e_bulk`-style references. Inconsistency to
state in the axis text: `log_tof`/`span_at_Uopt` are evaluated at each
candidate's own U_opt, `U_L` at U = 0 — candidates are compared at different
potentials. Add `_AXIS_DESCRIPTIONS` (definition, unit, sign, at which U,
reference electrode, T, trust gating) rendered under the plot.

**Items 4/6 — landing spot.** Make the candidate structure page the hub with
URL-addressable tabs (`?tab=structure|pathway|pourbaix|runs`; pattern =
`cad/detail.html.j2` mode buttons): pareto click → `?tab=pathway` showing the
best trusted pathway's profile inline (tier toggle intact) + the list of
others; Pourbaix tab = (b)+(c) when coverage data exists, else a "not
computed — N relaxations" stub. Blocker: the profile viewer is inline JS in
the pathway template — embedding it needs the payload builders factored out
of the pathway route (backlog `pathway-profile-renderer-unification`).

**Items 2/3 — small, shippable now.** (2) legend row: solid = chemical step
(NEB barrier, U-independent) · dashed = PCET (shifts n_H·eU, no barrier in
CHE). (3) one-line provenance footnote under the profile from
`results`/`config`: "TS: CI-NEB, 5 images → 0.15 eV/Å, mace:medium, 1 seed;
barriers carry the reactant's thermo correction; U-independent (CHE)" + the
trust/blocked_by status.

## Open questions / decisions log

- 2026-09-18 Reto: item 1 → levels b+c (surface Pourbaix + U_L/U_opt overlay),
  specced in `surface-pourbaix-view.md`. Item 5/8: `barrier` stays and must be
  defined precisely on the page (it is the largest single-step Ea on the
  route, TS minus that step's own preceding intermediate — NOT the height
  above the slab, which is the span); default y = `log_tof`; drop `energy`.
- Verified definitions (2026-09-18): `barrier` = `precis_pathway/analysis.py::
  rate_limiting_step` (max edge `barrier` on the root→target path, U = 0,
  NEB); `span` = `energetic_span` (Kozuch–Shaik); `log_tof` = microkinetics
  on the U = 0 free energies (`autocatpath/kinetics.py` takes no potential;
  the runner applies no shift) — NOT at U_opt as earlier notes said; `U_L` =
  −max ΔG over PCET steps at U = 0 (`electrochem.py`); `energy` = converged
  relax total energy on the calculator's zero.
- Prod state 2026-09-18 (213 live candidates): `barrier` 205, `U_L_abs` 208,
  `span_at_Uopt` 208, `energy` 196, **`log_tof` 32** (kinetics trust gate),
  `P_side` absent everywhere. A `log_tof` default y plots 15% of points
  today; the picker's "(n)" count makes that visible. Data hygiene: live
  `barrier` max 7462 eV and `span_at_Uopt` max 73.7 eV — pre-0.21 garbage
  rows that should be untrusted, not plotted. Axis block landed 75569104
  (2026-09-19); gripe filed as gr356741 (plausibility clamp in the harvest,
  barrier > 10 eV or span > 20 eV ⇒ trusted=false + reason, plus a prod
  re-harvest). `meta.frontier_viewport` is the stopgap.
- Rubric edit (Reto runs it; defaults follow rubric order via
  `frontier.py::plot_axes_for`): `log_tof` goes in as **optional** so the
  181 kinetics-less candidates stay evaluated on the required axes.
  ```sql
  UPDATE refs SET meta = jsonb_set(meta, '{rubric_objectives}',
    '[{"key":"U_L_abs","sense":"min"},
      {"key":"log_tof","sense":"max","optional":true},
      {"key":"barrier","sense":"min"},
      {"key":"P_side","sense":"min","optional":true}]')
  WHERE kind='quest' AND ref_id=164903;
  ```
  Effect: frontier dominance now includes `log_tof` for the 32 that have it
  and `barrier` for all; `energy` stops steering the proposer. No rerun.
- Items 2, 3 (legend row, TS footnote) and 4/6 (structure hub tabs): still
  Reto's call; 4/6 folds into `surface-pourbaix-view.md` item 3 if approved.

