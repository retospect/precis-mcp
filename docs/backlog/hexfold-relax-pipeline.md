---
status: draft
pillar: 3d-design
title: hexfold relax pipeline — grammar-fixed bonding, cheap whole-block polish, GFN2 only on junction fragments and periodic cells, cached modules
---

# hexfold relax pipeline

Source: research report 2026-10-14 (memory me485004; Reto: persist the
findings). Tags: **[L]** literature, **[O]** our relax1013 campaign
(me482127; table in `## Relax campaign 2026-10-13` of
[hexfold-toolkit](threads/hexfold-toolkit.md)), **[I]** inference, untested.
Refines the "route under consideration" of
[hexfold-relaxation-completion](hexfold-relaxation-completion.md) §1;
that item keeps the collapse acceptance and the viewer/MCP surface.

## Problem

Full-block GFN2-xTB is out of reach above ~1k atoms: 516 atoms cost ~12 s/step,
cubic scaling puts Y-A (5,790 atoms) at hours per step [O + I]. The
`fidelity='xtb'` rung (`src/precis/structure/relax.py`) is in-process, refuses
`cell=`, and is unavailable where tblite is (linux-aarch64 cluster). Prod
scenes need relaxing anyway (rule, Reto 2026-10-13: relax, no collapse).

## Finding (our data)

Per-carbon displacement before→after relax, binned by distance to the
nearest junction atom (sp3 or coordination-changed) [O]:

- fin-in: median 0.23–0.31 Å at 0–6 Å, 0.12–0.15 Å at 6–12 Å, 0.11 Å at
  12–15 Å, floor 0.07–0.08 Å at 15–25 Å.
- seam-out: 0.40 Å at 0–6 Å, 0.20 Å at 8–10 Å, 0.11 Å at 12–15 Å.
- Junction-induced relaxation dies out within **10–12 Å (4–5 rings)**. The
  ~0.08 Å floor is the stick generator's own error: the far field is already
  nearly relaxed; the error sits at junctions.
- Module buffer therefore **≥ 12 Å**. Caveat: this is in-plane. Out-of-plane
  buckling of free sheets is long-range [L Jain 2015], so modules cannot
  carry it (see Risks).

## Pipeline tiers [I]

- **T0 seed.** Analytic surface (ideal-surface-then-tile), cached modules
  pasted into junction regions by lattice-addressed atom id, stick relax for
  the rest.
- **T1 whole-block polish, fixed topology.** GFN-FF (`xtb --gfnff`, CPU) or
  MACE fine-tuned on the ~1,000 campaign FIRE frames with forces (on disk
  [O]; reproduces GFN2's errors, not DFT's). Converge to fmax 0.05 eV/Å,
  then the collapse check. Topology fixed from input is the point: the
  grammar already authored the right bonding.
- **T2 GFN2 on junction fragments.** Armchair-cut, H-capped, ≥12 Å buffer
  (zigzag cuts carry edge states [L Fujita 1996]); buffer frozen or
  subtractive ONIOM with T1 as low level. Axial features use periodic cells
  instead.
- **T3 spot DFT** (PBE-D3 or r2SCAN) once per new module type. First target:
  the `k3-sp3-z` periodic cell (~100 atoms), the first independent check of
  the sp3 dimer-chain verdict, which is a GFN2 result only.
- **Periodic / helical cells** for repeating features (straight seams, fin
  grafts, `rail-k3-120-z` tube bodies): one period + buffer ≈ 100–300 atoms.
  Rail tube: 2a axial × cross-section ≈ 150–300; Y seam: 2a × three 15 Å
  strips ≈ 100 + caps. Helical/objective BCs for chiral tubes (Dumitrica–
  James). Whether tblite's periodic GFN2 works for our cells, and whether
  DFTB+ helical accepts an xTB backend, are unchecked.
- **Module cache.** One precis structure per module, keyed by motif (seam
  row, fin out/in, tube–sheet foot, open-pole rim, cap, pore, lid, seam
  terminator) × curvature bin (radius ±0.5 Å) × rim type × hexfold version.
  Payload: relaxed core + buffer, method/backend/fmax/steps, energy, seed
  hash; provenance link to the GFN2/DFT job. Reuse gates, all required:
  converged at 0.05 eV/Å; buffer-edge displacement < 0.1 Å versus a run with
  a 6 Å larger buffer; no new contacts outside the expected bond pattern;
  after pasting, T1 max force on interface atoms < 0.2 eV/Å; one T3 check
  per module type. Reuse outside the radius bin is refused.

## Grammar checks (hexfold `check` codes) [I]

1. `seam.sp2_k3_equal120` — warn, then refuse: not a GFN2 minimum, dimerises
   into the sp3 chain [O]. Author seams only on GFN2-verified rows
   (`k3-sp3-z`, sp3 k4, sp3 fin grafts).
2. `seam.open_end` — a seam closes or carries a terminator module (guard H
   crowd to 1.35 Å and neighbouring guard carbons bond otherwise [O]).
3. `sheet.isolated_curvature_defect` — lone pentagon/heptagon in a flat
   unpinned sheet with no opposite-sign partner within 3 rings; pair it or
   author the corrugation.
4. `rim.zigzag_open` — prefer armchair rims; warn.
5. `junction.spacing` — < 25 Å between junction cores (two buffers): warn
   that the cache cannot serve it.

## Cheap A/Bs

- ASE `PreconLBFGS` vs FIRE on any tier (Packwood 2016: ≥2× fewer steps at a
  few hundred atoms; may misbehave off-crystalline). Our FIRE runs took
  100–313 steps [O].
- fp32 vs fp64 forces (dxtb, GFN1 in PyTorch; autodiff forces 2–5× slower
  than analytic [L]).
- pending: Mac timing probe + fp32 test (2026-10-14)

## Estimates (estimates, not measurements) [I]

- Y-A, 5,790 atoms re-authored on `k3-sp3-z`: T0 seconds; T1 GFN-FF ~10–30
  min (or fine-tuned MACE on GB10 ~1 min); T2 periodic seam cell minutes
  once, two seam-end fragments (~700–900 atoms) 1–4 h each in parallel.
  **~2–4 h first time, < 1 h on reuse.**
- Nanoreactor tunnel with reservoirs, ~10–20k atoms: modules once (rail-tube
  periodic cell minutes; two tube–sheet feet ~1,000 atoms 2–4 h each; one
  pore module < 1 h); T1 whole block < 1 h CPU. **~half a day first, ~1 h
  per later variant.**

## Risks

- Pre-fixed bonding cannot find reconstructions; a wrong seam row stays
  wrong. Mitigation: T2/T3 on every new module type.
- Long-wavelength buckling of free sheets needs periodic cells, pins or an
  authored corrugation (Ma 2009 mode as seed), not modules.
- No published universal-MLIP benchmark on curved sp2/sp3 carbon junctions;
  our MACE pilot (`fi473095`) did not converge in 300 steps [O]. MLIP is T1
  only after fine-tuning and a held-out fragment test.
- `fidelity='xtb'` refuses periodic cells; xtb and LAMMPS are not installed
  on the Mac; REBO's 1.7 Å switching onset sits just above the 1.60 Å dimer
  bond, so BOP forces there need checking.
- GFN2 is a model; the dimer-chain verdict has no DFT check until S2.

## Slices (ordered)

- **S1 grammar checks.** The five codes above in hexfold `check`. Accept:
  each fires on a fixture (equal-120 Y, open-ended seam, lone 5/7 in a flat
  sheet, zigzag rim, two junctions < 25 Å) and stays silent on the sp3-row
  scenes.
- **S2 periodic-cell relax + DFT check.** Lift `cell=` refusal for the xtb
  rung (or a cell-aware path) and relax the `k3-sp3-z` and `rail-k3-120-z`
  periodic cells; one PBE-D3/r2SCAN run on the `k3-sp3-z` cell. Accept: cell
  relax converges; DFT confirms or refutes the 1.6 Å dimer chain, recorded
  here and in the catalogue row.
- **S3 fragment cutter + GFN2 junction relax.** Cut armchair, H-capped
  fragments with ≥12 Å buffer; relax; paste back by atom id. Accept: Y-A
  assembled end to end, interface max force < 0.2 eV/Å, wall time within 2×
  of the estimate.
- **S4 module cache.** Structures keyed as above, gates enforced. Accept: a
  second variant reuses cached modules, relaxes in < 1 h, and a radius
  outside the bin is refused.
- **S5 whole-block polish tier.** Choose GFN-FF vs fine-tuned MACE vs
  PreconLBFGS-only from the probe numbers. Accept: chosen tier reproduces the
  campaign's GFN2 junction geometry on the held-out fin/seam fragments within
  0.05 Å. Blocked on the pending probe line above.

## References

Kuc & Seifert, PRB 74, 214104 (2006), doi:10.1103/PhysRevB.74.214104 ·
Zhu & Tománek, PRL 109, 135501 (2012), doi:10.1103/PhysRevLett.109.135501
(unverified; DOI read off the APS manuscript URL) · Jain et al., JPCC 119,
9646 (2015), arXiv:1510.08317 · Ma et al., PRB 80, 033407 (2009),
doi:10.1103/PhysRevB.80.033407 · Prodan & Kohn, PNAS 102, 11635 (2005),
doi:10.1073/pnas.0505436102 · Fujita et al., JPSJ 65, 1920 (1996),
doi:10.1143/JPSJ.65.1920 (unverified) · Chung et al., Chem. Rev. 115, 5678
(2015), doi:10.1021/cr5004419 (unverified) · Dimitrakakis et al., Nano Lett.
8, 3166 (2008), doi:10.1021/nl801417w (unverified) · de Tomas et al., Carbon
109, 681 (2016), doi:10.1016/j.carbon.2016.08.024 (unverified) · Spicher &
Grimme (GFN-FF), Angew. 59, 15665 (2020), doi:10.1002/anie.202004239 · Rowe
et al. (GAP-20), JCP 153, 034702 (2020), doi:10.1063/5.0005084 · Packwood et
al. (PreconLBFGS), JCP 144, 164109 (2016), doi:10.1063/1.4947024 · Dumitrica
& James, JMPS 55, 2206 (2007), doi:10.1016/j.jmps.2007.03.001 (unverified) ·
Zhang, James & Dumitrica, PRB 80, 115418 (2009),
doi:10.1103/PhysRevB.80.115418 · dxtb, JCP 161, 062501 (2024),
doi:10.1063/5.0216715 (unverified) · Park et al. (schwarzites),
arXiv:0912.0056 · Qamar et al. (ACE), arXiv:2210.09161
