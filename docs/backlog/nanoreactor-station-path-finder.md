---
status: draft
title: the pathway engine finds each station's transition state off-slab — bare, solvated, theozyme and under imposed strain — and emits a station record per TS
pillar: 3d-design
prio: normal
model: opus
blocked-by: reaction-energetics-ledger
---

# Station path finder (T1 of the nanoreactor chain)

Umbrella: [nanoreactor-no-nh3](nanoreactor-no-nh3.md). Engine:
autocatpath (`../catpath`, reference engine), glue in `src/precis_pathway`.

## Motivation / why

The nanoreactor's stations are defined by their transition states, and the
pocket is shaped to each TS. The real barriers depend on the pocket, and
the pocket depends on the TS: a loop. Enzyme design breaks it with a
**theozyme**: compute the TS among a few idealized, unattached catalytic
groups, then build a scaffold that holds those groups (Tantillo, Chen &
Houk 1998; Röthlisberger 2008 Kemp eliminase; Jiang 2008 retro-aldolase;
stub these papers before `ready`). This item does that for the nanoreactor. It
also adds a frame-free strain tier from mechanochemistry, which answers
"how much pre-strain makes N–O cleavage easy?" before any frame exists.

autocatpath already runs NO→NH3 on Pd: climbing-image NEB (CI-NEB), a gas
ledger, the computational hydrogen electrode (CHE), and a spread across
several ML potentials. Every environment it has today is a metal slab.

## In scope

**Environments (new in the engine), in tiers:**
1. `gas` — bare molecule. The pessimistic ledger: an upper bound on every
   barrier.
2. `implicit` — continuum solvent with a set dielectric (xTB ALPB/GBSA, or
   the DFT solvent model), standing in for the lining.
3. `theozyme` — the TS plus a small set of free model groups chosen by the
   caller: a proton donor (OH, NH3+), a carbonyl or anion, and a Pd4
   cluster where H2 splits. Group positions are optimized together with
   the TS, under loose restraints that keep each group plausible as a
   pendant tip (distance from the axis, no group passing through the
   lumen centre).
   - 3b. `strain` — a restraint scan. Constrain N···O, or the N–O axis
     orientation, over a range, and re-find the TS at each setting
     (COGEF), or apply a constant force (EFEI). The output is barrier
     against imposed strain or force: the stiffness and set-point the
     frame must deliver.

**Steps:** both H-addition branches (NO → HNO → H2NO → H2NOH and
NO → HNO → HNOH → H2NOH), and N–O cleavage three ways (neutral,
N-protonated, O-protonated). H supply is priced two ways, H atom and H2
with its splitting cost charged separately; protons plus electrons go
through CHE at a stated U.

**Backends and validation ladder:**
1. MLIP: UMA (OMol25) only for this chemistry, since it takes charge and
   spin. Frame geometry, conformers, first scans. The engine's spread
   across potentials is the escalation signal.
2. GFN2-xTB (tblite): charged and radical species, scans that need
   electrostatics. Catches the softening of universal MLIPs on strained
   points (Deng et al. on systematic softening; stub before `ready`).
3. DFT check at each TS and strained minimum: a range-separated hybrid on
   the theozyme cluster (about 50–150 atoms). A station's barrier is
   reported at the highest rung that ran on it, never mixed silently.

**Station record (output contract)** per TS, stored on the pathway run:
- geometry: reactant, TS, product and the minimum-energy-path frames
  between them (about 30 per station), registered to the tube axis —
  origin on N, +z along the reaction coordinate;
- energies: ΔG, Ea, rung, and model spread;
- for each frame: charges, and cube files for the electrostatic potential
  and HOMO/LUMO (sign-aligned frame to frame);
- per-atom strain energy (MLIP), or bond deviation from relaxed values,
  labelled with which one;
- for theozyme stations: the model groups' positions, orientations,
  charges and frontier orbitals relative to the axis. This is T3/T4's
  target spec.

The frames and cube files are the per-frame data contract
[nanoreactor-movie](nanoreactor-movie.md) (T6) renders.

## Explicitly NOT in scope

- The real lining, carbon wall, or long-range wall polarization. That is
  T5 (QM/MM in the assembled nanoreactor).
- Fine-tuning an MLIP on the DFT points. That comes once a few hundred
  points exist.
- Microkinetics beyond the Eyring dwell time per station.
- Network autodetection beyond the two named branches.

## Acceptance criteria

- One run over the named steps returns a profile per tier (gas, implicit,
  theozyme), with Ea and rung per step and the largest barrier flagged.
- The gas tier's thermodynamic ends match T0 within the method's stated
  error, or the mismatch is reported.
- The strain tier returns Ea against N···O restraint for cleavage, with at
  least 8 points and the restraint energy per point.
- A theozyme run returns the model-group placements as a station record
  readable by T3.
- Each station record carries frames plus cube files that T6 can render
  without re-computation.
- An MLIP disagreement above a set threshold on any strained point
  escalates that point to xTB automatically, and records it.

## Target + blast radius

autocatpath: new environment types, restraint-scan driver, station-record
export (engine release plus wheel bump, chemistry thread owns the
contract). `src/precis_pathway`: persist and views for station records.
Skill `precis-pathway-help`. Compute: xTB runs locally; DFT checks go to
the cluster through the existing dispatch.
