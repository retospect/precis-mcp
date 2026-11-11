---
status: draft
title: Interface-first protein design with a non-repeating contact pattern
pillar: 3d-design
thread: se-machine-design
---

# Interface-first protein design

Reto, 2026-10-31: specify a surface, make the mating pocket first, then
build a rigid protein behind it. Use disulfides and explicit charge
partners. Avoid folding predictions; design a strongly preferred energy
minimum using non-repeating geometry and chemistry. Consider aperiodic
tilings, the Teglon idea, and patterns at several scales.

```text
target surface → complementary contact pattern → fixed functional groups
  → connected protein backbone → packed core + crosslinks + charge network
  → competing arrangements + physical energy checks → candidate and evidence
```

The interface is the invariant; scaffold construction may fail rather than
move the contacts to accommodate an easy fold. “Pocket” uses se's existing
region vocabulary: it can be an open, flat or convex binding face.

## Software build draft

**First working version:** a Python workspace program that accepts a
target and interface specification, constructs bounded three-helix protein
candidates, and exports structures, sequences and check reports. It uses
the existing se/structure viewer. Physical screening is a separate explicit
operation, with results attached to the exact candidate it evaluated.
All modules and APIs in this section are proposed.

### Inputs, outputs and entry points

Inputs are a protein-aware target snapshot (or PDB/mmCIF plus an explicit
model/alternate-location selection), the interface intent, a versioned
fragment library and an evaluation/budget profile. A shape-only target
can exercise geometry; chemical and physical checks retain their missing
coverage.

The output is zero or more candidates, each with atomic coordinates,
sequence, residue/atom mapping, peptide and disulfide connectivity,
constraint assignments and diagnostics. Export mmCIF and FASTA from that
same record. The workspace run also carries search coverage, provenance
and any completed physical receipts. `no_candidate` names the blocking
contacts or constraints; a work cap returns best-so-far candidates with
incomplete coverage.

| Proposed entry point | Responsibility |
|---|---|
| `protein.prepare_target(source, selection)` | Parse bounded local input, preserve protein identity, resolve the selected conformer and produce an immutable `TargetSnapshot`. No network acquisition. |
| `protein.build(spec, target, library, budget)` | Pattern search, backbone construction, packing and bracing; returns `BuildResult`. No store access or physical-screening job submission. |
| `protein.check(candidate, spec, target)` | Shared geometry/chemistry checks; the same checks run before export and at trusted import. |
| `precis_bio.design.evaluate(request)` | Explicit OpenMM challenge of pinned candidates; returns `ScreenResult`. Called by the compute runner. |
| Workspace export/import | Extend `Run.block` and the existing importer to carry protein metadata, then bind the candidate as an ordinary atomic se block. |

### Data contracts

Use versioned typed records with canonical serialization; keep coordinates
and actual covalent bonds in the existing `Geom.scene`.

| Record | Contents that must survive every boundary |
|---|---|
| `TargetSnapshot` | Atom graph, residue/atom identity, selected model/conformer, source revision, coordinate frame, chemical-state assignment and content digest. |
| `ProteinDesignSpec` | The [intent record](#intent-record), with normalized units and explicit unresolved fields. |
| `ProteinCandidate` | `Geom`, residue-to-atom-label map, ordered residue identities, sequence derived from them, constraint assignments, construction stage and provenance. |
| `BuildResult` | Candidates, diagnostics, visited/pruned search counts, remaining bounds and completion reason: exhausted domain, work cap, cancellation or error. |
| `ScreenRequest` / `ScreenResult` | Frozen candidate/target identities, model/profile, predeclared challenges, per-case energy and force residuals, basin assignments and coverage. |

Keep author and label residue ids, insertion codes and atom names distinct
from `structure` atom labels. Any flattening/relabeling must update the
map. Tests must cover transforms and cache round-trips, not only initial
construction.

`src/precis_geom/out.py::Run.block` currently emits atomic extXYZ plus
bond data. Add a versioned, manifest-hashed
`blocks/<name>.protein.json` for residue identity and constraint mappings;
do not duplicate coordinates there. The writer, step cache and trusted
importer validate the map against the actual atom and bond graph.

Reuse `src/precis_diag/__init__.py::Diag` and
`src/precis_diag/__init__.py::RunResult`. Protein check states map to
`ok`, `error` or `not_checked`; unsupported and uncomputed reasons stay
distinct in the diagnostic data. Findings name the contact, residue or
crosslink plus its construction frame and a next action.

### Modules and dependencies

| Proposed home | Responsibility |
|---|---|
| `precis_geom.protein.model` / `.io` | Records, protein-aware input/output and atom-map validation. Gemmi is the proposed PDB/mmCIF parser; do not extend the current element-only whitespace reader into a second CIF parser. |
| `precis_geom.protein.pattern` | Finite matching, wrong-arrangement generation, bounds and counterexamples. |
| `precis_geom.protein.build` | Local fragment library, inverse rotamers, backbone closure, sequence packing and crosslink selection. |
| `precis_geom.protein.checks` | Geometry, stereochemistry, packing, charge, disulfide and interface checks shared with se. |
| `precis_bio.design` | OpenMM adapter, challenge generation, minimization and evaluation receipts. This code has no dependency on the existing folding engines. |
| `precis_se.protein` | Intent ops/persistence, result rendering, stale-evidence checks and the proposed `se_protein_screen` job adapter. |

NumPy, SciPy and RDKit already ship in `packaging/geom/pyproject.toml`.
Add Gemmi to the distributions that expose protein file I/O. OpenMM and
its parameter files belong in the compute environment, loaded only by
the evaluator. Keep the workspace wheel independent of `precis_bio`,
store and worker modules; extend its import-contract tests for the new
subpackage rather than widening the permitted server imports.

Use the existing compute-lane
`src/precis/workers/job_types/__init__.py::JobTypeSpec` and job-result storage.
`se_protein_screen` parents on the se design and consumes submission-time
snapshots. A changed live design makes the old receipt historical; dispatch
must not silently replace its inputs with the latest slug contents.
Retries reuse matching completed cases; cancellation preserves completed
receipts and marks unfinished obligations uncomputed. A completed job can
validly report that every candidate failed.

### Build order and exit criteria

| Stage | Deliverable | Exit criterion |
|---|---|---|
| 1. Identity and artifacts | Typed records, target reader, residue mapping, serialization and frozen fixtures. | Input → transform → cache → export/import preserves residue identity, bond topology, units and provenance; ambiguous input refuses explicitly. |
| 2. Mathematical pattern solver | Finite search and counterexample reports. | Exhaustive small fixtures agree with independent enumeration; repeated patterns, renaming tricks and truncated searches cannot manufacture a uniqueness pass. |
| 3. Backbone constructor | Inverse rotamers, helix placement and short-loop closure. | A known reachable interface produces a connected, stereochemically valid backbone; an impossible interface returns named conflicts without moving its anchors. |
| 4. Packing and bracing | Residue/rotamer assignment, buried-polar checks, charge partners and two-disulfide topology. | A controlled feasible fixture satisfies every construction constraint; clash, charge and crosslink controls fail for the right reasons. |
| 5. Physical screen | OpenMM adapter plus apo/bound competitors and recovery probes. | Unit/gradient and topology controls pass; unconverged or missing competitors remain uncomputed; reports contain actual model results without native-state bonuses. |
| 6. se integration | Intent persistence, workspace metadata support, job submission and `protein_design` view. | One example runs through build → import → screen → readback; an edited target or candidate invalidates its current evidence. |

Stages 1–3 produce the first inspectable geometry draft. The research
prototype needs all six stages. Backbone drafts explicitly retain
uncomputed packing, bracing and energy obligations; they cannot inherit
the fully constructed candidate's pass. Start the evaluator's adapter/control work
after stage 1 in parallel with construction; it does not need a successful
new binder. Validate serialization at stage 1 even though the full MCP/job
path lands at stage 6.

The main implementation uncertainty is stages 3–4: a set of attractive
contacts may have no realizable backbone within the chosen library, and
closing a chain does not ensure good packing. Benchmark those stages
before expanding topology families. Computational protein-geometry and
force-field review are needed alongside the Python implementation.

Start the small fixtures on a CPU laptop and the physical screen on a CPU
worker. GPU acceleration is an optional throughput improvement after
profiling. The profile's runtime caps are budgets, not measured performance
promises. Model calibration and experimental validation are separate from
the software exit criteria.

## Scope and existing seams

- `se` owns the design intent. Reuse pockets and region measures:
  `src/precis_se/pockets.py::PocketSpec` and
  `src/precis_se/properties/pocket_spec.py::pocket_from_surface`.
- `structure` owns the realized atoms and covalent bonds. The new domain
  must preserve residue identity, sequence order and atom names; the
  current `src/precis_bio/converge.py::parse_atom_site` returns only
  elements and coordinates and is insufficient for protein construction.
- Workspace construction uses `precis_geom`, its step cache and
  `src/precis_geom/out.py::Run`; the trusted import boundary is
  `src/precis_se/workspace_import.py::import_workspace_run`.
- `src/precis_se/pocket_realize.py` currently ranks supplied wall
  candidates using a capped GFN2 cluster. It neither builds backbones nor
  establishes whole-protein stability. Reuse its intent/check separation,
  not its pinned-wall calculation as evidence of protein rigidity.
- The existing `protein` kind predicts structures from sequences.
  This workflow does not call it. No AlphaFold/ESMFold, diffusion,
  predictor-confidence gate or learned folding surrogate is required or
  used to accept a candidate.

Related: pocket spec `me628986`, spec
`me629051` (graph memory),
[molecular-workspace-code](molecular-workspace-code.md). The protein
constructor can later realize a catalytic pocket, but binding alone
does not establish catalytic activity. [se-protein-chain-import](se-protein-chain-import.md)
is a visualization/import consumer, not a prerequisite for construction.

## Intent record

One protein-design record belongs to the binder block, beside the existing
domain records on `src/precis_se/ops.py::SeBlock`. Pockets and scalar
measures remain authoritative in their existing stores. Typed contact,
charge-partner and crosslink constraints reference those regions; they
are not a second property system.

| Field | Required meaning |
|---|---|
| `target` | Bound target block uid, structure revision/content identity, selected surface, and target-to-binder transform. Keep the surrounding target for steric checks. |
| `interface` | Pocket name; required and optional contacts with stable ids, functional-group class, position, orientation, tolerances, and allowed residue/rotamer choices. Coordinates live in the target's local frame. |
| `nonrepeating` | Required for both interface contacts and internal scaffold contacts/braces. Wrong-arrangement domain, permitted physical symmetries, spatial scales, match tolerances and minimum independent mismatch margin. |
| `scaffold` | Allowed segment/topology library, residue-count and loop-length bounds, amino-acid alphabet, construction envelope and keep-out/approach regions. |
| `crosslinks` | Requested disulfide pairs or bounded pair search, minimum/maximum count, and which separate scaffold elements they must brace. |
| `charge_policy` | `paired`: every ionized group of the designed binder has an explicit, geometrically compatible opposite-charge partner in each required state (default: free and bound). Include termini; target groups can supply partners only in the bound state. |
| `environment` | pH, temperature, ionic strength, solvent model, protonation assignment/method, terminal chemistry, and redox/disulfide state. Unknown values remain unknown. |
| `landscape` | Separate apo and bound gap, local-deformation and perturbation-recovery requirements, with physical model and parameter-set identities. |
| `budget` | Maximum search nodes/candidates, decoy counts, minimization work and runtime; deterministic ordering and any sampling seeds. |

Partial intent is writable, following se's suggestive contract.
Construction requires resolved target chemistry, contact tolerances,
library versions and finite budgets. Physical acceptance additionally
requires the environment and evaluation profile. Missing input is
`uncomputed` with the missing field named, never a default pass.

Geometry-only targets can carry a shape spec; chemical complementarity
stays unchecked until chemical labels are supplied. Protein targets use
residue/atom chemistry at the declared protonation state; do not
implicitly run the small-molecule GFN2 surface painter over a protein.

At the se boundary, coordinates and lengths use SI with explicit input
units; the atomistic kernel uses self-named Å/eV quantities. Convert once
at the adapter. Conditions and reports always label their units.

## Required non-repeating pattern

The useful mathematical object is a **finite set of oriented, chemically
typed contacts**, with an unambiguous intended matching at molecular
tolerances. Distinct ids, sequence letters or infinitesimal coordinate
jitter do not establish distinguishability.

For intended pose `T0`, declare its pose neighborhood `B`, a tolerance
guard band, and a wrong-pose domain outside both. This separates small
deformations from different registrations. Let `M(T)` be the maximum
compatible contact-matching score at pose `T`, using geometry and observable
chemistry. For every tested pose in the wrong-pose domain, require:

```text
M(T0) - M(T) >= pattern_margin
lost_independent_contacts(T) >= min_lost_contacts
```

For loss counting, independent contacts consume distinct chemical
interaction sites. Multiple atom pairs involving the same charged group
cannot multiply its credit. This is resource independence, not statistical
or thermodynamic independence. Contacts on one helix can provide different
matching information; whether that helix is braced is checked separately.
Evaluate translations, rotations, alternative registers, segment swaps and
repeated target sites; test mirror ambiguity in the geometry layer and
enforce L-amino-acid stereochemistry in realization.

The generator combines unequal spacing, differing contact directions,
complementary chemical classes and asymmetric scaffold bracing. It tests
the combined pattern after molecular tolerances and allowed rotamer
motion, including removal of a small declared number of contacts.
Ordinary alpha-helix periodicity is allowed; the assembled contact and
bracing pattern must resist wrong registrations.

Apply the requirement twice: to the interface and to the internal contact/
bracing graph under alternative segment registers, packing and pairings.
A uniquely docked interface can still have several competing backbones.
Check coarse segment arrangement, intermediate brace/contact spacing and
local chemical matching at declared scales; microscopic differences cannot
rescue a repeated arrangement below the model's spatial resolution.

**Backend:** bounded deterministic constraint search over candidate contact
frames and allowed chemical labels. Reject high-overlap wrong matchings,
then maximize the worst retained mismatch margin subject to backbone
reachability. Re-run the test after atomic realization and physical
relaxation; an abstract pattern alone is not an accepted protein.

Aperiodic tilings or Golomb-ruler-like unequal spacings can seed this
search. Start with finite unequal-spacing patterns and asymmetric chemical
labels. A toy translation fixture makes the distinction concrete:
`[0, 1, 2, 3]` has three coincident sites after shifting by one;
`[0, 1, 4, 6]` has at most one after any nonzero translation because all
its pair separations differ. These are dimensionless, exact point sets,
not residue positions. Realization adds molecular tolerances, noncollinear
geometry, contact directions and chemistry to distinguish 3-D poses.

A fractal is only a candidate generator: self-similarity can
create competing matches at another scale. An aperiodic infinite tiling
does not imply a unique finite patch or a unique molecular conformation.
The Teglon is a fictional tiling problem; it is inspiration, not a
chemical model or a proof obligation for this implementation.

If the target itself repeats indistinguishably, report the unresolved
equivalent poses. Uniqueness is only modulo explicitly accepted physical
symmetries. Do not invent target asymmetry to meet the requirement.

The report distinguishes exhaustive results in a finite enumerated model
from sampled coverage of continuous pose space. Exhausting the former
does not certify the latter.

### Finite search contract

Use deterministic branch-and-bound over finite contact-frame, residue,
rotamer and segment-placement tables. NumPy/SciPy suffice for v1; a
tiling engine or a learned model is not a dependency.

1. Each option carries its observable chemical class, oriented interaction
   geometry, compatible backbone frames, occupancy/capacity and source.
   Residue ids identify atoms; they never create a unique interaction type.
2. At a fixed pose, construct the compatible-contact bipartite graph and
   find the maximum matching, including unmatched sites. v1 gives one
   unit of credit per occupied chemical interaction site. Shared-atom
   capacity constraints and the independent-contact test are checked
   separately; duplicate atom pairs cannot multiply the credit.
3. Generate wrong-pose hypotheses from compatible point/normal
   correspondences, including noncollinear triples, then add the declared
   translation/rotation probes. Enumerate internal segment order,
   orientation, register and crosslink alternatives from explicit lists.
   Persist these domains so “exhaustive” has a reproducible meaning.
4. Prune partial designs only with a valid bound on remaining matches or
   a proved incompatibility within this library. Evaluate the worst wrong
   matching and the contact-deletion cases before retaining a design.
   Stable option ordering breaks equal-score ties.
5. Return selected options, worst counterexamples, visited/pruned counts,
   remaining search bounds and whether the finite domain was exhausted.
   A runtime or work cap produces a partial result.

Recompute contact-resource groups from realized chemistry; an author
cannot create independent sites by renaming them. Separately, a local
constraint-Jacobian rank check catches unresolved motion. Normalize
residuals and translation/rotation variables by declared scales and report
the rank threshold. Remove global rigid motions for a free connected
assembly; with a fixed target, the binder's relative translations and
rotations remain physical freedoms to test. Full local rank is a geometric
diagnostic, not global uniqueness or a thermal stiffness measurement.

The physical scorer includes interactions with **all** compatible partners.
Every positive group can interact with every negative group. There is no
native-partner-only attraction, native-contact bonus or energy penalty
attached merely to the label “wrong.”

## Construction

1. **Freeze the interface.** Select complementary functional groups and
   orient donors, acceptors, charged groups and hydrophobic contacts.
   Preserve required contacts and clearances; optional contacts may be
   dropped with an explanation. Record one immutable interface version.
2. **Work backwards from side chains.** Enumerate allowed inverse
   rotamers: placements of each residue's backbone compatible with its
   fixed functional group. Reject incompatible stereochemistry and
   combinations that cannot share a scaffold.
3. **Connect a backbone behind the interface.** Fit short ideal
   secondary-structure fragments to those placements and close bounded
   loops with a versioned fragment library. Require one continuous
   peptide chain, correct bond geometry, allowed backbone torsions,
   no crossings/clashes and no intrusion into the target or approach
   region. First implementation: canonical L residues, alpha-helical
   segments and short turns; arbitrary beta topology is later work.
4. **Pack and brace jointly.** Choose core residues/rotamers, short
   linkers and feasible cysteine pairs together. Prefer crosslinks
   between separate structural elements over redundant local braces.
   Revisit the discrete choices if packing or a crosslink fails; never
   stretch bonds or release interface constraints to finish.
5. **Complete the interaction network.** Satisfy charge partners and
   buried donors/acceptors, including backbone groups. Re-evaluate
   solvent exposure and packing after every sequence or crosslink
   change. Derive the sequence from the realized residue graph.
6. **Challenge the result.** Remove artificial construction restraints,
   minimize and perturb under the physical model, and evaluate the
   competing states below. A failed interface or pattern check returns
   to construction with its conflict set.

The constructor starts from contacts, not a prescribed whole-protein fold.
Fragments are reusable local building blocks. Search order is reproducible,
and bounded failure means “no candidate within this library and budget,”
not “no protein can satisfy this surface.”

### Initial building-block library

Use canonical amino acids, three alpha-helical segments and two short
turns for the first constructor. Helix lengths span 12–24 residues;
loop lengths span 2–6. Ideal helices start at phi/psi = −60°/−45° with
trans peptide bonds; these are construction seeds, not enforced physical
minima. Imported fragments retain their measured internal geometry.

Build a versioned library of capped single-residue geometries using
`src/precis_geom/mol/fragment.py::embed_molecule`, explicit stereochemistry
and atom-name maps. Its RDKit embedding and best-effort MMFF cleanup only
initialize local geometry. Enumerate declared chi-angle grids rather than
depending on a statistical rotamer predictor; keep aromatic groups planar
and identify chemically equivalent atom permutations. Store the grid,
seed, source chemistry and library digest.

Start the turn library with short experimentally resolved fragments,
including the alpha3D reference below, preserving source residue ranges
and redistribution notices. Loop closure may adjust permitted torsions
using an analytic internal-coordinate Jacobian; it must satisfy peptide
closure and stereochemistry before a candidate is emitted. No whole-chain
distance-geometry embedding substitutes for scaffold construction.

## Chemistry and rigidity constraints

| Constraint | Acceptance rule |
|---|---|
| Charge pairing | Explicit opposite-charge partners with compatible distance/orientation under the assigned protonation state; no net-charge shortcut or duplicate neutralization credit. A target-only partner does not satisfy the free-state requirement. v1 uses pairwise assignments; a multigroup network needs an explicit charge/capacity model. |
| Buried polar groups | Check atom-level donor/acceptor satisfaction and burial in both free and bound states. A target-satisfied contact must not become an unexplained buried unsatisfied group in the free binder. |
| Disulfides | Explicit Cys–Cys topology, correct cystine chemistry, complete bond/angle/torsion geometry and strain. One cysteine cannot participate in two disulfides; unused thiols are reported. |
| Redox compatibility | Required disulfides must be compatible with the declared operating condition. A reducing condition cannot inherit an oxidized-state pass. Competing disulfide pairings are challenged separately. |
| Packing | Occupied hydrophobic core, cavity/clash limits, and reported exposed hydrophobic patches. More salt bridges or crosslinks cannot compensate for a failed core. |
| Local freedom | Short loops, satisfied secondary-structure hydrogen bonds, and braces spanning independent elements. Gly/Pro are context-dependent choices, not a blanket forbidden list. |
| Interface retention | Maximum functional-atom displacement and direction error, not only average RMSD. Bound alignment uses the target frame; free-state comparisons align the scaffold core. |

Pairing every charge is a deliberate design restriction, not a law of
protein stability. It may make a target infeasible. The constructor must
report that rather than silently allow exposed unpaired charges. Salt
bridges remain screened, reversible interactions; they are not covalent
locks. Histidine and termini follow the declared chemistry rather than a
sequence-letter charge count.

## Energy landscape without folding prediction

Design for a preferred basin and penalize plausible alternatives. Maintain
separate evaluations for the **free binder** and the **target-bound
complex**; target contacts cannot supply all of the binder's rigidity.

For one sequence, environment and chemical-state model:

```text
gap_apo   = min E(other free-binder basins) - E(intended free-binder basin)
gap_bound = min E(other complex basins)    - E(intended complex basin)
```

Define basin membership using interface geometry, backbone packing and
crosslink topology. A solvent-facing side-chain rotation that preserves
these obligations can remain in the intended basin; do not demand a large
gap between every microscopic rotamer. Its entropy is still part of the
uncomputed free-energy contribution.

Both energies in a difference use the same composition, parameterization
and evaluation protocol. Minimize all states comparably without the
intended contact restraints. If a decoy returns to the intended basin,
record recovery rather than counting it as a distinct higher minimum.
If no alternative survives, report no measured gap and unresolved physical
coverage; do not return infinity.

Required competitors:

- Wrong docking poses, contact registers and alternative target patches.
- Alternative segment packing, loop closures and side-chain assignments
  **for the same sequence**. Alternative sequences are different designs.
- Chemically valid competing disulfide pairings; fixing only the desired
  bond topology would assume away this source of degeneracy.
- Partially opened/disordered structures and alternative oligomeric
  assemblies where the evaluator can compare their thermodynamics.
  Otherwise their coverage is explicitly missing.

Do not discard a physically plausible competitor because it violates the
designer's preferred contacts or pairing rules. Those failures are meant
to cost physical energy, not remove the competitor from the test.
Different protonation/redox states need their chemical-potential terms;
oligomer comparisons need consistent composition and concentration.

“Deep,” “rigid,” and “accessible” are separate checks:

| Property | Evidence |
|---|---|
| Preferred basin | Positive gap to the declared competitor set, reported in eV and relative to `kBT`, with the worst competitor and coverage. |
| Stiffness near the minimum | Response to bounded internal-coordinate perturbations and, where available, curvature/soft modes after removing rigid-body zero modes. Evaluate without artificial pinning. |
| Access to the basin | Recovery from a declared perturbation ensemble; report traps and alternate crosslink isomers. This is local evidence, not proof that an unfolded chain reaches the basin. |
| Environmental robustness | Repeat the relevant checks over the specified condition/protonation range; a missing condition is unchecked. |

A physical scorer must include protein torsional energetics, sterics,
electrostatics and solvation under an identified parameter set. Geometric
contact scores, a spring network or a pinned cluster cannot be relabeled
as this energy. No predictor is substituted if the scorer is unavailable.

Potential-energy gaps are not free-energy gaps. Entropy, solvent,
conformational multiplicity and kinetic traps can overturn a minimum-energy
ranking. Report free-energy preference only when the method estimates
those terms. Neither finite decoy search nor a non-repeating pattern
proves a globally unique protein fold or experimental affinity.

Keep hard chemistry/interface failures outside the score trade-off.
Among feasible candidates, rank gap, local deformation and recovery
evidence explicitly; do not reward atom count, number of charges or
number of crosslinks as substitutes for an energy landscape.

### First physical evaluator

Select **OpenMM with ff14SB and GBn2 implicit solvent** as the initial
effective-energy evaluator. This is a development screening model, not a
calibrated claim about solution free energy. OpenMM belongs in the explicit
compute environment; se reads and the pure pattern solver do not import it.

| Item | v1 contract |
|---|---|
| Parameters | `amber14/protein.ff14SB.xml` + `implicit/gbn2.xml`; record OpenMM version, parameter-file digests, platform and precision. |
| System | Nonperiodic, `NoCutoff`, `constraints=None`, all force groups included. No design-restraint forces in a scored system. |
| Chemistry | Construct topology from the explicit residue/bond map, including disulfides, termini and assigned protonation variants. Do not infer S–S bonds from proximity on file load. |
| Boundary | All binder atoms are movable. A rigid target is an explicitly declared external boundary, identical for all bound competitors; it supplies no evidence for apo rigidity. |
| Solvent | Explicit solute/solvent dielectrics and salt-screening parameter. Use the documented OpenMM screening convention; retain input ionic strength, temperature and the resulting parameter. |
| Minimization | OpenMM L-BFGS using its analytic forces; bounded iterations, followed by an independent check of the final movable-atom RMS force. Nonconvergence is `uncomputed`, not a high-energy decoy. |
| Units | Adapter converts Å ↔ nm and eV/particle ↔ kJ/mol (`1 eV = 96.485332123 kJ/mol`). Forces convert consistently; the gradient is minus the force. |

Rebuild the topology/system for each alternative disulfide matching.
With four cysteines there are three complete pairings; with six there are
15. The atoms, protonation and disulfide count must match before comparing
energies. A failed alternate-pairing minimization leaves that coverage
unresolved; it is not evidence that the pairing is unfavorable.

Protonation is explicit and fixed within each evaluation. A nominal pH
does not make this a constant-pH simulation. Changes in protonation,
redox chemistry, oligomerization or solvent model remain separate
obligations until their thermodynamic comparison is implemented.

**Derivative routes:** contact matching, residue identity, crosslink
choice and worst-decoy selection stay in the discrete outer search.
Loop closure uses the geometric Jacobian; continuous physical relaxation
uses OpenMM forces. Do not differentiate minimizer iterations or use
finite differences as the production energy gradient. Verify adapter sign,
units and coordinate transforms with directional finite differences at
several step sizes and a Taylor-remainder check; use OpenMM's Reference
platform for the small regression cases.

## Proposed se and workspace surface

These are proposed additions, not currently callable API:

- `edit(kind='se', ..., ops=[{'op': 'set_protein_design', 'block': ...,
  'spec': ...}])` validates and stores intent only. A corresponding
  `remove_protein_design` removes that record without deleting structures.
- `precis_geom.protein` supplies the pure typed records, pattern search,
  deterministic constructor and check functions. A workspace passes
  resolved target snapshots; build steps use `precis_geom.steps`.
- Thin adapters under `precis_se.protein` own se ops, persistence and
  findings. Use a forward migration for the block-owned domain record;
  do not repurpose the nucleic-acid `chain` record or add a content kind.
- Candidates are `Geom`/`structure` artifacts with a versioned residue-to-
  atom map, peptide/disulfide connectivity, sequence, constraint mapping,
  construction trace and evaluation receipts. Extend workspace/cache
  serialization so none of this becomes an untracked sidecar.
- Physical evaluation runs explicitly in the compute lane with recorded
  budgets and model identities. Reads do not start searches or simulations.
- `get(kind='se', ..., view='protein_design', args={'block': ...})`
  reports intent, candidate identity, each check's status, worst
  competitors, coverage and next unresolved requirement. Shared checks
  run at workspace finish/import; normal `drc` summarizes their findings.

Cache identity includes the exact target geometry/chemistry and relative
pose, interface and pattern constraints, sequence/topology, conditions,
libraries, scorer parameters, search protocol and seeds. The existing
pocket `class_hash` is not sufficient. Changes stale dependent evidence;
re-importing must preserve both intended contacts and actual residue ids.

Reports distinguish `pass`, `fail`, `uncomputed` and `unsupported` for
each obligation. A candidate can pass pattern matching while its energy
gap remains uncomputed. Overall acceptance requires every required check
to pass; never emit an unconditional `unique_fold` or `rigid=true`.

## First benchmark and development profile

Begin with synthetic contacts and experimental structural controls.
No application target or folding predictor is needed for these checks.
Prepare immutable fixture manifests before implementation tests consume
them; record atom selections, transformations, sources and expected
outcomes rather than selecting a new favorable patch at test time.

| Fixture | What it establishes |
|---|---|
| Exact finite pattern | The two rulers above, including every nonzero integer shift with possible overlap and every single-contact deletion. Repeat in 3-D with explicit orientations, a rotated/repeated control and a deliberately unresolved symmetry. |
| Reachable construction | Use experimental alpha3D, [PDB 2A3D](https://www.rcsb.org/structure/2A3D), as a source of local fragments and six exposed functional-group frames. A synthetic complementary patch supplies those frames to the constructor; withhold the full reference placement from the search. Include a version with incompatible anchors. |
| Crosslink/scorer control | Use the explicit disulfide topology of [PDB 5PTI](https://www.rcsb.org/structure/5PTI) to exercise residue-aware loading and all 15 pairings. Rebuild the chosen protonation state and record removal of crystallographic solvent/other species. This control does not require the natural protein to satisfy the design's charge policy or the alpha-only constructor. |
| Physical failure controls | Unpaired but net-neutral charges, a strained disulfide, a restrained flexible scaffold, an unconverged competitor and a deliberately lower-energy competing state must prevent the corresponding pass. |

The construction fixture checks recoverability within a known library.
It is not evidence of discovering a new stable protein. Its geometric
target has no physical binding score until a fully parameterized atomic
target is supplied. Likewise, the scorer must report the control
energies it obtains; never tune a native-state bonus to make a known
experimental structure win.

Proposed profile `development-v1`:

| Parameter | Initial value |
|---|---|
| Interface retention | Maximum functional-atom error 0.5 Å; direction error 15°. |
| Pattern rejection | At least two independent lost contacts for every enumerated wrong arrangement, including after removal of any one contact. A fixture whose realized interaction sites cannot provide that margin fails. |
| Scaffold | Three helices/short turns as above; at most 96 residues. Full-design runs require two disulfides with four cysteines; structural controls state their different topology explicitly. |
| Conditions | 298.15 K, nominal pH 7, ionic strength 0.15 mol/L, uncapped charged termini unless the input declares caps; explicit protonation variants. Dielectrics 1.0/78.5. |
| Salt conversion | OpenMM's documented `kappa = 367.434915 * sqrt(I / (epsilon * T))`, with I in mol/L, gives 0.930194605 nm⁻¹ for these conditions. |
| Discrete work | 100,000 search nodes, retain at most 16 geometrically feasible candidates, physically evaluate at most four. |
| Physical work | At most 256 minimizations per evaluated candidate, each capped at 2,000 iterations; job runtime cap 600 s. The quotas below are fixed before evaluation. Any unfinished quota remains uncomputed. |
| Convergence | Final movable-atom RMS force ≤1 kJ mol⁻¹ nm⁻¹; also record the maximum atom force. |
| Gap target | At least 20 `kBT` in each completed apo/bound competitor set: 0.51385 eV or 49.579 kJ/mol at 298.15 K. This is an engineering search target, not a calibrated stability threshold. |
| Local recovery | 32 prescribed perturbations per free/bound state, with backbone torsion changes up to 10° and side-chain changes up to 20°. Every completed probe must return within 1 Å core Cα RMSD and the interface limits; a trap fails, an unfinished probe is uncomputed. |

Per full-design candidate, reserve 32 wrong docking starts, 16 packing/
register starts in each free/bound state, 16 loop-opening starts in each
state, four starts for each of the three disulfide pairings in each state,
the 64 recovery probes and the two intended-state relaxations: 186 solves.
Remaining budget permits retries or additional challenges. Store starting
geometries, generation rules and seeds; duplicate starts do not fill a
quota. Minima that coalesce are reported as one basin with multiple
recoveries. These are sampled physical challenges, not exhaustive fold
enumeration.

Numerical values belong to this versioned development profile, not to
universal protein rules. Check sensitivity before using them to select
experimental candidates. A pass names its profile and tested domain;
free-energy, global-fold and assembly-kinetics claims remain unavailable.
Discrete outputs reproduce at fixed work budgets; physical outputs use
declared numerical tolerances, not cross-platform bitwise equality.

## Acceptance criteria

- An authored target patch becomes an se pocket plus protein intent and
  round-trips without loss of orientation, tolerances or chemical state.
  A changed target revision makes dependent evidence stale.
- A deliberately repetitive fixture fails wrong-register matching; an
  asymmetric fixture passes within its declared finite domain. Unique
  ids, tiny coordinate jitter, a rotated repeat and an unresolved target
  symmetry cannot manufacture a pass.
- A unique interface with an internally repetitive scaffold still fails
  the internal-pattern requirement. Checks cover the declared spatial
  scales and physically indistinguishable chemical labels.
- A finite pattern search can report exhaustive coverage; a truncated
  search reports its bounds and untested domain, never global uniqueness.
- Changing residue ids or declared partner names cannot change a chemical
  match or physical energy. Wrong compatible partners remain available;
  actual chemistry/geometry changes can alter the result.
- A reachable contact fixture yields a connected, stereochemically valid
  protein candidate with sequence and explicit disulfides. An impossible
  anchor/loop combination yields named conflicts without moving anchors.
- A net-neutral candidate with a distant unpaired charge fails. Tests
  cover termini, protonation changes, duplicate partner credit and the
  difference between free and target-bound satisfaction.
- A short S–S distance with invalid cysteine torsions fails; a required
  oxidized brace under reducing conditions cannot pass. Alternate
  disulfide isomers appear in coverage, not silently excluded.
- Physical checks include a false-positive control: a candidate held
  rigid by construction restraints or target contacts fails when those
  supports are removed. A lower-energy alternative rejects the claimed
  gap; no alternative found leaves coverage unresolved.
- Rebuilding identical inputs reproduces the candidates and receipts.
  Sequence, topology, environment or model changes invalidate the
  corresponding evaluations.
- The physical adapter passes unit/sign/gradient checks, evaluates all
  configured force groups, and refuses to score unknown residue templates.
  A required unconverged competitor cannot inflate the reported gap.
- A worked example records the intended structure, sequence, pattern
  margins, free/bound checks, worst alternatives and missing evidence.
  It invokes no folding predictor and makes no experimental-success claim.

## Readiness

The finite solver, construction-library route, physical backend and
synthetic benchmark are specified above; the
[software build draft](#software-build-draft) defines their modules,
contracts and six delivery stages. This remains a draft until the fixture
manifests and development profile are reviewed. Before claiming physical
usefulness, run the controls and calibrate the margins against
the measured errors and sensitivity of the selected model. A real target
surface and its operating environment are inputs to a later design run,
not prerequisites for implementing the synthetic benchmark.

On implementation, update the owning package docstrings and se/workspace
skills; delete this item only when its slices are complete.

## Method references

- [Cao et al., 2022, target-structure binder design](https://doi.org/10.1038/s41586-022-04654-9):
  precedent for enumerating side-chain interactions and finding backbones
  that can present them; not a required engine.
- [Bhardwaj et al., 2016, constrained peptide design](https://doi.org/10.1038/nature19791):
  experimentally demonstrated disulfide-constrained designs; not evidence
  that an arbitrary contact pattern will fold.
- [Smith et al., An aperiodic monotile](https://doi.org/10.5070/c64163843):
  aperiodicity concerns admitted infinite tilings; the finite molecular
  matching and energy tests above remain necessary.
- [OpenMM force fields and implicit solvent](https://docs.openmm.org/latest/userguide/application/02_running_sims.html):
  parameter-file selection, dielectric constants and salt-screening convention.
- [OpenMM model preparation](https://docs.openmm.org/latest/userguide/application/03_model_building_editing.html)
  and [minimization API](https://docs.openmm.org/latest/api-python/generated/openmm.openmm.LocalEnergyMinimizer.html):
  explicit chemical variants, minimizer force groups and convergence units.
- [Gemmi Python API](https://project-gemmi.github.io/python-api/):
  structural file I/O and protein identity for the input/output adapter.
