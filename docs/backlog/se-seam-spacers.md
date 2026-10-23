---
status: draft
title: Insulating seams — literature survey + demo that (CH2)n/C2H4 spacers make seam-local relax safe
pillar: 3d-design
prio: normal
model: opus
---

# Insulating seams: literature survey + demo

Reto, 2026-10-23: "We have insulating seams (like c2h4 spacers for dft) so
local chunks remain ok (for the backlog — lit search and demo to see these
spacers are ok!)". Owner thread:
[structure-workspaces](threads/structure-workspaces.md) (horizon).
Consumers: [structure-workspaces](structure-workspaces.md) §Relax
hierarchy (c) (seams, buffer radius) and
[se-flow-kernel](se-flow-kernel.md) slice 4b §Build-step stack cache
(seam-local relax above a `join`).

## Motivation / why

Pre-relaxed modules are the stored product (Reto 2026-10-23: "we can
benefit from module pre-relaxing"): each module relaxed once, capped,
under its key, so a join starts with far atoms already near their
minimum and only the seam buffer moves — free a buffer around the
declared seams, freeze the far atoms, check residual forces. That payoff
holds only if a seam exists that decouples the two chunks — otherwise
the force check fails on every join and the full relax of (c) runs each
time. Saturated linkers (C2H4, (CH2)n) are the assumed insulators. The assumption is unverified for our chunks and our rungs
(xtb, DFT), and electronic insulation says nothing about mechanical
insulation: mismatch strain crosses a spacer that decouples electrons.
This item turns the assumption into numbers: which spacer, how many CH2,
what buffer radius, and when the force check fires anyway.

## In scope

**Slice 0 — literature survey.** Run through the session MCP's
`perplexity-research` kind on prod once built (`put(kind=
'perplexity-research', ...)`); cite the resulting `perplexity-research:
<id>` ref(s) here. Questions, each answered with sources:

- Fragment methods and what they assume at the cut: FMO, ONIOM,
  subsystem/embedding DFT, link-atom (H-cap) capping — where the cut may
  be placed, the error per cut, how the buffer region is chosen.
- Decay of electronic coupling through saturated σ linkers: the
  attenuation factor β per CH2 (tunnelling through alkane bridges;
  superexchange), and how it compares with π/conjugated linkers.
- Mechanical/strain transmission through linkers: how far lattice or
  geometry mismatch strain propagates past a flexible saturated spacer,
  and what makes a spacer stiff enough to carry it.

Output: a short table (method · cut rule · buffer rule · error estimate ·
source) and a one-paragraph β summary with the cited range, written into
this file.

**Slice 1 — demo.** Two chunks joined by (CH2)n, n = 1..4, plus a C2H4
spacer as Reto named it; each computed at `xtb` (GFN2, the existing
`precis.structure.relax.relax` rung) and at one DFT level (GPAW on
`PRECIS_DFT_NODE`, the `dft-fast` rung). For each n compare local
observables on each chunk against the same chunk isolated and H-capped:
partial charges, bond lengths in the chunk, forces on chunk atoms after
the joined relax, and HOMO/LUMO localisation (fraction of the frontier
orbital on each chunk). Find the n at which every delta drops below its
threshold (charges 0.02 e, bond lengths 0.01 Å, forces 0.1 eV/Å — the
`relax.residual_fmax` of §Relax hierarchy (c) — and frontier
localisation > 0.9 on one chunk). A strained case — the two chunks with
a deliberate lattice-period mismatch across the spacer — shows the
mechanical caveat: the electronic deltas pass while the force delta on
the far chunk does not, and the seam-local relax's buffer-growth loop
catches it.

Chunks: two hexfold units from the library design (`precis_se/library.py`;
a `cccR` prism and a k3 seam fragment) so the demo uses the chunks the
assembly DAG will join. The demo runs as a `struct_relax` job
(`struct_runs` cache), not in a sandbox.

## Slice 0 results (2026-10-23)

Three reports on the prod `perplexity-research` kind:
perplexity-research:507382 (fragment/cut rules), :507385 (β per CH2),
:507387 (strain transmission). Papers cited as minted stubs; the fetch
worker enriches them.

### Cut rules and boundary errors

| Method | Cut rule | Buffer rule | Error estimate | Source |
|---|---|---|---|---|
| FMO (BDA/BAA) | localized saturated σ bond with sp³ bond-detached atom; never double/aromatic/conjugated bonds; disulfides intact | no cap; fragment holds the cut — ≥20–30 atoms or ~2 residues per fragment near hard cuts; FMO2 dimers / FMO3 trimers recover the cut terms | no transferable per-cut error; ≈1–3 kcal/mol whole-system (charged peptides ~6); gradients ≈0.026/0.176 eV/Å RMS/max vs full ab initio | perplexity-research:507382; pa507392 (Kitaura 1999) |
| ONIOM (link H) | single localized σ C–C at an sp³ atom, ≥3 bonds from any connectivity change (closer ⇒ discontinuous PES); no two cuts on one boundary atom | grow the model layer until relative energies stable (region-choice example: 1.53 → 0.31 kcal/mol); link H collinear at ≈1.09 Å | not per-cut: 0.3–1.5 kcal/mol with a good partition, ~4 kcal/mol with a poor medium layer; barriers 0.8–1.9, reaction energies 2.5–4.4 kcal/mol | perplexity-research:507382; pa507393 (Svensson 1996) |
| Subsystem DFT / FDE | no plain covalent cut (semilocal FDE fails across a bond); covalent cuts need 3-FDE conjugate caps or projection-based embedding | 3-FDE: complementary cap per severed bond (peptide unit + CH3), cap region within 3 bohr (1.6 Å) of cap atoms | FDE ≈0.5 kcal/mol MUE only for noncovalent dimers — no defensible bound for a covalent cut; projection embedding <1 kcal/mol with 3–4 embedded C atoms | perplexity-research:507382; pa507394 (Wesolowski–Warshel 1993), pa507395 (Jacob–Visscher 2008) |
| Link-atom / H-cap | only localized near-nonpolar C(sp³)–C(sp³); avoid amide C–N, polar/ionic, aromatic, metal bonds | H cap collinear at 1.09–1.10 Å; integer cap charge; ≥3 bonds from the functional group gives protonation-energy error <1 kcal/mol | untuned H-link deprotonation MUE 0.65–0.91 eV; charge-balanced 0.22–0.30 eV; tuned cap 0.056–0.17 eV; cap shifts up to ~0.3 e summed CM5 charge; placement ±0.01 ⇒ ±0.02 Å | perplexity-research:507382; pa507396 (Singh–Kollman 1986), pa507405 (Antes–Thiel 2000) |

The shared acceptance rule replaces "error per cut": **boundary
convergence** — move the boundary one bond out and require the target
relative energies, forces and charges stable to tolerance (typically 1
kcal/mol ≈ 0.043 eV). That is exactly the seam-local relax's
buffer-growth loop with `relax.residual_fmax`.

### β summary — electronic coupling across the spacer

Charge transfer across a saturated alkane bridge is off-resonant σ/σ*
superexchange (McConnell 1961, pa507389): k = k0 exp(−βR),
G = G0 exp(−βN). The measured consensus over five decades of
experiments is **β ≈ 0.8–1.2 per CH2 (≈ 0.63–0.95 Å⁻¹ with 1.27 Å per
all-trans CH2; ≈1 per methylene as the common number)**, against
0.1–0.5 Å⁻¹ for π-conjugated wires (perplexity-research:507385). The
spread is family, not noise: pulse-radiolysis rigid spacers 1.15 per
bond ≈ 0.91 Å⁻¹ (Closs–Miller, pa507397); photoinduced ET through 4–12
σ-bond bridges 0.92 per bond (Oevering et al. 1987, pa507398);
temperature-jump SAM rates 1.21±0.05 per CH2 (Smalley 1995, pa507399);
CP-AFM 0.94±0.06 Å⁻¹ with an oligophenylene control at 0.42±0.07 Å⁻¹
on the same platform (Wold 2002, pa507400); break junctions
0.72–0.84 Å⁻¹ (Venkataraman 2006, pa206). Rates drop a decade per
~2 CH2 and |H|² scales follow the rate β (k ∝ |H_DA|²), so the (CH2)n
ladder n = 1..4 covers roughly 1.5–6 decades of coupling — the right
bracket for slice 1's search of the n at which the chunk-local deltas
fall below threshold. β is silent about mechanics, though:

### Strain transmission — the mechanical caveat

There is **no static mechanical attenuation factor per CH2**
(perplexity-research:507387): a chain pulled taut is a series spring
carrying the full axial force (all-trans alkane axial ≈ 47 nN, modulus
≈ 305–309 GPa — pa507401 Radiom 2018, pa507402). What decays with
contour length is orientational *registry* (persistence length
l_p ≈ 0.3–0.7 nm ≈ 2–6 CH2), absorbed by tilt and gauche conversion
(first gauche ≈ 0.54–0.63 kcal/mol, pa507403 Klauda 2005; gauche-butane
≈ 6× softer axially than trans). One 7-Å flexible organic bilayer
absorbed 14.6% of epitaxial mismatch while the bonded inorganic slab
moved only 1.66% (Lei 2022, pa507404); in a packed framework, by
contrast, a transformation front ran 76 unit cells deep. Reads for the
seam: mechanical insulation needs *free torsions* in the buffer, not
length; a frozen far chunk stays force-coupled through a taut spacer;
and the slice 1 force delta can fail while every electronic delta
passes — the `residual_fmax` check is not optional.

Slice 1 parameter hints from the survey: the (CH2)n ladder n = 1..4
brackets the β decades; buffer radius ≥ 3 bonds past the seam (the
link-atom/ONIOM convergence rule) as the starting default to be
calibrated by measurement; thresholds in slice 1 stay proposals until
the demo numbers land.

## Explicitly NOT in scope

- Building the seam-local relax itself (structure-workspaces slice 4a)
  or the `join` step (se-flow-kernel slice 4b); this item supplies their
  numbers.
- Conjugated or metallic linkers; a second DFT functional; k-point
  convergence beyond the service's default rule.
- A general fragment-method implementation (FMO/ONIOM as a service).
  The survey names them; the seam-local relax stays our own fixed-mask
  relax with the force check.

## Acceptance criteria

- Slice 0: the table and β summary above are in this file with
  `perplexity-research:<id>` refs for every row.
- Slice 1: one table per n (and the C2H4 row) with the four deltas at
  both rungs, plus the strained-case row; a recommended default spacer,
  buffer radius and residual-force threshold for seam-local relax (the
  per-join record's defaults) written into
  structure-workspaces §Relax hierarchy (c) in the same commit, with the
  threshold each number clears; the strained case shows a force delta
  above threshold with the electronic deltas below it.
- The demo structures and runs are `struct_runs` rows reachable from
  this file's refs; no numbers live only in a chat.

## Target + blast radius

Docs and numbers only until slice 1 runs: `precis.structure.relax.relax`
(existing rungs, no change), `struct_relax` jobs on `PRECIS_DFT_NODE`,
the library design's units. Changes the defaults the seam-local relax
reads (`relax.scope: 'interfaces'` shell radius, `residual_fmax`) — the
blast radius is every post-join relax once slice 4a of
structure-workspaces lands.

## Open questions / decisions log

- 2026-10-23: filed on Reto's direction (verbatim above). Thresholds in
  slice 1 are proposals, to be set by the survey's error estimates.
- 2026-10-23: slice 0 done (survey §Slice 0 results; spend ≈ $6.4 on
  three perplexity-research calls). Slice 1 remains, needs the DFT node.
- Which DFT level — `dft-fast` as the service's default; a tighter level
  only if the xtb/DFT disagreement at the chosen n is above threshold.
