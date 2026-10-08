---
status: draft
title: "One molecule renderer with SE and structure adapters"
pillar: 3d-design
prio: high
---

# Shared molecule renderer — source map and proposed migration

Requirement gr473402 remains OPEN. The first extraction slice is complete and
source/local-gate accepted: the SE atom/bond renderer now uses one shared Three
core and explicit host boundary. A standalone structure fixture uses that same
core with synthetic decoded records. This does not migrate the production
structure viewer or integrate completion and stable-ID transport.

## Completed extraction and remaining integration — 2026-10-08

The extraction baseline is landed gr473090 at2e17994b2, preserving its exact
validation target, keyboard/reveal, ambiguity and revision/race guards. The
reviewed checkpoint is00cab5ac3; current-main integration preserves its eight
source/test hashes. Common clipping/picking, interpolation, snapshot replacement,
marker/focus and resource disposal are exercised by the actual SE browser and
standalone fixture. Foreign/stale selection and unavailable-context picks are
refused. Invalid graph identities/endpoints leave valid atoms visible and
selectable, expose an unavailable graph reason and refuse the entire bond layer.
Local gates passed: full29299, focused35, types2584 and browser52 checks including
12 graph-negative cases, with no runtime errors. These are fixture/source gates,
not production runtime or scientific completion proof.

Remaining work requires separately sequenced source review: production structure
host migration and parity; coherent SE identity transport with stable rows and
oriented periodic images; accepted shared-codec/completion adapter integration;
full history/input-after and diagnostics applicability/provenance presentation.
Consume the accepted Hex490 read derivation without copying its classifier.
The larger requirement is unfinished; no new jobs, compute, geometry mutations
or shared chat changes belong to this extraction.

The source map below records the earlier91d6 baseline and proposal history.
Its old pending/absent statements describe that baseline, not current acceptance.

## Exact source and review boundary

Read-only baseline: local origin/main91d6af1353630cac8ca800d712d1a9f30231c90d,
not a fresh fetch or deployed-version assertion. Selected source bytes are
preserved under ../../baseline/, with SHA256s in ../../baseline-manifest.json.
Python discovery first used native search(scope='precis') then bounded symbol
source/outline reads. The served index is a different tree; the pinned local
git objects, not the served index, establish this map's baseline.

gr473090 is separately frozen in .scratch/gr473090, based on4a7d7409; it has
not landed in this baseline. Its accepted route8630570ac/webdoc e729ea704/
test2be76387 plus JS/template/browser payload must be integrated by its actual
landing before a shared-viewer implementation baseline is frozen. It supplies
validation identity, exact target, explicit bound-version pin, ambiguity refusal,
keyboard/reveal and async guards. Never reconstruct or overwrite its candidate.

No rendered SE/structure parity comparison was run for this mapping. Existing
gr473090 fixture browser evidence proves only that bounded click interaction.
Source-present features below are not production/browser acceptance.

## Checked seams and parity

| Capability | SE source at baseline | Structure source at baseline | Shared core / adapter destination |
| --- | --- | --- | --- |
| Atom/bond rendering | blocktree-3d.js:961 _setupAtomicOverlay, instanced meshes | detail.html.j2:657 renderGeom, 3Dmol models/cylinders | One extracted Three sphere/cylinder implementation; both invoke it |
| Orbit/zoom/fit | three-cad-viewer; scene scale bar | 3Dmol zoomTo/zoom/slab | Shared camera/reveal operations through typed host; SE host preserves CAD controls |
| Clipping | atomic/smooth/target materials follow live CAD planes at1052 | front slab slider at697 | Shared clip state/material propagation and hit filtering; kind chrome selects planes/slab |
| Atom inspection | pickAtom at1349 returns block UID+ordinal+hover | label/element/cart/frac/constraint/neighbors at588 | Core returns exact atom/image key; adapter supplies read-only metadata/rows |
| Bond inspection | Cylinders drawn, atom-only raycast | clickable bond cylinder and order/kind/provenance/image at614 | Core supports bond picking; explicit bond ID and image, no pair-only alias |
| Highlight/reveal | block selectPath; gr473090 exact port marker/reveal overlay | element/moved/op-hover atom halos | Shared marker/highlight mechanics; SE keeps block/port policy, structure supplies atom keys |
| Keyboard selection | gr473090 native finding buttons | no equivalent molecule selection control evidenced | Common accessible controls and selection events; preserve finding buttons |
| Loading/error | phased progress _makeProgress:815, _fetchAtomicPayload:889, WebGL fallback:177 | synchronous inline geometry, boot polls for 3Dmol at685 | Shared fetch/build/first-frame/error surface; indeterminate where size/progress unknown |
| Atomic/smooth/target | coords/smooth/faces/deviation and lazy target3d | unavailable | Core draws optional supplied surface layers; structure absent capability explicitly unavailable |
| Strain/chemistry | bond/angle arrays; generic1.42Å/120° controls | inspection strengths/relationships | Hex labels reference applicability; core colors supplied values only; no universal sp2 assumption |
| Cell/PBC/images | bound structure but payload exports index pairs, not image/provenance | lattice and image-correct bond endpoints; payload omits explicit PBC flags | Typed structure adapter retains cell/PBC/image identities; SE must not reuse lossy pairs for periodic geometry |
| Input/after/overlay | design revision, no analogous whole-scene run comparison | initial/relaxed/overlay modes, moved list:756 | Core renders independent snapshot layers with role-qualified keys; structure owns comparison/history |
| Revision/history | _tree_at/checkpoints; historic bindings may still resolve live structures | structure_load(version), run pin; live _viewer may choose old successful run | Hex coherent envelope + exact snapshot availability; adapters refuse mismatches |
| Assembly/tree/ports | scene3d shapes, isolation, connections, topology, block poses | not applicable | SE adapter retains these; no synthetic SE tree for structure |
| Notes/chat/Apply | shared partial, block handle callback | same partial, atom label callback | Reuse PCB-owned shared chat as-is; local adapter only calls existing designChatAddHandle |
| Completion/provenance | shared envelope architecture reviewed; typed implementation pending | run panel and runs_status currently jobs+count | Consume Hex classifier verbatim; independent status token, all-block mixed summary |
| Export | canvas PNG/SVG snapshot and atom files | XYZ/PDB/CIF export route | Preserve existing contracts; renderer migration does not alter export semantics |

Source specifics: structure.py _geom_payload:628 carries full cartesian
coordinates and image-offset bond endpoints; its frac display values are rounded.
Its _viewer:701 applies a run's final_geometry to a loaded scene; when version
is None it does not filter on_version. That is not evidence of a correct pinned
input/after pair. Historical selection must use Hex's actual snapshot contract.
blocktree_view.py _build_atomic_block_payload:1510 emits rounded display coords,
index-only bonds, hover strings and no stable atom labels/image provenance. Those
fields cannot be the common identity contract. Current pick route:1998 resolves
the LIVE tree/ordinal; snapshot-bound inspection must replace that unsafe adapter
path or return unavailable, never silently inspect a different revision.
Current pickAtom chooses the first ray hit without checking clipping planes;
the extraction must test visible clipped/hidden hits rather than assume shaders
also filter raycasts. Structure refreshDetail:516 re-matches bonds by endpoints
only and client micDist:500 searches all27 images; preserve image/PBC semantics
explicitly instead of carrying these shortcuts into the common core.

## Actual shared renderer, not a shell or a second molecule engine

Retain the existing vendored Three0.184.0 and extract the SE overlay's atom/bond
instancing, surface draw, clipping, picking/highlight and phased build into
static/molecule-core.js. Both clients call its replace/selection/overlay methods.
Remove structure's inline renderGeom/3Dmol boot after parity acceptance. Keep
3Dmol vendor assets: refs/pathway_detail.html.j2 also uses them, and routes/refs.py
imports _geom_payload. Preserve that helper's legacy response shape for callers;
add a separate typed common adapter rather than breaking pathway rendering.

A small typed host owns scene/camera/render surface/lights and resize/disposal.
SE supplies a ThreeCadHost that isolates existing private _rendered access;
structure supplies a standalone Three host with its real molecule/cell, not a
fake SE block tree or an empty made-up bound design. The host does not draw a
second set of molecule meshes. Common camera math, bounds, clip and reveal
operations live in the core. Structure orbit input may reuse vendored
static/three/examples/jsm/controls/OrbitControls.js, which imports bare three;
that file is from the older CAD stack, so r184 compatibility needs an explicit
browser/controls fixture before adoption. Do not load its separate r160 module
on the molecule page. A required compatible control asset is a review point,
not an unapproved dependency addition or vendor bundle change.

Keep SE hierarchy/envelope/topology rendering and structure run/revision panels
in their adapters. Initial extraction preserves behavior, then structure migrates
to the same core; no permanent dual-renderer mode. One Three import map per page.

## Proposed typed rendering interface (names provisional)

Use Python TypedDict payloads plus JS JSDoc discriminated types; no TypeScript
toolchain or new dependency. Rendering types describe drawing and interaction,
not a second persisted chemistry/completion model.

```
SnapshotIdentity = {design_kind, ref_id, version, geometry_token, role,
                    run_id?, se_revision?, block_uid?, binding?}
AtomKey = {snapshot_token, structure_ref_id, atom_label, image:[int,int,int],
           block_uid?}
BondKey = {snapshot_token, structure_ref_id, bond_row_id_or_receipt_key,
           i_label, j_label, image:[int,int,int], block_uid?}
MoleculeLayer = {identity, atoms:[{key, element, cart_A, metadata}],
                 bonds:[{key, start_A, end_A, order, kind, provenance}],
                 cell:{lattice_A, pbc:[bool,bool,bool]}?,
                 placement:{rotation, translation_m, scene_scale},
                 surfaces?, diagnostics?, selectable, role}
Selection = AtomKey | BondKey | {kind:'anchor', shown_identity, block_uid,
                               port_name, world_point, binding}
```

Atom identities use the scene's unique label plus exact snapshot/structure
namespace; persistent row handles are preserved when supplied by Hex, not invented
from array index. Bond identity uses actual row/receipt identity where available;
otherwise the exact endpoint labels/image/kind/provenance and a captured key
provided by the adapter, with ambiguous duplicates unavailable. Reversed bonds
include the inverted translation; do not conflate a different periodic edge.
Instances map to keys in a captured immutable table. Image atoms retain both base
label and lattice translation. A selectable periodic bond endpoint may render
its image atom; same base label at another image remains a distinct selection.

Physical input units are Å with exact source coordinates; SE rigid placement is
metres and its display scale explicit. The common transformation converts once
to host units. Float GPU buffers/display rounding are drawing approximations,
never identity/evidence or rewritten source geometry. Fit bounds include visible
atoms, cell edges, bond/image endpoints and optional layers. Role-qualified ghost
input and solid after layers carry separate identities; mismatched/absent atoms
do not inherit selections across roles by ordinal or nearest point.

Core interface: replace(layers, generation), setClip(state), setVisibility(keys),
setOverlay(layer), highlight(selection), reveal(selection), clearSelection(),
dispose(). Emits selection with exact identity/key, loading phase, unavailable,
error and first-frame events. All async loads/builds/inspection compare captured
generation and shown identity; status-only polls never replace geometry. Pointers
ignore hidden instances and clipping-rejected hits; keys survive repeated image
instances. Core does not fetch latest atoms or infer chemistry.

Inspection uses supplied metadata or a bounded read endpoint accepting the exact
shown token/key. SE adapter retains gr473090 finding validation and selectPath;
anchor markers use its verified world point and rendered binding. Structure
adapter maps visible label/image selections into atom/bond detail, moved/legend
hover and the existing chat callback. No shared design_chat/turn/partial edits.

## APIs, file ownership and missing shared fields

Existing GETs stay: /se/{slug}/scene3d.json, atomic3d.json, target3d.json,
pick, validation-targets (after gr473090 landing); /structure/{slug}?rev=&run=,
/run/{id}, /runs_status and /export.{fmt}. Existing POST/Apply/relax/chat
contracts are preserved and never invoked on reads. A proposed additive
/structure/{slug}/molecule3d.json?rev=&run=&role= and snapshot-aware SE payload
adaptation provide typed layer data; response/error/status semantics require
coordinator review. Keep geometry ETag and status_token independent. No new MCP
verb or public API/schema break.

| Proposed owned file | Change |
| --- | --- |
| static/molecule-core.js | Extract reusable atom/bond rendering, picking, clip/highlight/reveal, phases and disposal |
| static/molecule-host.js | Isolate ThreeCad host access and independent structure host, camera/clip operations |
| static/structure-viewer.js | Structure-only view modes/inspection/moved/legend adapter; imports core |
| static/blocktree-3d.js | SE adapter calls core; retains hierarchy/topology/ports/validation guards |
| molecule_view.py (new typed web adapter) | Format coherent read snapshots into layer identities; no classifier/compute |
| routes/structure.py | Add coherent geometry read adapter; preserve existing runs/revisions/exports and _geom_payload callers |
| routes/blocktree_view.py | SE typed snapshot/image/target adapter and guarded inspection; preserve gr473090 |
| structure/detail.html.j2, blocktree/detail3d.html.j2 | Shared rendering mount/controls; keep kind panels and existing chat include |
| tests/precis_web/ + browser proof | Both adapters invoke same renderer; periodic/history/race/selection/pixel fixtures |
| precis_web/__init__.py | Compact rationale for core/adapters, identity and unknown evidence |

Hex owns Store.structure_completion_snapshot/completion_envelope, history/handler/
chemistry contracts and completion classification. Reuse the20:48 viewer contract
review and the subsequently adopted contract amendments below. Need concrete
typed rendering fields from Hex: labelled exact atom rows and constraints;
cell/PBC with actual historical availability; declared/inferred bond identity,
image, order/kind/provenance; coherent input/output role and selected run; all-block
SE bindings; diagnostic values plus method/reference/applicability/units and
target receipt identity. Existing1.42Å/120° strain arrays must not become a general
claim for sp3 fins, other elements or unknown chemistry. Missing surfaces/checks
are unavailable. No automatic rebond or new diagnostic computation in web/core.
PCB's design_turn.py/design_chat.py/_design_chat.html.j2 and conversation plumbing
are excluded. No collision found or seam change needed for mapping; reuse callback.

## Reviewed shared-envelope amendments and adapter consequences

Architecture adopted from owner2488b2d49fe66b4cf6a5e744f179cc5d0afc851c with
coordinator-required amendments in fleet-state/cycle2/
hexfold-completion-contract-amendments.md, SHA256
adcab229b55c7bfd90161a83f5b87f3158300395258de9ec7ac6a9a05f6afcff.
Review receipt cycle2/hexfold-completion-contract-review.json at
2026-10-07T21:01:09.401827Z. Control relaxation-completion spec SHA256
d86f7ab31913dd63f3cbeb7a8d9492a3e04f9035a81a15f9366651a82e6224ea.
This supersedes this map's earlier envelope-pending architectural status;
Hex first shares exact v1 receipt/envelope types before classifier code.
Combined viewer map review and concrete types remain pending, so no web source
implementation is authorized by this update. Hex alone publishes gr472846.

Adapters consume one domain/version-prefixed canonical exact-snapshot identity,
shared with receipt validation. Include ref/version and atom/bond row identities,
cell/PBC, labels/elements/unrounded coordinates, fixed-axis masks and scientific
intent (charge/spin/oxidation/declared hybridisation), bond endpoints/order/kind/
provenance/images. Preserve null versus zero and numeric precision; reject
nonfinite data, duplicate labels, dangling edges and malformed/missing cells.
Declared hybridisation is intent, not electronic proof. No synthetic lattice
fallback or rounded compute-cache key may supply geometry identity. Web formats
these fields but does not independently reimplement the canonical encoder.

The shared store snapshot orders independent aggregates, respects supplied conn
transaction ownership and consistent live-kind/retirement filters. Adapters do
not insert commits or broaden the snapshot query. Explicit run selection remains
structure-scoped even outside a bounded recent-history list: missing/wrong-owner
selection is unavailable, never latest-run fallback. Polling excludes full force
arrays/trajectories; expose truncation as such, not as proof a run is absent.

Render request/job state and evidence qualification independently. Envelope names
request_id, policy_id, selected displayed role, selected evidence and earlier
qualified evidence separately, with stable request ordering/tie-breakers. A new
failed attempt can coexist with earlier screening evidence; neither changes
the other's success. Effective policy absent means no invented targets/authority
and finished=false. Existing-rung level qualification remains separate from
constraints and cell policy. A free-atom/fixed-cell nonperiodic run is not cell
optimisation. Web never upgrades projected/constrained force scope to free force.

Use only complete validated versioned receipt producer/adoption provenance,
typed input/output mapping, method/checkpoint/settings, target comparator,
force units/scope, termination, usage and complete post-check criteria/version.
A JSON object named completion, success job, lower energy, bare pass or zero
validator errors alone qualifies nothing. Unknown/contradictory receipt/run/job
facts remain unknown with the shared reason; no numerical tolerance promotes a
failed force criterion. Cap-stop is unconverged; verified final-step convergence
may qualify. Passing required post-checks bind the same geometry/graph/policy.

Shown-token mismatch supplies a reload reason and suppresses shown finished,
qualified level and selected qualified evidence. Latest observed identity may
appear only clearly labelled as latest; it cannot attach to the old canvas.
Historical atom intervals do not prove historic cell/PBC. structure_load(version)
still using current metadata is insufficient: require an actual historic
atoms/bonds/cell/PBC snapshot or show historical-unavailable. Never use current
cell with historic atoms for input/after or a selected run.

Status token covers selected request/run, policy/holds, material status and
post-checks/recommendations, excludes read time/nondeterministic ordering, and
does not change the geometry token/ETag. Held/unavailable policy preserves
historical qualification as labelled history without claiming current finish.
Cache origin is receipt-contained or coherently read from the exact source run,
possibly another structure, with verified source identity/reuse mapping. Missing
source cannot qualify; no cache_key-only proof or unbounded origin recursion.

All reads avoid model imports/device discovery/jobs/writes/prompts. v1
next.can_submit=false; viewer shows next.fidelity/resources/reason informationally.
Future lineage budget reservation and idempotent finish stay Hex-owned and
separately sequenced; rename/new output hash/retry cannot reset cumulative caps,
and runtime-only extension never raises300steps. Shared renderer does not alter
optimizer/writer/generator/dispatch or adopt/backfill legacy run records.

## Stages and meaningful acceptance

1. Coordinator reviews the combined map; Hex shares concrete reviewed v1 types
   and rendering additions under its architecturally accepted contract.
   Future fresh implementation tree explicitly contains landed gr473090 and the
   reviewed envelope. Preserve all original candidate/scratch and version order.
2. Extract SE renderer into core+host, prove SE atomic/port/validation behavior
   remains equivalent. No structure migration claim yet.
3. Build structure typed read adapter and migrate it to that same core, preserving
   cell/PBC/image and input/after history. Remove its duplicate molecule engine.
4. Canonical fixture/browser/source acceptance, then assigned version/ship.

| Browser/fixture | Required witness |
| --- | --- |
| SE multi-block atoms, validation port, hidden block | Real rendered marker at transformed verified anchor; keyboard/reveal/replacement; ambiguous/missing refused; gr473090 late guards retained |
| Nonperiodic structure through same core | Pixel-visible atoms/bonds, exact atom and bond keys/details, camera/clip/zoom, legend/moved hover |
| Nonorthogonal periodic cell, image-crossing edge | Correct rendered endpoints/cell; base label plus image selection; two translations not aliased; allowed PBC axes respected |
| Clip/hidden nearest hit | Clipped/hidden atom cannot intercept selection; visible next target selected, camera includes edge/image bounds |
| Input/after/history with different sets | Correct role-bound coordinates and ghost overlay; missing historic geometry unavailable; selection clears on identity replacement |
| Rapid loads, old inspection/status response | Old generation/shown token discarded; status failure changes status token without geometry ETag/rebuild |
| Partial data, WebGL failure and slow build | Explicit loading/empty/unavailable/error states, first-frame timing, phased/indeterminate progress without fabricated ETA |
| Mixed completion and constrained/capped/legacy | All blocks counted; Hex state displayed unchanged; provenance and unfinished checks visible; no guessed convergence/reference |
| Declared graph plus separate clash evidence | Original bonds/provenance remain; diagnostic evidence distinct, no rebond/write |
| Chat and read-only routes | Existing handle callbacks/notes/Apply behavior intact; GET/poll/browser drawing issue no mutation/compute |

Adapter fixtures additionally cover historic atoms with missing historic cell,
selected run omitted by recent-history truncation versus genuinely absent,
failed current request plus prior qualification with separate policy IDs, stale
token suppressing qualification while labelled latest identity is returned,
nonfinite/null/zero identity inputs, projected versus raw forces/fixed-cell policy,
unsupported receipt and cross-structure cache origin with missing source. Core
tests consume supplied envelope outcomes; Hex owns classifier/state-table tests.

Reuse tests/precis_web/test_structure_sql.py, test_structure_run_page.py,
test_structure_file_export.py, test_blocktree_view.py, test_se_pick_route.py,
tests/test_web_se_atomic3d.py and landed test_validation_selection.py; preserve
pathway _geom_payload tests. Add exact pixel and key/coordinate oracles through
the real vendored renderer, not mock-core-only tests. Freeze manifest before full
scripts/test/types/Ruff/format and changed-line coverage; inspect actual exits
and hashes before acceptance messages. This mapping runs no gates or browser.

Reto allows roundish sp3 fins. No teardrop absence failure criterion. Existing
pilot cap-stopped forces and Y clashes remain unchanged scientific limitations.
No compute/rebond/import/service/deploy, new window, version slot or denial bypass.


# gr473402 combined map review

Architecture accepted: extract the existing SE Three molecule renderer into one
core, with an SE host/adapter and a standalone structure host/adapter. Preserve
the pathway 3Dmol caller and legacy `_geom_payload` response. This is source-map
acceptance, not implementation or browser acceptance.

The current owner map is 130ac4a90c26bffe71d6c7dc2879f13d3987f6969fcd768230849bb86a6172ee,
superseding 1031ca227dc36d098167b278bb15e9d85015ea49a559ffac15988eea3adb9be8.
Use the current Hex control eb7b70f85d233f7c89778a7e543bc5857db3c884cea7884d1c17d108466f3914
and its reviewed amendments; the earlier d86f7ab3 reference is historical.

## Required refinements

1. Separate physical geometry identity from displayed scene identity. Repeated
   instances of one structure require block UID, design revision, binding and
   exact placement identity in the scene/selection namespace. Moving a block
   changes shown-scene identity even when its atomic geometry token is unchanged.
   Shared physical tokens come from Hex's canonical encoder; the web must not
   invent a competing scientific hash. Clip, visibility and hover are display
   state, not new physical geometry or convergence evidence. Include the selected
   role and immutable layer mapping in async guards.
2. Make selection a discriminated union with explicit atom/bond/anchor kinds.
   Define lattice row convention, integer image orientation, Å-to-host transform,
   rigid placement order and plane coordinate space. Reject nonfinite and
   unsupported transforms. Test repeated bindings with different transforms,
   nonorthogonal partial-PBC cells, reversed periodic edges and clipped nearest
   hits. Do not use a 27-image minimum-distance guess to identify a clicked edge.
3. Atomic-to-smooth interpolation and target surfaces are visualization layers.
   At intermediate positions, never label the mesh hit coordinate as a measured
   atom coordinate or a relaxed output. Either retain an explicitly labelled
   source-atom association with separate displayed/source coordinates, or make
   that interpolated surface non-atom-selectable. Diagnostic scalars remain bound
   to the actual input snapshot and reference, not the interpolated position.
4. Source `_geom_payload` calls `detect_bonds` when the authored graph is empty.
   Preserve that caller's legacy behavior. For the new adapter, Hex specifies the
   existing inference policy and versioned transient graph identity where this
   fallback is needed. Label inferred edges distinctly; never persist them or
   treat inferred/display bonds as authored chemical proof. No new inference or
   rebonding algorithm is authorized. Unknown graph or chemistry may render with
   explicit unavailable diagnostics; it must not invent bonds or convergence.
5. Geometry availability and scientific completion availability are independent.
   Valid current geometry remains drawable if no run/policy/receipt exists or a
   status request fails. Suppress unsupported status claims, not valid geometry.
   Conversely, a historical run label cannot justify mixing historic atoms with
   current cell/PBC. Explicit stale/unavailable responses carry the requested
   identity and a reason; never silently fall back to latest. Selected run data
   and all SE bindings follow the reviewed coherent snapshot contract.
6. Keep one Three module identity and one host render lifecycle. Before choosing
   the existing OrbitControls asset, test it against the actual r184 import map;
   no silent second Three instance or vendor upgrade. Disposal must cancel RAF,
   observers/listeners, pending builds and owned buffers/materials without
   disposing shared host resources. Bound requests/build batches and test repeat
   mount/replace/dispose, resize, WebGL loss and late completion. Rendering tests
   cover actual picking and pixels, not only mocked events.

## Next assignments and implementation boundary

Viewer25 adopts these amendments and shares concrete render/selection types,
transport outcomes and example fixtures with Hex. In separate owned scratch,
read-only baseline capture and a small synthetic browser compatibility harness
using the existing vendored assets are authorized now. No source extraction,
production adapter edits or vendor changes in this preparation step. Preserve
the accepted gr473090 checkpoint and all its evidence.

Hex supplies exact geometry/receipt fields, enum/null semantics, effective
policy precedence, historical availability and pure all-block SE aggregation
as already assigned. Viewer formatting does not duplicate that classifier.
Agree the fields/examples before extraction; no compute outcome is needed.

The first production extraction baseline must contain the actual gr473090
landing and the reviewed concrete Hex interface. Return those identities and
the fixture/compatibility result for a concrete extraction assignment. This
does not hold gr473090 shipping on the shared-viewer work. Python8.35.21 retains
its active slot; gr473090 is next after actual landing and explicit assignment.
The separately assigned narrow nanopub UI work can proceed in its own slice.

Viewer25 remains sole gr473402 native writer. Hex owns shared geometry/state;
PCB owns shared chat/turn/partial/conversation plumbing. No new windows, compute,
services, deployment, version bump or ship are authorized by this review. Do
not Docker prune or write/run scripts in shared /tmp.


## Combined geometry contract review — 2026-10-07T21:58:01.024608+00:00

Combined Hex5a0246b supplement accepted with periodic comparison mapping proof for atom/image/basis correspondence; unknown mapping means comparison unavailable, no new mapper. Physical/scene/graph identities and provenance remain separate. Full concrete display selector and wire examples still pending. Existing gr473090 landing boundary and viewer-only web ownership unchanged. See coordinator cycle2/hexfold-molecule-contract-review.md. No new compute, writer, source version or ship slot.


## First extraction assignment — 2026-10-08

The actual gr473090 landing is 2e17994b2f309fb191ff974fa3e38c76dd5215bb,
tree79a86d679fc4e2e640e5b3777ce199cf89ebb833, Precis8.35.22; it exactly
matches tested ac93a981/CI37751724218. This supersedes the landing wait above.
The e38 concrete interface and viewer ae93add1/ed293950 alignment are accepted;
16 prior synthetic browser checks retain their original 0d7c/0efd scope.

Viewer25 may create a fresh isolated extraction slice at that exact landed SHA,
preserve prior slices/evidence, and implement the first reviewed map slice:
extract existing SE atom/bond rendering, clipping, picking/highlight/reveal,
loading phases and disposal into molecule-core.js/molecule-host.js; have the
SE adapter use it while preserving gr473090 selection and stale guards. Add a
standalone structure host fixture consuming the same core and the reviewed
exact tagged-wire/render contract. Keep changes in assigned web client/template,
web tests and compact owning rationale files. Shared PCB chat/conv files remain
excluded; no vendor upgrade, fake SE tree or scientific classifier in web.

This slice must preserve stable atom/bond/image and placed-scene identities,
current/input/adopted-output roles, run and design tokens, exact integer keys
with explicit unsupported GPU-range refusal, and labelled unavailable history,
cell/basis/diagnostics. No geometry/status fallback or computation on reads.
Exercise actual pixels/picking, clipped hits, transformed/repeated bindings,
partial-PBC/nonorthogonal fixtures, stale responses and replacement/disposal;
rerun the relevant gr473090 interaction regressions. Prior synthetic proof is
not proof of extracted code. Run the appropriate normal gates on a frozen
candidate and return its exact manifest for review.

Do not wire production completion/status endpoints to unaccepted Hex source or
copy that classifier. Hex710cde0cd has a separate three-finding correction
review; it is not a blocker for this renderer-only extraction and fixtures.
Production structure-route migration and the coherent read integration are the
next reviewed slice after that dependency is accepted. Keep pathway 3Dmol and
legacy _geom_payload callers unchanged. No version/ship slot or deployment is
assigned here; dogfood29 retains sole lifecycle authority. No new compute,
scientific writer, rebonding, original-geometry mutation or new window.
