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
answered. Questions 8 and 9 are decided (Reto, 2026-10-18); next is
slice 2, split into 2a and 2b below. Slices 3–5 wait on 2.

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
- **Pocket spec from surface (slice 2, shipped).** 2a: the four
  wall-side computers (`precis_se.properties.computers`), the
  atoms:/sites: region resolution (`SeTree.structures`), DRC's
  `measure_unmet`/`measure_uncomputable`, and core migration 0228
  seeding `electrostatic potential` (V) and `binding energy` (eV).
  2b: `mol:<block>@<clearance Å>`/`mpatch:<block>:<id>` tokens,
  `precis_se.properties.pocket_spec` (patch clustering + inversion),
  the handler-level `pocket_from_molecule` op, and the pocket
  `class_hash` (se migration 0022).
  shipped: 6955949c5 (2a), this commit (2b).

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

### 6. Categorical measure targets (follow-up, Reto 2026-10-21, later)

Slice 2 stores the `hydrophobicity index` requirement (`hydrophobic` on a
nonpolar patch) in the measure's reason text, because `add_measure`
takes only numeric targets/bands and the measurand is categorical
(allowed values hydrophilic / amphiphilic / hydrophobic, migration 0182).
DRC therefore cannot check it. Accepted for v1. This slice lets
`add_measure` carry a categorical target (one of the measurand's allowed
values, refused by name otherwise), gives `hydrophobicity index` a
computer in `COMPUTERS` (classification of the wall region's painted
polarity), makes DRC emit `measure_unmet` on a mismatch, and moves
`pocket_from_molecule` from the reason text to the target. Migration if
the measure row needs a text target column.

test: a categorical target round-trips through `view='pockets'` and
`view='ops'`; an allowed-value check refuses `nonpolar`; DRC reports
`measure_unmet` for a hydrophilic wall facing a nonpolar patch.

### 7. Station frame (follow-up, 2026-10-21)

**Station frame rule.** A pocket's name carries its own molecule's
canonical body frame → wall rotation
(`<wall>-<molecule>@z<z0>-<z1>r<a>-<b>-<c>`, integer Å and degrees, `m` =
minus, suffix omitted at zero); the tube's frame (θ = 0, z = 0) is fixed
by the product's canonical pose at its exit station (e.g. NH3 for
NO→NH3). Today the wall frame is the wall block's own pose frame; the
anchoring from the product's exit-station pose is not built:
tube frame from the product's exit-station pose.

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
   **Decided (Reto, 2026-10-18, "Q8 Good"):** `PocketSpec.shape` stays a
   string and gains one token `mol:<block>@<clearance>` (the offset vdW
   surface of that molecule block, world frame). A region facing a patch
   uses the selector `mpatch:<block>:<id>`; patch ids come from the
   stable clustering in `pocket_spec.py`.
9. **Dependencies.** Region-property slice B was blocked on
   `measures-substrate` (realised values in `measures` with a
   `subject_selector`), slice C on the class lattice. Build them first,
   or keep realised values on the se side until they ship?
   **Decided (Reto, 2026-10-18, "Q9: Sounds good"):** realised values
   stay on the se side (computers in `COMPUTERS`, signature
   `(tree, measure) -> float | None` in `precis_se/properties/__init__.py`);
   slice 2 does not wait on `measures-substrate` or the class lattice.
   The class hash is sha256 over the canonical region list, stored on
   the pocket and promoted to a `class` node when the lattice ships.
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

- **Decided (Reto, 2026-10-18), from the readiness judge's proposal:**
  Q8 and Q9 as above. Slice 2 fixes the migration number (next free core
  migration after the current head) and the default clearance (0 Å, Q4,
  provisional) as the slice-2 value.

- **Ready-check, slice 2 (2a + 2b), 2026-10-20:**
  - blocker: `mpatch:<block>:<id>` (Q8) is a new selector kind, but the grammar lives in `src/precis_se/datums.py` (`parse_selector`, `same_region`, canonical form, `resolve`, error roster) and `ops._vet_regions` calls `parse_selector` strictly; slice 2's "Owning code" lists neither `datums.py` nor `pockets.py`. A builder must guess its parse, canonical text, resolve behaviour (lenient like `atoms:`?) and DRC treatment (`datum_unresolved`).
  - blocker: the `mol:<block>@<clearance>` shape token (Q8) is refused today: `ops._vet_pocket_shape` sends anything but `hull` to `_validate_envelope`. Not in Owning code; clearance unit (Å vs m; se is metres) and negative/zero handling unstated.
  - blocker: `COMPUTERS` is never called. Only `is_checked` reads it (`drc.py`, `handler.py`); nothing evaluates `(tree, measure) -> float | None`, and `handler.py` prints `realised: —` always. Registering entries alone clears `measurand_unchecked` but checks nothing, and the test paragraph is satisfied by stubs returning None. Spec must say who calls computers (DRC? a view?), what a computed value is compared with, and what a computer returns on a wall with no bound atoms.
  - blocker: the wall-side computer is unspecified. "Slice 1's painter run on a wall cluster" needs a region-to-atoms mapping (`atoms:`/`sites:` do not resolve in `datums`, "deferred to the bound structure"), the cluster cut (slice 4 owns that helper), a charge source (GFN2 on a whole tube?), and what "net partial charge" / "H-bond donor count" of a region means. Two builders will produce different computers.
  - blocker: `hydrophobicity index` is categorical with allowed values hydrophilic/amphiphilic/hydrophobic (migration 0182); spec's required value `nonpolar` is not one, so the write is refused. Say which value (likely `hydrophobic`) or whether the painter's polarity class `nonpolar` maps to it.
  - blocker: "the opposite sign of the patch's ESP band" has no definition: no band edges, no patch ESP statistic (mean? extremum?), no magnitude rule, no unit/relation form for the `electrostatic potential` measure. Also "patches cluster adjacent points of the same class" is unspecified (adjacency radius, class key = hbond+polarity+ESP sign?, id numbering, "stable" under re-run). `surface.py` has no patch class today (only `AtomClass`/`PaintedSurface`); the patch type is new.
  - blocker: class hash storage. "Stored on the pocket" needs a new `PocketSpec` field, an `se_pockets` column (new se migration in `src/precis_se/migrations/`, latest 0021; spec says only one core migration), persistence code and `view='pockets'`/`ops` export (`tests/test_se_ops_export.py` `_VERDICTS`). Canonical region list (ordering, rounding of ESP bands, whether measures are included) is undefined, so "same pose, same hash" is not pinned; also whether hash covers clearance/charge/multiplicity.
  - blocker: op shape. `pocket_from_molecule` is not registered anywhere; it needs GFN2 and a bound-structure read, so it is handler-level (`atomic/apply.py::HANDLER_LEVEL_OPS`, like `bind_structure`) not a pure `_OPS` entry, contradicting "writes through the existing `add_pocket` and `add_measure` paths" in `ops.py`. Arg names (`molecule`, `wall`, `station`/z-window form, `clearance`, `name`, charge/multiplicity) are not given, and the station z-window has no stated effect on output (Q1 open). "Named for the station" has no naming rule.
  - advisory: next free core migration is 0228 (head 0227_card_description_chunk_kind.sql; 0182 is the seed model, INSERT-missing-only, `proposed` status, needs `measurand` start node). Spec should name 0228 and the units: V for ESP, eV for binding energy, dimension_kind/si_vector.
  - advisory: `binding energy` is seeded in 2a but nothing in slice 2 uses it (slice 3 does); `electrostatic potential` is used for patch bands only.
  - advisory: "DRC on a wall with no computed values reports no `measurand_unchecked`" is trivially true once keys are registered; restate as "reports it for a control measurand with no computer" plus a per-measurand check of all three.
  - advisory: no posed-NO fixture with a tube wall block exists. `tests/test_se_molecule_surface.py::_design(store, slug, elements, coords)` builds only a single molecule block `mol` (NO coords `_NO`, real-tblite cases skip without tblite). The wall block and its binding must be built by the slice-2 test; say how (a plain block with an envelope, no atoms, suffices for region round-trip but not for any computer).
  - advisory: 2a and 2b are separable commits: 2a (migration + computers) has no code dependency on 2b, 2b's regions only reference the seeded measurands. The Q8 tokens (blockers 1-2) belong to 2b; the COMPUTERS-invocation gap (blocker 3) to 2a. Not a scope split needing new backlog files.
  - advisory: model is `opus`; fine for the tier. Skill `precis-se-regions-help` update is in the blast radius but absent from slice 2's Owning code.
