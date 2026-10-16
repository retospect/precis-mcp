---
status: idea
title: Potential- and charge-dependent rates in catpath kinetics, ported from the CatMAP electrochem stack
pillar: 3d-design
---

# Potential- and charge-dependent rates in catpath kinetics, ported from the CatMAP electrochem stack

Needs design before it is draft (Reto, 2026-10-16: "add it to the pile; we'll
need to design some more").

## Why

The thread asks whether NO→NH₃ wins every fork *kinetically* over a U/pH
window. `autocatpath.kinetics.solve` has no U: barriers are Eyring on
`max(barrier, dG, 0)`, and the computational hydrogen electrode shift lives
only in the thermodynamic post-processing (`autocatpath/electrochem.py`). A
kinetic margin over a potential window is undefined until rates move with U.

The Ringe/Fuhrmann stack solves this for CO₂ reduction:
[CatmapInterface.jl](https://github.com/j-fu/CatmapInterface.jl) (MIT) takes
CatMAP rate constants (CatMAP: GPL-3.0, `medford15`) and adds, in
`src/corrections.jl`:

- transition-state shift `β·(φ_we − φ − U_pzc)` on electrochemical steps;
- surface-charge term `a·σ + b·σ²` on adsorbates and transition states;
- per-adsorbate hydrogen-bond stabilisation (CatMAP `hbond_dict`);
- local-pH shift of the H/OH free energies.

LiquidElectrolytes.jl (MIT) supplies σ, local potential and local pH from a
Poisson–Nernst–Planck double-layer and transport solve. catpath and precis
are GPL-3.0-or-later, so licensing does not block copying; implement from
the papers and use the Julia code as a cross-check.

## Sources (stubs minted 2026-10-16; read before design)

- pa492262 — Maaß 2026 ChemRxiv preprint of the method paper (Electrochim.
  Acta 578 149932 is pa456069, a one-page preview only). Read first: exact
  correction set and how σ / local pH feed the rates.
- pa492258 — Ringe 2020 Nat. Commun.: σ-dependent energies, σ(U).
- pa492259 — Ringe 2019 EES: cation / field effects.
- pa492260, pa492264 — Chan & Nørskov 2015, 2016 JPCL: potential dependence
  of barriers, charge extrapolation.
- pa492265 — Vijay 2022 JPCL: force-based β, possibly per step from existing
  NEB transition states.
- pa492261 — Bohra 2019 EES: modified PNP double-layer model.
- pa492263 — Euzenat 2026 ACS Electrochem.: second coupled
  microkinetic-transport implementation (LLNL) to compare against.

## Candidate slices (to be designed)

1. **β·U in `kinetics.solve`.** Per-step symmetry factor on H⁺+e⁻ steps
   (default 0.5, per-step override, provenance recorded), U and pH as solve
   arguments. Feeds `pathway-selectivity-u-ph-window` and
   `pathway-kinetics-promotion-gate` a kinetic `U_sel`.
2. **Surface-charge terms.** Fit `E(F) = E₀ + μF − ½αF²` per intermediate
   from QE sawtooth-field single points (`tefield`/`dipfield` already in
   `dft.py`), map to `a·σ + b·σ²`; Helmholtz `σ = C_dl·(U − U_pzc)` as the
   no-transport default. DFT tier only — MLIPs have no field response, so
   results without field runs must say "β·U only".
3. **CatMAP parity export.** `.mkm` + `energies.txt` from `results_json`
   (CatMAP species names, `^` transition states, `vib`-tier frequencies,
   barrier source tagged); run CatMAP once and compare TOF and degree of
   rate control with catpath's linear master equation. Decides whether a
   nonlinear mean-field solve (site balance, bimolecular terms, lateral
   interactions) is needed.
4. **Transport coupling (deferred).** External Julia job running
   LiquidElectrolytes.jl + CatmapInterface.jl on the slice-3 export; only if
   slices 1–2 show the U/σ terms change a selectivity call.

## Open design questions

- Where β comes from per step: fixed 0.5, Vijay force-based from the NEB
  transition state, or Chan–Nørskov charge extrapolation — and what the
  BEP-estimated barriers get.
- U_pzc and C_dl per candidate surface: computed (work function), tabulated,
  or quest parameters.
- Engine-side (catpath) vs precis-side split; overlaps catpath's
  `docs/backlog/beyond-che-potential-response.md` and
  `jdftx-gcdft-backend.md` — fold into one design there or reference.
- Network-basis stamp: β/σ settings must join the correction-set basis
  (`pathway-correction-basis.md`) so margins with and without them never
  compare.
- Whether CatMAP's GPL-3.0 Python package becomes an optional extra for
  slice 3 or runs out-of-image only.
