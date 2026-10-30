---
status: draft
title: One spin-polarised DFT point on an Fe-doped Pd(111) anchor (SPSO slice 8 spin check)
pillar: quests
prio: normal
---

# One spin-polarised DFT point on an Fe-doped Pd(111) anchor

<!-- Specced item filed from SPSO slice 8 (docs/backlog/
surface-pourbaix-staircase-optimizer.md, Rulings 2026-10-29, review item -30).
Nothing here is launched: no job is dispatched until the estimate below is
measured and Reto has the number. -->

## Motivation / why

Slice 8 reads Fe-doped Pd(111) (subject A, st202238; subject B, st463208)
off `mace:medium` scans. Fe is magnetic and `mace:medium` is not spin-aware,
so every Fe number in the slice-8 table rests on a model that cannot see
magnetism. The Ta spin check (pd-hydride-substrate.md, "design note §22i")
found no moment on Ta and does not transfer to Fe. Reto ruled on 2026-10-29
(-30) that the paper stands on `mace:medium` plus one spin-polarised DFT
point on an Fe anchor, as its own item. Slice 8's table carries a provenance
row "spin check: pending <this item>" until this lands.

One point bounds the MLIP's magnetism error at that one site. It does not
validate the Fe results in general, and the paper says so.

## In scope

1. **A single-point DFT run through the existing GPAW path** on one Fe anchor
   structure, in two states from the same geometry: spin-polarised and
   spin-paired. Both on the clean Fe slab (A) and on the anchor's slab with
   its adsorbate (the lowest-`gamma` Fe-slab anchor of A at −0.3 V_RHE, named
   by slice-8 stage 1).
2. **The quantity reported:** the spin effect on the anchor's `gamma0`,
   `Δγ0_spin = [ΔE_spin(slab+ads) − ΔE_spin(clean slab)] · (1 / area)` with
   `ΔE_spin = E(spin-polarised) − E(spin-paired)` at the fixed `mace:medium`
   geometry, compared to that anchor's `sigma_gamma`. Gas references cancel
   when the adsorbate is non-magnetic (H, OH from water); an O anchor needs
   an O2 reference and is flagged.
3. **Machinery gaps this needs closed** (verified in `src/`, 2026-10-30):
   - No single-point mode: the container always runs an optimizer
     (`gpaw_relax.py`, `opt.run(fmax, steps=max_steps)`); whether
     `max_steps=0` is a clean single point is unchecked.
   - `struct_relax` writes only `fidelity`, `model`, `steps`/`max_steps`,
     `cell`, `fmax_ev_per_angstrom` to the container's `params.json`; the
     `dft` block (`functional`, `spin_polarized`, k-points, smearing) that
     `gpaw_kwargs_from_params` reads is never passed, so a caller cannot
     choose it. Defaults apply: RPBE, LCAO dzp, `spin_polarized=True`.
   - No per-species initial magnetic moment: neither `atoms_from_poscar` nor
     the container sets one. GPAW started with `spinpol=True` and zero
     initial moments stays at the non-magnetic solution, so the default run
     would report "no moment" whatever the physics is (to verify against
     GPAW's behaviour on a seeded vs unseeded Fe slab before trusting any
     result).
   - The `magmom` scalar is written, but the per-atom moments are not.
4. **A measured cost** before any real run: a timed single point on a
   smaller proxy (the pristine Pd slab, same cell) on the lane that will run
   it, then the stated bound for the Fe slab.

## Explicitly NOT in scope

- No BEEF-vdW or any ensemble (td470248 is estimate-only; memory 470247
  holds the unmeasured planning formula).
- No geometry relaxation in DFT; no NEB; no spin-polarised pathway or
  barrier anywhere.
- Not a validation of MACE energies against DFT in general: functionals
  differ (RPBE here, MACE trained on PBE), so absolute DFT-vs-MACE `gamma0`
  differences are not the quantity (see Open questions).
- Not an Fe–Pd alloy survey: one slab, one anchor, one site.
- No new quest; nothing here touches qu164903's `meta`.

## Acceptance criteria

1. A unit test for each closed gap: `dft` params pass from the job into
   `params.json` and into `gpaw_kwargs_from_params`; initial magmoms by
   species reach the ASE atoms (a seeded Fe atom reports the seeded moment
   before the SCF); `max_steps=0` performs one SCF and no optimizer step (or
   the item states the alternative it built).
2. A control: on the seeded Fe slab, the spin-polarised run reports a
   non-zero total moment, or the item records that Fe in this site converges
   to zero moment from the seed and reports that as the result.
3. Wall time of the Pd-proxy single point measured and recorded; the Fe run's
   stated bound (GPU-hours) written next to it. Owned lanes count as $0 cash
   (Reto 2026-10-29), so the bound is wall time, with the existing 4 h cap
   (`PRECIS_DFT_RELAX_TIMEOUT_S`) as the ceiling; if the proxy implies the
   cap is exceeded, stop and ask.
4. The result row for the slice-8 table: anchor (structure id, adsorbate,
   n), `E` for the four calculations, `Δγ0_spin`, `sigma_gamma` of that
   anchor, the verdict (Fe columns kept, or withdrawn and the table labelled
   MLIP-only), and job ids.

## Target + blast radius

`src/precis/workers/job_types/struct_relax.py` (params pass-through),
`src/precis_dft/_container/gpaw_relax.py` and `precis_dft/structures`
(magmoms, single point), tests beside them. Compute: ssh_node lane with
`has_gpaw` (`PRECIS_DFT_NODE`). Docs: slice-8 provenance row; the
`precis-structure-help` relax example if the `fidelity` string it uses
(`dft`) still maps to nothing.

## Open questions / decisions log

- **The compared quantity (decided, coordinator 2026-10-30).** The bar is
  `Δγ0_spin` (spin-polarised minus spin-paired at fixed geometry and
  functional) against the anchor's `sigma_gamma`; slice 8's text now says
  the same. A spin-polarised-DFT-vs-`mace:medium` `gamma0` gap would compare
  two functionals as well as magnetism (MACE misprices H* on Pd by about
  0.25 eV, open question 2), so it is reported beside the verdict as
  information only and voids nothing. `Δγ0_spin` itself needs no gas
  references for a non-magnetic adsorbate (they cancel); the informational
  gap does, under the same functional, and no code in the repo mints
  H2O/H2/O2 references today, so that row is reported only if the references
  are cheap to add, and its absence does not block acceptance.
- **Which anchor.** Known only after slice-8 stage 1 has scanned A; this item
  can build the machinery first and wait on the choice for the run.
- **Where it runs.** `PRECIS_DFT_NODE` has no default; a GPU lane with
  `has_gpaw` must be named, and the code comment in `struct_relax.py` that a
  37-atom spin-polarised relax takes about 47 h is not a measurement of a
  single point.
- **Functional.** Default RPBE LCAO dzp; whether the paper wants PBE (to match
  §22f's "PBE single points") is a decision for Reto.
