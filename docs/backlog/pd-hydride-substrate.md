---
status: draft
title: Pd candidates are evaluated on the hydride phase their operating potential implies, with bare Pd as the reference
pillar: 3d-design
prio: high
---

# Pd candidates are evaluated on the hydride phase their operating potential implies

## Motivation / why

Reto, 2026-10-02 (review-queue `answered/reto-pd-hydride-1.md`, addendum
and clarification): under cathodic operation Pd absorbs H and turns β-PdH
(x ≈ 0.6–0.7, lattice +3.5%) below a plateau near +0.05 V_RHE. Every Pd
NO→NH₃ peak-FE potential found is −0.3 to −0.4 V_RHE. All 232 live
qu164903 candidates are bare Pd(111) at 3.89 Å, so its pathways ran on the
wrong substrate. Full plan, sources and costs:
`~/.claude/projects/-Users-reto-precis-mcp/reviews/catalysis-selectivity.md`
§15; approval item `catalysis-selectivity-16`.

## In scope

1. **Stage 0, validate the potential.** catpath's MACE-MP-0 medium on bulk
   PdHx, octahedral H:
   - variable-cell relax, with disordered arrangements at x ≥ 0.25;
   - ZPE from H vibrations;
   - the formation free energy per Pd,
     g(x) = [E(x) − E(0)]/N_Pd − x·(½E_H₂ + ΔZPE + gas terms) − T·s_conf(x),
     with ideal-mixing s_conf = −k[x ln x + (1−x) ln(1−x)] on the octahedral
     sublattice. Each x uses its lowest-energy arrangement; the spread over
     arrangements is reported.
   - The step potential is minus the slope of the **common tangent** between
     the α and β branches (two-phase coexistence), not where a differential
     ΔG_abs crosses −eU (design review S1).

   **Pass criteria** (Reto, `catalysis-selectivity-16`; design review S2):
   - β at every potential in the window: any computed step anodic of about
     −0.1 V_RHE passes. The plateau to ±0.1 V of +0.05 V is reported as a
     diagnostic, not a gate.
   - a(β)/a(Pd) on the same potential = 1.035 ± 0.01. The absolute a(β) is
     reported.

   On a failure:
   - Plateau miss: calibrate the H₂ reference to the measured plateau. The
     result then says the plateau is an input, not a validated output.
   - Lattice miss: DFT spot-check before any re-run.
2. **Levels from the computed steps**, not from a fixed set. Within
   0.1 V of the operating point or window edge → evaluate both levels and
   flag the result ambiguous.
3. **Stage 1–2:**
   - β lattice: mean ± spread over the arrangements;
   - Pd(111) slab at a_β with interlayer octahedral H at x_β;
   - the candidate's modification re-applied;
   - relax z.
4. **Stage 3:** neb-tier re-run of the merged frontier's top 10. Each
   result is a new candidate. It carries its bare twin in meta
   (`bare_twin_ref`), not the taproot `refines` relation (design review);
   if a link relation is wanted, ask knowledge-mesh for the slug. It is
   tagged `substrate_level = {phase, x, a, U_step}`.
   - **Strain controls (design review S3).** The 232 bare slabs sit at
     3.89 Å; the potential's own Pd is near 3.95 Å. For each of the top 10,
     add bare Pd strained to a_β with no H, and, budget allowing, bare Pd at
     the potential's own a(Pd). Without them the result cannot separate
     strain from hydride. If only one control fits the budget, keep the
     strained one.
   - **Failure accounting.** "Subsurface H hydrogenated the intermediate"
     (an endpoint that changed identity by taking lattice H) is counted
     separately from reconstruction failures, because its rate is a
     result.
   - Bare-Pd results stay as the reference. The level joins the network
     basis, so bare and hydride margins never rank against each other. The
     frontier ranks the level that matches the operating point.
5. **precis fixes:**
   - `struct_relax` keeps the relaxed lattice and allows a tight fmax;
   - preflight passes H-loaded slabs.
6. **The stage "substrate state at (U, pH)"** sits next to the Pourbaix
   gate. A candidate whose ΔG_abs stays positive across the window skips
   it. No operating point → flag, never a default.
7. **qu164903's operating point** comes from the literature: proposed
   U = −0.3 V_RHE, window −0.2 to −0.4 V, pH 7 (item 16). It is written to
   the quest only after the papers' reference scales are confirmed.

Stated in every result: there is no operando evidence of PdH under NO
reduction itself (the nearest is CO₂RR operando TEM, β below −0.2 V_RHE).
Surface H* coverage at −0.3 V is modelled on neither substrate. The
comparison stands, but absolute margins do not describe the operating
surface.

Related (not this item): `frontier._candidate_from_structure` lifts every
numeric top-level structure meta key into a ranking measure. Every
bookkeeping key needs listing in `_META_NON_MEASURE`, which is fragile; an
allowlist of measure keys would be safer (design review §20.7).

## Explicitly NOT in scope

- Re-equilibrating H content along a pathway: it is fixed per run, and the
  result text says so.
- The catpath "substrate state" step: a brief for the catpath session after
  the precis prototype has numbers.
- Surface Pourbaix / H* coverage effects on the criterion. That belongs to
  `pathway-selectivity-u-ph-window.md`.

## Acceptance criteria

- Stage-0 numbers are recorded beside their pre-set thresholds:
  - a(Pd), a(β);
  - the x(U) steps, with the plateau.
- Each re-run candidate carries `substrate_level` and a `refines` link to
  its bare twin.
- The frontier never ranks a bare margin against a hydride one (test).
- qu164903's logbook records the operating point and its sources.

## Target + blast radius

`src/precis/workers/job_types/struct_relax.py` (lattice write-back, fmax),
`src/precis/quest/preflight.py`, `src/precis/quest/compute.py` +
`frontier.py` (level in the basis, ranking by operating point), a stage-0
script or job. No catpath change for the first pass.

Compute, on castor+pollux:
- stage 0: ≈ 1 GPU-h;
- β re-run, top 10 at one level: ≈ 15 GPU-h;
- strain controls: ≈ 15 GPU-h per control set at neb tier, or ≈ 1 GPU-h at
  screening tier.

Plan: the strained control at neb tier (like-with-like with the β run).
The own-lattice control runs at screening tier unless the β result makes it
decisive. It competes with qu164903's ticks for the two GPU slots.
Proposed, not yet asked: set `fidelity_promote_*` to 0 on that quest for
the duration, and record the pause in the quest logbook.

## Open questions / decisions log

- **Decided, Reto 2026-10-02 (`catalysis-selectivity-16`):** approved as
  staged. Stage 0 gates the top-10 β re-run. Lattice on expansion
  (3.5 ± 1%). Plateau miss → calibrate the H₂ reference; DFT only on a
  lattice miss. U = −0.3 V_RHE, window −0.2 to −0.4 V, pH 7, written only
  after the reference-scale check.
- **Design review §15 (2026-10-02):** S1 common tangent with s_conf; S2
  pass = β across the window, lattice by ratio; S3 strain controls; no
  `refines`; separate failure count for hydrogenation by subsurface H.
- **Stage 0 ran (2026-10-02, design note §16; `scratch/pdh-stage0/`).**
  MACE-MP-0 medium, float64 CPU, 2x2x2 conventional cell.
  - Gates pass: a(β)/a(Pd) = 1.032 at x = 0.59; x = 1.0 across the window,
    with the last step at −0.098 V_RHE.
  - But the potential makes H–H net repulsive, so it has no α/β
    miscibility gap at this sampling (arrangement noise matches the hull
    curvature at mid x). The "+0.11 V step" is a grid artefact.
  - It overbinds absorbed H by about 0.19 eV per H, so H loads from
    +0.25 V. P1 passes because of that error.
  - It is validated for geometry at a given x, not for x(U).
  - **Decided, Reto 2026-10-02 (`catalysis-selectivity-18`):** x ≈ 0.625
    from experiment, geometry from MACE. Proceed with stages 1–3 (≈ 15
    GPU-h plus the strain controls).
- **Open (`catalysis-selectivity-17`):** the reference scale of pa5303 and
  pa166889 sits behind publisher SI paywalls (403). It is Reto's check.
  Until then no operating point is written.
