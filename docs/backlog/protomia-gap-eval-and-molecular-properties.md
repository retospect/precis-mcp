---
status: draft
title: Eval Protomia hands-on, then close the molecular-property gap it exposes
prio: normal
---

# Eval Protomia hands-on, then close the molecular-property gap it exposes

Source for every capability claim below:
<https://aitomistic.com/protomia/docs/features> and `/docs/capabilities`,
read 2026-09-20. Claims are *their* marketing copy — Phase 0 exists to
check which of them hold.

**This is not a competitive item.** Précis has no buyers and is not chasing a
market; the reason to read another group's tool carefully is that it is a free
survey of what is reachable in this problem space and at what cost. Protomia is
useful here as a *measuring stick for our own hole*, and the hole is real
whether or not they exist.

## Motivation / why

Protomia Solution 2 (Aitomistic) is an LLM workbench over MLatom/PySCF/xTB/CP2K
that overlaps our `structure` lane and diverges sharply above it. Reading their
docs surfaced one structural hole on our side: our fidelity ladder
(`structure/relax.py::_RENTED_RUNGS` — `ff`/`xtb`/`ml`/`dft-fast`/`dft-tight`)
returns **geometries and energies only**. There is no Hessian anywhere in
`src/precis/structure/`, and therefore no vibrational, thermochemical, or
spectroscopic property at all. A user asking "what is the IR spectrum of this"
gets nothing from us today.

That gap is one Hessian away from machinery we already run: the ASE calculators
behind `relax.py::_ml_calculator` and the GPU compute job lane already produce
forces, and `structure/cache.py` already memoises them label-paired.

Their claimed capabilities we have **no** path to:

- Frequencies → IR + Raman, ZPE, thermochemistry.
- Excited states: UV–Vis vertical excitations, nuclear-ensemble band shapes,
  S₁/S₂ geometry *and* frequency optimisation, the Kasha emission workflow.
- Orbitals/densities as cube files (HOMO/LUMO/ρ).
- Transition-state *search* with the imaginary mode, plus IRC. (We have NEB
  barriers via autocatpath — `structure/ops.py` — i.e. the path, not the TS
  eigenvector or the IRC confirmation.)
- Molecular dynamics trajectories.
- Method breadth: AIQM1/2/3, UAIQM (near-full periodic table), OMNI-P2x,
  GFN2-xTB/ODM2*/OM2, PySCF WFT with arbitrary basis, CP2K band gaps. We ship
  MACE-MP / MACE-OFF23 / CHGNet / EMT / GPAW (`relax.py::route_ml_model`,
  `relax.py::_ml_calculator`).
- Structure intake: common name, SMILES→3D, `.pdb`/`.sdf`/`.mol`/`.gjf`, and a
  photo of a drawn structure. Our `structure/importers/` are external-DFT-DB
  adapters keyed on `(dataset, config_id)`; there is no name→3D or image→3D door.
- Gaussian input-file preparation for offline runs under the user's own licence.
- Word round-trip: EndNote-deterministic citation insertion, reviewer-response
  scaffolding from the editorial email with comments kept verbatim.

Where we are ahead (recorded so a later reader does not re-derive it): Protomia
is session-shaped. No persistent corpus, no finding/nanopublication provenance
with disputes, no autonomous job factory, no quests, no catpath retrosynthesis,
no design kinds (`se`/`cad`/`pcb`/supply). Its literature layer is
search-then-verify-the-DOI, a lookup rather than a corpus. None of that is at
risk; only the property ladder is.

## In scope

**Phase 0 — eval (do first; gates everything below).** Register on the free
tier at <https://aitomistic.xyz/>, run a fixed probe set, and write the result
up as a short findings note in this file:

1. Frequencies + IR on a small organic (benzene or similar) — does it return a
   real spectrum, and at what method/cost?
2. TS search + IRC on a textbook reaction — does the IRC actually confirm the
   endpoints, or is it a chained optimisation with a confident summary?
3. UV–Vis on a chromophore — vertical excitations vs nuclear ensemble.
4. Image→structure: photograph a drawn structure, check the geometry it reads out.
5. A periodic slab + adsorption through CP2K, for direct comparison with our
   `structure` lane.
6. Whether "prepares Gaussian input" means a validated deck or a plausible-looking one.

Each probe records: what was claimed, what came back, wall time, and whether an
unasked-for method substitution happened.

**Phase 1 — Hessian rung.** Add a vibrational-analysis rung over the existing
ASE calculators: finite-difference Hessian → normal modes, ZPE, thermochemistry
(RRHO), IR intensities where the calculator exposes dipole derivatives. New
compute job type alongside `workers/job_types/struct_relax.py`; results cached
like relax results, keyed on the same content address plus the displacement step.

**Phase 2 — structure intake.** name→3D and SMILES→3D behind
`handlers/structure.py::put`, plus `.pdb`/`.sdf`/`.mol` readers. The existing
"an uploaded structure is authoritative, never regenerated from a name lookup"
discipline must hold: a name-derived geometry is marked as such in provenance.

Phases beyond 2 (excited states, cube-file export, TS eigenvector + IRC,
element coverage past our current MLIPs) are deliberately left unscoped, and
the criterion for picking one up is **which of them unblocks open scientific
work** — a property a live quest, pathway or draft actually needs and cannot
currently get — not what a competitor advertises. Phase 0 tells us what is
reachable and at what cost; the ordering comes from our own queue.

## Explicitly NOT in scope

- Rebuilding Protomia's chat-workbench surface — drag-a-folder working
  directories, XYZ action buttons, save-chat-to-markdown, in-chat structure
  rendering as a product affordance.
- Word/EndNote round-tripping of a manuscript we did not author. Our authoring
  path stays draft-native.
- Licensing or shipping AIQM/UAIQM/OMNI-P2x, or adopting MLatom as an engine.
- Multi-tenant hosting, billing tiers, or a free public compute tier.
- Excited states and cube files in *this* item — deferred, not dropped.

## Acceptance criteria

1. Phase 0 written up in this file: six probes, each with claim / observed /
   wall time / substitution-or-not. Any claim that does not reproduce is stated
   plainly as not reproducing.
2. Phase 1: a frequency run on a small organic returns normal modes, ZPE and an
   RRHO thermochemistry block; the lowest three modes of a relaxed minimum are
   real (no spurious imaginaries beyond translation/rotation); a known TS
   returns exactly one imaginary mode.
3. Phase 1 results are cached and re-served on an identical second call, and a
   cross-method comparison is refused the same way
   `handlers/structure.py::guard_energy_comparable` already refuses a
   cross-fingerprint ΔE.
4. Phase 2: "benzene" and a SMILES string both produce a relaxable scene; an
   uploaded `.xyz` still wins over a name lookup; provenance distinguishes the two.
5. The owning package docstring (`src/precis/structure/__init__.py`) gains the
   new seam, and this file is deleted in the shipping commit.

## Target + blast radius

`src/precis/structure/` (new vibrational module; `relax.py` rung table;
`cache.py` keying; `importers/`), `src/precis/handlers/structure.py`
(`put`, `get` views), `src/precis/store/_structure_ops.py`,
`src/precis/workers/job_types/` (new job type next to `struct_relax.py`),
and the `structure` skills under `src/precis/data/skills/`.

## Open questions / decisions log

- **Q (Reto):** is Phase 0 worth paying for past the free tier, or is the free
  allowance enough for six probes? Free tier is one LLM and a limited weekly
  allowance; paid starts at 1,000 RMB/month academic trial.
- **Q:** finite-difference Hessian only, or analytic where GPAW/PySCF offers it?
  Finite difference is calculator-agnostic and works today for every rung;
  analytic is cheaper but engine-specific.
- **Q:** IR intensities need dipole derivatives, which MACE does not give us.
  Does Phase 1 ship modes+thermochemistry without intensities, or wait for a
  rung that can produce a spectrum?
- **Q:** does SMILES→3D pull in a new dependency? rdkit exists but only behind the
  `chem` extra (`pyproject.toml`, gating the autocatpath engine) — the
  `structure` core is numpy-only by design, so either the extra becomes
  load-bearing for a core intake path or the conversion routes through the
  compute job lane.
