"""Whole-structure mobility/stability over the axial subgraph — the
Maxwell/Calladine analysis backing ``view='stability'``
(docs/backlog/se-tension-elements-and-prestress.md rung 4, built together
with rung 1's ``axial`` class so the counting and the unilateral member
land as one piece; docs/backlog/structural-solution-space.md slice 1).

Model, stated once (the view header repeats it): one pin node per block
that terminates at least one ``axial`` connect, at the block's own pose;
every axial member is a pin-ended two-force member along the
block-to-block segment. Non-axial connects and envelope contact are NOT
modelled — this is the axial subgraph's answer, honestly scoped, not a
general mobility analysis of the whole design.

The counting is Calladine's extension of Maxwell's rule: with ``j`` nodes,
``b`` members and ``c`` grounded translations (``objectives.fixed``),
``m − s = 3j − c − b`` where ``m`` = independent inextensional mechanisms
and ``s`` = independent self-stress states — both read off one SVD of the
equilibrium matrix. Whether a first-order mechanism is actually stabilized
by an available self-stress is the second-order (product-force /
geometric-stiffness) test of Pellegrino & Calladine, run on the internal
mechanism subspace with the stress matrix of a sign-feasible self-stress
state.

TRIPWIRE CONTRACT (two-party, recorded in
docs/backlog/se-tension-elements-and-prestress.md rung 2 and
docs/backlog/cad-machine-spec.md §NOT-in-scope): any constraint-vs-DOF
counting that cannot run the ``m − s`` + second-order machinery must
report ``"first-order mobile; may be prestress-stabilized — not checked"``
rather than a bare "mechanism" verdict. :func:`classify` satisfies the
contract by construction and falls back to that exact line whenever the
second-order test cannot be run.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from precis_se import joints as se_joints
from precis_se.ops import ConnectSpec, SeTree

#: The verbatim tripwire line (see module docstring) — kept as one
#: constant so the contract's wording cannot drift between emitters.
TRIPWIRE_LINE = "first-order mobile; may be prestress-stabilized — not checked"

#: Relative singular-value cutoff for rank decisions. Deliberately looser
#: than machine epsilon: node coordinates are agent-entered poses, and a
#: mechanism that only exists below 1e-9 of the largest singular value is
#: numerical noise, not a design property.
_RANK_RTOL = 1e-9


@dataclass
class MemberRow:
    """One axial member as analysed (or skipped, with the reason)."""

    subject: str  # 'a.port—b.port'
    a_block: str
    b_block: str
    length: float | None
    role: str  # tie | strut | rod | undeclared
    params: dict[str, float]
    self_stress: float | None = None  # coefficient in the reported state
    skipped: str | None = None


@dataclass
class StabilityReport:
    j: int
    b: int
    c: int
    rank: int
    m: int  # all inextensional mechanisms, rigid-body included
    rb_dim: int  # rigid-body modes the supports leave free
    m_internal: int  # m − rb_dim
    s: int
    verdict: str
    members: list[MemberRow] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _member_role(params: dict[str, float]) -> str:
    tension = params.get("tension_capacity")
    compression = params.get("compression_capacity")
    if tension is None or compression is None:
        return "undeclared"
    if compression == 0.0:
        return "tie"
    if tension == 0.0:
        return "strut"
    return "rod"


def axial_connects(tree: SeTree) -> list[tuple[ConnectSpec, dict[str, float], str]]:
    """Every stored connect with a valid ``axial`` joint, as
    ``(connect, numeric params, role)`` — no geometry judgment (each
    consumer applies its own: this module skips-and-reports degenerate
    members, :mod:`precis_se.formfind` refuses them). Malformed joints
    are silently absent here — they are already ``malformed_joint`` DRC
    errors."""
    out: list[tuple[ConnectSpec, dict[str, float], str]] = []
    for conn in tree.connects:
        if not conn.joint:
            continue
        try:
            joint = se_joints.validate_joint(conn.joint)
        except se_joints.JointError:
            continue
        if joint["class"] != "axial":
            continue
        params = {
            k: float(v)
            for k, v in (joint.get("params") or {}).items()
            if isinstance(v, (int, float))
        }
        out.append((conn, params, _member_role(params)))
    return out


def _axial_members(tree: SeTree) -> list[MemberRow]:
    """Every stored axial connect as a :class:`MemberRow` — degenerate
    geometry is kept, marked ``skipped``."""
    rows: list[MemberRow] = []
    for conn, params, role in axial_connects(tree):
        subject = f"{conn.a_block}.{conn.a_port}—{conn.b_block}.{conn.b_port}"
        row = MemberRow(
            subject=subject,
            a_block=conn.a_block,
            b_block=conn.b_block,
            length=None,
            role=role,
            params=params,
        )
        if conn.a_block == conn.b_block:
            row.skipped = "both ends on the same block — no line of action"
        elif conn.a_block not in tree.blocks or conn.b_block not in tree.blocks:
            row.skipped = "an endpoint block does not exist"
        else:
            pa = np.asarray(tree.blocks[conn.a_block].pose, dtype=float)
            pb = np.asarray(tree.blocks[conn.b_block].pose, dtype=float)
            length = float(np.linalg.norm(pb - pa))
            if length <= 0.0:
                row.skipped = (
                    "endpoint blocks share one pose — zero length, no "
                    "line of action (set_pose)"
                )
            else:
                row.length = length
        rows.append(row)
    return rows


def _fixed_axes(tree: SeTree, name: str) -> list[str]:
    fixed = tree.blocks[name].objectives.get("fixed")
    return [str(a) for a in fixed] if isinstance(fixed, list) else []


def _rank(matrix: np.ndarray) -> int:
    if matrix.size == 0:
        return 0
    svals = np.linalg.svd(matrix, compute_uv=False)
    if svals.size == 0 or svals[0] == 0.0:
        return 0
    return int(np.sum(svals > _RANK_RTOL * svals[0]))


def classify(tree: SeTree) -> StabilityReport:
    """Run the counting + the second-order test over ``tree``'s axial
    subgraph. Pure over the tree; no store access. See the module
    docstring for the model, the counting and the tripwire contract."""
    members = _axial_members(tree)
    live = [row for row in members if row.skipped is None]
    notes: list[str] = []
    if any(row.skipped for row in members):
        notes.append(
            f"{sum(1 for r in members if r.skipped)} member(s) skipped — "
            "see the member table"
        )

    node_names = sorted({row.a_block for row in live} | {row.b_block for row in live})
    j, b = len(node_names), len(live)
    if b == 0 or j < 2:
        return StabilityReport(
            j=j,
            b=b,
            c=0,
            rank=0,
            m=0,
            rb_dim=0,
            m_internal=0,
            s=0,
            verdict="no axial members — stability analysis does not apply",
            members=members,
            notes=notes,
        )
    if any(tree.blocks[name].array for name in node_names):
        notes.append(
            "an endpoint block is an array — analysed at the array node's "
            "own pose; array expansion is not modelled"
        )

    index = {name: i for i, name in enumerate(node_names)}
    coords = np.array([tree.blocks[name].pose for name in node_names], dtype=float)

    axis_index = {"x": 0, "y": 1, "z": 2}
    fixed_mask = np.zeros(3 * j, dtype=bool)
    for name in node_names:
        for axis in _fixed_axes(tree, name):
            fixed_mask[3 * index[name] + axis_index[axis]] = True
    c = int(fixed_mask.sum())
    free = ~fixed_mask
    n_free = int(free.sum())

    # Equilibrium matrix over free DOFs: column k is member k's unit
    # direction, +u at its a-node and −u at its b-node (tension-positive).
    equilibrium = np.zeros((3 * j, b))
    lengths = np.empty(b)
    for k, row in enumerate(live):
        ia, ib = index[row.a_block], index[row.b_block]
        u = coords[ib] - coords[ia]
        lengths[k] = float(np.linalg.norm(u))
        u = u / lengths[k]
        equilibrium[3 * ia : 3 * ia + 3, k] = u
        equilibrium[3 * ib : 3 * ib + 3, k] = -u
    a_free = equilibrium[free, :]

    u_svd, svals, vt = np.linalg.svd(a_free)
    smax = float(svals[0]) if svals.size else 0.0
    rank = int(np.sum(svals > _RANK_RTOL * smax)) if smax > 0.0 else 0
    s = b - rank
    m = n_free - rank

    # Rigid-body modes the supports leave free: the span of the six
    # generators (3 translations, 3 rotations about the centroid),
    # restricted to motions that hold every fixed DOF at zero.
    rigid_full = np.zeros((3 * j, 6))
    centroid = coords.mean(axis=0)
    for i in range(j):
        rigid_full[3 * i : 3 * i + 3, 0:3] = np.eye(3)
        rigid_full[3 * i : 3 * i + 3, 3:6] = _cross_matrix(coords[i] - centroid)
    if c:
        gen_null = _null_space(rigid_full[fixed_mask, :])
        rigid_free = rigid_full[free, :] @ gen_null
    else:
        rigid_free = rigid_full[free, :]
    rb_dim = _rank(rigid_free)
    m_internal = m - rb_dim
    if c == 0:
        notes.append(
            "no block declares objectives.fixed — the structure is "
            f"free-floating; {rb_dim} rigid-body mode(s) are counted in m "
            "and reported separately as rb"
        )

    self_stress_basis = vt[rank:, :].T if s else np.empty((b, 0))
    verdict = _verdict(
        live,
        lengths,
        index,
        coords,
        free,
        u_svd[:, rank:],
        rigid_free,
        self_stress_basis,
        m_internal=m_internal,
        s=s,
        notes=notes,
    )

    return StabilityReport(
        j=j,
        b=b,
        c=c,
        rank=rank,
        m=m,
        rb_dim=rb_dim,
        m_internal=m_internal,
        s=s,
        verdict=verdict,
        members=members,
        notes=notes,
    )


def _cross_matrix(v: np.ndarray) -> np.ndarray:
    """``_cross_matrix(p) @ w == w × p`` — the rotation generator column
    block for a node at offset ``p`` from the centroid."""
    x, y, z = (float(a) for a in v)
    return np.array([[0.0, z, -y], [-z, 0.0, x], [y, -x, 0.0]])


def _null_space(matrix: np.ndarray) -> np.ndarray:
    if matrix.size == 0:
        return np.eye(matrix.shape[1])
    _u, svals, vt = np.linalg.svd(matrix)
    smax = float(svals[0]) if svals.size else 0.0
    rank = int(np.sum(svals > _RANK_RTOL * smax)) if smax > 0.0 else 0
    return vt[rank:, :].T


def _sign_feasible(live: list[MemberRow], state: np.ndarray) -> tuple[bool, list[str]]:
    """Does ``state`` (tension-positive coefficients) respect every
    member's declared one-sidedness? Returns the offenders when not.
    Members without a capacity pair constrain nothing (honest absence)."""
    offenders: list[str] = []
    tol = 1e-9 * float(np.max(np.abs(state))) if state.size else 0.0
    for k, row in enumerate(live):
        coeff = float(state[k])
        if row.role == "tie" and coeff < -tol:
            offenders.append(f"{row.subject} (tie asked to carry compression)")
        elif row.role == "strut" and coeff > tol:
            offenders.append(f"{row.subject} (strut asked to carry tension)")
    return (not offenders, offenders)


def _verdict(
    live: list[MemberRow],
    lengths: np.ndarray,
    index: dict[str, int],
    coords: np.ndarray,
    free: np.ndarray,
    mech_basis: np.ndarray,
    rigid_free: np.ndarray,
    self_stress_basis: np.ndarray,
    *,
    m_internal: int,
    s: int,
    notes: list[str],
) -> str:
    if m_internal <= 0:
        if s > 0:
            return f"rigid (statically indeterminate — {s} self-stress state(s))"
        return "rigid (statically determinate)"
    if s == 0:
        return (
            f"first-order mobile ({m_internal} mechanism(s)) — NOT "
            "stabilized: no self-stress state exists"
        )

    # Internal mechanism subspace: the inextensional modes minus their
    # rigid-body component.
    q_rb = _orth(rigid_free)
    internal = mech_basis - q_rb @ (q_rb.T @ mech_basis) if q_rb.size else mech_basis
    internal = _orth(internal)
    if internal.shape[1] != m_internal:
        # counting and projection disagree about the internal-mechanism
        # subspace — numerically degenerate geometry; running the
        # second-order test over a wrong-sized subspace would be a
        # confident wrong answer, so the tripwire contract owns this exit
        # (reviewer finding 2026-09-09: any mismatch, not just empty).
        return TRIPWIRE_LINE

    undeclared = sum(1 for row in live if row.role == "undeclared")
    candidates: list[np.ndarray] = []
    for col in range(self_stress_basis.shape[1]):
        state = self_stress_basis[:, col]
        for signed in (state, -state):
            feasible, _ = _sign_feasible(live, signed)
            if feasible:
                candidates.append(signed)
    if not candidates:
        _, offenders = _sign_feasible(live, self_stress_basis[:, 0])
        return (
            f"first-order mobile ({m_internal} mechanism(s)) — the "
            "self-stress state is infeasible for the declared members: "
            + "; ".join(offenders)
        )
    if s > 1:
        notes.append(
            f"s = {s}: feasibility and stabilization checked per basis "
            "vector only, not over combinations"
        )

    for state in candidates:
        stress = _stress_matrix(live, lengths, index, coords.shape[0], state)[free, :][
            :, free
        ]
        quad = internal.T @ stress @ internal
        eigvals = np.linalg.eigvalsh(quad)
        scale = float(np.max(np.abs(quad))) or 1.0
        if np.all(eigvals > 1e-9 * scale):
            _record_state(live, state)
            suffix = (
                f" — feasibility partially unchecked: {undeclared} member(s) "
                "declare no capacity pair"
                if undeclared
                else ""
            )
            return (
                f"prestress-stabilized ({m_internal} mechanism(s) stiffened "
                f"by the reported self-stress state){suffix}"
            )
    _record_state(live, candidates[0])
    return (
        f"first-order mobile ({m_internal} mechanism(s)) — NOT stabilized "
        "by the available self-stress state(s)"
    )


def _record_state(live: list[MemberRow], state: np.ndarray) -> None:
    """Stamp the reported self-stress coefficients onto the member rows,
    normalized so the largest magnitude is 1 (the state is a ray — only
    ratios and signs mean anything)."""
    peak = float(np.max(np.abs(state))) or 1.0
    for k, row in enumerate(live):
        row.self_stress = float(state[k]) / peak


def _orth(matrix: np.ndarray) -> np.ndarray:
    if matrix.size == 0:
        return matrix.reshape(matrix.shape[0], 0)
    u_svd, svals, _vt = np.linalg.svd(matrix, full_matrices=False)
    smax = float(svals[0]) if svals.size else 0.0
    rank = int(np.sum(svals > _RANK_RTOL * smax)) if smax > 0.0 else 0
    return u_svd[:, :rank]


def _stress_matrix(
    live: list[MemberRow],
    lengths: np.ndarray,
    index: dict[str, int],
    j: int,
    state: np.ndarray,
) -> np.ndarray:
    """The geometric-stiffness (stress) matrix of ``state``: the
    force-density Laplacian ``Ω`` (``q = t/L`` per member) Kronecker the
    3×3 identity — the second-order test's quadratic form."""
    omega = np.zeros((j, j))
    for k, row in enumerate(live):
        q = float(state[k]) / float(lengths[k])
        ia, ib = index[row.a_block], index[row.b_block]
        omega[ia, ia] += q
        omega[ib, ib] += q
        omega[ia, ib] -= q
        omega[ib, ia] -= q
    return np.kron(omega, np.eye(3))


# ── capacity findings (rented by precis_se.drc) ──────────────────────────


def capacity_findings(tree: SeTree) -> list[tuple[str, str]]:
    """Warn-tier ``(subject, detail)`` pairs about axial members' declared
    capacities vs their declared loads. Directional honesty: ``preload``
    is signed (tension-positive) so its check is directional; a connect's
    ``force`` objective is a world-frame vector whose sense along the
    member is not knowable here, so only its axial *component magnitude*
    is compared, against the member's best-case capacity."""
    findings: list[tuple[str, str]] = []
    for row in _axial_members(tree):
        if row.skipped is not None:
            findings.append(
                (row.subject, f"axial member not analysable: {row.skipped}")
            )
            continue
        tension = row.params.get("tension_capacity")
        compression = row.params.get("compression_capacity")
        if tension is None or compression is None:
            missing = [
                name
                for name, val in (
                    ("tension_capacity", tension),
                    ("compression_capacity", compression),
                )
                if val is None
            ]
            findings.append(
                (
                    row.subject,
                    f"axial member is missing {' and '.join(missing)} — "
                    "tie/strut behaviour and the self-stress sign check "
                    "are unchecked (set_joint params)",
                )
            )
            continue
        preload = row.params.get("preload")
        if preload is not None:
            if preload > tension:
                findings.append(
                    (
                        row.subject,
                        f"preload {preload:g} N exceeds the member's "
                        f"tension capacity {tension:g} N",
                    )
                )
            if -preload > compression:
                findings.append(
                    (
                        row.subject,
                        f"asked to carry {-preload:g} N in compression "
                        f"against a {compression:g} N buckling/crush "
                        "ceiling",
                    )
                )
        force = _axial_force_component(tree, row)
        if force is not None:
            best = max(tension, compression)
            if force > best:
                findings.append(
                    (
                        row.subject,
                        f"declared load has a {force:g} N component along "
                        f"the member against a best-case capacity of "
                        f"{best:g} N (direction not resolvable at graph "
                        "tier — checked against the larger capacity)",
                    )
                )
    return findings


def _axial_force_component(tree: SeTree, row: MemberRow) -> float | None:
    conn = _find_connect(tree, row)
    if conn is None:
        return None
    force = conn.objectives.get("force")
    if not isinstance(force, list) or len(force) != 3:
        return None
    pa = np.asarray(tree.blocks[row.a_block].pose, dtype=float)
    pb = np.asarray(tree.blocks[row.b_block].pose, dtype=float)
    u = pb - pa
    norm = float(np.linalg.norm(u))
    if norm <= 0.0:
        return None
    return abs(float(np.dot(np.asarray(force, dtype=float), u / norm)))


def _find_connect(tree: SeTree, row: MemberRow) -> ConnectSpec | None:
    for conn in tree.connects:
        subject = f"{conn.a_block}.{conn.a_port}—{conn.b_block}.{conn.b_port}"
        if subject == row.subject:
            return conn
    return None
