---
status: idea
title: an instrumented carbon reaction tunnel walks NO to NH3 station by station, and needs a seven-tool chain (T0–T6) to design
pillar: 3d-design
prio: low
---

# Instrumented reaction tunnel: NO → NH3 (bluesky)

Reto, 2026-10-04. This is the concrete instance of
[cnt-channel-staged-catalysis](cnt-channel-staged-catalysis.md), whose
reading list, findings and design principles still apply. It is
hypothetical by design: the aim is to find which geometry and energetics
are self-consistent, not to build it. Most of the value is the toolchain
(§Tooling), which serves any staged-catalysis channel.

## Design brief

**Structure.** Circular graphene tubes run between two graphene sheets. The
outer carbon is a rigid, unstrained bound. The gap between the sheets is an
H2 reservoir. H2 enters the tunnel through pores at set axial positions, so
pore placement meters H per station. Pores must be real openings (vacancy
rings of 8 or more atoms); a heptagon is closed to H2. A Y-junction on the
outside may relieve strain where the radius changes.

**Lining.** At each station, pendants (sugar rings, some lipids) form a
charge tunnel that changes down the tube and holds the substrate in pose.
Each pendant is anchored to two Y-junctions so it cannot rotate; a third or
fourth anchor pins tilt. That turns its fields into crisp functions instead
of ensembles. Pendants are designed to sit in their relaxed pose, with no
strain. A Pd cluster at a station is an option for splitting H2.

**Chemistry: the tethered hydroxylamine route.** N–O is not cleaved early,
because free O or OH attacks the carbon, worst at defects (pores, seams,
Y-junctions). Rule: reactive O/OH only when metal-bound, H-bond-caged in
the sugar lining, or moved on at once. The likely sequence is
NO → HNO → H2NO (or HNOH) → H2NOH → protonated H2NOH → N–O cleavage →
NH3 + H2O. That is five H, not three. Near cleavage, the wall's charge is
load-bearing: it stabilises ammonium/oxonium-like intermediates. Option
kept open: route the O to a separate bilayer instead of making water.

**Stations.** Each station is defined by its transition state (TS) to the
next step: coordinates, reacting geometry, charges. The pocket is shaped to
that TS, so it fits the path, not the molecule. A station is an ensemble
(conformers + vibration). All TSs are registered onto one tube axis,
anchored on N (it persists into NH3), with the reaction coordinate pointing
downstream. A station is four co-registered fields on the same surface:
steric envelope, electrostatic potential, frontier-orbital lobes
(HOMO/LUMO), H-bond topology. Wall compliance is ignored (rigid carbon).

**Representation.** Every field is a function on the tube surface of z
(axial) and θ (angular, periodic): a Fourier series in θ whose coefficients
are smooth in z (splines or Chebyshev). Symmetry shows as sparsity, a
drifting maximum as phase drift, and the whole is differentiable. Radius is
also a field, r(z, θ): the inner boundary of the lining, not the carbon.
The centreline stays straight. A bend the substrate feels comes from
lopsided lining thickness over a short z span; bends sit at H-delivery
stations and linkers are smooth runs. A curved centreline (or the
Weierstrass–Enneper smooth layer) only where a step needs a hard turn.

## Energetics so far

Tabulated gas-phase data, 298 K, H2O as gas (`chemicals` package; Hf from
ATcT/TRC):

| step | ΔH kJ/mol | ΔG kJ/mol |
|---|---|---|
| NO + 5/2 H2 → NH3 + H2O | −378.5 | −332.0 |
| NO + 3/2 H2 → NH2OH | −134.6 | (no S° for NH2OH in the table) |
| NH2OH + H2 → NH3 + H2O | −243.9 | — |
| NO + 1/2 H2 → HNO | **+15.8** | ≈ +32 (hand estimate) |
| H2 → 2 H | +436 | — |

What this says:
- The H–H bonds are already paid for inside the overall −332 kJ/mol ΔG per
  NO (−664 for 2 NO + 5 H2). The surplus after splitting H2 is large:
  about 3.4 eV per NO, roughly seven ATP equivalents.
- Run as an electrochemical cell (protons through the lining, electrons
  through the conducting tube), −332 kJ/mol over 5 e⁻ is E° ≈ 0.69 V.
- About two thirds of the energy is released at the cleavage step, so the
  tethered route stores its energy until the end. Cleavage is downhill by
  ~244 kJ/mol; its problem is the kinetic barrier, not the supply.
- The **first** step is uphill from H2 (+16 kJ/mol ΔH). Starting from an H
  atom it is −202. Station 1 needs either an H2-splitting site or a strong
  binding pocket for HNO.
- Using the surplus: spending it as a ratchet (every later step strongly
  downhill, so nothing goes backwards) is free. Harvesting it into work
  that drives the uphill first step needs explicit coupling, mechanical or
  electrical. The electrical route (the tube as a wire) is the plausible
  one.

## Tooling — the chain

| # | tool | item | input → output |
|---|---|---|---|
| T0 | reaction energetics ledger | [reaction-energetics-ledger](reaction-energetics-ledger.md) (draft) | balanced equation → ΔH/ΔG/E° per step, with the source per value |
| T1 | station path finder | [reaction-tunnel-station-path-finder](reaction-tunnel-station-path-finder.md) (draft) | intermediates → TS per step in four tiers, station records |
| T2 | instrumentable tube generator | [hexfold-instrumentable-tunnel](hexfold-instrumentable-tunnel.md) (idea, blocked on k = 3 seams) | tube spec → carbon net, attachment sites (z, θ, normal), pores |
| T3 | track designer | this file, below | T1 station records + T2 sites → target field stack in (z, θ), energy as one axis |
| T4 | ring/pendant designer | this file, below | anchor pair + target fields per ring → ranked pendants |
| T5 | assembled check | this file, below | tube + lining + substrate → T1 re-run inside the designed pocket |
| T6 | reaction movie | [reaction-tunnel-movie](reaction-tunnel-movie.md) (draft) | station records → four-pane MP4 + web scrubber |

**Order.** T0 → T1 tiers 1–3 (bare, implicit solvent, theozyme) and 3b
(imposed strain) → T3/T4 matching the theozyme → T5 → T6. T2 runs in
parallel on the hexfold side. T1 can run before T4 because the theozyme
tier stands in for the pocket: the TS is found among a few free model
groups, and T4's job becomes holding those groups in place
(inside-out enzyme design). The bare tier is only an upper bound on each
barrier; the theozyme tier is the earliest real answer to "does this need
a drive?". The strain tier (3b) turns "how strained should the frame be?"
into a stiffness and set-point T2 must deliver.

**Method trust.** ML potentials handle the carbon frame well. The reacting
molecule held under strain is their weak point: stretched bonds near
dissociation, systematic softening (strain energies and barrier lowering
come out too small), radicals and charged species (only UMA/OMol25 takes
charge and spin), and no long-range electrostatics past a ~5–6 Å cutoff.
So MLIP screens, GFN2-xTB covers charge and electrostatics, and DFT
checks each station's TS. T5 runs DFT on the reacting core and MLIP on the
frame (QM/MM). The ladder lives in the T1 spec.

**T3, track designer.** Builds the field stack (Fourier in θ × Chebyshev
in z) for r, steric, electrostatic potential, frontier orbitals and H-bond
topology. Stations are pinned from T1 records (theozyme group placements
are the targets); linkers are smooth interpolation. The energy profile
from T1 runs along z as one axis, so a bend, a pore and a barrier line up.
Differentiable, so station geometry can later be optimised by gradient
(skill `differentiation`). Uses `precis_surface` only where the
centreline must bend. Spec once T1's station record exists.

**T4, ring/pendant designer.** Inverse design per ring: two pinned anchor
sites from T2 plus the target fields at a spot from T3, which for a
theozyme station is "a hydroxyl here, pointing this way". Generators:
motif library and filter; LLM fragment growth from both anchors with
literature retrieval; a conditioned generative model, last. One verifier,
geometry first: relax, reject if the pendant does not span its anchors
without strain, then compute fields and rank by match. Each ring is
computed in a sliding window of about 3 rings with far rings as fixed
background, so cost grows linearly with length. The carbon wall conducts,
so it is an equipotential boundary, not part of the window. Spec once T3
exists.

**T5, assembled check.** Places the substrate in the assembled lining and
re-runs T1 per window (QM/MM). Does the designed pocket lower the barrier
the theozyme promised? Emits station records T6 can render.

## Open questions

1. Per-station barrier ledger (T1), especially N–O cleavage and the uphill
   first step. This decides whether a drive (H2 pressure gradient,
   azobenzene/rotary clock, or electrical) is needed.
2. Water versus routing the O to a separate bilayer.
3. Correlation length along the lining, which sets T4's window size.
4. Literature: reactive-O attack rates on defective or strained nanotube
   walls; electrochemical NO reduction to NH3 (Cu, Pt), whose hydroxylamine
   mechanisms give free energies to check T1 against.
5. H supply: H2 plus a splitting site at each station, or protons plus
   electrons through the tube. This changes which steps are uphill.

## Explicitly NOT in scope

- Synthesis routes. The design is hypothetical.
- Wall mechanics. The carbon is treated as rigid.
- Minting a quest. That waits on T0 + T1 results and on Reto.
