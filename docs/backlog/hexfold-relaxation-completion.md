# Hexfold relaxation completion and visible provenance

Status: coordinator-adopted scope, refined against existing source. Owner: Hexfold
for compute/completion/MCP provenance; gripe-fixer for viewer presentation after
its active batch. No source implementation, job launch or deployment is implied
by this spec. User policy: gr472846, completion request 2026-10-07.

## Owner adoption and measured boundary

Adopted in the isolated Hexfold completion slice from current main; the replay
repair remains separately landed. The initial fin/Y pilot is complete:
`fi473095`, `fi473189`, `fi473386`, before `fi472892`. All three reached the
unchanged 300-step limit without force convergence; neither fin developed a
convincing teardrop. Y developed 16 original-graph close-contact findings and
large residual forces on the seam itself. A method label, lower energy,
unchanged stored bonds or zero validator errors cannot establish completion.

The r5 execution baseline is three sequential whole-block runs: `ball12`
(2,406 atoms, 40 minutes), `ball12r11` (2,466 atoms, 40 minutes), then
`cyl12open` (1,950 atoms, 30 minutes); total 110 minutes. These are hard runtime
caps, not predicted convergence times. Use the same cached small checkpoint,
float64 CPU, FIRE 0.05 eV/A, maxstep 0.03 A, 300 steps, eight CPU threads and
16 GiB. Preserve exact saved coordinates, atoms, bonds and composition; release
all positional constraints, fixed nonperiodic cell, no added passivation or
cross-section tether. Record input/checkpoint hashes and full finite plan before
launch. Separate after candidates, zero incremental external/cloud spend.
Only the already authorized finite critical runtime extension is available;
record its evidence/reason/revised cap before overrun and report both routes.
It does not lift the step, model or resource limits.

Existing GPU route readiness is read-only. No service reassignment, downloads,
new model or held standalone-host use. Unless exact cached checkpoint, compatible
runtime and available bounded capacity are verified, use the established CPU
runner rather than silently substituting the generic model router.

Source review found the current generic ML implementation uses BFGS and a
0.05 force floor, while the approved pilot uses FIRE. Worker writeback defaults
missing convergence to true and unconditionally describes success as converged.
Correctness work must remove those assumptions before automatic completion can
reuse that path. Existing external import rows and scratch-derived structures
also need explicit evidence provenance; legacy success is not retroactive proof.

## User outcome

Reto wants Hexfold to perform the applicable bounded relaxation and learn from it
before calling an atomistic candidate finished. Repeated permission questions
should stop within the authorized resource/spending envelope. The viewer and
MCP should expose the actual relaxation state and recommend the next useful
phase. Relax the saved hexfold-dogfood-r5 examples as additional candidates,
preserve originals, and explain what their tubes do after relaxation.

## Premise checked

Native SE read of hexfold-dogfood-r5 shows three blocks: ball12 (2406 atoms),
ball12r11 (2466), cyl12open (1950), with separated display poses. Labels record
fidelity=stick and tethered relaxation. These are existing spring-preview
results, not evidence of an unconstrained MACE minimum. The structure skill
already documents clean/ml/dft rungs, relaxation history, convergence/steps/
energy/max-force, geometry stats and invalidation after graph edits. Extend
these seams rather than inventing a second compute system. Python MCP root is
/app, different from this task worktree; no edits through the deployed index.

## Completion policy

For an atomistic artifact, generated/source-reviewed/relaxation-attempted/
converged/post-validated are distinct outcomes. A source fix can ship while an
artifact remains unrelaxed. A geometric candidate is not physically finished
until the requested model/constraint/force target is met and post-checks run.
Timeout, step limit, failure, unavailable backend or scientific hold remain
explicit terminal/pending states; none becomes converged automatically.

Within standing authorized limits, suitable cached-model relaxation is a normal
phase and does not ask Reto again. Current three-pilot approval gr472846 remains
bounded by its exact model/resources/step limits and conditional finite runtime
extension. The r5 follow-on is explicitly requested and authorized; owner records a
finite per-block resource plan before starting, within the same resource/model
envelope. No repeated permission question inside that envelope. Do not silently change model,
constraints, paid services, step budget or unrelated science. Costs over $25,
new sensitive service changes and unrelated held BEEF/DFT/NEB work retain their
own approval requirements. Being below $25 alone does not lift a hold or
authorize another model/run. Completion may truthfully say blocked and why.

## Initial execution and where it runs

Current approved baseline: cached MACE-MP small, ASE FIRE, fmax=0.05 eV/A,
maxstep=0.03 A, max300 steps, 8 CPU threads/16GiB, all atoms free, no section
shape tether, no xTB, $0 incremental external/cloud spend. Original caps are
15/25/90min for out/in/Y, with Reto-authorized finite extension only when
critical to useful convergence, documented before overrun.

Existing cluster GPU worker is a candidate for future larger runs; MACE supports
CUDA. Do not assert faster runtime or compatible cached model before checking
installed runtime/capacity. Use the existing scheduler/resource limits, not the
standalone paper-work node; no service reassignment or deployment. Cached CUDA/runtime/capacity verification on the existing compute route is
read-only readiness work; it does not authorize service reassignment or use of
the held standalone paper host. CPU remains a valid bounded fallback. Changing
device is not changing physical fidelity;
compare equivalent model/precision/constraints and preserve provenance.

For r5, start with each complete block independently: ball12, ball12r11,
cyl12open. Keep each block's bonded tube/sheet assembly intact. Inspect actual
connectivity before separating anything. A combined whole-scene relaxation is
justified only when studying real inter-block contacts/chemical connections;
co-displayed separated variants should not be treated as an interacting device.
Preserve the original saved scene; write separate versioned after candidates.

## Learning from the run

Use exact before/after snapshots and atom identities, model checkpoint hash,
backend/device/precision, boundary conditions, optimizer settings, timestep/
step history, runtime and any cap extension. Report initial/final energy and
max force, convergence/stop reason, atom counts/composition/connectivity, bond
length/angle distributions, coordination, rings, all clash families including
H caps, and consistent POAV summaries. Do not infer a missing metric as zero.
Measure tube radius/profile, bending, collapse/flattening and joint shape.
Compare authored bonds with geometric coordination/contact changes; an unchanged
stored bond list is not evidence that all physical connections survived. Use
surface deviation from the ORIGINAL authored target only when its receipt or an
explicit accepted target is available. Legacy r5 lacks a trusted implicit target
receipt: report unavailable rather than reconstructing or fitting one. Align equivalent atoms
for displacement comparisons without fitting away the deformation of interest.
Report constrained and free relaxation separately. Optimization is a model's
local minimum, not measured material stability, finite-temperature robustness
or a DFT result. More expensive corroboration is a separate justified phase.

## Relaxation level and recommendation

Preview: stick/clean, constraints visible; makes geometry readable.
Screening: cached ML/FIRE with actual maximum force <0.05 eV/A, post-checks.
Refinement proposal: tighter 0.02 eV/A only if remaining forces or observable
changes matter; justify model suitability and resources before escalation.
Independent validation: a separately suitable model or higher-level method
when needed for the scientific claim; no automatic xTB/DFT or MD approval.

A run terminated because its step/time budget was exhausted is attempted and
non-converged, not the screening level. Verified force convergence exactly at
the final allowed step can qualify; the step number alone decides neither case.
Record raw and unconstrained forces for constrained cases so frozen
atoms cannot make a misleading free-relaxation claim. Levels depend on both
method and convergence/constraints, not a single relaxed=true boolean.

## Viewer and MCP contract

For each block/bound structure, show exact-version status:
unknown, preview-only, queued, running, converged, stopped-unconverged, failed,
held/unavailable. Display method, model, force target/achieved maximum, steps,
constraints and whether free, run/candidate ids and original version. Assembly
summary must reveal mixed block states. Preserve relaxation history; editing
coordinates/bonds invalidates current geometry's status without erasing past
runs. Never infer physical relaxation from filenames, prose or stick-preview.

Viewer: compact method/state badge; details on request; before/after toggle or
comparison; running progress and terminal reason. Surface the next appropriate
phase as a bounded action, showing resources/constraints relevant to the user.
MCP: current-version structured relaxation/provenance and human digest; when a
generation/finish response leaves preview-only geometry, recommend ML screening
and its target/resource estimate, or state the explicit hold. Reuse existing
structure history/stats and deployed worker paths. Keep seven verbs. New JSON
fields may be additive; schema/response-breaking changes require review first.

## Source-checked reuse and safeguards

Reuse `structure.relax.RelaxResult`, handler `_relax_summary`/`_record_run`,
`struct_runs`, `meta.last_relax`, the existing content-addressed cache, stats,
label-paired forces and geometry-version invalidation. The generic ladder also
has `emt` and `geo`; those are not evidence of the requested MACE screening.

The composition router may choose `mace_off` for a neutral nonperiodic organic
scene. Explicitly identify the approved MACE-MP **small checkpoint**; the current
family-only `_cached_ml_calculator` call does not pin a small variant or device.
Do not silently substitute composition routing or a library default. Cache and
run identity must distinguish actual checkpoint/hash, model variant, precision,
device, dispersion, optimizer/settings, constraints and input geometry. Extend
existing params/metadata additively where sufficient; do not create a second run
store or schema merely to render a badge. A cache hit is reuse, not new compute.

The shared calculator is safe for sequential calls, not concurrent use. Preserve
existing lane/resource serialization; no parallel three-candidate submission.
MACE stays explicitly selected, no xTB/DFT/MD or unrequested dispersion fallback.
Record unavailable checkpoint/device honestly instead of a silent substitution.

Automatic completion belongs to the authorized generation/finish workflow and
existing bounded job lane. Keep deterministic preview generation available as
preview. Do not add blocking model load/relaxation on the request hot path or
launch jobs from `get`, stats, viewer loads or polling. A policy-enabled finish
submits at most one equivalent run for the exact geometry/model/constraints;
repeated finish/poll requests reuse its state. Unknown applicability, unsupported
backend or a hold returns that state and a useful next step. It cannot claim a
finished physical candidate. No new generic scheduler or worker service.

Budget identity follows the authorized candidate lineage, not only its latest
geometry/cache key. Outputs, retries and continuations retain the original
authorization and candidate identity. A capped output, renamed clone or cache
miss cannot renew step/runtime allowance. Record authorization, original input,
parent attempt, cumulative resources and extensions in existing metadata; reserve
remaining allowance atomically before submission. Repeated finish requests reuse
the attempt. Exhaustion recommends inspection or an expressly authorized
continuation, never automatic resubmission; unknown lineage cannot submit.
Runtime extension alone cannot increase the authorized step limit.

Match provenance to exact current geometry/version, never merely a scene name
or model label. Changed geometry invalidates current status while preserving
history. Free relaxation requires actually free atoms/cell policy as recorded;
masked/approximate forces cannot prove unconstrained convergence. Record the
requested force target with the backend's actual termination result and actual
maximum force; timeout/step-limit remains unconverged even after 300 steps.
Tighter 0.02 is a justified bounded refinement proposal, not a silently upgraded
default or an excuse to exceed the unchanged step/model/resource envelope.

## Sequenced owners and acceptance

### First implementation slice: coherent read contract

Proposed shared callable (for coordinator/viewer contract review):
`structure.completion.completion_envelope(snapshot, *, shown_geometry_token=None,
selected_run_id=None, policy=None)`. Its input comes from
`Store.structure_completion_snapshot(ref_id, conn=None)`: one SQL statement
with independent lateral aggregates for current ref/cell, labelled atom rows,
declared bond rows, relevant runs and jobs. This is a read, not a compute trigger.
The exact geometry token covers version/cell/PBC, ordered atoms and bonds plus
row identities. It must detect same-version replacement and graph edits; the
rounded, bond-independent compute-cache key cannot substitute for it.

The additive version-1 envelope has `geometry` (ref/version/token), `binding`
(`exact`, `unknown`, `stale`, `historical-unavailable`), `state` (the states
above), `reason`, independent `constraints` (`free`, `constrained`, `unknown`),
nullable `evidence`, `post_checks` (state/outcome), `finished`, `next`
(phase/reason/can_submit/resources), and a separate `status_token`. Evidence
separates input/output identities, actual method/checkpoint/settings, requested
and achieved force/scope, termination/caps/runtime and run/job/cache-source IDs.
Absent legacy values stay null. `finished` additionally requires completed
passing post-checks bound to the same geometry and graph.

Expose a qualified `level` using existing fidelity names, not a second ladder:
`{fidelity: "ml", qualification: "attempted"}` for a bound capped ML run.
Fidelity is the recorded existing rung or null; qualification is `unknown`,
`attempted`, `converged` or `post-validated`, derived from the same evidence.
Keep constraints independent. Add nullable `next.fidelity` for an existing rung
recommended by the bounded plan. Method/checkpoint/target remain explicit;
an attempted ML run does not claim completed screening.

Selection is explicit: a selected missing/stale run cannot silently fall back
to another run. Without a selected run, describe the most recent request for
the effective completion policy and retain earlier qualified evidence separately.
A failure of an unrelated refinement policy must not erase an already valid
screening result. Record which request and policy the top-level state describes.
With no effective policy, show only verified recorded evidence; do not invent
method/force/constraint targets or authorization, and leave `finished=false`.

Run success or old `converged` fields alone are insufficient: local `on_version`
currently denotes saved output while worker `on_version` denotes staged input;
external imports also stamp convergence without a force criterion. Preserve raw
history but return unknown binding/termination until a versioned, validated
`struct_runs.params.completion` receipt exists. Future writers capture input and
saved output identities transactionally; unadopted worker output is a run
artifact, not the current structure. This uses existing JSONB, no new run store.
Explicit failure or exhaustion classifies the current geometry as
`stopped-unconverged` only when that evidence is exactly bound to it; otherwise
preserve the historical result while current binding/termination stays unknown.
An output convergence result cannot label unchanged input atoms converged.

Polling with a nonmatching shown token returns stale/unknown and requires a
geometry reload. Historical selection without the actual historic atoms/cell
returns historical-unavailable. The independent status token includes selected
run/request, material job/run changes, post-check outcome and effective policy,
holds and recommendations, including failures without new runs. SE adds revision/block UID
and resolved structure binding; every block participates in assembly status.

First slice exposes this pure derivation and additive MCP read/digest with
`next.can_submit=false` until the exact authorized dispatch contract exists.
Follow-on slices add explicit cached-checkpoint/FIRE and resource enforcement,
validated external-run evidence import, then idempotent policy-enabled finish
submission after generation persistence. This sequencing does not claim that a
read-only envelope fulfills the automatic-relaxation policy by itself.

1. Hexfold owns current pilots, r5 per-whole-block finite plans/execution and
   the learning report. Preserve originals and input atom identities. No whole
   r5 assembly run without a distinct physical inter-block question and plan.
   Record critical finite walltime extension rationale/cap before an overrun;
   the runtime permission does not silently lift the 300-step limit.
2. Hexfold owns the underlying completion policy and additive MCP/run-state
   contract, including structure handler and SE-generation seams. First produce
   a source/API map and state-transition contract against current main. Keep
   the active replay-fix gate/tree untouched; new work uses a fresh slice.
3. Existing window25 gripe-fixer owns viewer presentation after its active batch:
   existing web templates/client code only, consuming the agreed state envelope.
   No concurrent edits to Hexfold's handler/generator/compute paths. Prepare a
   read-only presentation map now; implement after the envelope is reviewed.
   No new owner window or resurrection of closed viewer sessions.
4. Graph-memory retains the timestamp-loader/version-seam assignment. Coordinate
   all user-visible version bumps and landing order through the coordinator;
   this feature does not take over its reserved successor to the replay fix.

Acceptance covers exact-version invalidation and stale results, mixed blocks,
cache-reuse provenance, capped/failed/held states, no compute-on-read, idempotent
finish submission, actual checkpoint identity, force/constraint honesty,
before/after identity and unavailable targets. Viewer status cannot say finished
or converged for an unsupported or incomplete record. Complete meaningful scoped
and full repository gates for each implementation slice; code acceptance and
measured geometry acceptance remain distinct. Schema or breaking API changes
must follow repository thresholds rather than being hidden in this feature.
Include verified convergence at the final allowed step versus budget exhaustion;
saved output versus unadopted worker output; competing policies and absent policy;
unsupported receipt versions, missing/nonfinite/inconsistent force evidence,
wrong output identities, dangling bond endpoints, incomplete historical selection
and cache reuse without source provenance. Include the positive case: an exact
result satisfying the effective policy with passing bound post-checks yields
`finished=true`, without submitting any job during the read.
Also cover a capped output acquiring a new geometry key: repeated finish submits
zero additional jobs, concurrent requests cannot reserve duplicate allowances,
and a runtime-only extension preserves the original step budget.

## Viewer map review and adapter ownership — 2026-10-07

Window25's presentation map is accepted. Source inspection confirms live
`_viewer(version=None)` can select a succeeded result from an older input
version; runs_status currently returns pending jobs and run_count without
geometry/status identity. This is a source finding, not a production repro.

After Hex's shared completion envelope is reviewed, window25 exclusively owns
the web adapters in routes/structure.py and routes/blocktree_view.py, alongside
the mapped templates, blocktree client, web tests and owning web rationale.
Hex owns the reusable state/provenance derivation, handler/generator/compute
paths and shared response contract; do not duplicate a classifier in web code.
No concurrent edits to these two web route files by Hex. Implementation remains
sequenced after envelope review; the replay gate stays untouched.

The envelope must separate displayed geometry identity (structure id, version
and trusted fingerprint when available), run input/output identity and historical
selection. Bind SE summaries additionally to design revision/block UID and the
resolved structure identity; a historic binding is not proof of historic atoms.
Unknown bindings remain visible. Obtain coherent geometry/version evidence or
return an explicit stale/unknown state on races; do not attach a separately read
latest version to older loaded geometry. Live result selection must match the
displayed input or output identity, explicitly labelled; a saved converged output
does not make its input converged. Pinned historic runs load and label their
actual selected snapshot or report unavailable, never overlay them onto unrelated
current atoms.

Expose canonical state, independent free/constraint qualifier, actual method,
requested/achieved force and scope, caps/stop reason, run/job/candidate links,
post-check completion/outcome and bounded recommendation. Missing legacy fields
remain unavailable. A succeeded job is not convergence, and convergence alone
is not completed post-checks. Preserve before/after atom identity and history.

Use an independent status token covering geometry identity, selected run/request,
material job/run/state changes, post-checks, effective policy and holds, including
terminal failure without run-count growth.
Guard client application by request generation AND shown geometry identity.
Opaque content tokens are sufficient; no new schema counter is required merely
for polling. Polls/GETs never launch work. Do not put progress in the geometry
ETag or rebuild geometry for progress alone; changed geometry identity requires
geometry reload before a new status badge can attach. Aggregate all assembly
blocks, including hidden/isolated/missing ones, without implying whole-assembly
convergence from the visible subset. Existing validation/clipping stays intact.

Evidence: gr472846 policy comment and fi472892 before measurements; existing
structure and atomic package docstrings plus the source seams above. Source
URLs reviewed by communicator:
https://mace-docs.readthedocs.io/en/latest/guide/ase.html
https://mace-docs.readthedocs.io/en/latest/guide/cuda_acceleration.html
https://docs.ase-lib.org/ase/optimize.html


# Coordinator contract review — read-only completion v1

Reviewed owner docs commit2488b2d49fe66b4cf6a5e744f179cc5d0afc851c,
including its additions to78fdc30fa. The proposed shared snapshot and pure
envelope are the first implementation slice. The following clarifications
are required in the adopted spec and implementation.

1. Define one versioned canonical exact-snapshot encoding shared by receipt
   validation and adapters. Include ref/version, atom and bond row identities,
   cell/PBC, labels/elements/coordinates, fixed-axis masks and other scientific
   atom intent (charge, spin, oxidation, declared hybridisation), bond endpoints,
   order/kind/provenance/periodic images. Preserve null/zero distinctions; reject
   nonfinite coordinates, duplicate labels and dangling endpoints. A declared
   hybridisation field remains intent, never electronic proof. Never round
   coordinates or reuse the compute cache hash for display identity.
2. The one-statement store read must use independently ordered aggregates,
   preserve caller transaction ownership when conn is supplied, and filter ref
   kind and row retirement consistently. Explicit selected run lookup is scoped
   to the structure and cannot disappear behind a recent-history limit. Missing
   or wrong-owner selection is explicit, with no fallback. Bound history/job
   reads and omit full trajectories/force arrays from ordinary polling; expose
   truncation without misclassifying an omitted result as absent. Do not add a
   migration/index without a demonstrated need and separate threshold review.
3. Keep request/job state distinct from evidence qualification. A new failed
   request may be visible alongside earlier qualified evidence; it cannot
   rewrite that evidence or make the current geometry converged. Describe the
   selected policy/request and selected displayed role explicitly. No effective
   policy means no invented targets and finished=false.
4. Validate the complete versioned receipt, its producer/adoption provenance,
   input/output mapping and the actual displayed role. Presence of an arbitrary
   JSON object named completion is insufficient. Unknown versions, contradictory
   fields, missing/nonfinite forces or missing trustworthy output identity stay
   unknown. No legacy backfill/adoption or run-record writes in this slice.
5. Resolve the spec's final-step wording: a recorded cap stop remains
   unconverged; a verified final-step force convergence result may qualify.
   The step number alone proves neither. Validate the recorded criterion and
   comparator, force units/scope and actual termination together; reject
   contradictory evidence instead of selecting the favorable field. Separate
   fixed cell from fixed atoms. A free-atom, fixed nonperiodic-cell run need
   not claim cell optimisation; constrained forces cannot prove free convergence.
6. Passing post-checks must identify their version, criteria and complete
   required checks, bound to the same graph/geometry and policy. A bare pass
   flag, zero validator errors or lower energy does not establish completion.
   Missing checks remain unavailable. Scientific acceptance and Reto's visual
   assessment are separate: roundish sp3 fins are not failures for lacking a
   teardrop; their force-unconverged evidence and Y clashes remain facts.
7. Geometry tokens are stable under status-only changes. Status tokens include
   request selection, policy identity/holds, material status and post-checks;
   exclude read time and nondeterministic ordering. A held/unavailable policy
   does not erase historical convergence. No model imports, job submission,
   package/device discovery, permission prompt or DB writes on any read path.
8. Give the token encoding a domain/version prefix and typed canonical numeric
   normalization without precision loss. Malformed/missing cell data must not
   acquire the positions helper's synthetic lattice fallback. A mismatching
   shown token returns a reload reason and suppresses shown-geometry finished,
   level and qualified evidence; latest identity may still be returned clearly
   labelled. Historical atom intervals do not prove historical cell/PBC:
   structure_load(version=N) still uses current ref metadata. Require the real
   historic snapshot or report historical-unavailable.
9. Cache-origin evidence must be self-contained in a validated receipt or read
   coherently from the referenced source run, potentially on another structure.
   Check exact source identity and reuse mapping, not merely a cache_key match.
   A missing source cannot qualify completion. No unbounded recursive traversal.

## Minimum typed contract before classifier code

Define and share the actual v1 types with viewer25 before coding the classifier.
No additional user approval is needed for this required design clarification.
The versioned receipt must contain typed input/output identity and binding role
(input request, adopted output, or unadopted artifact), producer/adoption
provenance, request/policy identity, exact method/checkpoint/settings, constraints
and cell policy, force units/scope/raw-versus-projected status, requested target
and comparator, achieved force, actual termination and convergence, caps/usage,
source run/cache origin and bound post-check identity/results. Unknown values
are explicitly nullable, never defaulted to success. Conflicts among receipt,
row and job data degrade the affected claim to unknown with a reason.

The envelope additionally names request_id, policy_id and earlier qualified
evidence separately from the selected evidence. Specify deterministic policy
matching and latest-request ordering with a stable tie-breaker. A new request's
queued/running/failed state and existing exact screening qualification may
coexist; do not let the former erase the latter or let the latter claim the new
request succeeded. finished applies only to the explicitly identified effective
policy and shown geometry with complete passing evidence/checks. Explicit run
selection cannot fall back to earlier evidence to satisfy it. Keep any earlier
qualification labelled separately. No numeric rounding tolerance upgrades a
force failure to success.

Hex owns structure/completion.py, the narrow store snapshot seam in
store/_structure_ops.py, additive structure-handler read/digest integration,
their tests and owning rationale. Reuse structure_positions_snapshot's
coherence discipline without changing its existing consumers. All web routes,
templates and clients remain window25's; PCB's chat/conversation ownership
also remains reserved. No SE generation, optimizer, worker writeback or
dispatch changes in this first slice. next.can_submit is always false.

Follow-on designs retain explicit FIRE/checkpoint/resource enforcement,
honest worker receipts and external evidence adoption, then lineage-bound
atomic allowance and idempotent finish. The existing BFGS path is not a bug
merely for being BFGS; it cannot stand in for the specifically approved FIRE
policy. The source-confirmed missing-converged=True and unconditional success
wording must be corrected in the reviewed writer slice, not bypassed by web
classification or silently reinterpreted as valid legacy evidence.

Acceptance: pure deterministic state-table tests, malformed/legacy/foreign
receipt refusal, exact input/output and final-step cases, same-version rewrite
and status-only token cases, and real dev-DB coherent-snapshot tests. Verify
reads cannot write/submit/load a model. Existing web fixtures consume the shared
contract when adapters are subsequently implemented. Run normal source gates
on a frozen payload; no real production or scientific jobs for testing. Source
review remains required before ship; no version or ship slot is assigned here.

## Owner readback and v1 design decisions

Coordinator review adopted from the control spec observed 2026-10-07T21:11:25Z,
SHA256 `d86f7ab31913dd63f3cbeb7a8d9492a3e04f9035a81a15f9366651a82e6224ea`.
The scientific executions are now complete, separately from feature work:
original pilots `fi473386`; r5 before/plan `fi473393`, after comparisons
`fi473401`, `fi473404`, final learning `fi473424`. All six capped runs were
unconverged; no runtime extensions or model substitution. The r5 total was
41.360856 minutes, with exact native after readback and preserved originals.
These external receipts are evidence reports, not native run-history adoption.

### Source/API map at the owner task base

The reviewed task source base is `91d6af135`; Python navigation was consulted
first, then local leaves because the served index root differs from this tree.
This source map does not certify any deployed checkout.

| Existing seam | v1 use or deferred change |
| --- | --- |
| `store/_structure_ops.py:StructureMixin.structure_positions_snapshot` | Copy its one-statement coherence and caller-owned connection discipline into a separate completion snapshot reader; retain existing consumers. No synthetic lattice fallback. |
| `store/_structure_ops.py:StructureMixin.structure_runs` | Read bounded raw receipt/provenance and run identities in the same snapshot, rather than relying on its current abbreviated projection. |
| `handlers/structure.py:StructureHandler` | Add a completion read/digest delegating to the shared pure classifier. No compute, discovery or writes. |
| `handlers/structure.py:_relax_cache_address` | Existing compute cache remains; its rounded physical hash cannot bind display geometry, graph or post-check identity. |
| `structure/relax.py:_cached_ml_calculator` and `_relax_ml` | Deferred explicit checkpoint/FIRE/resources. Family-only cache and current BFGS/force floor do not implement the approved pilot policy. |
| `workers/job_types/struct_relax.py:_record_run` | Deferred writer correction: missing convergence defaults true and success wording can contradict actual termination. Never infer proof from those legacy fields. |
| `precis_se/atomic/generate.py:finish_generate` | Deferred policy finish hook after generation persistence. First slice does not edit generation or dispatch. |
| Existing web structure/blocktree routes | Viewer25 consumes shared types; Hex does not edit web adapters. |

### Exact tokens and deterministic selection

Define the geometry token domain as `precis.structure.geometry:v1:sha256:` and
status token domain as `precis.structure.completion-status:v1:sha256:`. Hash
UTF-8 canonical JSON with explicit schema field names, deterministic object key
order and independently row-ID-sorted atom/bond arrays. Do not include wall
clock read time. Preserve exact strings; no implicit Unicode label rewriting.

Canonical scientific numbers use reduced numerator/denominator integer strings:
finite integers, binary floats (`as_integer_ratio`) and database decimals share
one exact rational normalization. This introduces no tolerance or precision
loss. Signed zero normalizes to zero; null, missing, false and numeric zero remain
distinct typed values. Row identities and versions are validated integers;
booleans cannot stand in for numbers. Reject nonfinite or unsupported values,
missing/malformed cells, duplicate row IDs/labels and dangling bond endpoints
before issuing a geometry token. All scientific atom/bond intent named above
participates. This is display/receipt identity, not the existing compute key.

The snapshot reader accepts explicit `selected_run_id` in addition to `ref_id`
and optional caller `conn`. Query it separately within the same SQL statement
and scope it to the owning structure, independently of the bounded recent
history. Initial ordinary-history limits are 64 runs and 64 jobs; return explicit
truncation/selection completeness. A missing selection or a truncated search
cannot be silently replaced with some convenient successful run. Explicitly
requested policy selection must not treat an omitted older policy request as
absent. Either read that selected request coherently or mark selection unknown.
Omit trajectories and per-atom force arrays from this ordinary read.

Match policies by exact recorded policy ID and canonical policy fingerprint,
including criterion, constraints, required checks and method settings. Request
ordering is `(created_at, request_id)` descending with a stable integral-ID
tie-breaker; unparseable timestamps/identities yield unknown selection. Do not
infer request time from a terminal update. Explicit run selection describes only
that run; earlier evidence remains separately labelled and cannot satisfy it.
Without explicit selection, choose the newest matching request only when the
snapshot proves selection complete. Receipt/row/job conflicts yield a reasoned
unknown claim, not a favorable-field precedence rule.

Request state and geometry qualification coexist. A queued/running/failed retry
does not erase earlier exact evidence or make the retry successful. `finished`
requires a known effective policy, exact shown adopted-output identity, trusted
method/constraint/force evidence and complete versioned passing post-checks for
that policy. The criterion's explicit comparator is applied without rounding
tolerance; cap termination cannot be upgraded merely from a low force value.
A verified convergence termination on the final permitted step can qualify.

Hold metadata distinguishes blocking submission from blocking completion.
Neither erases earlier qualified evidence. The first slice always returns
`next.can_submit=false`, irrespective of either flag. An acceptance-blocking
hold prevents `finished`; a submission-only hold does not rewrite a historical
qualification. Missing policy or ambiguous hold scope never implies approval.
Shown-token mismatch suppresses shown-geometry evidence/level/finished and
requires reload. Historical atoms without their actual historical cell/PBC
remain unavailable.

A parsed receipt is not a trusted receipt. The validator must recognize an
implemented producer schema and verify its coherent row/request and adoption
provenance, mapping, output identity and self-contained cache-origin proof.
Arbitrary producer strings or a JSON `completion` object cannot grant trust.
No current legacy writer is retroactively recognized in v1; later reviewed
writers/adopters must establish that provenance. This slice neither imports
external pilot reports into `struct_runs` nor writes a validation stamp.

### Concrete shared v1 types

The accompanying [Python 3.11 type contract](hexfold-relaxation-completion-types.pyi)
defines the snapshot, request/run records, parsed versus verified receipt,
effective policy and public envelope. It is a specification artifact, not the
classifier implementation. Delete or migrate this companion with the owning
spec on ship; keep its definitions aligned with the reviewed source types.

Unknown scientific values are explicitly nullable. `VerifiedProof` is derived
only by the trusted verifier and cannot be deserialized from raw receipt JSON.
Receipt assessment belongs to the coherent read's derived validation result;
it is not an additional stored stamp. No legacy producer qualifies by name.
`request_id` uses the existing integral job ref ID, not a new request store.
The public envelope labels `latest_geometry` and `shown_geometry` separately,
with `selected_request`, `selected_evidence`, `earlier_qualified_evidence` and
explicit history completeness. A selected-run argument inconsistent with the
snapshot's requested selection yields unknown, never another lookup or fallback
from the pure classifier. Decoder support and no-compute deterministic tests
must precede any adapter relying on these claims.


## Viewer handoff clarification — accepted interface review

Window25's read-only review c658341871ff0515ca8684f92da7b445e66d81c275a6038be50662dec78cd2a3
is compatible with the reviewed first slice. It reviewed an earlier proposal;
owner2488b2d49 plus coordinator amendments remains the authoritative baseline.
Concrete v1 types and example payloads must precede classifier code and web
adapter implementation. Supply examples covering the accepted ten-case matrix
and same-version replacement, absent history, malformed receipts, unknown
constraints, final-step outcomes and competing requests/policies. These are
interface examples, not executed tests or scientific acceptance.

Name observed geometry identity separately from requested/shown identity and
selected input/output role. Specify unavailable/stale response shape explicitly;
the endpoint may preserve its existing HTTP convention, but must not return a
bare success badge or silently fall back to current geometry. Deleted/missing
resources and unsupported history retain their explicit existing errors or a
documented unavailable response, with no retry/model/compute side effect.

The adapter must render the geometry from the captured snapshot or use the
shared typed hasher on its exact loaded rows and compare against the envelope.
It cannot attach a fresh independently fetched token to old scene coordinates.
Keep request-generation and shown-identity guards on every client update.

Hex also supplies the shared pure SE aggregation contract: design revision,
block UID, resolved structure binding and exact/unknown per-block geometry
identity for ALL blocks, including hidden, isolated and dangling ones. Define
mixed/unknown summary rules and empty/non-atomistic applicability explicitly;
no vacuous all([]) completed assembly. Web may format counts/badges, but cannot
infer completion through its own classifier. Atomicity across SE revision,
binding and structure snapshots must be specified or a race returns unknown.
Propose callable/typed examples alongside the v1 structure contract; no web
route edits by Hex. Broader adapter/common-renderer implementation continues
to await its separately reviewed file/API map and sequencing.

## Concrete viewer transport and shared SE aggregation

The type companion includes `requested_geometry_token`, independently of
`latest_geometry` observed by the read and `shown_geometry` actually verified.
On stale input, echo the requested token, return the observed latest identity,
set shown geometry/evidence/level null and finished false, and give
`binding=stale`, `state=unknown`, reason `geometry_changed`, next phase inspect
with geometry reload as its reason. Do not return the latest completion badge
as if it belonged to the requested atoms. A token is opaque to the client.

The first response attaches an additive completion envelope to the existing
structure read/digest. Web transport keeps its existing HTTP/error conventions;
an absent/deleted structure remains NotFound, with no fabricated envelope.
An existing structure with a missing/foreign selected run returns the explicit
selection reason and unknown evidence, never a fallback run. Missing historical
atoms or cell/PBC returns `historical-unavailable`, shown geometry null, no
qualified shown evidence, finished false. A legacy/import/cache result lacking
validated provenance remains visible as raw history but cannot supply a level.
Matching the current version alone is insufficient.

Window25 renders atoms from the captured `GeometrySnapshot`, or calculates the
shared exact token from the actual loaded rows using the same codec. It must not
attach a separately fetched current token to previously loaded atoms. Responses
echo the requested geometry token; the client also checks its own render/request
generation before applying them. A late response is discarded even if it was
valid at read time. Status-only changes leave the geometry token/ETag stable.
Reads have no retry that can load a model, mutate metadata or launch work.

### Pure SE callable and identity

`se_completion_envelope(snapshot: SeCompletionSnapshot, *,
shown_design_token: str | None = None) -> SeCompletionEnvelope` is owned by Hex.
It consumes already captured block bindings and structure envelopes, performs
no I/O and uses the same structure classifier. Window25 may format its counts
and badges but must not derive another completion state.

The snapshot carries design ref/revision plus a domain-versioned exact SE token,
stable block UID and current row identity, applicability, bound kind/design,
port atom/axis/phase bindings, resolved structure ID and exact or unknown
geometry identity for every block. Hidden/isolation hints never filter the
inventory or prove anything. A structure-kind binding is atomistic even when
dangling; an unbound or unresolved block is not automatically non-atomistic.
Non-atomistic applicability requires known authored kind/schema evidence;
otherwise mark unknown. Conflicting block/port bindings remain unknown.

The SE token domain is `precis.se.completion-geometry:v1:sha256:`. Its canonical
payload includes revision, exact saved block/port rows, binding identities,
hierarchy and saved poses plus each resolved structure geometry token. Use the
same numeric codec and stable UID/row ordering as the structure contract.
Presentation-only hidden/isolation toggles do not alter scientific completion.
Pose changes can change the design token but cannot create inter-block forces,
contact evidence or a physical whole-assembly interpretation.

Source premise: `precis_se.persist.load_tree` performs multiple queries;
`_record_revision` records an SE checkpoint in the same transaction as its save.
An ordinary read-committed connection shared by those queries is not by itself
a coherent SE-plus-structure snapshot. The proposed read adapter must obtain
design revision, all live bindings and distinct bound structure snapshots in
one SQL statement with independent ordered aggregates. Reuse the structure
snapshot projection and existing tables; no new schema is assumed. Never alter
a caller's transaction/isolation ownership. If coherence or a complete inventory
cannot be established, return unknown with `snapshot_incoherent` or
`inventory_incomplete`, not assembled evidence from separate moments.
Such partial captures cannot establish a trusted design token: `latest_design`
is null unless a separate complete coherent capture establishes that identity.

The existing historic SE checkpoint proves the SE binding only. It does not
prove historic structure atoms/cell/PBC. Each pinned block therefore needs the
actual historical structure snapshot or remains historical-unavailable.
Foreign templates must be resolved within a bounded coherent read or marked
unknown; no unbounded recursive traversal. Oversized inventories may return a
documented incomplete response, never silently omit blocks from completion.

### Aggregation rules and transition examples

Every block contributes one entry. Explicitly non-atomistic entries count as
not-applicable. An atomistic block with unknown/dangling binding contributes
unknown, regardless of a stale nested envelope. With coherent, complete
inventory, a uniform set of applicable states yields that state; distinct
states yield mixed. Unknown applicability or binding prevents an all-finished
claim; counts preserve each known state so mixed never conceals the unknowns.
An incomplete/incoherent snapshot or mismatched shown design token yields
top-level unknown and suppresses shown block qualification.

`all_atomistic_blocks_finished` is true only when the complete inventory has
at least one atomistic block, no unknown applicability/binding, and every
atomistic block's exact shown envelope is finished under its identified policy.
Before accepting a nested envelope, compare the resolved structure ID, captured
block geometry and nested `completion.shown_geometry`, including exact tokens,
and require `completion.binding=exact`. A mismatch suppresses that nested
envelope, sets the block binding unknown with `nested_geometry_mismatch`, and
cannot count as finished even if the input nested boolean says true.
Empty or entirely non-atomistic scenes return not-applicable and false, never
vacuous completion. Summary scope is explicitly per-block-only. v1 always
returns `physical_assembly_assessed=false` and whole-assembly `finished=false`;
the UI may say all block checks complete, not that a physical interacting
assembly was relaxed. Assembly-level checks would require their own policy and
evidence. This does not authorize a combined r5 run.

| Captured blocks or transition | Shared result |
| --- | --- |
| One exact completed block, hidden preview block, dangling atomistic binding | mixed; all three counted; one finished, one preview, one unknown; all-blocks-finished false |
| Isolate only the completed block in the viewer | Same completion result and counts; presentation cannot hide unfinished blocks |
| Every atomistic block has exact policy-qualified passing checks | all-atomistic-blocks-finished true; per-block scope; whole-assembly finished still false |
| No blocks, or only explicitly non-atomistic blocks | not-applicable; both finished flags false |
| Unbound block with unknown applicability | unknown; do not assume non-atomistic |
| Design or bound structure changes while an old request is in flight | stale/unknown for old shown token; reload geometry before applying new status |
| Job becomes failed without geometry or SE revision changing | Same SE geometry token; changed completion status token and failed block/request details |

SE status tokens use `precis.se.completion-status:v1:sha256:` and include the
design token, ordered block binding/completion status tokens, policy/selection,
inventory completeness and derived summary. Exclude polling timestamps and
presentation visibility. No model load, database write, job submission or
permission question occurs in either pure callable or any read adapter.

### Published interface examples and final disambiguation

The accompanying [synthetic JSON projections](hexfold-relaxation-completion-examples.json)
contain 28 structure cases and 12 SE cases, plus client scenarios and a mapping
to the viewer's accepted ten-case matrix. These are deliberately labelled
recursive projections, not full TypedDict instances, executed tests, valid hash
fixtures or production evidence. Every omitted nested key is unspecified, not
a default, null or proof. Positive cases assume a future reviewed producer and
verifier; arbitrary JSON cannot make today's legacy writer qualify.

`selected_evidence` may describe the selected run's explicitly labelled
unadopted artifact or a policy-mismatching result. That can be inspected, but
cannot set the shown geometry's top-level level/post-checks/finished. It is not
an implicit fallback. Stale shown identity suppresses selected evidence entirely.
`earlier_qualified_evidence` stays separately labelled; neither collection can
turn a failed selected request into a success.

Scientific `policy_digest` covers method, criterion, constraints and required
post-checks, excluding mutable holds and submission budget. The latter remain
independent authority checks and status-token inputs. A submission-only hold
may yield held with finished true for an already qualified result; a
completion-blocking hold cannot. Changing a hold never rewrites prior force
qualification. `next.phase=inspect` also means ordinary review of a completed
result, not necessarily a defect or a recommendation to run more compute.
The JSON's `reason_code_vocabulary` freezes the example reasons for this
proposal; adapters preserve unfamiliar future reasons as explanatory unknowns,
never map an unrecognized code to success.

`CompletionSnapshot.geometry` denotes current observed rows. Optional
`selected_geometry` and its role carry only an actual verified historic
snapshot for the explicit selected run, including its own cell/PBC. Thus
`latest_geometry` can remain current while `shown_geometry` is legitimately
historic. First-slice readers without that verified snapshot set these fields
null and report historical-unavailable. They do not reconstruct historic cell
from current metadata. SE likewise separates observed `latest_design` from its
captured live or verified historic design; a historic design does not by itself
provide historic bound structures.

This frozen deliverable is contract preparation. It does not authorize web
adapter overlap, add a persistence schema or implement model/worker/SE-generation
changes. Concrete source, API and example review precedes classifier code.

### Shared molecule viewer: exact geometry and diagnostic supplement

This supplements the same typed proposal; it is not a second completion
envelope. Viewer25 owns renderer/selection types and both web adapters. Hex
supplies `MoleculeGeometryEvidence`, reusing `GeometrySnapshot`, exact identities
and `CompletionEnvelope`. Combined map/type review still precedes source work.
No schema, job, model, rebond, version or ship is authorized by these fields.

Source checked at task base `91d6af135`: native Python search/symbol reads first,
then the matching local leaves (served index is a different tree).
`Cell.frac_to_cart` is row-vector `frac @ lattice`; `_geom_payload` uses stored
bonds if nonempty, else `detect_bonds`. Its legacy payload remains unchanged.
`detect_bonds(scene, tolerance=1.2)` uses covalent radii and inclusive
`d <= 1.2 * (r_i + r_j)`, emitting each unordered pair once with the existing
pair kernel's image offset and `provenance=inferred`. The kernel searches its
existing bounded image neighborhood; do not advertise a new general lattice
solver or infer chemical stability from its output. No new inference algorithm
or reinterpretation of a clicked edge is part of this proposal.

**Physical identity and periodic orientation.** Lattice rows are a/b/c in Å;
for endpoint image `n`, `cart_A = (frac + n) @ lattice`. Image components are
integers and must be zero on nonperiodic axes. Stored edge orientation is
`i@0 -> j@image`; a translated instance is `i@k -> j@(k+image)`.
Reversing it is `j@0 -> i@(-image)` plus the corresponding common translation,
not a new minimum-distance search. Preserve row IDs, exact labels, image,
order/kind/provenance. Never alias parallel periodic edges by label pair or
array ordinal. v1 renders persisted snapshots only: an unadopted artifact can
retain explicitly labelled completion evidence, but its geometry/instance is
unavailable until a reviewed adoption supplies actual persisted identities.
Never invent a structure ref or atom-row IDs for an external artifact. Historical
snapshots require their actual row identities, cell and PBC. An ambiguous duplicate without a
trusted edge ID is not selectable. Invalid image data disables that edge with
a reason; valid atom geometry can remain drawable. Fractional/GPU rounding is
presentation only and must not feed physical tokens or measured values.

**Scene identity.** Repeated SE bindings use separate `SceneInstanceIdentity`
with design revision/token, block UID, binding and exact placement token, role,
run and captured layer ID. Standalone structure has null design/block/binding
and explicit identity placement. Shared canonical encoder domains are
`precis.structure.placement:v1:sha256:` (ordered parent/local rigid transforms)
and `precis.structure.scene-instance:v1:sha256:` (all instance fields except its
own token, including positive finite host-units-per-metre scale). These are
display-scene identity, not physical-geometry identity.
The already specified SE token retains whole-design placement/bindings.
Moving a block changes scene identity without changing atomic physical identity;
clip/visibility/hover never changes either. Adapters consume the producer's
tokens rather than hashing rounded coordinates themselves.

Use row-vector rotations in the shared boundary: convert local Å to metres
once, then apply each child-to-parent rigid transform as `p @ R + t_m`, child
first through root; apply one declared host-units-per-metre display scale last.
Require finite proper orthonormal rotations and translations; unsupported
affine/shear/reflection input is unavailable, not silently repaired. Validate
rotations using maximum absolute error in `R.T @ R - I` and `det(R) - 1`
at most `1e-10`, without rounding or normalizing their token payload.
Viewer25's host adapter owns explicit plane-coordinate conversion and
must put points and clip planes in the same declared host space. Interpolated
smooth/target hits are not measured atom coordinates: retain separately labelled
source association or make the interpolated layer non-atom-selectable. Selection
and async guards include scene instance, physical token, role, immutable layer
mapping and render generation.

**Graph evidence.** `GraphIdentity.source` distinguishes stored graph,
receipt-linked comparison graph, transient inferred display graph and unknown.
Per-edge provenance remains independent: a stored graph may contain authored
and previously inferred rows. A retained pre-relax graph is a comparison graph
only when the lineage is evidenced, never proof that physical bonds survived.
`receipt-comparison` requires an exact `comparison_source_graph_token` on the
identified input plus verified `mapping_proof`, `atom_mapping_digest` and
`mapped_atom_count`. The reviewed mapper proves the source graph/labels and
displayed target atom identities correspond, and binds both geometry tokens;
require coverage of every compared endpoint and disclose the comparison
population. A bare source ID, reordered labels or unverified digest is not
proof: mark graph basis unknown and graph-dependent diagnostics unavailable.
Transient fallback is permitted only where the existing empty-graph inference
policy is explicitly selected and the bounded read budget permits it; otherwise
render atoms and mark graph unavailable. Do not fill holes in a partial graph.
The graph token domain `precis.structure.graph:v1:sha256:` includes the physical
token, source, completeness, ordered exact edges and comparison/inference
provenance. Inferred edges have null row IDs and exact captured keys bound to
that graph token; canonical endpoint orientation includes image inversion.
Inference receipts name the actual algorithm revision, parameters and dependency
digest (kernel/cell and radii table); absent versions mean token unavailable.
These edges are never persisted by reads, adopted as bonds, or used as proof of
post-validation. The renderer never runs a bond detector or a 27-image guess
to identify a click.

**Diagnostic truth and applicability.** Each `GeometryDiagnostic` is bound to
exact physical geometry, graph basis when applicable, atom/image or bond keys,
method/version, quantity/units, parameters, population and completeness/counts.
Families keep declared bond measurements, angle/POAV, clashes, short contacts,
bond opening, coordination, shape and target deviation separate. In particular,
an excluded two-hop short contact is not zero evidence merely because a clash
family reports zero. Cutoff/comparator, hop exclusions, hydrogen treatment and
comparison graph must be explicit for applicable families. Bounded/truncated
evidence says so; zero complete findings differs from unavailable diagnostics.
Graph-dependent quantities without a trusted graph are unavailable. Diagnostics
are supplied owner evidence, not new calculations in the renderer, and cannot
set `finished` outside the existing classifier/post-check policy.

References carry applicability, chemistry scope, units and method/version;
target-based references additionally require receipt identity and proof.
Unknown is null with reason, never a default zero or universal sp2 reference.
Raw bond lengths and angles can remain measured while strain is unavailable.
The existing 1.42 Å / 120° reference is not automatically applicable to sp3 fins,
other elements or unknown chemistry. Four-coordinate authored intent alone is
not measured electronic hybridization. POAV requires its method's supported
neighborhood and must disclose which atoms were excluded. No synthetic legacy
r5 target receipt is allowed. Reto's 20:17:52Z assessment permits roundish sp3
fins; missing teardrop is observation, not their failure criterion. Actual pilot
force caps and Y clash/contact evidence remain unchanged; an sp2 teardrop remains
an unmeasured hypothesis, not a new compute instruction.

`MoleculeGeometryEvidence` separates geometry from completion availability:
valid geometry remains drawable when completion is unavailable or a poll fails.
A stale geometry request cannot borrow current geometry under its old token.
No historic atoms/current cell mixture; no role/ordinal fallback. All required
typed keys exist, unknown values are null, empty diagnostic lists imply nothing
without `diagnostics_complete`, and arbitrary JSON cannot self-certify proof.

Review examples: a nonorthogonal partial-PBC edge and reversed image preserve
the same physical segment; two placed instances share physical token but differ
in scene token; cap-stop changes only completion status; a raw sp3 angle has no
automatic 120° strain; two-hop new contacts coexist with zero reported clash
pairs; missing legacy target yields unavailable deviation; exact geometry with
failed status transport remains drawable. These are requirements for later
owner fixtures, not executed implementation/browser acceptance.
The companion JSON includes seven additional synthetic geometry/diagnostic
projections under `geometry_supplement_cases`, using the same explicitly partial
representation as the original examples.


## Interface refinement after coordinator reviews 21:53Z and 21:58Z

Adopts control revision SHA256
`49a8b87f856e67fa9307754dd76ef2881cce591aef731cd26a960e0e1b3cbd49`,
which supersedes the relayed `d86f7ab3` review without removing this document's
prior contract history. The frozen `80d1d95ce` and `5a0246b` reviews accept the
current/history, SE aggregation and geometry supplement contracts with the
following refinements. Narrow current snapshot and exact codec source work is
authorized independently. Classifier and public MCP adapters wait review of
this interface packet. No writer/optimizer, migration, generation, web, version
or shipping change is included.

### Requested display and resolution

`DisplaySelector` is an explicit discriminated caller request: `current`,
`persisted` (exact GeometryIdentity, input-request or adopted-output role and
nullable run ID), or `artifact` (exact ArtifactIdentity, unadopted-artifact role
and nullable run ID). Reader/service and pure envelope accept it and echo it in
both snapshot and envelope. An omitted reader selector defaults to current;
an omitted pure-envelope selector uses the captured snapshot selector. It never
replaces an explicit request with current. A supplied pure selector must equal
the captured selector; mismatch is conflict and cannot qualify evidence.

`DisplayResolution` records resolution independently: exact, stale, unsupported,
missing, conflict or invalid, with resolved geometry/artifact, role and reason.
An unsupported artifact retains its requested artifact identity as labelled
evidence, never a fabricated persisted identity. Unsupported or missing geometry
has null shown_geometry. A malformed selector is invalid, not a current default.
The requested persisted identity must match ref ID, version and full token; an
old identity alone does not establish an available historical snapshot.

A non-null selector run_id and separately supplied selected_run_id must agree;
otherwise resolution is conflict. When only one is supplied it determines the
captured explicit run lookup; it must belong to this structure. The pure call
must agree with snapshot.selected_run.requested_run_id. A current selector may
independently inspect a run, but that run never silently chooses input/output.
Persisted roles remain distinct even when input and output geometry identities
are equal. `selected_geometry_role` describes the captured historical snapshot,
not the caller's request. A role without corresponding verified evidence cannot
borrow an available snapshot of another role.

The existing shown_geometry_token is an additional presentation consistency
check, not a selection mechanism: a conflicting token is stale, never fallback.
These request and resolution fields participate in the status token. First-slice
historical/artifact rendering may explicitly remain unsupported; no loader or
adopter is introduced. Selection failure does not destroy an independently valid
current geometry snapshot or prevent its separately requested display.

### Lossless transport, independently of scientific hashes

`completion_codec` encodes every Python integer, including IDs, versions, counts
and format numbers, as `{"$int":"<canonical decimal>"}`. JSON clients must decode
that to arbitrary precision or retain the validated decimal string; Number
coercion is not allowed. Every finite float, Decimal and Fraction is encoded as
`{"$rational":{"n":"<signed decimal>","d":"<positive decimal>"}}`, reduced to
coprime terms, with zero represented as 0/1. Float uses its exact binary ratio;
Decimal uses its exact decimal ratio. Negative zero normalizes to zero. Decoder
returns int or Fraction: exact numeric value is preserved, not source runtime
numeric class. Scientific canonicalization explicitly calls canonical_number;
it does not confuse an integer-valued scientific quantity with an integer ID.

Booleans are only JSON booleans, never numbers; null is explicit unknown and is
not a missing required key. Strings remain exact. Lists and tuples encode to
arrays, dictionaries require string keys. Reserved tag keys in ordinary data,
extra tag fields, raw JSON numbers, noncanonical integer strings, unreduced
ratios, nonpositive denominators, nonfinite values and duplicate JSON keys are
rejected. Transport limits bound depth, item count, numeric digits and UTF-8
text size. These are decoding resource limits, not scientific truncation; an
exceeded limit fails the payload rather than dropping evidence.

The complete wire fixture file is separate from the earlier 47 explanatory
projections. Its builder validates all required typed keys recursively before
encoding and after decoding and checks wire round trips, including a large ID
and exact decimal/binary values. Positive proof records in fixtures are synthetic
verifier outputs; parsing fixture JSON does not make any DB receipt trusted.
This fixture validation is shape/transport evidence, not classifier execution.

### Periodic comparison proof and noncircular graph tokens

A receipt comparison graph additionally requires `periodic_mapping` with exact
source/target geometry, source/target cell digests, endpoint-image mapping digest,
cell/basis correspondence digest, compared endpoint count, complete-population
flag and verified proof. This proves every compared endpoint's absolute image
and basis correspondence, not just atom labels. Geometry tokens and digests must
agree with the enclosing graph, source identity and displayed target snapshot.
Missing or mismatched proof makes comparison graph and dependent diagnostics
unavailable. Wrapped atoms can require changed endpoint images even with the
same lattice; changed bases require independently evidenced basis correspondence.
A first slice without such a mapper deliberately returns unavailable. No mapper,
rebonding or new diagnostic computation is introduced.

Graph canonical preimages exclude their own token/digest and every derived
PhysicalBondKey.graph_token. Hash ordered exact source edges and provenance
first, then attach that resulting token to selection keys. Identity mismatch
disables affected graph selection/diagnostics while valid atom geometry remains
drawable. Completion transport failure likewise leaves valid geometry drawable.

### Current source seam

`StructureMixin.structure_completion_geometry_snapshot(ref_id, *, conn=None)`
provides only raw current geometry input to the later complete reader. One SQL
statement captures live ref metadata and independent ordered atom/bond aggregates,
including row IDs. Metadata JSON text is decoded with Decimal; SQL float columns use float8send
hex and exact binary decoding (real promotes exactly to double), avoiding
text-output rounding and preserving their different numeric semantics. Missing/retired/wrong-kind
returns None; no current-cell fallback or historical mixture. It does not claim
a CompletionSnapshot or load run/request evidence yet. The full reader and pure
classifier remain the reviewed next source seam, with next.can_submit=false.


## Accepted first read slice implementation

Root concrete-interface acceptance: control SHA256
`4c4d78cf75e1ff8ccdeffb747662b8610187d6e4c1cd4774f13db0536226d6ad`.
Frozen e38 types/selector/wire/periodic requirements are closed; historical
proposal bytes above remain. The bounded implementation reuses existing stores:

- `store._structure_completion.read_completion_snapshot`, exposed by the typed
  `Store.structure_completion_snapshot`, captures geometry, bounded runs/jobs
  and an independently selected run in one statement. No trusted legacy producer
  exists; raw completion JSON never becomes a verified receipt.
- `structure.completion_types` and `tests/fixtures/structure/completion-wire.json`
  hold durable types and complete payload fixtures independently of this backlog.
- `structure.completion_identity` hashes exact scientific rows and stored graphs;
  graph preimages contain no own token. Hashing has a separate bounded budget
  from wire transport, accommodating the existing 5,790-atom Y structure.
- Pure `completion_envelope` separates request/policy/evidence, role and geometry,
  cap versus convergence, free versus constrained force criteria, verified cache
  origin and policy postchecks bound to the exact stored graph.
- `precis_se.completion.se_completion_snapshot` captures the all-block binding
  inventory and structure rows in one statement. Pure `se_completion_envelope`
  never treats empty/incomplete/unknown bindings as finished, or independent
  block relaxation as physical assembly relaxation.
- Additive MCP `completion`/`completion_digest` views use the shared decoder and
  derivation. Numeric request args require exact wire tags. Unsupported historic
  or artifact roles remain explicit; no loader/adopter is added.

No read performs generation, compute, model discovery, mutation or dispatch.
Every `next.can_submit` remains false. No schema, optimizer/writer, web route,
version or ship changes belong to this slice. Synthetic verified fixtures test
scientific predicates; they are not production producer or geometry acceptance.

## Bounded source-review corrections H1–H3

Coordinator review4054719a of710cde0cd is applied after the newer34d139902
full suite terminated (29404PASS/exit0). Original frozen manifests, e38/710
failures and all raw gate logs remain preserved. No active gate was interrupted.

- H1: when automatic selection has no matching captured request, incomplete
  request coverage cannot qualify an older run as the current result. Keep
  qualified earlier evidence visible. Exact explicit run selection and a
  complete no-request window retain their independent semantics.
- H2: absent/unrecognized SE binding is unknown applicability, not evidence
  of a non-atomistic block. Supported authored cad/component/part bindings
  with a design provide positive non-atomistic evidence; atomic bindings,
  atomic mode and structure port references retain atomistic precedence.
  Unresolved templates/arrays remain unknown.
- H3: exact complete empty SE inventory is not-applicable, with both finished
  flags false. Incomplete/incoherent/stale empty captures remain unknown.

Deterministic tests reproduce8failures/8passes on original34 source, including
bounded/unknown/truncated request windows, supported and unknown block kinds,
mixed finished+unknown inventory, and exact/incomplete/incoherent/stale empty
captures. Corrections remain within accepted types and read-only boundaries:
zero trusted legacy producers, next.can_submit=false, no writer/optimizer/
adoption/generation/schema/web/compute/version/ship. Focused, types, lint and
full canonical regression validate the corrected frozen source.
