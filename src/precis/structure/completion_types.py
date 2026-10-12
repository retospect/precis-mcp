"""Read-only completion and molecule evidence types, shared across adapters.

All TypedDict keys are required; unknown values are explicit None. A type is
not validation or authority. Only the reviewed producer/adoption verifier may
construct VerifiedReceipt, and v1 may have no available trusted producer.
Raw DB JSON, legacy booleans and producer names never constitute that proof.
Tokens use the separately specified exact, domain-versioned canonical codec.
No trajectories, per-step force arrays or recursive cache traversal on reads.
"""

from decimal import Decimal
from fractions import Fraction
from typing import Literal, TypedDict

Fidelity = Literal[
    "check",
    "stick",
    "clean",
    "0",
    "geo",
    "emt",
    "ff",
    "xtb",
    "ml",
    "gpaw",
    "dft-fast",
    "dft-tight",
]
State = Literal[
    "unknown",
    "preview-only",
    "queued",
    "running",
    "converged",
    "stopped-unconverged",
    "failed",
    "held",
    "unavailable",
]
Binding = Literal["exact", "unknown", "stale", "historical-unavailable"]
DisplayRole = Literal["input-request", "adopted-output", "unadopted-artifact"]
ProofStatus = Literal["verified", "unavailable", "invalid"]
ExactNumber = (
    int | float | Decimal | Fraction
)  # codec rejects bool/nonfinite, preserves ratios
Scalar = str | int | float | Decimal | Fraction | bool | None
Vector3 = tuple[ExactNumber, ExactNumber, ExactNumber]
Image3 = tuple[int, int, int]
Lattice3 = tuple[Vector3, Vector3, Vector3]


class GeometryIdentity(TypedDict):
    format: Literal[1]
    ref_id: int
    version: int | None
    token: str


class AtomSnapshot(TypedDict):
    row_id: int
    label: str
    element: str
    fractional: Vector3
    fixed_mask: int
    magmom: ExactNumber | None
    oxidation: ExactNumber | None
    charge: int | None
    hybridization: str | None  # authored intent, not electronic evidence


class BondSnapshot(TypedDict):
    row_id: int
    i: int  # atom row identity
    j: int
    image: Image3
    order: ExactNumber
    kind: str
    provenance: str


class GeometrySnapshot(TypedDict):
    identity: GeometryIdentity
    source: Literal["live-statement", "verified-historic-snapshot"]
    lattice: Lattice3  # no synthetic fallback
    pbc: tuple[bool, bool, bool]
    atoms: list[AtomSnapshot]
    bonds: list[BondSnapshot]


class ArtifactIdentity(TypedDict):
    artifact_id: str
    snapshot_codec: str
    snapshot_digest: str
    atom_identity_digest: str
    graph_digest: str | None
    cell_digest: str
    natoms: int


class CurrentDisplay(TypedDict):
    kind: Literal["current"]


class PersistedDisplay(TypedDict):
    kind: Literal["persisted"]
    role: Literal["input-request", "adopted-output"]
    geometry: GeometryIdentity
    run_id: int | None


class ArtifactDisplay(TypedDict):
    kind: Literal["artifact"]
    role: Literal["unadopted-artifact"]
    artifact: ArtifactIdentity
    run_id: int | None


DisplaySelector = CurrentDisplay | PersistedDisplay | ArtifactDisplay


class DisplayResolution(TypedDict):
    status: Literal["exact", "stale", "unsupported", "missing", "conflict", "invalid"]
    geometry: GeometryIdentity | None
    artifact: ArtifactIdentity | None
    role: DisplayRole | None
    reason: str | None


class InputIdentity(TypedDict):
    persisted: GeometryIdentity | None
    artifact: ArtifactIdentity | None
    physical_input_digest: str | None  # not a substitute for exact identity


class OutputIdentity(TypedDict):
    kind: Literal["adopted-output", "unadopted-artifact", "unavailable"]
    persisted: GeometryIdentity | None
    artifact: ArtifactIdentity | None
    atom_mapping_digest: str | None
    mapped_atom_count: int | None
    adoption_record_id: str | None


class Proof(TypedDict):
    status: ProofStatus
    verifier: str | None
    verifier_version: str | None
    evidence_record_id: str | None
    bound_receipt_digest: str | None
    reason: str | None


class VerifiedProof(TypedDict):
    # Internal verifier output, not a raw JSON signature or producer assertion.
    status: Literal["verified"]
    verifier: str
    verifier_version: str
    evidence_record_id: str
    bound_receipt_digest: str


class ProducerProvenance(TypedDict):
    producer_id: str | None
    producer_version: str | None
    writer_contract: str | None
    source: Literal["computed", "external-adopted", "cache-reuse", "unknown"]
    source_record_id: str | None


class Method(TypedDict):
    fidelity: Fidelity | None
    model_family: str | None
    model_variant: str | None
    checkpoint_sha256: str | None
    backend: str | None
    backend_version: str | None
    device: str | None
    precision: str | None
    dispersion: bool | None
    optimizer: str | None
    optimizer_version: str | None
    optimizer_settings: dict[str, Scalar]


class Constraints(TypedDict):
    atoms: Literal["free", "constrained", "unknown"]
    fixed_atom_count: int | None
    fixed_axis_count: int | None
    atom_mask_digest: str | None
    additional_constraints: list[str] | None  # None != no constraints
    cell: Literal["fixed", "inplane", "full", "unknown"]
    pbc: tuple[bool, bool, bool] | None


class ForceCriterion(TypedDict):
    quantity: Literal["max-atomic-force-norm"]
    units: Literal["eV/angstrom"]
    comparator: Literal["lt", "le"]
    target: ExactNumber
    scope: Literal["all-atoms-raw", "constraint-projected"]


class ForceObservation(TypedDict):
    units: Literal["eV/angstrom"] | None
    max_raw: ExactNumber | None
    max_projected: ExactNumber | None
    criterion_value: ExactNumber | None
    criterion_scope: Literal["all-atoms-raw", "constraint-projected"] | None
    approximate: bool | None
    output_artifact_digest: str | None


class Termination(TypedDict):
    reason: Literal[
        "force-converged",
        "step-cap",
        "time-cap",
        "cancelled",
        "backend-failure",
        "resource-limit",
        "unknown",
    ]
    backend_converged: bool | None
    steps: int | None
    criterion: ForceCriterion | None
    forces: ForceObservation
    cell_criterion: dict[str, Scalar] | None  # fixed cell needs no cell target
    initial_energy_eV: ExactNumber | None
    final_energy_eV: ExactNumber | None


class ResourceCaps(TypedDict):
    steps: int | None
    wall_seconds: ExactNumber | None
    cpu_threads: int | None
    memory_bytes: int | None
    external_usd: ExactNumber | None
    cloud_usd: ExactNumber | None


class ResourceUsage(TypedDict):
    steps: int | None
    wall_seconds: ExactNumber | None
    peak_memory_bytes: int | None
    external_usd: ExactNumber | None
    cloud_usd: ExactNumber | None


class RuntimeExtension(TypedDict):
    decision_id: str
    recorded_at: str  # durable UTC timestamp; never polling time
    previous_wall_seconds: ExactNumber
    revised_wall_seconds: ExactNumber
    reason: str
    evidence_ids: list[str]


class Lineage(TypedDict):
    authorization_id: str | None
    candidate_id: str | None
    original_input: InputIdentity | None
    parent_attempt_id: str | None
    attempt_id: str | None
    allowance_record_id: str | None
    caps: ResourceCaps
    attempt_usage: ResourceUsage
    cumulative_usage: ResourceUsage
    extensions: list[RuntimeExtension]


class CheckRequirement(TypedDict):
    check_id: str
    implementation_version: str
    criterion_digest: str
    criteria: dict[str, Scalar]


class CheckResult(TypedDict):
    requirement: CheckRequirement
    outcome: Literal["pass", "fail", "unavailable"]
    evidence_id: str | None
    reason: str | None


class PostChecks(TypedDict):
    format: Literal[1]
    policy_id: str | None
    policy_digest: str | None
    geometry: GeometryIdentity | None
    artifact: ArtifactIdentity | None
    graph_digest: str | None
    suite_version: str | None
    required: list[CheckRequirement] | None
    results: list[CheckResult]
    state: Literal["unknown", "pending", "complete"]
    outcome: Literal["unknown", "pass", "fail"]


class CacheSourceEvidence(TypedDict):
    # Flattened, verified evidence; never recurse through another cache receipt.
    ref_id: int
    run_id: int
    receipt_digest: str
    input: InputIdentity
    output: OutputIdentity
    method: Method
    constraints: Constraints
    termination: Termination
    producer: ProducerProvenance
    producer_proof: VerifiedProof
    output_proof: VerifiedProof


class CacheOrigin(TypedDict):
    kind: Literal["fresh", "cache-reuse", "unknown"]
    cache_key: str | None
    source: CacheSourceEvidence | None
    source_lookup: (
        Literal["self-contained", "coherent-direct-read", "unavailable"] | None
    )
    reuse_mapping_digest: str | None
    mapped_atom_count: int | None
    mapping_proof: Proof | None


class CompletionReceiptV1(TypedDict):
    # Parsed declared content. Must pass semantic and provenance validation.
    format: Literal[1]
    request_id: int | None
    policy_id: str | None
    policy_digest: str | None
    run_id: int | None
    job_id: int | None
    input: InputIdentity
    output: OutputIdentity
    producer: ProducerProvenance
    method: Method
    constraints: Constraints
    termination: Termination
    lineage: Lineage
    cache: CacheOrigin
    post_checks: PostChecks | None


class VerifiedReceipt(TypedDict):
    # Only produced by the reviewed verifier; never cast raw DB JSON to this.
    receipt: CompletionReceiptV1
    receipt_digest: str
    producer_proof: VerifiedProof
    adoption_or_artifact_proof: VerifiedProof
    consistency_proof: VerifiedProof  # row/job/receipt/force/identity agreement


class ReceiptAssessment(TypedDict):
    status: ProofStatus
    verified: VerifiedReceipt | None
    reason_codes: list[str]


class RunRecord(TypedDict):
    ref_id: int
    run_id: int
    job_id: int | None
    request_id: int | None
    on_version: int | None  # legacy: does not define input versus output
    created_at: str | None
    raw_status: str | None
    raw_fidelity: str | None
    raw_model: str | None
    raw_converged: bool | None
    raw_max_force: ExactNumber | None
    raw_n_steps: int | None
    raw_provenance: str | None
    raw_completion: object | None
    # Reader/service supplies verifier-owned assessment against coherent DB
    # provenance. Never deserialize this field out of raw_completion. Until a
    # reviewed producer exists it is unavailable, including legacy records.
    receipt_assessment: ReceiptAssessment


class RequestRecord(TypedDict):
    request_id: int  # v1 durable job ref ID; not a new request-ID namespace
    job_id: int | None
    owner_ref_id: int
    policy_id: str | None
    policy_digest: str | None
    input: InputIdentity | None
    created_at: str | None  # durable UTC; order with request_id, never read time
    state: Literal["queued", "running", "succeeded", "failed", "cancelled", "unknown"]
    failure_reason: str | None
    result_run_id: int | None
    lineage: Lineage | None


class PolicyHold(TypedDict):
    hold_id: str
    reason: str
    blocks_submission: bool
    blocks_completion: bool
    authority_record_id: str | None


class EffectivePolicy(TypedDict):
    policy_id: str
    policy_digest: str
    version: str
    state: Literal["applicable", "held", "unavailable", "unknown"]
    reason: str | None
    holds: list[PolicyHold]
    request_id: int | None
    method: Method
    constraints: Constraints
    criterion: ForceCriterion | None
    required_post_checks: list[CheckRequirement] | None
    lineage: Lineage | None
    authority_proof: Proof


class ReadWindow(TypedDict):
    limit: int
    returned: int
    truncated: bool
    coverage: Literal["complete", "bounded", "unknown"]
    next_cursor: str | None


class RunSelection(TypedDict):
    requested_run_id: int | None
    status: Literal["not-requested", "found", "missing", "wrong-owner", "unavailable"]
    record: RunRecord | None  # explicit lookup separate from bounded history


class CompletionSnapshot(TypedDict):
    format: Literal[1]
    requested_display: DisplaySelector
    display_resolution: DisplayResolution
    ref_id: int
    geometry: GeometrySnapshot | None  # current observed rows
    selected_geometry: GeometrySnapshot | None  # actual verified historic snapshot
    selected_geometry_role: DisplayRole | None
    geometry_reason: str | None
    raw_generated: object | None
    raw_last_relax: object | None
    runs: list[RunRecord]
    requests: list[RequestRecord]
    selected_run: RunSelection
    run_window: ReadWindow
    request_window: ReadWindow
    snapshot_consistency: Literal["single-statement", "unavailable"]


class QualifiedLevel(TypedDict):
    fidelity: Fidelity | None  # existing ladder, never a new numerical rank
    qualification: Literal["unknown", "attempted", "converged", "post-validated"]


class QualifiedEvidence(TypedDict):
    request_id: int | None
    policy_id: str | None
    run_id: int
    role: DisplayRole
    geometry: GeometryIdentity | None
    artifact: ArtifactIdentity | None
    assessment: ReceiptAssessment
    level: QualifiedLevel
    constraints: Constraints
    post_checks: PostChecks | None


class NextPhase(TypedDict):
    phase: Literal[
        "inspect", "screen", "wait", "post-validate", "refine", "resolve-hold"
    ]
    fidelity: Fidelity | None
    reason: str
    can_submit: Literal[False]  # first slice cannot launch anything
    resources: ResourceCaps | None


class CompletionEnvelope(TypedDict):
    format: Literal[1]
    requested_display: DisplaySelector
    display_resolution: DisplayResolution
    requested_geometry_token: str | None
    latest_geometry: GeometryIdentity | None
    shown_geometry: GeometryIdentity | None
    shown_role: DisplayRole | None
    binding: Binding
    state: State
    reason_codes: list[str]
    request_id: int | None
    policy_id: str | None
    selected_request: RequestRecord | None
    selected_run: RunSelection
    effective_policy: EffectivePolicy | None
    selected_evidence: QualifiedEvidence | None
    earlier_qualified_evidence: list[QualifiedEvidence]
    constraints: Constraints | None
    level: QualifiedLevel | None
    post_checks: PostChecks | None
    finished: bool
    next: NextPhase
    run_window: ReadWindow
    request_window: ReadWindow
    earlier_evidence_complete: (
        bool  # false when bounded reads cannot prove completeness
    )
    status_token: str


class SeDesignIdentity(TypedDict):
    ref_id: int
    revision: int | None
    token: str  # exact captured SE inventory/bindings/poses, versioned domain
    source: Literal["live-statement", "verified-historic-snapshot"]


class SePortBinding(TypedDict):
    row_id: int
    name: str
    bound_design: str | None
    bound_atom: str | None
    axis_atom: str | None
    phase_atom: str | None
    resolved_structure_id: int | None


class SeBlockCompletionInput(TypedDict):
    block_uid: int
    row_id: int | None
    name: str
    parent_uid: int | None
    applicability: Literal["atomistic", "non-atomistic", "unknown"]
    applicability_reason: str | None
    bound_kind: str | None
    bound_design: str | None
    port_bindings: list[SePortBinding]
    resolved_structure_id: int | None
    binding_status: Literal["exact", "dangling", "unknown", "not-applicable"]
    binding_token: str | None
    geometry: GeometryIdentity | None
    completion: CompletionEnvelope | None
    # Optional presentation context, never an aggregation filter or proof.
    hidden: bool | None
    isolated: bool | None


class SeCompletionSnapshot(TypedDict):
    format: Literal[1]
    # Null unless established by a complete coherent identity capture.
    latest_design: SeDesignIdentity | None
    design: SeDesignIdentity | None  # captured live or verified selected history
    reason_codes: list[str]
    complete_inventory: bool
    snapshot_consistency: Literal[
        "single-statement", "verified-historic-snapshot", "unavailable"
    ]
    blocks: list[SeBlockCompletionInput]  # every block, stable UID ordering


class SeBlockCompletion(TypedDict):
    block_uid: int
    name: str
    applicability: Literal["atomistic", "non-atomistic", "unknown"]
    binding_status: Literal["exact", "dangling", "unknown", "not-applicable"]
    resolved_structure_id: int | None
    binding_token: str | None
    geometry: GeometryIdentity | None
    completion: CompletionEnvelope | None
    reason_codes: list[str]


class SeCompletionCounts(TypedDict):
    all_blocks: int
    atomistic_blocks: int
    non_atomistic_blocks: int
    unknown_applicability_blocks: int
    unknown_binding_blocks: int
    atomistic_blocks_finished: int
    states: dict[str, int]  # canonical structure states plus not-applicable


class SeCompletionEnvelope(TypedDict):
    format: Literal[1]
    requested_design_token: str | None
    latest_design: SeDesignIdentity | None
    shown_design: SeDesignIdentity | None
    binding: Binding
    state: Literal[
        "unknown",
        "mixed",
        "not-applicable",
        "preview-only",
        "queued",
        "running",
        "converged",
        "stopped-unconverged",
        "failed",
        "held",
        "unavailable",
    ]
    reason_codes: list[str]
    scope: Literal["per-block-only"]
    complete_inventory: bool
    counts: SeCompletionCounts
    blocks: list[SeBlockCompletion]
    # Requires exact equality of resolved ID, captured geometry and nested
    # shown geometry (including token), plus exact nested completion binding.
    all_atomistic_blocks_finished: bool
    # Independent block evidence does not assess physical inter-block assembly.
    physical_assembly_assessed: Literal[False]
    finished: Literal[False]
    next: NextPhase
    status_token: str


# Geometry/diagnostic supplement for the existing shared viewer proposal.
# These records reuse the identity codec and completion envelope above.
# They do not define web controls, a second classifier or a persistence schema.
class PhysicalAtomKey(TypedDict):
    geometry: GeometryIdentity
    atom_label: str
    atom_row_id: int  # persisted snapshot only in v1; never an invented ordinal
    image: Image3


class PeriodicComparisonMapping(TypedDict):
    source_geometry: GeometryIdentity
    target_geometry: GeometryIdentity
    source_cell_digest: str
    target_cell_digest: str
    endpoint_image_mapping_digest: str
    cell_basis_mapping_digest: str
    compared_endpoint_count: int
    complete_population: bool
    proof: Proof  # must be verified for every compared endpoint and cell basis


class GraphIdentity(TypedDict):
    geometry: GeometryIdentity
    token: str | None
    source: Literal["stored", "receipt-comparison", "inferred-display", "unknown"]
    complete: bool
    comparison_source: InputIdentity | None
    comparison_source_graph_token: str | None
    atom_mapping_digest: str | None
    mapped_atom_count: int | None
    mapping_proof: Proof | None
    periodic_mapping: PeriodicComparisonMapping | None
    algorithm: str | None
    algorithm_revision: str | None
    dependency_digest: str | None
    parameters: dict[str, Scalar]
    reason: str | None


class PhysicalBondKey(TypedDict):
    graph_token: str
    bond_row_id: int | None
    captured_edge_id: str  # shared producer key; not a renderer array index
    i: PhysicalAtomKey
    j: PhysicalAtomKey  # absolute image; j.image - i.image is the edge image


class DisplayBondEvidence(TypedDict):
    key: PhysicalBondKey | None  # unknown/ambiguous identity is not selectable
    graph: GraphIdentity
    order: ExactNumber | None
    kind: str | None
    provenance: str | None
    start_A: Vector3 | None
    end_A: Vector3 | None
    reason: str | None


class SceneInstanceIdentity(TypedDict):
    scene_token: str  # shared exact scene codec, not a competing physical hash
    geometry: GeometryIdentity
    role: Literal["current", "input-request", "adopted-output"]
    run_id: int | None
    layer_id: str  # captured mapping; different roles never share selection
    design: SeDesignIdentity | None  # null for standalone structure
    block_uid: int | None
    binding_token: str | None
    placement_token: str
    host_units_per_metre: ExactNumber  # positive finite display conversion


class DiagnosticReference(TypedDict):
    reference_id: str | None
    method: str | None
    version: str | None
    applicability: Literal["applicable", "inapplicable", "unknown"]
    applicability_reason: str | None
    chemistry_scope: str | None
    reference_value: ExactNumber | None
    units: str | None
    target_receipt_id: str | None
    target_proof: Proof | None


class GeometryDiagnostic(TypedDict):
    diagnostic_id: str
    geometry: GeometryIdentity
    graph: GraphIdentity | None
    family: Literal[
        "declared-bond",
        "angle",
        "poav",
        "clash",
        "short-contact",
        "bond-opening",
        "coordination",
        "shape",
        "surface-deviation",
    ]
    status: Literal["measured", "unavailable", "inapplicable", "invalid"]
    quantity: str
    value: ExactNumber | None
    units: str | None
    atoms: list[PhysicalAtomKey]
    bonds: list[PhysicalBondKey]
    method: str | None
    method_version: str | None
    reference: DiagnosticReference | None
    parameters: dict[str, Scalar]  # cutoff, comparator, exclusions, etc.
    population: str | None
    total_count: int | None
    reported_count: int | None
    complete: bool
    reason: str | None


class MoleculeGeometryEvidence(TypedDict):
    format: Literal[1]
    geometry: GeometrySnapshot | None
    geometry_status: Literal["available", "stale", "unavailable", "invalid"]
    requested_geometry_token: str | None
    instance: SceneInstanceIdentity | None
    graph: GraphIdentity | None
    display_bonds: list[DisplayBondEvidence]
    diagnostics: list[GeometryDiagnostic]
    diagnostics_complete: bool
    completion: CompletionEnvelope | None
    completion_status: Literal["available", "unavailable"]
    reason_codes: list[str]
