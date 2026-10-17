---
status: in-progress
title: pocket from molecule — paint a posed molecule's surface with properties, invert it into a region-property pocket spec, realise a tube wall that meets it, refine GFN2 → MLIP → DFT
pillar: 3d-design
thread: nanoreactor
prio: high
model: opus
---

# Pocket from molecule

Reto, 2026-10-17. First concrete driver of
[se-intent-to-realize-loop](se-intent-to-realize-loop.md); it fills
slices B and C of [se-region-property-layer](se-region-property-layer.md)
(slices 1–2 below). Umbrella: [nanoreactor-no-nh3](nanoreactor-no-nh3.md).

**Status (2026-10-17):** slice 1 is built (its section below says
what); its surface settings are provisional until open question 4 is
answered. Next: slice 2, which needs open questions 8 and 9 answered
first. Slices 3–5 wait on 2.

## Use case — the nanoreactor tunnel

NO enters a tunnel, so the tunnel wall must carry the properties NO
likes. The wall is then strained (squeezed or pulled: mechanochemistry)
so the bound species is primed to take an H. The H is delivered, the
wall twists, and so on. Each step is a **station** along the tube, and
each station has its own pocket spec.

A station here is the se-walker idea applied to the tube: a declared
state along a track (`precis-se-walker-help`: `declare_states`,
`view='stations'`). The tube block is the track. Each station is a
z-window on it, and the window's pocket spec is the target for that
step. How far the walker ops are reused is open question 1.

Two pocket cases:

- **Case a, attractive pocket.** Pose a molecule (NO). Compute its
  surface and paint it with properties. Invert the painted surface into
  a pocket spec that attracts the molecule.
- **Case b, held shape.** Pose NH3, remove one H, and keep the remaining
  NH2 frozen at the NH3 geometry. The pocket must hold that shape and
  keep the vacancy open toward the H source. This is transition-state
  (entatic-state) stabilisation, a concrete instance of
  [ts-stabilizing-pocket](ts-stabilizing-pocket.md).

## What exists

- **Pockets and region measures (slice A, live).**
  `src/precis_se/pockets.py::PocketSpec` names a set of region selectors
  plus a shape. A region's properties are ordinary measures whose datum
  is the region selector (`src/precis_se/pockets.py::region_measures`).
  Measurands come from the taxonomy
  (`src/precis_se/properties/measurand.py::measurand_resolver`; seeds in
  core migration `0182_se_measurand_seed.sql`: net partial charge,
  surface charge density, electric field magnitude, H-bond donor count,
  H-bond acceptor count, hydrophobicity index, dipole moment, contact
  angle). There is no electrostatic-potential or energy measurand.
- **Computer registry, empty.**
  `src/precis_se/properties/__init__.py::COMPUTERS` maps a measurand to
  the function that computes its realised value. It is empty today, so
  DRC reports `measurand_unchecked` for every non-geometric measure.
- **GFN2 (tblite).** `src/precis/structure/relax.py::_xtb_calculator`
  builds `TBLite(method="GFN2-xTB", electronic_temperature=1000.0)` and
  `src/precis/structure/relax.py::_relax_xtb` relaxes on it. Since
  slice 1 both take `charge` and `multiplicity`, and so does
  `relax(fidelity='xtb')` (slice 1 below). The structure handler and
  the `struct_relax` worker still pass the defaults.
- **MLIP (MACE).** The `ml` rung of
  `src/precis/structure/relax.py::relax` uses MACE.
  `src/precis/structure/relax.py::route_ml_model` routes organic
  compositions to MACE-OFF23. The quest's `reaction_config.mlip` block
  (`backend`, `model` or `models`, `device`, `dtype`) is how autocatpath
  runs MLIPs (`src/precis/quest/compute.py::_autocatpath_mlip_specs`,
  `src/precis_pathway/runner.py`). MACE takes neither charge nor spin.
- **DFT (GPAW).** Two paths: the `dft-fast`/`dft-tight` relax rungs
  dispatch a `struct_relax` job to the GPU node (`precis-structure-help`
  §Relax — the GPU dispatch path), and autocatpath reads a `dft` key in
  the quest reaction config
  (`src/precis/handlers/quest.py::_REACTION_CONFIG_KEYS`). Both are
  slab-oriented today.
- **Molecule posing.** The structure op `from_smiles` (rdkit ETKDG)
  mints a fragment; an se atomic block binds to it.
- **Tube.** `src/precis_se/atomic/generators/tunnel.py::build_tunnel_reservoirs`
  builds the tunnel with a site list (z, θ, inward normal);
  `src/precis_se/atomic/generators/tunnel.py::tunnel_strain` re-scores
  junction and seam strain on any coordinates.
- **Seam catalogue.** `src/hexfold/catalogue.py::SeamMotif` and
  `src/hexfold/catalogue.py::EdgeMotif` key joinable geometries. They
  carry geometry and hybridisation, not chemistry.
- **Gap field.** `src/precis_se/atomic/clearance.py::gap_field` measures
  nearest gaps between two atom clouds.
- **Molecule surface (slice 1).**
  `src/precis_se/properties/surface.py::paint_surface` samples a
  scaled-vdW surface and paints it; there is no SAS or SES code.

## Slices

### 1. Molecule surface with properties — built 2026-10-17

What it does is in the docstrings of
`src/precis_se/properties/surface.py` (the painter and the read) and
`src/precis/structure/relax.py::_xtb_calculator` (charge and
multiplicity), and in skill `precis-se-regions-help` §Paint a
molecule's surface. In short:

- `relax(fidelity='xtb', charge=, multiplicity=)` hands both to tblite.
  The default stays neutral with multiplicity undeclared (zero unpaired
  electrons, what every run got before). A declared multiplicity that
  does not fit the electron count is refused
  (`src/precis/structure/relax.py::check_charge_multiplicity`). Other
  rungs refuse a non-default charge or multiplicity.
- `get(kind='se', view='surface', args={'block', 'charge'?,
  'multiplicity'?, 'scale'?, 'density'?, 'format'?})` runs one GFN2
  single point on read and returns the per-atom table, or with
  `format='json'` every point: position and normal in the se world
  frame, owning atom, ESP (V), H-bond class and direction, polarity.
  The JSON is the payload the se 3D viewer leaf owes rendering; the
  rendering is se-3d-viewer's (`src/precis_web/blocktree_3d.py`).
  Omitted charge is the sum of declared atom charges; omitted
  multiplicity is the lowest the electron count allows, labelled as a
  default.
- Per-atom spin population: tblite returns none for non-spin-polarised
  GFN2, and the read says so.

Tests: `tests/test_se_molecule_surface.py`,
`tests/test_structure_relax_xtb.py` (OH- charged, NO and NO+ open-shell
and charged; the real-backend cases skip without tblite).

Measured 2026-10-17 (tblite on macOS): NO relaxes to 1.155 Å as a
doublet, NO+ to 1.052 Å. At 1.15 Å, NO's GFN2 energy is the same
with zero unpaired electrons (the old default) and as a doublet
(−185.8907 eV): without spin polarisation the two half-filled pi*
orbitals carry the same total density either way. Spin-polarised GFN2
(`spin_polarization=1.0`) lowers it by 0.196 eV. Whether slices 3–5
turn spin polarisation on for doublets is open question 12; where the
read can run in prod is open question 13.

### 2. Pocket spec from surface (case a)

**Owning code:** new `src/precis_se/properties/pocket_spec.py`; a new se
op `pocket_from_molecule` in `src/precis_se/ops.py` that writes through
the existing `add_pocket` and `add_measure` paths; one forward-only core
migration seeding the measurands `electrostatic potential` and
`binding energy`.

**Input:** a design, the molecule block, the wall block (the tube),
the station z-window, and a clearance in Å.

**Output:** one pocket on the wall block, named for the station:

- shape: the molecule surface offset outward by the clearance;
- one region per surface patch (patches cluster adjacent points of the
  same class), each carrying measures: the opposite sign of the
  patch's ESP band; for each acceptor, H-bond donor count ≥ 1 on the
  wall opposite it; for each non-polar patch, hydrophobicity index
  `nonpolar` opposite it.

This is the region-property layer's slice C: the canonicalised region
set is hashed into a `class` node. Slice 1's painter, run on a wall
cluster instead of a molecule, is the layer's slice B: it registers in
`COMPUTERS` as the computer for net partial charge, H-bond donor and
acceptor count, and electrostatic potential, so DRC checks those
measures instead of reporting `measurand_unchecked`.

test: `tests/test_se_pocket_from_molecule.py` (new) — on posed NO,
`pocket_from_molecule` writes a pocket whose regions round-trip through
`view='pockets'` and `view='ops'`; every acceptor patch has a donor
requirement facing it; re-running on the same pose yields the same
class hash; DRC on a wall with no computed values reports no
`measurand_unchecked` for the three computed measurands.

### 3. Constrained-shape spec (case b)

**Owning code:** `src/precis_se/properties/pocket_spec.py` (a held-shape
mode of `pocket_from_molecule`).

**Input:** the parent molecule (NH3), the label of the removed atom,
charge and multiplicity of the fragment (NH2: 0 and 2), the wall block,
station window and clearance.

**Output:**

- strain energy at GFN2: E(NH2 frozen at the NH3 geometry) minus
  E(NH2 relaxed with `_relax_xtb`), both doublets;
- the vacancy direction: the unit vector from N along the removed N–H;
- a pocket like slice 2's, plus steric wall regions that hold the
  frozen geometry, H-bond partner positions, a keep-open cone along the
  vacancy direction (no wall atom inside it), and a `binding energy`
  measure whose band requires at least the strain energy as
  stabilisation.

**Requirement (Reto 2026-10-17, question 11):** the band is on the
H-addition step, not plain binding: NH2 + H in the pocket must be
downhill and low-barrier versus free (reaction energy and barrier, both
compared with the free case). Exact form open pending literature
(papers queued 2026-10-17); the binding-energy measure above is a
placeholder until then.

test: `tests/test_se_held_shape_pocket.py` (new) — NH3 minus one H
gives a positive GFN2 strain energy; the vacancy direction is within 1°
of the removed N–H; the pocket carries the keep-open region and the
binding-energy requirement; a wall atom placed inside the cone is a DRC
error naming the region.

### 4. The realize loop

**Owning code:** new `src/precis_se/pocket_realize.py`, the first
implementation of `se-intent-to-realize-loop`'s pick → join → check
loop; a cluster-cut helper beside
`src/precis_se/atomic/clearance.py::gap_field`.

**Input:** a pocket spec from slice 2 or 3; a candidate set (open
question 2); an iteration cap.

**Candidates (Reto 2026-10-17, question 2):** per station, choose R
groups (hydroxyl/cyclodextrin-like hydrophilic, alkyl hydrophobic) and
their positions on a wall-tethered carbon-ring scaffold with CH2
separators. N/B substitution and pore-rim termination are alternatives,
not v1-required.

**Steps:** for each candidate wall, cut a cluster (wall atoms within a
radius of the pocket, dangling bonds H-capped), place the molecule in
the pocket, relax at GFN2 with the far wall pinned, then score:
binding energy, each region's computed value against its band (slice 1
computers), and for case b the RMSD of the held fragment from its
frozen shape after the relax. Rank, keep the best, adjust the candidate
and repeat until the score stops improving or the cap is hit. A run
that ends without a candidate inside every band reports which regions
failed.

**Output:** a ranked candidate list with per-region mismatch, the
energies, and provenance per pick (candidate source, spec class hash,
iteration).

This is a research loop, not a guaranteed solver.

test: `tests/test_se_pocket_realize.py` (new) — on a fixture of three
hand-built wall patches (bare graphene, N-doped, OH at a pore rim) and
the NO pocket spec, the loop ranks all three, terminates within the cap,
names the failing regions of the losers, and every result carries its
provenance.

### 5. Refinement ladder ("it'll be wrong first")

**Owning code:** `src/precis_se/pocket_realize.py`; MLIP through the
`ml` rung of `src/precis/structure/relax.py::relax` with the quest
`reaction_config.mlip` block's backend/model fields; DFT through GPAW
(path in open question 5).

**Steps:** GFN2 screens and ranks all candidates. MACE relaxes and
re-scores the top N. GPAW checks the best few. Where a level disagrees
with the level below (a rank swap, or an energy gap above a threshold),
the disagreement corrects the pocket spec and the loop re-runs at GFN2.

**Output:** per candidate, the binding and strain energies, geometry and
method/model/version at every level it reached, plus the level-to-level
differences. The cheap level's error is measured from these rows, not
assumed.

test: `tests/test_se_pocket_refine.py` (new) — with MLIP and DFT
stubbed to fixed offsets, a candidate carries energies at all three
levels; a stubbed rank swap between GFN2 and MLIP triggers one spec
correction and one GFN2 re-run; the stored rows give the GFN2 error per
candidate.

## Honest limits

- GFN2 is a screen. Any claim about energies, N–H bond formation or
  radical behaviour needs DFT.
- Slice 4 is a research loop. It can end without a candidate.
- MACE has no spin or charge input. Its accuracy on radicals and on
  strained carbon must be checked against DFT on a few cases before it
  ranks anything.

## Explicitly NOT in scope

- The strain tier itself (barrier against imposed N···O restraint) —
  [nanoreactor-station-path-finder](nanoreactor-station-path-finder.md)
  tier 3b. This item takes its set-points as station inputs.
- Dynamics, solvation, transport of NO along the tube.
- The DFT engine and the GPU dispatch; this item calls them.
- Rendering code in `precis_web`.

## Acceptance

- NO gives a pocket spec for a tube wall, with at least one realised
  candidate scored at GFN2, MLIP and DFT.
- The NH2 held-shape spec carries its strain energy at GFN2 and at DFT.

## Target + blast radius

`src/precis_se/properties/` (surface, pocket spec, computers),
`src/precis_se/ops.py` and `src/precis_se/handler.py` (one op, one
view), `src/precis_se/pocket_realize.py`,
`src/precis/structure/relax.py::_xtb_calculator` (charge and
multiplicity; every existing caller keeps neutral singlet defaults),
one core migration for two measurand seeds,
`tests/test_se_ops_export.py`'s `_VERDICTS` map for any new `SeBlock`
field, skills `precis-se-regions-help` and `precis-se-atomic-help`.

## Open questions

1. **Stations.** Reuse the walker's `declare_states` (one state per
   station, tube as track) or name pockets by station z-window only?
   The walker ops assume DNA footholds and leg occupancy.
2. **Candidate set for slice 4.** The seam catalogue and pattern groups
   carry geometry, not chemistry; hexfold has no per-atom chemistry
   channel. The wall's charge and H-bond partners must come from
   somewhere: N/B substitution, pore-rim termination (OH, NH2,
   carbonyl), or pendants (nanoreactor-no-nh3 §T4). Which ones are in
   v1, and who generates them?
   **Answered (wall chemistry).** Reto 2026-10-17: attach functional
   groups to the inside of the tube: hydrophilic "sugar" bits (hydroxyl,
   cyclodextrin-like) and hydrophobic "oil" bits (alkyl). Scaffold: a
   carbon ring held inside the tube, inward-facing active groups on CH2
   separators, e.g. CH2-CH(R)-CH(anchor-to-wall)-CH2-CH(R)-CH2-…,
   anchored to the wall at some positions and carrying R pendants at
   others. Design-and-simulate only; no synthesis route yet. N/B
   substitution and pore-rim termination stay as alternatives, not
   v1-required. Consequence: slice 4 (see there).
3. **Overlap with T3/T4.** nanoreactor-no-nh3 plans T3 (a field stack
   in z, θ from station records) and T4 (pendant designer). Does this
   item replace T3 for the first stations, feed it, or sit beside it?
   Station records already promise ESP cubes and H-bond topology.
   **Partly answered.** Reto 2026-10-17: a stack of tethered rings
   along z is the long-run form of the T3 field stack
   (nanoreactor-no-nh3), each ring defining what the molecule sees at
   its station. This item feeds T3/T4 rather than replacing them.
4. **Surface parameters.** vdW versus SES, the radius scale, probe
   radius, point density, and the default clearance.
   **PROVISIONAL (builder pick 2026-10-17, not Reto's answer).** Slice 1
   runs on these until Reto rules; the surface values live in one
   place, `src/precis_se/properties/surface.py::PROVISIONAL_SETTINGS`,
   and `scale`/`density` can be overridden per read.
   - Surface: union of scaled vdW spheres, no SES. It needs no probe,
     every point sits on one atom's sphere (so its owning atom, and the
     classes copied from it, are unambiguous), and the slice-1 test is
     stated against it. SES would mostly smooth crevices a wall atom
     cannot reach anyway.
   - Radii: Bondi, the table `src/precis_se/atomic/clearance.py` already
     measures gaps with, so a surface and a clearance read agree.
   - Scale 1.0: the contact surface itself. Slice 2 offsets the pocket
     by its own clearance, so a scale above 1 would count the gap
     twice.
   - Probe: none (follows from no SES).
   - Density 3 points/Å²: about 0.6 Å between points, finer than
     H-bond geometry, and 123 points on NO, 110 on water, so the JSON
     payload stays small. At least 12 points per atom.
   - Clearance (slice 2's default, not yet in code): 0 Å, meaning wall
     atoms' vdW spheres touch the molecule's. H-bond contacts sit
     inside that and steric ones near it; slice 2 should revisit this
     once a wall exists to test against.
5. **DFT path and settings.** `struct_relax` `dft-*` rungs or
   autocatpath's `dft` config; functional (station-path-finder names a
   range-separated hybrid; GPAW's hybrid support is limited), spin
   polarisation for doublets, cluster size.
6. **MLIP choice.** Reto named MACE (autocatpath's `mlip` config);
   station-path-finder names UMA (OMol25) for this chemistry because it
   takes charge and spin. MACE-OFF, MACE-MP or UMA for the
   molecule-in-wall complex?
7. **Spec correction rule.** How a level disagreement changes the spec
   (shift a band, change the clearance, add a per-term offset fitted
   from the pairs), and the thresholds for "disagrees" and for loop
   convergence.
8. **Shape grammar.** `PocketSpec.shape` accepts a cad envelope string
   or `hull`; an offset molecular surface is neither. Regions facing a
   molecule patch have no selector form either (`patch:` is on a cad
   face, `atoms:`/`sites:` name wall atoms that do not exist yet). New
   tokens, or a different anchoring?
9. **Dependencies.** Region-property slice B was blocked on
   `measures-substrate` (realised values in `measures` with a
   `subject_selector`), slice C on the class lattice. Build them first,
   or keep realised values on the se side until they ship?
10. **Viewer payload.** Whether the vendored three-cad-viewer colours
    per vertex, or the surface needs the shared molecule renderer
    ([se-structure-shared-molecule-viewer](se-structure-shared-molecule-viewer.md)).
11. **Case b stabilisation.** Is "at least the strain energy" measured
    as binding of the held fragment, or as the margin between held and
    relaxed NH2 in the same pocket (ts-stabilizing-pocket's differential
    form)?
    **Answered (requirement).** Reto 2026-10-17: the pocket should
    mechanochemically entice addition of the missing H: the held,
    strained NH2 should make H uptake downhill and low-barrier. The
    requirement is on the H-addition step (reaction energy and barrier
    for NH2 + H in the pocket versus free), not plain binding energy.
    Exact form open pending literature (papers queued 2026-10-17).
12. **Spin polarisation for doublets.** Plain GFN2 with a multiplicity
    only changes occupations (slice 1, measured on NO). tblite's
    spin-polarised GFN2 gives a spin density and different energies.
    Turn it on for NO and NH2 in slices 3–5's strain and binding
    energies, or leave open-shell energetics to the DFT rung?
13. **Where the surface read runs in prod.** The MCP container is
    linux-aarch64 and has no tblite (no PyPI wheel; only the compute
    hosts install the wheelhouse build), so `view='surface'` answers
    `Unsupported` there today. Options: put the wheelhouse tblite wheel
    in the MCP image; dispatch the single point as a job and read the
    stored result; or a cheaper charge model (Gasteiger, EEQ) as a
    labelled fallback. Deploy-side, so it is Reto's call.
