---
status: draft
title: "Review matched inward and outward trivalent fin junctions"
pillar: 3d-design
prio: high
---

# Trivalent fin junction feasibility

Reto asked on 2026-10-07T20:22:44Z: “We should also have... a variant of
the tube in and tube out with an sp2 carbon joiner? Is this reasonable.”
Hex window9 owns a source-only feasibility and topology plan. Return the
reviewable plan before implementation, generation or new relaxation.

## Checked premise

The deployed 4a7d7409 source already dispatches `fin-k3-120-z` through
`hexfold_scene` to `precis_se.atomic.generators.fin120.build_fin120`.
BEFORE finding fi472892 distinguishes this family from the four-coordinate
`fin-sp3-z` used for the completed inward/outward pilots. Read that finding,
td471803, gr472846 and existing thread/catalogue history; perform final
all-status dedup before native publication. Reuse an existing requirement
when it covers this request, preserving history and one body writer.

The adapter calls `hexfold.fin120.seam_tube`, then `stick_relax_pinned`.
Seam atoms and their three neighbours are pinned at authored 120 degrees;
the strip is tethered to a plane. Thus invoking the current builder also
runs relaxation. Do not invoke it during this source-only assignment.
Its degree/angle checks and reported eight-cycle seam faces are source
evidence to inspect, not fresh measured results. Trace the exact upstream
dependency version and tests before claiming its graph satisfies the plan.

`tube=[wall_rows, periods]` here uses row pairs per wall half, not the same
parameter meaning as the sp3 armchair construction. Do not copy numeric
dimensions and call the results matched without explaining the geometry.
The old operational proposal to generate k3 fins is not execution authority
for this new source-only review.

## Deliverable

1. Identify what already exists, its exact source/dependency identity,
   catalogue entries and persisted candidates. Explain the smallest reuse
   path; do not propose a duplicate generator by default.
2. Specify separate inward/outward variants with common comparison
   invariants: longitudinal period/length, wall circumference or comparable
   radius, strip extent, seam length, boundary conditions and termination.
   State unavoidable differences from the preserved sp3 originals and
   distinguish authored targets from measured geometry.
3. Trace seam adjacency, carbon coordination versus total coordination,
   connectivity, ring/face construction and boundary valence. Distinguish
   declared faces from an independently defined graph-cycle census. Map
   the seam, far-side closure, end/strip boundaries and existing `none`,
   `open` and H termination behavior, including replay fidelity. Removing
   H alone does not construct a sound trivalent junction.
4. Explain which angles/planarity are imposed by pins and which degrees
   of freedom remain. Propose later checks for local angles, bond lengths,
   pyramidalisation, all close contacts including graph-neighbour
   exclusions, topology changes and cross-section shape. Existing
   `surface_deviation` is unavailable without a trusted target; do not fit
   a desired teardrop and report that as independent agreement.
5. Separate structural plausibility from electronic hybridisation. Degree
   three, nominal 120-degree geometry and a stored `hybridisation=sp2`
   label are not electronic proof. Flag any overclaim in current findings
   for a proposed wording correction; no source edit in this phase.
6. Return exact files/APIs/tests affected, limitations and an optional
   future finite generation/validation proposal. No execution authorization
   follows merely from listing commands or resource caps. Coordinator
   reviews the plan and assigns any subsequent work/version separately.

## Scientific framing

Trivalent carbon junctions are reasonable to investigate. Atomically
resolved nanotube/graphene sp2 connections have been reported by
[Zhu et al., Nature Communications 3, 1225 (2012)](https://www.nature.com/articles/ncomms2234).
Theoretical tube/graphene junction work studies nonhexagonal rings at
specific junction geometries:
[Physical Review B 79, 165434 (2009)](https://journals.aps.org/prb/abstract/10.1103/PhysRevB.79.165434).
These support broad feasibility, not this longitudinal fin graph, its
particular ring census, electronic state or a guaranteed teardrop.

Reto's 20:17:52Z visual assessment accepts that existing sp3 fins may be
roundish and that fin-out looks decent. Absence of a teardrop is not an
acceptance failure for those sp3 shapes. Their 300-step unconverged force
receipts and the Y clash evidence remain unchanged. An sp2-seam teardrop
is a hypothesis to assess, not a measured result or prescribed target.

## Ownership and limits

### Boundary discussion clarification

The communicator's complete native help read, reported20:26:49Z, identifies
existing ordered rims/ports with lattice/type/count, sp2-hex-only join dispatch,
hole ports and a collar solver. These are attributed help/source premises;
Hex must verify their exact deployed and task implementations before relying
on them. Generic sp3 joining and an arbitrary curved-seam interface have not
been established. Keep fuse/join and a fourth-bond sp3 graft distinct.

Extend the source map with a conceptual common boundary identity for an open
edge, closed hole/rim and a real cut: exact structure snapshot, ordered atom
chain/rim, open/closed topology, orientation, existing port/lattice typing and
termination. Map this onto current types first; this discussion does not
authorize a new schema or replacement port system. Explain typed seam
realization separately from boundary selection. Intended sp2/sp3 bonding must
remain distinct from measured coordination, pyramidalisation and relaxation
provenance. State which combinations current code supports and which need
reviewed topology design. A curved path is not obtained safely by bending
stored coordinates. Viewport clipping is visual; a real cut changes graph and
termination and is outside this source-only assignment. No generation/jobs.

Hex returns one source map/topology plan and attributed native readback;
communicator relays the result without a competing body edit. Existing
viewer25 owns the separate SE/structure common-viewer map; Hex reuses the
same honest geometry/completion vocabulary without changing viewer files.
Preserve all originals and completed pilot receipts. No new structures,
MACE/xTB jobs, generator invocation, runtime extension, bond rewriting,
version bump, ship or deploy under this assignment. The separately
authorized r5 follow-on retains its own finite plan; this assignment does
not add candidates to it. No new window, Docker prune or shared /tmp
scripts. Other science, BEEF, NEB and real-board holds remain in force.

## Owner source-only review — 2026-10-07

**Verdict:** reasonable to investigate as a trivalent geometric candidate;
reuse the existing `fin-k3-120-z` generator. Its electronic sp2 character,
free-relaxed shape and stability are unestablished. No builder, graph generator,
relaxer, test or model was invoked in this review. Counts and dimensions below
are deductions from source formulas, not new generated/measured results.

### Exact identity and prior evidence

Task base `0e1e4d997325659f6719aa6352be612e308d1584` was freshly fetched from
origin/main into a separate feasibility worktree. Relevant graph, adapter and
test files are byte-identical to deployed
`4a7d7409eb5d5d90b28b30b96a3ad5192b7795a7`; source manifest records exact blobs
and SHA256s. Package metadata differs only in Precis version 8.35.19→8.35.21,
not these algorithms. That later version is not a deployment claim.

The requested control spec hash `a4b86a35…` was subsequently extended by the
coordinator's boundary clarification: adopted full hash
`a535ed88f51938e8f3cd15c832fb9732851f0cb9255d1cf5266e2f828aabf21a`.
The assignment receipt records that lineage. The unchanged adopted copy and
25-file source/dependency manifest are retained outside the shipping payload.

`hexfold` is **bundled** under `src/hexfold`, MIT, version `0.3.1`, included by
the root wheel's package list. There is no separate external hexfold dependency
to upgrade. Fin120 first landed in
`a250a7fbbf8854a53f62ab4a96d533537fe9aecb`; explicit termination replay is in
deployed `4a7d7409`. Locked numerical dependencies are NumPy 2.5.1, Numba 0.67.0,
llvmlite 0.49.0 (SciPy 1.18.0 is also in the project lock but is not this
builder's direct algorithm). These are lock identities, not a new installed
runtime probe. Native Python discovery preceded task-local source reads;
the indexed server root is not the task worktree.

`fi472892` establishes that persisted `hexfold-dogfood-fin-out` UID213 and
`hexfold-dogfood-fin-in` UID214 are **fin-sp3-z**, with 19 four-coordinate graft
hosts, 963C+68H and 1603C+100H. Their separate after candidates are st473094 and
st473123. The Y UID212/st472104 and after st473377 are three open sheets, not
the requested fin. `td471803` holds the Y/catalogue history; gr472846 already
owns the fin chemistry, relaxation and teardrop requirement. Native fin-family
searches returned these known designs, but ranked search does not prove that
no other persisted k3 candidate exists. This review establishes no persisted
fin-k3 pair. The thread's historical `[10,20]` out / `[18,20]` in proposal and
its old deployment hold do not authorize generation now.

Catalogue coverage: `hexfold-seam-type-catalogue.md` R13 selected contract and
R17 variant A specify the equal-120 finite zigzag k3 motif; the current toolkit
Resume names the fin120 extension. Unequal 180/90/90 variant B remains separate
and measurement-blocked. Native all-status requirement dedup selects existing
gr472846 for the source-plan supplement, with td471803 and fi472892 pointers;
do not create a duplicate generator, gripe or empirical finding.

### Source/API and test map

| Path / entry | Existing responsibility / review consequence |
| --- | --- |
| `src/precis_se/atomic/generators/hexfold_scene.py::build_hexfold_scene` | Exclusive typed dispatch to fin120; no mixed feet |
| `src/precis_se/atomic/generators/fin120.py::_normalize`, `build_fin120` | Validates wall rows 4–40, periods 2–60, strip rows 2–12, side in/out, positive finite tether; validates termination before graph call |
| `src/hexfold/fin120.py::seam_tube`, `wall_radius`, `_top_dangling` | Three-sector graph, side-dependent seed, far-side fuse and inward clearance |
| `src/hexfold/y_junction.py::_sheet` | Finite honeycomb sectors with two guard columns and private open zigzag segment |
| `src/hexfold/join.py::compose_k3`, `_place_k3` | Exactly three compatible segments, equal120 selector, phase zero, graph/face index checks and seam spokes/faces |
| `src/hexfold/stick.py::stick_relax_pinned`, `_angle_springs`; `_stick_kernel.py` | One 3000-iteration spring preview, seam/neighbour pins and plane tether; not MACE force convergence |
| `src/precis_se/atomic/generators/hexfold_spec.py::_block_from_net`, `_terminate_open_edges` | H appended after carbon preview, report/cap-clash metadata and generated-scene replay |
| `src/hexfold/fin.py::fin_tube`, `_strip`; SE `generators/fin.py` | Preserved sp3 comparator uses fourth-bond graft, trimmed strip and cylinder tether; different topology |
| `tests/test_se_fin120.py` | Determinism, degrees, closed declared faces, seed bond distances, 4p pins, helper clash check, inward refusal; small outward persisted preview |
| `tests/test_se_scene_replay.py`, `tests/test_se_terminate.py` | Explicit H/none/ports-open replay, omission shape, invalid-mode early refusal, H placement/tag and clash reporting |
| `tests/hexfold/test_seam_k3.py`, `test_join.py`, `test_rim_type.py` | Segment admission, copied-index/face validation, typed boundary and composition coverage |
| `tests/test_hexfold_import_boundary.py` | Bundled package isolation; no Precis imports into hexfold |

These tests were read, not run. Existing fin120 graph fixtures cover
out `(w6,p4,s3)`, out `(w10,p20,s4)` and in `(w18,p20,s4)`, not a matched
w18 in/out pair. They do not establish free physical convergence, electronic
hybridisation or matched post-cap contact cleanliness. Replay parameterizes all
three modes for fin120, but does not assert portless `ports-open` equals H.
No source edits are proposed as part of this assignment; a later wording or
validation slice would use the exact seams above and normal assigned gates.

### Adjacency, faces, boundaries and valence

Let `p=periods`, `w=wall row pairs`, `s=strip row pairs`, `T=s+2w`.
Each sector contributes `2(p+2)rows` carbon atoms. At each axial period a new
central atom has one spoke to each sector's dangling site; seam atoms do not
form a direct axial bonded chain. The sector walks supply paths between seam
sites. Each sector is connected, and spokes connect all three. Far-side
closure adds one cross-wall bond at each of `p+2` top positions, including guards;
adjacent top positions close `p+1` six-faces. This closes the circumference,
not the finite axial ends. Four degree-one carbon guard stubs remain.

| Source-derived carbon graph quantity | Formula | w18,p20,s4 |
| --- | --- | ---: |
| Carbon atoms N | `2(p+2)T+p` | 1780 |
| C–C edges B | `(3p+5)T+p-4` | 2616 |
| Degree1 / degree2 / degree3 C | `4 / (2T+p) / (N-4-2T-p)` | 4 / 100 / 1676 |
| Seam atoms / pinned atoms | `p / 4p` | 20 / 80 |
| Declared six-/eight-faces | `(p+1)(T-2) / 3(p-1)` | 798 / 57 |
| Connected graph cycle rank | `B-N+1=(p+1)T-3` | 837 |
| H deficits, if capped | `3N-2B=2T+p+8` | 108 |

Declared faces are checked simple closed bond walks, not an independent graph
cycle census or basis: their total 855 exceeds cycle rank 837. A later census
must name its algorithm (e.g. chordless simple cycles within an explicit length
bound), preserve the stored face inventory separately, and disclose truncation.
The 57 eight-faces are not evidence that exactly 57 eight-cycles exist under
every graph-cycle definition. Ring corner warnings referenced to a regular
octagon (135°) are not the local seam's nominal120° criterion.

`H` is the default and fills carbon degree deficits according to authored
hybridisation: two H on degree-one sp2 C and one on degree-two C, at1.09 Å.
Seam C already has three C neighbours and receives no H. Thus capped boundary
C has three total neighbours but fewer C neighbours; distinguish carbon-only
degree from total coordination. Expected default total is1888 atoms/2724 bonds.
H is appended **after** stick relaxation; these H coordinates have not received
that minimization, so cap contacts require separate checks. Removing H is not
a construction of a new trivalent C junction.

`none` leaves all those deficits bare. `ports-open` skips only exported-port
dangling atoms (or the port walk fallback); fin120 has `net.ports=()`, so its
skip set is empty and it caps like H while recording a different requested
mode. Literal `open` is invalid; physical open ends and a termination mode are
different concepts. Explicit modes persist in replay after fi472844; omitted
mode retains the default and omitted field shape. Future pair inputs should
use explicit `terminate:"H"` to eliminate implicit boundary-policy ambiguity.

### Matched inward/outward proposal, not an executable instruction

Primary comparison: the same `tube:[18,20]`, strip `rows:4`, `k_tether:1.0`,
explicit `terminate:"H"`, with **only side in/out** different (plus names).
Both use the same carbon graph, atom counts, edge policy, axial period,
wall-material extent, strip extent and seam length. The side chooses spatial
registration; it does not change adjacency. All dimensions below are analytic
seed geometry in Å, not observed relaxed values.

At `sigma=1.42`, axial period is `a=sqrt(3)*sigma=2.459512`.
The wall-radius 20-step recurrence satisfies
`R*(pi-asin(sqrt(3)*sigma/(2R)))=(1.5w-0.5)*sigma`.

| Seed quantity | Out | In |
| --- | ---: | ---: |
| Shared wall-circle R | 12.370092 | 12.370092 |
| Seam-line radius R ± sigma/2 | 13.080092 | 11.660092 |
| Strip root C radius | 14.500092 | 10.240092 |
| Strip tip radius | 21.600092 | 3.140092 |
| Strip-only radial height | 7.100000 | 7.100000 |
| Seam-to-tip radial reach | 8.520000 | 8.520000 |

The shared seed circumference is `2*pi*R`; do not call this a measured or
constrained final circumference. Inward tip clears the axis by3.140092 Å,
above the source's2 Å admission threshold; w17 is the minimum for s4.
The carbon z extent is `[-a,20.5a]`, length52.879511 Å; seam extent
`[0,19a]`, length46.730731 Å. The far-fuse chord is
`2R*sin(sigma/(2R))=1.419220 Å`, not exactly sigma; the radius test checks
an arc relation. Finite axial ends remain open, nonperiodic; periodic wrapping
and end fusion are not implied.

Preserved sp3 seeds use armchair index n, not w: out n10 radius6.780001 Å,
in n18 radius12.204001 Å. Each has19 interior fourth-bond hosts spanning
18a=44.271219 Å,18 graft six-rings, and different trimmed end guards. Its
combined seed extent is50.419999 Å. The existing p20/s4 values therefore do
not make seam length, endpoints, radius or atom count identical across families.
The new w18 pair is matched **to each other**; comparison to the old outward
sp3 n10 candidate is not a controlled hybridisation comparison. Use fixed
membership central bins and report those differences, never rescale saved
coordinates or claim numeric parameter equality proves physical matching.

### Constraints, hypotheses and proposed wording corrections

The analytic seed already fixes the local equal120 seam arrangement and its
cusp/notch registration. The adapter pins the p seam atoms plus3p neighbours;
only other atoms can move. The strip has a y=0 plane tether with default k=1;
the wall has no surface tether but remains bonded to the fixed joint and strip.
Carbon bond springs, face-derived angle springs and repulsion remain active.
Its reported spring force is not a physical eV/Å convergence certificate.
Pinned zero angle/triple-product residual is imposed construction evidence.
Even later free ML relaxation would test whether this seeded motif survives,
not prove a teardrop emerged from an unbiased initial circle.

Proposed later wording correction, **not edited here**: replace
`seam.geometry`'s “Measured tethered degree-three sp2 seam” with
“Three-coordinate authored seam under pinned/tethered preview”; preserve
`hybridisation` only as authored intent, alongside actual degree and residuals.
`tube.cross_section` currently chooses “teardrop”/“heart-shaped notch” solely
from side: report measured radial descriptors with seed/pin attribution instead
of declaring that shape to be an emergent physical result. The same overclaim
appears in fin120 module descriptions and catalogue shorthand. Also qualify the
toolkit Resume's old “validate clean” against fi472892's H-cap warnings. Preserve
all earlier evidence as dated history; do not rewrite measured numbers.

Primary literature supports broad CNT/graphene covalent junction feasibility,
not this lengthwise three-sector seam: Zhu et al.'s atomic microscopy concerns
grown CNT–graphene junctions; González et al.'s electronic model places six
heptagons around a different tube/plane junction. Their results do not validate
our eight-face catalogue, inferred electronic labels or shape prediction.
Reto accepts roundish sp3 fins visually; forces at300steps and Y clashes remain
as recorded. Existing r5 scope is separate. No chemical stability claim follows
from visual acceptance, three neighbours or nominal120° angles.

### Common boundary identity using existing pieces

Boundary selection and seam realization are separate. A conceptual boundary
reference combines the exact structure/graph snapshot, source instance/region,
ordered atom IDs and image offsets, open/closed flag and endpoints, dangling
subset, orientation/frame, lattice/sigma, typed rim/count and termination
receipt. Use the same exact snapshot/graph vocabulary in the existing completion
proposal; this is not a new schema or an alternative port system.

| Existing boundary/operation | Current source support / limitation |
| --- | --- |
| `hexfold.build.Port` | Cyclic full rim walk, ordered dangling subset, word, normal, b/b_expected; rim_type zN/aN/mixed is derived from pattern, not just size |
| `GeneratedPort` / `_rim_payload` | Existing SE lattice `sp2-hex`, rim kind/word/N/type and atom bindings; mirror does not retain the full Hx walk/normal, so do not reconstruct missing order from geometry |
| `_ZigzagSegment` + `compose_k3` | Private open S-D-…-D-S segment with explicit endpoints; exactly3 compatible segments, equal120 and phase0 only; not3 arbitrary closed Ports |
| Generic SE `JOINERS` | Only `(sp2-hex,sp2-hex)` dispatch; generic sp3-diamond or heterogeneous joining is not established |
| Cyclic fuse/hole/collar | Named hole boundaries and constrained discrete collar solver exist; compatible equal-count z/a may use the specific30° 5–7 adapter, not an arbitrary curved-seam solver |
| sp3 fin graft | Dedicated fourth-bond construction; not generic lattice dispatch or an sp2 fuse |
| Fin120 far closure/end boundaries | Direct known cross-wall pairs; no exported ports. An arbitrary selected open edge cannot be substituted without topology review |
| Real cut vs clipping | A real cut changes atom/edge/face/termination identity; clipping only hides geometry and creates no chemical boundary |

Curved paths, arbitrary oriented surface edges, unsupported boundary types and
unequal-dihedral variant B need separate topology design/measurement. Bending
coordinates does not supply that graph construction. Bounds/normal/port name
alone cannot establish boundary equivalence or an allowed seam realization.

### Optional future validation plan — coordinator review only

If separately assigned, first validate the existing matched pair as **two
isolated dev builds**, one per side with the explicit parameters above. Proposed
cap:8CPU/16GiB,10min per side,20min total,$0 external; includes the existing
3000-iteration pinned preview, no MACE/xTB, retries, extensions or prod writes.
These are proposed hard limits, not performance estimates or permission.
Stop on refused inputs, cap/nonfinite result or inconsistent identity/counts.

Record pre/post-H graph and connected components, all C-only/total degrees,
declared faces and separately defined bounded cycle census, seam/far-fuse/end
membership, edge lengths, local angle distributions, normalized triple product
and method-qualified POAV/pyramidalisation. Include bond opening, all short
contacts split by hop distance and H/C families, cap overlaps and changed
neighbour candidates without automatically rebonding. Check current and seed
geometry separately; zero findings under an exclusion rule is not all-clear.

Shape checks use fixed seed memberships/axis and central axial bins, both
principal transverse widths, profile asymmetry, self/opposite-wall contacts and
collapse descriptors. Compare constrained seed/preview honestly; neither a
fitted teardrop nor an unavailable revolution-target receipt supplies an
independent target. `surface_deviation` remains unavailable. Later free bonded
relaxation/electronic work would need its own candidate/model/budget assignment;
the completed pilots' GO supplies no extra candidates or reset step allowance.

Review first, then coordinator assigns any code/wording/generation slice.
No source implementation, tests, generation, new job, version, ship or deploy
was performed for this feasibility report.


## Exposed-edge hydrogenation policy follow-on

Reto's new request: “Ah we should by default hydrogenate the edges at the
appropriate time. Do we have an sp2 joined carbon tube Y (with sheet inside,
and one with sheet outside?” The accepted `1bce7d756` source review remains
complete. This is a source audit and proposed next phase, not generation or
writer authorization. Fold this policy into the coordinator's existing
completion/boundary spec; do not create another generator or approval item.

### Availability and existing semantics

`fin-k3-120-z` already provides side `in` / `out`: three half-sheet sectors
meet at a degree-three seam, and two sectors close into the tube wall. This
is a tube plus internal/external sheet with a Y-shaped local cross-section,
not a branch joining three tubes. Intended sp2 topology is not electronic
hybridisation proof. The saved dogfood fins identified by BEFORE `fi472892`
are sp3 fourth-bond grafts. A stored, H-matched sp2 in/out pair has not been
established; ranked search is not a complete inventory. The accepted w18/p20/s4
pair with explicit H remains a two-build proposal, not authority to execute.

Source bytes in the ten-file audit match canonical deployed `91d6af135`
exactly. Native symbol discovery preceded focused local reads; the indexed
server root is not this task tree. No builder or test was invoked in this audit.

| Existing seam | Actual behavior | Policy consequence |
| --- | --- | --- |
| `generators/hexfold_spec.py::terminate_mode` | Omitted mode is H; accepts H, none, ports-open; rejects literal open | Retain compatibility and explicit exceptions; do not invent a new mode |
| `_terminate_open_edges` | Caps under-coordinated authored C sp2/sp3; skips exported rim dangling atoms (or atom walk) for ports-open; none adds nothing | Degree deficit is the current heuristic, not proof that a site is a genuinely exposed edge |
| `_block_from_net` | Appends caps after carbon stick/tether preview and carbon judgement; records mode/count/hosts/clashes/bond length and terminated:h | The existing default is not automatic post-assembly/pre-physical-relax finalization or proof of closed-shell chemistry |
| `fin120.py::build_fin120` | Portless net; side-specific carbon preview precedes H addition | ports-open is H-equivalent here; explicit H makes a future pair's policy unambiguous |
| `hexfold/build.py::_apply_terminate` | Explicit hx termination runs after connects, adds H and consumes matched ports; existing spec H is not duplicated by the SE cap helper | Preserve consumed attachment identity and distinguish explicit spec termination from appended generator caps |
| `atomic/join.py::_rebuild_block` | Removes the full recorded appended-H tail from generated parts before composing, not only the selected rim's caps; original stored part remains intact | The materialized composite must later finalize all remaining exposed edges; do not mistake the original part's H tag for composite saturation |
| `atomic/join.py::_hexfold_join` | Creates a scene from composite elements/bonds and a join record; no cap/finalization call or termination mode in that record | This is the concrete post-join coverage gap; join is not a universal automatic H path |
| `tests/test_se_scene_replay.py` | Explicit H/none/ports-open survive replay; omitted mode retains omitted input shape | Preserve deployed fi472844 behavior; record requested versus effective policy without rewriting old replay input |

The cap helper only supports one/two missing directions: it caps at most two,
skips degree-zero and unknown-hybridisation sites, and does not itself establish
valence correctness for every carbon graph. New automatic completion must report
remaining deficits/overcoordination or ambiguous boundary intent explicitly,
rather than infer closed-shell completion from an H count or tag.

Cap checks also need a precise family audit. The existing placed-H versus
existing-atom loop calls `profile.clash_bar("C", elements[0])`, not the actual
H/other-element pair. Current default C–H and C–C fallback bars both equal1.8Å;
H–H has its own1.5Å bar, so do not claim all current counts are numerically
wrong. Preserve actual pair species, declared-bond exclusions and separately
reported short two-hop carbon contacts. Existing terminate.clash findings are
WARNs and retain the caps; a report's ok flag is not cap-clean evidence.

### Concrete next phase proposed to coordinator

1. Resolve supported topology changes, real cuts and attachment consumption
   first. Use existing ordered rim/port/hole and graph-cut identities. Visual
   clipping and SE display offsets do not create physical edges or contacts.
   Keep future attachment rims explicitly reserved/open until consumed or
   explicitly released; never H-cap a site that still belongs to an unresolved
   attachment. Unknown exposure/valence remains a named blocker.
2. Finalize genuinely exposed, unreserved supported carbon boundaries with the
   existing H-placement machinery before any physical relaxation input is
   frozen. Preserve explicit none and ports-open intent and show its consequence;
   do not silently change an explicitly bare experiment. Default H is the
   policy for eligible exposed edges, not a universal cap-all-degree-deficits
   or bond-inference algorithm.
3. Validate supported valence/coordination, duplicate H and finite placement,
   consumed/reserved rim membership, C–H/H–H cap contacts, and the independent
   short-contact family omitted by ordinary two-hop exclusion. No atom is
   silently deleted or rebonded to make a check pass. A failing cap/valence
   check prevents automatic geometry-finished status or unattended dispatch;
   it does not authorize repair computation.
4. Retain requested/effective termination, boundary source, cap host/atom
   mapping, preserved reservations, exclusions, cap findings and implementation
   identity using existing generated/provenance/run records. Replay must retain
   explicit none/ports-open and omitted-default input shape. Adding H changes
   composition, physical geometry identity and the relax/cache input; use the
   existing version/hash invalidation paths, never attach bare-input force
   evidence to the capped candidate. Render termination and exemptions via the
   existing shared completion envelope and viewer owner, not a parallel ladder.
5. First source slice should address the supported post-join finalization and
   replay/validation seam with fixtures for an unused versus consumed rim,
   existing spec H, explicit none, portless ports-open, a real cut versus
   clipping, unsupported deficits, species-correct cap checks and idempotence.
   Root assigns the exact writer phase, API disposition and source gates before
   implementation. No optimizer, model selection, job dispatch or new production
   candidate is part of this proposed source slice.

Original r5 bare inputs and all after candidates remain immutable; `fi473424`
and the visual assessments are preserved. No retroactive H, rerun, extra steps,
rebonding, compute or two-build execution is authorized here. The separately
validated read-only completion slice `34d139902` remains frozen and unshipped;
this policy addition does not mutate its accepted interface or source receipt.

## Adopted policy: bounded writer patch and test plan

Adopted source-only assignment: `STATE/cycle2/hexfold-default-hydrogenation-policy-assignment.json`
(observed2026-10-08T09:17:42.051873Z). Its completion control eccf7e0d and
boundary control3d6f6227 preserve their preceding versions. Current control
files also contain later conditional testpiece/flange clarifications; those
are retained upstream, not new execution authority in this audit. The earlier
hygiene-session31247 wording is historical relative to completed read slice
34d139902/full29404PASS. Preserve both terminal e38 failures and subsequent
successful immutable receipts; no gate is restarted or state regressed.

### Phase and operation map

| Operation/source seam | Actual order / applicability | Bounded change required |
| --- | --- | --- |
| `hexfold/build.py::build` | Primitive defects/real holes, collar resolution, assembly, `_apply_connects`, then explicit `_apply_terminate` | Reuse this graph-before-explicit-caps ordering; no visual clipping interpreted as surgery |
| `_apply_connects` | Supported menu attachment/fuse, then k>=3 seams and bond connections; consumed ports resolved in graph | Reserved versus consumed/exposed boundary intent must be resolved before automatic caps; no generic sp3 or arbitrary curved join assumed |
| `generators/fin120.py::build_fin120` | Validate mode; build net; pinned/tethered carbon preview; carbon findings; `_block_from_net` appends H | Keep preview labelled constrained carbon geometry; do not call it physical relaxation of the stored capped system |
| `generators/hexfold_scene.py` / `hexfold_spec.py` | Shared cap helper after preview/judgement, with cap-specific findings appended | Separate pre-cap carbon findings from post-cap validation; do not transfer carbon force evidence to the capped identity |
| `atomic/join.py::_rebuild_block` (hexfold origin) | Validates/strips recorded appended H tail and rebuilds bare graph using stored coordinates | Preserve original inputs; validate removed cap identity/host mapping and reserved rims; no unrecorded atom deletion |
| `hexfold/join.py::compose` | Fuse/adapter and seam-local pinned preview; consumed endpoint ports removed, others prefixed and retained | Keep carbon preview bare; uniform sigma has no C-H rest-length support, so do not feed new H into this preview as a shortcut |
| `atomic/join.py::_hexfold_join` | Serializes every atom as sp2, every bond aromatic; prepares scene/ports/record | A cap-only call here is insufficient: H must be s/pairwise order1, carbon metadata preserved, counts/envelope/record agree |
| `_rebuild_block` (join origin) | Recompose recorded parts, compare exact bare element sequence to stored sequence, use stored coordinates | New capped composites would otherwise fail replay; validate and remove only recorded generated cap tail for subsequent topology composition, without altering old bare records |
| `finish_join` / existing pending join | Persist prepared candidate after operation validation | Run pure finalization/validation in preparation before writes, retain atomic failure/no orphan behavior; do not add read-side finalization |
| Completed structure/SE completion reads34d139902 | Coherent snapshots and pure classification, no trusted legacy producer | Remain read-only; an H tag/count or generic converged flag cannot create termination/force evidence |

The first bounded writer implementation must be limited to the existing
supported carbon join path. Block carries elements/graph/rings/ports, not a
general bond-order/hybridisation chemistry model. Use explicit supported sp2
eligibility and existing authored metadata, and return unsupported or ambiguous
for other cases. Degree three is coordination, not a literal aromatic bond-order
sum or electronic proof. Do not widen chemistry support during this repair.
Existing H default alone also cannot distinguish a temporarily reserved port
from a finally exposed edge: all retained attachment rims stay reserved until
explicitly consumed or released; absent finalization intent is not permission
to cap a future attachment.

### Minimal patch boundaries for root review

1. In `hexfold_spec.py`, factor the existing deterministic cap placement and
   pair-specific cap validation so the supported composite preparation can
   reuse it without invoking a builder or canonical-frame rewrite. Pass actual
   eligible and reserved host identities, verify finite/nonzero neighbour
   geometry, reject unsupported deficits/overcoordination, and use actual
   H/other species thresholds. Existing generator defaults/modes remain intact.
   Keep carbon coordinates and stable labels unchanged; append caps with a
   deterministic mapping. Re-finalizing the same graph/policy must not add H.
2. In `atomic/join.py`, integrate only after topology/attachment resolution and
   carbon preview, before PendingJoin persistence. Preserve the current local
   frame and attachment relabeling. Serialize correct H hybridisation/bond
   metadata, recompute candidate counts/envelope, and preserve requested versus
   effective mode and reservation/consumption/cap maps in existing generated
   provenance. Root reviews the exact metadata extension/API disposition before
   implementation; this document does not invent a public schema or operation.
   Legacy records with no termination record retain legacy bare replay.
3. Update both hexfold- and join-origin reconstruction to verify capped versus
   bare identity consistently, including nested/chained joins, original part
   versions and cap-tail validation. Explicit spec H is never double-added or
   silently treated as disposable generated caps. Preserve explicit none and
   ports-open and the omitted-default scene replay input shape.
4. Before any later physical-relax dispatch, the writer must supply the actual
   post-cap graph/composition/geometry and validation outcome through existing
   invalidation/provenance mechanisms. A topology edit invalidates cap/force/
   postcheck evidence. This patch does not implement dispatch, optimizer,
   receipt adoption, a new completion classifier or viewer policy. Read paths
   remain mutation-free and next.can_submit remains false in the current slice.

### Meaningful regression matrix (plan only, not executed)

- `tests/test_se_terminate.py`: supported exposed versus reserved hosts,
  explicit none/ports-open, portless mode semantics, existing spec H, repeated
  finalization, zero-length/nonfinite neighbour geometry, unsupported deficits,
  species-sensitive C-H/H-H thresholds and retained diagnostic pairs. Assert
  unchanged carbon coordinates and correct H bond/host identities.
- `tests/test_se_join.py`: capped new supported composite with untouched
  originals; consumed rim receives no H, retained reserved rim stays open,
  genuinely exposed eligible sites cap once. Verify counts/metadata/envelope,
  chained/nested replay, bare legacy replay, edited/malformed cap-tail refusal,
  reserved/consumed port identity and no orphan on finalization failure.
- `tests/test_se_scene_replay.py`: keep all fi472844 explicit-mode,
  early-invalid-mode and omitted-default input-shape guarantees.
- `tests/test_se_join_geo.py` and `tests/hexfold/test_join.py`: carbon-only
  preview/rung/guard-band invariants, no new H in sigma-uniform seam relaxation.
  Use existing real-hole/collar/connect fixtures to test graph-cut boundary
  intent; a display-only clip changes neither graph nor termination.
- Existing completion identity/read tests: capped composition differs from bare
  identity; old force/postcheck evidence cannot bind to it; repeated reads
  perform zero generation/termination/dispatch. Preserve independent two-hop
  carbon-contact diagnostics rather than hiding them behind ordinary clash
  exclusions.

After root assigns the exact writer slice: focused tests, canonical full
scripts/test and container typecheck, payload lint/format and immutable source
review. This is a test plan, not an executed validation or ship warrant.
No code, saved structures, jobs, optimizer settings, step limits or web source
changed in this source-only audit.

## Conditional two-attachment candidates: source-only discretization and budget

Adopt assignment `STATE/cycle2/hexfold-two-attachment-testpieces-assignment.json`
(SHA256 prefix aa55ab4d, observed2026-10-08T09:19:26Z), control boundary8a580150
and completiondba591a2. This follows completed local read implementation
34d139902, still unshipped and awaiting root source review. It is preparation,
not generic-seam implementation or permission to construct candidates.

### Same parent and two distinctly bonded variants

The control explicitly defines approximately1nm as **axial height**, not tube
diameter. Proposed common parent: finite armchair (12,12), five axial repeats,
open ends, C-C reference1.42Å. Source `lattice.py` gives translation T=(1,-1),
period a=sqrt(3)*1.42=2.459512Å, circumference51.12Å and radius8.136001Å.
There are240 parent carbon atoms. The atomic axial planes span
(5-1/2)a=11.067805Å (1.106780nm); four repeats span8.608293Å, farther from10Å.
The nominal five-cell length5a=12.297561Å is not the carbon bounding height.
Reference z=0 is the lowest carbon plane; +z follows T, angle0 is +x and
angle180 is -x in the seed frame. This is a deliberately wider1.627200nm
diameter tube to leave room for the internal sheet, not a1nm diameter claim.

Use three central attachment columns j=1,2,3 with z=j*a, span4.919024Å.
At angle0 their A-site representatives are (j,-j,A); adding (6,6) gives
(6+j,6-j,A) at angle180 with identical axial coordinates. The half-chiral
translation is an integer lattice vector, so this even-n seed has exactly
opposite rows, zero angular rounding error and no axial phase offset.
The two host sets are disjoint; the final graph must also prove their local
neighbourhoods do not overlap after any seam surgery. Angles are seed site
azimuths, not promises that a free relaxed cross-section stays circular.

Each short sheet uses two row pairs (the current minimum), radial height
2.84Å from its attachment rim, with equal three-column seam/graft span.
The external sheet starts at +x and extends outward; the internal sheet
starts at -x and extends toward the axis (+x). These are two attachments on
ONE tube in EACH candidate:

| Candidate | Connectivity target | Required distinction |
| --- | --- | --- |
| `hex-two-attachment-sp2` | At BOTH axial lines, two wall half-sheets plus the attached sheet meet through a trivalent k3-intent seam | Must replace/reconstruct supported wall boundaries; adding a fourth bond to an intact trivalent wall is not sp2 |
| `hex-two-attachment-sp3` | At BOTH lines, retain three wall bonds and add one C-C graft to each selected host, making six distinct four-coordinate wall hosts | Sheet rim carbons remain trivalent; “both sp3” describes the two graft seams, not every sheet atom |

Both descend from the same specified parent geometry/topology, but sp2 seam
reconstruction necessarily changes atom/bond identity and may require defects.
Do not call the finished variants atom-for-atom matched, give invented final
C/H/ring counts, or force identical relaxed dimensions. Final graph/ring census,
ordered cut boundaries and endpoint treatment must be part of generic-seam
review before either piece can be generated.

### Source feasibility and concrete conflicts

- `generators/fin.py::_normalize` and `fin120.py::_normalize` each require
  exactly ONE feature; neither accepts angle or an axial subset. Scene dispatch
  only routes the singleton fin type. There is no established combined
  generator, and two one-fin runs do not construct this shared tube.
- `hexfold/fin.py::fin_tube` selects the angle-zero generatrix, filters out
  non-degree-three end sites, then uses all eligible sites. Selecting the
  central three and the opposite row needs the future reviewed generic
  attachment capability. For three graft columns and two strip row pairs,
  the existing trimmed-strip formula has axial extent0.5a..3.5a after placement
  at z=a:1.229756..8.608293Å. Both sheets fit within the parent carbon planes;
  this is a source-derived footprint, not an emitted structure.
- `hexfold/fin120.py::seam_tube` creates a single cusp/notch with one k3
  seam and a far-side wall fuse. Rotating another complete result would
  duplicate the wall, not install a second seam. `compose_k3` only accepts
  three finite planar carbon segments with equal spacing/counts, phase0 and
 120/120/120 intent; it is not a generic curved-cut solver.
- The current untrimmed `y_junction.py::_sheet` guard columns for three seam
  sites span -a..3.5a. Translated to the proposed z=a seam start they reach
 0..4.5a, exactly both parent end planes. There is **zero end-plane separation**
  for that naive reuse, unlike the trimmed sp3 strip. Endpoint cap/valence and
  wall-cut closure therefore need explicit supported treatment. Do not trim
  coordinates, invent end bonds or silently lengthen the parent to conceal it.
  Root must review the finite graph treatment or the exact shared discretization
  revision before generation.
- In the cylindrical sp3 seed, the inward carbon tip radius is
  R-1.42-2.84=3.876001Å. A conservative1.09Å inward H projection leaves2.786001Å.
  The existing carbon-only2Å axis clearance needs n>=10 for two row pairs;
  n12 supplies additional cap margin at modest cost. These are geometric
  screening bounds, not proof of no cap, opposite-wall or inter-sheet clash.
  For the reconstructed/deformed sp2 wall, recompute from actual candidate
  coordinates; the circular bound does not certify it.
- Unsupported valence/bond order, nonfinite placement, host overlap, crossed
  seam bonds, incompatible rim counts, shared consumed ports and endpoint
  cap clashes stop preflight. Report declared faces, graph cycle census and
  any needed defects separately. Local120-degree intent and four-coordination
  labels are not electronic proof; no teardrop is authored or required.

Scalar arithmetic receipt: `.scratch/source-review/two-attachment-source-arithmetic.json`.
Only Python stdlib math evaluated formulas; no project import, builder, graph,
coordinate generation or test ran. Nine source blobs were compared byte-for-byte
with canonical deployed91d6. Native fin_tube search/outline agrees; native
indexed root differs from the task tree, so exact local source is the audit base.

### Conditional future MCP sequence and replay

1. After root source review and live generic-capability deployment, inspect the
   actual MCP help/source capability and owner-provided exact serving version.
   Confirm two same-parent attachments, opposite integer-lattice rows, finite
   sp2 boundary treatment, explicit mode/reservations and replay are supported.
   These operations are currently **UNAVAILABLE**; no guessed JSON opcode,
   local builder, raw DB or alternate API may stand in for MCP capability.
2. Future MCP construction inputs are exactly parent(12,12), five repeats,
   columns1..3, two-row-pair sheets, external0/internal180, and both seam modes
   sp2 or bothsp3 per candidate. Record parent identity and endpoint/defect
   treatment approved for the sp2 variant. Execute both graph attachments
   with rims reserved, then resolve every attachment and finalize exposed-edge
   H using the reviewed policy. No physical relaxation between attachments.
   Exact wire operation names/arguments remain unavailable until the generic
   implementation has a verified MCP contract; this is a parameter/ordering
   plan, not an executable call.
3. Get each candidate's structure stats and all diagnostic families through
   MCP; verify one connected carbon system, intended coordination, explicit
   C-H/bond orders, cap and two-hop contacts, shape/end clearance and exact
   final graph identity. Bind before evidence and cumulative lineage budget.
   Preserve requested/effective termination, atom/port/cap maps, ordered
   operations, both input/output identities and implementation version.
4. Replay BOTH attachments plus termination into a distinct candidate and compare
   exact deterministic identity under the defined replay contract; do not
   duplicate physical relaxation. Editing either attachment invalidates cap/
   force/postcheck evidence for the whole candidate. Reads/polls do no work.
5. Only under subsequently active execution authority and verified MCP resource/
   model controls: one bounded relaxation per validated candidate, sequentially.
   Save two distinct after candidates and all before/after evidence; never
   overwrite parent, these inputs or the six earlier pilots. Illustrative names
   above are not native IDs, stored objects or new API schema.

### Finite minimal-compute proposal — no launch authority

Two candidates only; one physical run each after both attachments and H.
Propose the same cached MACE-MP small checkpoint
`2ddb079cee0e131eaaf6912ba581b394551ead283e95c99cfe78c605d10b5736`;
verify the actual calculator/checkpoint at launch, never infer small from a
family selector or substitute mace_off. FIRE fmax0.05eV/Å, maxstep0.03Å,
all atoms free, fixed nonperiodic cell, no wall/cross-section/fin tether.
8CPU threads and16GiB peak, sequential, incremental external/cloud cost$0,
no paid fallback/xTB/DFT. Proposed walltime15min each/30min total, with rough
planning expectation5–15min each; this is not a benchmark or convergence
promise. Missing cached model, MCP resource controls or capacity blocks launch.

Each lineage has at most300 cumulative physical optimizer steps,600 across
the two independent lineages. Stages, retries, restored runs, revised graph
hashes or renamed outputs do not reset the counter. Prefer no staged runs;
record initial/final energy, actual max force, steps and walltime. Stop on
convergence, walltime/step cap, nonfinite evaluation or invalid geometry.
No automatic extension inherits from the earlier six-pilot GO. Any future
runtime revision must be finite, recorded before cap expiry and lie within
then-current explicit authority; it cannot expand300steps.

Acceptance records measured convergence separately from visual outcome:
bond/angle/POAV, declared versus comparison graph, all clash/short-contact
families including two-hop pairs, seam neighbourhoods, H and C contacts,
both sheet footprints, end/opposite-wall clearance, shape/collapse and
displacement from seed memberships. Seed is not a trusted relaxed target.
Capped means stopped, not converged; local minimum is not thermal stability.
No new generation/jobs/implementation/version/ship is performed by this plan.

## Conditional flange Y: closed-boundary source plan

Adopt assignment `STATE/cycle2/hexfold-flange-y-testpiece-assignment.json`
(SHA256 prefix6a30137b, controls boundary42d9d790/completiond21b44de), request
`inbox/hexfold-flange-y-testpiece-request.md`. Its later same-piece
09:24:48 third-direction H and09:28:38 local120-degree clarifications are
included. This is the third distinct conditional candidate, after the two
opposing-attachment variants; not another variation of a three-tube branch.

### Labelled topology and geometry intent

Propose one tube ending through a finite sheet, with a narrow flange carrying
a **closed** Y seam. Three carbon sectors meet along that loop: T connects
to the tube, S connects to the surrounding sheet, F is the short exposed
flange lip. Hydrogen terminates the eligible distal edge of F, not the
already trivalent Y seam and not a substituted tube/sheet bond.

```text
Local section normal to seam tangent tau (tau points around the loop):

             T: tube continuation
            /
           / 120 degrees
          Y ---------------- S: surrounding sheet continuation
           \
            \ 120 degrees
             F: exposed flange lip ---- C_F ---- H caps

C_Y = seam loop through Y; it points out of this local section.
C_T / C_S = attachment rims leading into T / S (consumed on attachment).
C_F = distal boundary of F (exposed; eligible remaining valences get H).
```

For an unambiguous local-frame proposal let tau=e_phi, with the section
spanned by outward e_r and axial e_z. Directions AWAY from Y are
S=e_r, T=-e_r/2+sqrt(3)e_z/2, F=-e_r/2-sqrt(3)e_z/2. All pairwise dot products
are -1/2. These are local surface-sector directions, not seam tangents and
not automatically atom-to-atom bond vectors. If T were kept vertical and S
flat radial immediately at Y, their90-degree angle could not also be an
equal120-degree three-sector junction. A supported collar/transition must
therefore bend between the far tube and local T direction. This mathematical
sketch is design intent, not an authored relaxed cross-section or proof that
a carbon graph with those directions closes around the loop.

For small-scale review, retain the proposed(12,12)/five-repeat tube reference
from the two-attachment plan. A nominal Y radius near8.14Å, sheet outer radius
near15Å, and a two-row-pair F lip (~2.84Å transverse width) are **size targets
only**, not executable lattice parameters. The projected inward/up/down
clearance must be measured on actual graph coordinates. No sheet extent,
cyclic period count, defect recipe or final atom count is claimed feasible
from those geometric targets. Prefer a candidate <=1000 total atoms including
H for the proposed budget; if the reviewed discrete graph exceeds this, stop
and return a revised finite plan rather than silently expand cost or delete
topology. Do not inherit an arbitrary clip/annulus as a graph-cut operation.

### Boundary identity and exact reuse disposition

| Boundary | Topological role and end conditions | Existing representation / limitation |
| --- | --- | --- |
| C_T | Closed ordered tube-end rim, consumed by T attachment | Existing tube Port atom/dangling walk, word/count/frame and chirality; do not conflate the port walk count with N dangling sites |
| C_S | Closed ordered real hole in the sheet, consumed by S attachment | Existing hole Port and graph removal via _apply_hole; specify incident ring/site/direction and retain the removed disk identity |
| C_Y | Closed three-sector junction, no open seam endpoints | Requires cyclic three-sector correspondence and final wrap bonds; current private _ZigzagSegment is OPEN and cannot represent this by dropping endpoint checks |
| C_F | Closed perimeter walk of an exposed carbon edge (physically open valences), not a fourth attachment | Existing boundary/atom identity plus termination hosts; all three Y sectors retained, H only eligible distal-edge deficits |
| Far tube end / sheet outer perimeter | Closed boundary walks with exposed eligible sites after final assembly | Default H after joins; explicit none/ports-open/reservations remain visible and intentional |

A “closed boundary walk” does not mean a sealed tube, satisfied valence or
periodic simulation cell. The finished finite system stays nonperiodic;
cyclic seam wrapping is graph topology, not PBC. Only actual graph surgery
creates C_S; viewer clipping/display offsets do not.

Reuse `hexfold/build.py::_apply_hole` for deterministic real hole selection
and ordered boundary recovery; `_solve_collar` and `_record_collar` for
supported discrete defect placement/expanded replay; `_apply_connects`
for supported fuse/attachment consumption; the reviewed H-finalization seam
for exposed-edge completion. Collar disjointness is explicit: hole disk plus
one corona versus defect wedge disks. Source restricts sheet symmetry to
k in{1,2,3,6}, and tube collars require k dividing gcd(n,m). The n12 reference
admits divisor symmetries such as6, but this is only a necessary source
condition, not a collar solution or port-count match.

Existing tube/closed-hole fuse and the specific adapter are TWO-rim seams.
They do not produce a third sheet sector by adding an H cap or by changing
metadata. `join.py::compose_k3` / `_segment_frame` demand three planar
straight equal-spacing zigzag segments, open endpoints, phase0 and120/120/120.
That is useful local graph/validation reference, **not a cyclic flange
implementation**. No general sp3 join, cyclic k3 mapper or arbitrary curved
seam is assumed. An open straight local coupon could test a helper but would
not fulfil this closed tube/flange request; it is not added as an extra
candidate or fallback.

Before construction the generic cyclic seam must return: exact ordered
C_T/C_S/C_F/C_Y atom identities and orientation, compatible dangling counts/
words, one-to-one mappings and phase around the full loop, closure bonds,
all declared faces/defects and an independent cycle census. The transported
local frame must return consistently after one circuit, without a gap,
twist, duplicate host, self-intersection or unrecorded lattice mismatch.
Unequal counts or required defects remain explicit; neither a rounded
drawing nor a collar-fit suggestion establishes those bonds. Current source
does not supply this composition, so exact cyclic graph counts and a runnable
MCP operation sequence are unavailable pending reviewed implementation.

### Valence, phase ordering and replay

Resolve the real sheet hole, both T/S attachments, collar and all three
sector mappings before any cap. Every intended Y carbon has the supported
trivalent neighbourhood; adding H to a saturated seam would violate that
contract. Validate actual neighbouring atoms/bond orders, not just a degree
tag. F's exposed distal rim, the far tube end and sheet outer boundary can
then receive H where eligible; future reserved attachment rims stay open.
Check cap duplication, host conflicts, remaining/over-valence, zero bonds,
finite coordinates, seam/end/contact crossings and all H-C/H-H/C-C contacts
including short two-hop pairs. Local sp2 intent/pyramidalisation and120-degree
angles are measured separately; no electronic or thermal stability claim.

Future MCP sequence remains conditional: verify live generic cyclic capability
and exact serving implementation; create distinct input through that supported
contract; compose/cut/attach both bonded sectors and F; finalize eligible H;
inspect structure stats/all diagnostic families; freeze before geometry/graph/
termination identity and cumulative budget; then a single bounded physical
relaxation into a separate after candidate. Missing opcode/phase/mapper is
reported unavailable, never replaced by local builders, raw database writes,
a direct Python call or guessed operation parameters.

Replay records the selected hole/ring/direction and original graph, collar
expanded defects, all ordered rim maps/orientations/phases, wrap bonds,
consumed/reserved boundaries, requested/effective termination, cap hosts,
source version and exact pre/post geometry. Replaying T must not discard S
or F. Any graph/termination edit invalidates cached force/postcheck evidence.
Use existing generated/provenance and shared completion identity stores;
no second schema/classifier or read-side generation. Originals, both earlier
proposed variants, all six measured pilots and fi473424 remain intact.

### Finite aggregate proposal for the full conditional set

THREE candidates total: both-sp2 opposing sheets, both-sp3 opposing sheets,
and this one flange Y. Propose15min per candidate,45min aggregate; sequential
8CPU/16GiB, cached exact MACE-MP small checkpoint
2ddb079cee0e131eaaf6912ba581b394551ead283e95c99cfe78c605d10b5736,
FIRE fmax0.05eV/Å/maxstep0.03Å, all atoms free/fixed nonperiodic cell,
no shape tether, $0 incremental external/cloud and no paid/xTB/DFT fallback.
Flange rough planning5–15min depends on the reviewed <=1000-atom topology;
no timing measurement or convergence promise is implied.

At most300 cumulative physical optimizer steps per lineage,900 for the three
independent lineages; no reset by retry, name, stage, updated graph or cap
change. No previous pilot extension is inherited. Step/wall cap, convergence,
nonfinite evaluation or invalid geometry stops the run. Any finite runtime
revision must be recorded before overrun and covered by then-current explicit
authority; it never raises the step ceiling. This table is a budget proposal,
NOT run authorization.

Require before/after energies and actual forces, method/constraint/checkpoint
identity, steps/runtime, cap findings, valence and declared/comparison graph
diagnostics, all short contacts/POAV, local-frame seam angles around the whole
loop, flange/tube/sheet shape and collapse. No trusted relaxed target exists
from the drawing. Cap stop is not convergence and local minima do not establish
thermal stability. Root reviews the cyclic graph/collar feasibility and
sequences implementation/live capability before future MCP-only use. No
builder/test/job/rerun/overwrite/writer/version/ship/new issue was performed.


## Post-join writer design: exact private seams for root review

Adopt `cycle2/hexfold-h-policy-control-adoption.json`: boundary68b74daf /
completion2a8ffcd3, preserving all preceding requests. This refines the
1966af199 plan; it does not authorize its implementation. H1–H3 are already
corrected at490b3819b: focused129, types2594 and full29420 tests pass with
terminal exit0. Their source acceptance remains separate. No gate is restarted.

### Bounded support and cap provenance

First support only the existing carbon sp2 fuse/adapter composition, with
provable ordered graph/port mappings. No general sp3, combined-fin or curved
cyclic seam is introduced. Authored H and H of unknown origin are never
stripped. The current carbon-only seam preview cannot correctly carry them;
the first slice therefore returns unsupported BEFORE preview or writes when
such H is present, preserving the original structures. It must not silently
convert H to carbon/sp2 or select a different topology.

Existing `generated.terminated` records contain count and
`hosts=sorted(set(h_hosts))`, not an ordered H-to-host map. A count, H tail,
tag or element-sequence match alone is insufficient provenance. For a legacy
generator record, reconstruct the expected bare topology from its exact
recorded spec, distinguish explicit spec H from appended caps, and verify
the actual stored bonds: each claimed generated H has exactly one carbon
host, pairwise order1, no other incidence; all non-cap bonds agree with the
replayed graph. Validate count, ordinals, unique-host projection, elements,
recorded mode and source identity together. An ambiguous or edited record
returns unavailable, not a guessed removable set. Stored coordinates remain
authoritative: do not require caps to remain at their generated coordinates
after relaxation, or replay an optimizer to establish provenance.

For newly prepared outputs record the complete ordered cap-to-host mapping,
generated origin and exact input/output identities. This is a proposed
additive extension to existing generated provenance, not a new table or
public operation. A field's presence is not itself trusted proof; it must
match the loaded graph and provenance. Old bare join records retain their
existing replay interpretation and do not gain a fabricated H policy.

Verified generated caps may be separated from a COPY of the carbon skeleton
during composition because the existing preview has a single C–C rest
length. After topology and preview, regenerate eligible caps deterministically
on that candidate; record the old-to-new cap mapping and which consumed or
reserved hosts no longer receive caps. This may reposition generated caps;
it is explicit candidate preparation, not preservation of their old force
evidence. Never modify the input structures, delete authored H, or claim
that stripping/re-capping is physical relaxation.

### Proposed private types and callables

All names below are proposed, not implemented. Use immutable in-memory
records; no second persisted graph or completion schema. Ordinals index the
captured, ordered input or composite rows and must be accompanied by the
existing identity/label mapping at each boundary.

| File / callable | Exact proposed inputs → result | Responsibility |
| --- | --- | --- |
| `atomic/join.py::_verify_generated_caps` | `(scene: Scene, generated: Mapping[str, Any], bare: Net) -> _VerifiedCaps` | Pure validation of existing stored atoms/bonds against rebuilt bare topology and generator record; returns ordered (H ordinal, C ordinal) pairs and bare-to-stored ordinal map, or raises `_JoinStale`. No removal based only on count/tail. |
| `atomic/join.py::_rebuild_join_input` | `(store: Store, struct_slug: str) -> _JoinInput` | Store-aware replacement behind existing `_rebuild_block` callers: captured source identity, bare Block, ordered labels, verified caps, original termination request and reservation provenance. Both hexfold and join origins use the same verifier; retain existing tuple wrapper only where needed. |
| `atomic/join.py::_resolve_termination_scope` | `(composite: Composite, *, requested: str | None, reserved_ports: frozenset[str], released_ports: frozenset[str]) -> _TerminationScope` | Pure: requested/effective mode, consumed/reserved/released boundary identities and eligible/reserved host ordinal sets. `None` denotes omitted input, never explicit none. Ports must refer to the actual resulting composite; reject unknown, overlapping or ambiguous reservations. |
| `atomic/join.py::_termination_net` | `(composite: Composite, scope: _TerminationScope) -> Net` | Lossless supported-carbon adapter with unchanged ordinal order, bonds, rings, lattice and proven sp2 intent. Reject unsupported species/chemistry or missing correspondence; no builder, reframe or topology inference. |
| `generators/hexfold_spec.py::_terminate_open_edges` | Existing `(net: Net, coords: np.ndarray, mode: str)`, plus keyword-only `eligible_hosts: frozenset[int] | None = None` → existing host/position lists | Reuse existing placement algorithm. Omitted mask preserves generator behavior; a provided empty mask adds no H. Validate allowed hosts and finite/nonzero neighbours before normalizing. |
| `generators/hexfold_spec.py::_finalize_termination` | `(net: Net, coords: np.ndarray, *, scope: _TerminationScope) -> _TerminationResult` | Pure helper shared with `_block_from_net`: existing carbon rows unchanged; append deterministic H/s atoms and pairwise order1 C–H bonds; ordered cap pairs, validation findings and requested/effective provenance. Scope/result types live beside this helper so plugin imports remain one-way. |
| `generators/hexfold_spec.py::_validate_termination` | `(net: Net, coords: np.ndarray, cap_hosts: tuple[int, ...], cap_positions: np.ndarray) -> tuple[HxFinding, ...]` | Actual species-pair cap contacts, allowed valences and finite geometry; no relaxation and no suppression of two-hop contact diagnostics. Use existing report/profile types. |

`_VerifiedCaps` carries ordered pairs and ordinal maps, never a bool trusted
solely from metadata. `_JoinInput` carries original provenance separately
from carbon assembly coordinates. `_TerminationScope` carries requested
mode (including omission), effective mode, eligible/reserved host sets and
boundary dispositions. `_TerminationResult` carries the finalized rows,
bond metadata, cap mappings and findings. Concrete dataclass field definitions
and the additive generated-record encoding are part of the reviewed writer
slice, not a new API commitment in this source-only packet.

For post-join default H, retained attachment ports are RESERVED by default;
consumed ports disappear only through real composition. Non-port exposed
eligible sites can be capped. Releasing a retained port requires explicit
supported finalization intent; no inference from its being unconnected in
one snapshot. `ports-open` keeps all remaining port dangling sites open,
even if a separate release set would otherwise expose them. Explicit
`none` adds no generated H and remains visible as an intentional override.
Conflicting declarations return unavailable. The first implementation need
not expose a release operation: it may support only automatic reservations
and reject requests it cannot express. No new opcode is proposed here.

The supported validator uses authored sp2 coordination and actual C/H
neighbours, distinguishing C–H order1 from the existing aromatic representation.
It must not equate the sum of aromatic4/3 orders with integer valence, infer
electronic sp2 from degree, or silently cap unknown hybridisation. Reject
overcoordination, duplicate/self bonds, unsupported deficits and nonfinite/
zero-length neighbour vectors. Preserve authored/unknown H on failure.
Existing cap-contact WARN findings remain visible; this plan does not silently
change all WARNs to fatal or claim a contact-free physical result.

### Integration, replay and proof invalidation

`_hexfold_join` resolves and verifies inputs, composes supported carbon
topology, performs its existing labelled preview, resolves reservations,
then finalizes/validates the candidate before constructing `PendingJoin`.
Serialize per-species hybridisation and bond kind/order, replacing the
current unconditional sp2/aromatic assignment. Preserve its local frame and
label/port relabeling; do not invoke `_block_from_net` wholesale and apply
canonicalization twice. Recompute final envelope and counts from finalized
rows. `finish_join` stays the single existing persistence path, reached only
after validation. Failures create no output structure or partial SE binding.

Replay compares the original requested mode including omission separately
from the resolved effective mode, boundary state and ordered cap mapping.
Persisted source version alone is insufficient: validate actual graph/row
identity so same-version rewrites and edited cap bonds cannot replay as the
old inputs. Chained joins must replay the recorded cap transformation at
each node, not recompute yesterday's policy using today's defaults. Bare
legacy joins remain bare legacy outputs; ambiguous old capped records fail
explicitly. Never consult a mutable catalogue to repair replay identity.

Reuse `Store.structure_save` and existing generated metadata; no migration
or modification of archived rows/runs. The new output identity includes its
actual species, atoms, bonds, cell and PBC through the accepted canonical
encoder. A parent/pre-cap force result cannot qualify that output, even if
carbon coordinates coincide. Copy no parent `last_relax`, converged flag,
cache origin or postcheck into the candidate. Preserve old runs as history;
do not delete caches globally. The shared completion reader remains the
only classifier and `next.can_submit=false` remains unchanged. A future
writer must validate existing cache/proof invalidation on the exact capped
identity before any separately assigned physical dispatch; this plan changes
neither optimizer nor worker defaults.

### Exact fixture / file plan for the later writer slice

| Existing test file | New deterministic cases and decisive assertions |
| --- | --- |
| `tests/test_se_terminate.py` | Two generated H on one host proves unique-host list is insufficient; exact cap pairs retain multiplicity. Empty eligible set; reserved/released conflict; H/ports-open/explicit none/omitted H; repeat finalization adds zero duplicates. Zero/nonfinite vector and unsupported deficit rejected. Different test profile H–C vs C–C limits proves actual species lookup; H–H findings retained. |
| `tests/test_se_join.py` | Existing Z8 fuse and Z12/A12 adapter inputs: unchanged original scene hashes; carbon-only preview; consumed rim uncapped, retained rims reserved, eligible outer edges capped once. Correct H/s and C–H pairwise order1 serialization, final counts/envelope and no orphan on failure. |
| `tests/test_se_join.py` | Legacy count/tail with wrong host, extra H bond, missing cap, negative/malformed count, unknown origin or same-version graph edit refuses BEFORE compose/save. Explicit spec H, authored scene H and unknown H remain unchanged and unsupported, not stripped. Valid legacy generated cap proof accepted only with all graph checks. |
| `tests/test_se_join.py` | Chained/nested capped outputs replay through exact recorded mappings; original version/identity mismatch fails. Legacy bare joins remain replayable. Policy/default changes do not reinterpret prior outputs. Released-port intent unsupported by the available operation returns unavailable. |
| `tests/test_se_scene_replay.py` | Preserve fi472844 omitted-default shape, exact explicit modes and early invalid-mode rejection; requested none cannot turn into effective H on replay. |
| `tests/test_se_join_geo.py` / `tests/hexfold/test_join.py` | Existing carbon preview and guard-band behavior retained; a spy asserts no H enters the uniform-sigma preview. No new physical compute. |
| `tests/test_structure_completion.py` | Capped versus bare identity differs; parent success and pre-cap proof do not qualify output; authored/unknown H failure creates no evidence; reads still perform zero generation/dispatch. Use existing fixtures/decoder, not a competing classifier. |

Implementation files are bounded to `atomic/join.py`, shared
`generators/hexfold_spec.py`, owning docstring rationale and the listed
regressions. Store, optimizer, web, standalone hexfold chemistry and public
operation changes require a separately sequenced design if implementation
uncovers a dependency. Run focused regressions, canonical full suite,
container types and Ruff only AFTER source implementation is assigned;
the present packet executes no builders, test fixtures or compute.

Exact availability remains: existing fin-k3-120-z provides inward/outward
local three-half-sheet tube/sheet seams, not a three-tube Y. A saved matched
H-terminated pair is not established by this audit. All optional builds,
combined attachments and cyclic flange construction remain unexecuted.


## Narrow writer checkpoint following root review1966af199

Adopt `cycle2/hexfold-h-patch-plan-root-review.json`, boundaryc05eea95 /
completionffbea679. This is ONLY the remaining private signature,
existing-provenance examples and callsite checkpoint. The accepted file/test
plan is unchanged. It supersedes any earlier suggestion that merely
retaining ports establishes positive exposure intent.

Read-slice priority is already satisfied locally: immutable490b3819b,
receipt `cycle2/hexfold-review-corrections-final.json` SHA256
5d814be431b2dd2fffd740bba9d89ae6664839bbcff1339a9bd2bf5c783f5262.
H1/H2/H3 corrected; full suite 29420 pass, types 2594, focused 129, all exit 0.
Green34 and its historical failures remain preserved. Await corrected
source acceptance, not first review or permission to apply those fixes.

### One internal preparation contract; no public release operation

Refine the proposed scope helper to the following private signature:

```python
def _resolve_termination_scope(
    composite: Composite,
    *,
    requested: _TerminationRequest,
    parent_modes: tuple[_ParentTermination, _ParentTermination],
    exposure: _ExposureIntent | None,
) -> _TerminationScope:
    ...
```

`_TerminationRequest` fields: `present: bool`, `mode: Literal["H",
"ports-open", "none"] | None`; absent is exactly (False, None), and
explicit none is (True, "none"). Reject inconsistent pairs.
`_ParentTermination` fields: `effective_mode: Literal["H", "ports-open",
"none"] | None`, `request_known: bool`. Unknown must stay unknown.
`_ExposureIntent` fields: `eligible_hosts: frozenset[int]`,
`reserved_hosts: frozenset[int]`, `consumed_ports: tuple[str, ...]`,
`reserved_ports: tuple[str, ...]`. These reference the validated composite
and existing boundary mapping, not caller-invented atom IDs; the caller
must prove complete disposition of relevant exposed deficits. No released
port field, release opcode or interpretation of an unconnected port as
exposure is introduced.

Missing exposure intent, conflicting parent effective modes, unknown
parent policy needed to resolve a default, invalid mappings or incomplete
coverage raise `BadInput` before preview/persistence. A new request does
not silently override conflicting parent modes in this bounded phase.
An explicit none request is recorded, but does not excuse ambiguous
composition/provenance. A legacy bare result can still be reconstructed
without becoming eligible for a new finalizing operation.

A verified exposure contract does not currently exist on every join call.
Where the existing operation cannot supply one, the proposed new finalizing
path returns unavailable. The global policy is not a synthetic per-boundary
receipt. Root can sequence the supported caller later without inventing a
release in this helper.

`_finalize_termination(net, coords, *, scope) -> _TerminationResult` remains
the single pure finalizer from e1633b030; it calls the existing
`_terminate_open_edges` placement with the validated host mask, followed
by species/valence/contact validation. Its only caller-facing inputs are
the proven graph, current candidate coordinates and resolved scope.
Reconstruction uses `_verify_generated_caps(scene, generated, bare)`
before creating any carbon-only view; no authored/unknown H deletion.

### Exact existing record shapes and example distinctions

Existing `atomic/generate.py::generated_record` copies only this allowlist:
`hexfold, terminated, spec, report, rings, n_atoms, n_bonds, seed_kind,
measures, chiral_index, radius_A, surface_meridian, surface_target,
fillet_radius_A, theta_p_max_deg, scene, plan`, prefixed by `generator`.
It does NOT persist `canonical_json`, `ports`, `regions` or generic
input `params`. Thus a generic hexfold record's effective mode cannot
recover whether H was omitted or explicitly requested. Retain request
unknown on legacy records; do not manufacture an omitted request.

Exact existing termination subrecord template from `_block_from_net`
(the host variables are source expressions, not invented observed IDs):

```python
{
    "element": "H",
    "mode": terminate,
    "count": len(h_positions),
    "hosts": sorted(set(h_hosts)),
    "clashes": len(h_clashes),
    "bond_A": net.lattice.sigma_CH_A,
    "why": "open edges capped after the relax and the judgement so renders, "
           "se reports and a DFT handoff see a closed-shell edge; the report "
           "describes the carbon net",
}
```

Source-backed field projections below are examples, not claimed complete
native records or executed builds. The existing test fixture is exactly
`"hexfold 0.2\\nt: tube(8,8, len=4)\\n"`.

| Case | Existing recorded data / saved graph distinction | Required reconstruction/preparation result |
| --- | --- | --- |
| Bare generated input | generator=hexfold; fixture spec; terminated mode="none", count=0, hosts=[]; saved128C/0H | Reconstruct unchanged bare input. Explicit effective none visible; generic record alone does not retain original request shape. New finalization still requires exposure and compatible parents. |
| Generated H input | Same spec; terminated element="H", mode="H", count=32, unique hosts length32, clashes=0, bond_A=1.09; saved128C+32H, each H/s and pairwise C-H order1 | Verify actual ordered H-host incidence against replay and record. No cap IDs inferred from count. Omitted versus explicit H remains unknown in generic legacy record. |
| Authored H input | Same spec plus `terminate: t.* = H\\n`; recorded generated cap count=0, yet saved32H and terminated:h tag | H belongs to authored spec, not generated tail. Preserve it; bounded carbon-only join unsupported before preview/save. |
| Bare legacy join | generator="join"; lattice, parts, seam, seam_radius, relaxer, report, n_atoms, n_bonds; NO terminated field | Recompose as legacy bare; no default H/termination receipt invented. |
| Nested join | A parts entry points to another join-origin structure, with its version, port, n_atoms and frame | Recurse through the same verifier and recorded seam/radius. Each layer retains its own policy/provenance; source identity and cap map must match. Unknown inner policy cannot become known at the outer layer. |
| Malformed tail | generated cap count32 but final claimed atom is C, or a claimed H has extra/wrong-host bonds; negative/noninteger count | Refuse as stale/malformed before slicing rows, preview or writes. No truncation/coercion repair. |
| Conflict / absent intent | Parent effective H versus none (or H versus ports-open); or exposure=None even with equal H parents | Fail closed before persistence; matching modes alone is not proof of exposed edges. |

Existing join `parts` entries have exactly `block, structure, version,
port, n_atoms, frame`; frame is [3x3 rotation, translation], first side
identity/zero, second side the actual compose transform. The existing
join record has no generic termination request, cap map or exposure field.
Proposed additive evidence must not be presented as already stored.
New generated-record fields must be explicitly passed through the
allowlist if generation later uses them; this source-only checkpoint
does not modify that allowlist or create a metadata producer.

### Exact preparation and reconstruction callsites

- `prepare_join` dispatches the validated sp2-hex pair to
  `_hexfold_join`; its pending-record lookup protects same-batch ownership.
  Keep that protection and deferred persistence.
- `_hexfold_join` currently calls `_rebuild_block` twice (lines590–591),
  selects its preview (648), and calls `compose` (668). Place input cap
  verification and parent-policy/intent rejection before preview selection;
  resolve final host/port mapping after topology composition, finalize
  before scene serialization and before SE reparenting. Preserve the
  original tree on validation failure through existing apply transaction/
  preparation behavior; assert this in the accepted no-orphan fixtures.
- `_rebuild_block` hexfold branch currently strips by count (410–428);
  replace that decision with verified generated-cap mapping. Its join
  branch recursively calls itself (436–437), composes recorded topology
  (455), then compares stored elements. Validate generated caps at each
  layer before bare comparison; legacy bare comparison remains available.
- `PendingJoin.generated` is prepared around913–944.
  `finish_join` (1072) calls `structure_save(..., version=1,
  meta_extra={"generated": pending.generated})` before binding ports.
  All conflict/intent/provenance rejection must precede this call.
  No missing intent can be supplied by a later read, poll or cache hit.

No new map, generator, builder/test execution, physical steps, saved output,
runtime implementation, public operation, web edit, version or ship.
Writer implementation remains after corrected490 source acceptance and
this narrow checkpoint review.


## Exact provenance examples and parent/request table

Root callable review `cycle2/hexfold-h-writer-callable-root-review.json`
(f78c1b3a) accepts the private boundaries; current controls remain
c05eea95/ffbea679. The remaining metadata checkpoint is
[hexfold-h-provenance-examples.json](hexfold-h-provenance-examples.json).
It proposes the exact additive `generated.termination_v1` layout, ordered
cap/host/bond multiplicity, boundary and policy source, source/pre-cap/final
identity bindings, and nested recorded transformations. Existing fields and
proposed additions are explicitly separated. All examples are synthetic,
not executed structures, canonical hash outputs or trusted producers.

The file names the existing canonical preimages and their acyclic order:
captured physical rows → geometry token → stored graph token → termination
provenance. No receipt/policy or graph token enters its own geometry
preimage. Candidate artifact binding precedes persistence; a prospective
persisted-row identity is bound only in the future save transaction.
This identifies a writer implementation requirement, not authority to
change persistence now. No public release operation or release field exists.

All new preparations first require complete verified exposure intent and
nonconflicting known parent modes; otherwise they fail before compose/save.
This table is deliberately conservative. “None” below means the explicit
string `"none"`; omitted request is `present=false, mode=null`.

| Matching parent modes | Omitted request | Explicit H | Explicit ports-open | Explicit none |
| --- | --- | --- | --- | --- |
| H / H | H, existing default | H | ports-open restriction | none, intentional restriction |
| ports-open / ports-open | preserve ports-open | unavailable; do not relax parent boundary constraint | ports-open | none |
| none / none | preserve none, never default H | unavailable | unavailable | none |
| Unequal known modes | unavailable | unavailable | unavailable | unavailable |
| Unknown required parent mode | unavailable | unavailable | unavailable | unavailable |
| Exposure intent absent/incomplete, any parents | unavailable | unavailable | unavailable | unavailable |

Legacy reconstruction is distinct: absent termination authority remains
unknown and bare, rather than invoking this new-preparation table. Authored
or unknown H fails before the carbon-only preview and remains untouched.
Malformed mappings and same-version graph edits return no pending/saved
output. The synthetic nested positive case preserves each recorded policy
and transformation; its negative legacy-parent case refuses to invent one.

Read490b3819 is already SOURCE ACCEPTED (a690e248); H1–H3 closed and
full29420 verified. No read-gate repeat. This packet adds only the JSON and
table; implementation/builders/runtime fixtures/compute/version/ship remain
unassigned, viewer25 retains exclusive8.35.24.
