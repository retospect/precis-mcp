"""Whole-structure mobility/stability over the axial subgraph — the
Maxwell/Calladine analysis backing ``view='stability'``
(docs/backlog/structural-solution-space.md rung 4, built together with
rung 1's ``axial`` class so the counting and the unilateral member land
as one piece; slice 1 of the same doc's build order).

Model, stated once (the view header repeats it): one node per block
that terminates at least one ``axial`` connect, at the block's own pose;
every axial member is a pin-ended two-force member along the segment
between its two PORT world positions (block pose ∘ port pose; a port with
no pose sits at the block origin). A block whose members meet it at
different points is a rigid body with 6 DOF (3 translations + 3
rotations) so a member force ``u`` at port point ``p`` loads it with the
wrench ``(u, (p − pose) × u)`` — without that, a hub's spokes all act
through one point and carry no moment about the axle (gr334787). A block
all of whose members attach at its origin stays a 3-DOF pin node.
``objectives.fixed`` grounds translations only; rotations stay free.
Non-axial connects and envelope contact are NOT
modelled — this is the axial subgraph's answer, honestly scoped, not a
general mobility analysis of the whole design.

The counting is Calladine's extension of Maxwell's rule: with ``j`` nodes,
``b`` members and ``c`` grounded translations (``objectives.fixed``),
``m − s = n − c − b`` (``n`` = 3 per pin node + 6 per rigid body) where ``m`` = independent inextensional mechanisms
and ``s`` = independent self-stress states — both read off one SVD of the
equilibrium matrix. Whether a first-order mechanism is actually stabilized
by an available self-stress is the second-order (product-force /
geometric-stiffness) test of Pellegrino & Calladine, run on the internal
mechanism subspace with the stress matrix of a sign-feasible self-stress
state.

The same assembled system backs the rung-5 prestress check
(:func:`prestress_report`, structural-solution-space slice 3): declared
member ``preload``s must be a self-stress state of the geometry — forces
that balance at every free node with no external load — within a
tolerance; undeclared members are completed by least squares and the
implied forces vetted against role sign and capacity pair.

TRIPWIRE CONTRACT (two-party, recorded in
docs/backlog/structural-solution-space.md §Tripwire contract and
docs/backlog/cad-machine-spec.md §NOT-in-scope): any constraint-vs-DOF
counting that cannot run the ``m − s`` + second-order machinery must
report ``"first-order mobile; may be prestress-stabilized — not checked"``
rather than a bare "mechanism" verdict. :func:`classify` satisfies the
contract by construction and falls back to that exact line whenever the
second-order test cannot be run.

The warn-tier ``prestress_state`` DRC rule (:mod:`precis_se.drc`) is
:func:`prestress_report`'s DRC face. The generator for this checker is
:mod:`precis_se.formfind`; the DOF probe in the DRC reports ``axial`` as an
honest skip, and capacity findings fold into ``view='drc'``. The checker's
formfind still places its nodes at block poses (ports ignored; Reto
2026-10-11) and says so in its own honesty note.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from precis.cad.vec import as_vec3 as cad_as_vec3
from precis.cad.vec import pose as cad_pose
from precis_se import joints as se_joints
from precis_se.ops import ConnectSpec, SeTree, effective_ports

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
    #: World-frame attachment points (block pose ∘ port pose) of the a and
    #: b ends; ``None`` while the member is skipped before they exist.
    a_point: np.ndarray | None = None
    b_point: np.ndarray | None = None


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
    #: ``'a.port—b.port'`` of every valid ``rigid`` connect — NOT in the
    #: equilibrium matrix (gr334788), so the counts below are silent about
    #: whatever they hold together.
    unmodelled_connects: list[str] = field(default_factory=list)

    @property
    def partial(self) -> bool:
        """True when the design has rigid connects this analysis ignored."""
        return bool(self.unmodelled_connects)


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


def port_world(tree: SeTree, block: str, port: str) -> np.ndarray:
    """World position of ``block.port``: block pose ∘ port pose. A port
    with no pose (the normal box-level case) or one that does not resolve
    sits at the block origin — the pre-gr334787 behaviour, kept as the
    honest fallback rather than inventing an offset."""
    node = tree.blocks[block]
    origin = np.asarray(node.pose, dtype=float)
    spec = effective_ports(tree, node).get(port)
    if spec is None or spec.pose is None:
        return origin
    xf = cad_pose(cad_as_vec3(node.pose), cad_as_vec3(node.rot))
    return np.asarray(xf.apply(cad_as_vec3(spec.pose)), dtype=float)


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
            pa = port_world(tree, conn.a_block, conn.a_port)
            pb = port_world(tree, conn.b_block, conn.b_port)
            length = float(np.linalg.norm(pb - pa))
            if length <= 0.0:
                row.skipped = (
                    "endpoint attachment points coincide — zero length, no "
                    "line of action (set_pose / set_port_pose)"
                )
            else:
                row.length = length
                row.a_point, row.b_point = pa, pb
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


#: An attachment arm shorter than this (metres) is "at the block origin":
#: the block stays a 3-DOF pin node.
_ARM_EPS = 1e-12


@dataclass
class _System:
    """The assembled axial system every whole-structure analysis shares —
    one assembly, so :func:`classify`'s equilibrium matrix and
    :func:`prestress_report`'s can never disagree about the structure.
    Empty (``live == []``) when there is nothing analysable.

    DOF layout: node ``i`` owns ``dof[i]`` consecutive rows from
    ``start[i]`` — 3 translations, then (rigid bodies only) 3 rotations.
    Reusable by any rigid-body analysis (gr334788's rigid-connect groups)."""

    members: list[MemberRow]  # every stored axial connect, skipped included
    live: list[MemberRow]
    node_names: list[str]
    index: dict[str, int]
    coords: np.ndarray  # (j, 3) block poses
    dof: np.ndarray  # (j,) 3 or 6
    start: np.ndarray  # (j,) first row of each node
    fixed_mask: np.ndarray  # (n,) bool — objectives.fixed translations
    equilibrium: np.ndarray  # (n, b) — tension-positive unit columns
    lengths: np.ndarray  # (b,)
    arms: list[tuple[np.ndarray, np.ndarray]]  # per live member: (r_a, r_b)
    notes: list[str]

    @property
    def n(self) -> int:
        return int(self.dof.sum())


def _skew(v: np.ndarray) -> np.ndarray:
    """``_skew(r) @ w == r × w``."""
    x, y, z = (float(a) for a in v)
    return np.array([[0.0, -z, y], [z, 0.0, -x], [-y, x, 0.0]])


def _assemble(tree: SeTree) -> _System:
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
    if any(tree.blocks[name].array for name in node_names):
        notes.append(
            "an endpoint block is an array — analysed at the array node's "
            "own pose; array expansion is not modelled"
        )

    index = {name: i for i, name in enumerate(node_names)}
    coords = np.array(
        [tree.blocks[name].pose for name in node_names], dtype=float
    ).reshape(j, 3)

    arms: list[tuple[np.ndarray, np.ndarray]] = []
    rigid_body = np.zeros(j, dtype=bool)
    for row in live:
        assert row.a_point is not None and row.b_point is not None
        ra = row.a_point - coords[index[row.a_block]]
        rb = row.b_point - coords[index[row.b_block]]
        arms.append((ra, rb))
        if float(np.linalg.norm(ra)) > _ARM_EPS:
            rigid_body[index[row.a_block]] = True
        if float(np.linalg.norm(rb)) > _ARM_EPS:
            rigid_body[index[row.b_block]] = True
    dof = np.where(rigid_body, 6, 3).astype(int)
    start = np.concatenate([[0], np.cumsum(dof)[:-1]]).astype(int)
    n = int(dof.sum())

    axis_index = {"x": 0, "y": 1, "z": 2}
    fixed_mask = np.zeros(n, dtype=bool)
    for name in node_names:
        for axis in _fixed_axes(tree, name):
            fixed_mask[start[index[name]] + axis_index[axis]] = True

    # Equilibrium matrix: column k is member k's unit direction, +u at
    # its a-end and −u at its b-end (tension-positive), with the moment
    # arm r × (±u) in the rotation rows of a rigid-body node.
    equilibrium = np.zeros((n, b))
    lengths = np.empty(b)
    for k, row in enumerate(live):
        ia, ib = index[row.a_block], index[row.b_block]
        assert row.a_point is not None and row.b_point is not None
        u = row.b_point - row.a_point
        lengths[k] = float(np.linalg.norm(u))
        u = u / lengths[k]
        ra, rb = arms[k]
        equilibrium[start[ia] : start[ia] + 3, k] = u
        equilibrium[start[ib] : start[ib] + 3, k] = -u
        if dof[ia] == 6:
            equilibrium[start[ia] + 3 : start[ia] + 6, k] = np.cross(ra, u)
        if dof[ib] == 6:
            equilibrium[start[ib] + 3 : start[ib] + 6, k] = np.cross(rb, -u)
    return _System(
        members=members,
        live=live,
        node_names=node_names,
        index=index,
        coords=coords,
        dof=dof,
        start=start,
        fixed_mask=fixed_mask,
        equilibrium=equilibrium,
        lengths=lengths,
        arms=arms,
        notes=notes,
    )


def _rigid_connects(tree: SeTree) -> list[str]:
    """``'a.port—b.port'`` of every connect with a valid ``rigid`` joint
    — the connects the axial-only equilibrium matrix cannot represent."""
    out: list[str] = []
    for conn in tree.connects:
        if not conn.joint:
            continue
        try:
            joint = se_joints.validate_joint(conn.joint)
        except se_joints.JointError:
            continue
        if joint["class"] == "rigid":
            out.append(f"{conn.a_block}.{conn.a_port}—{conn.b_block}.{conn.b_port}")
    return out


def _scope_honesty_notes(tree: SeTree, node_names: list[str]) -> list[str]:
    """Two scope-honesty checks over ``objectives.fixed``/``force``
    (gripe 334783), so the stability header never reads as "fine" when it
    silently skipped something: (a) a block carrying a declared load or
    support that is not a node of the analysed axial subgraph — its
    ``fixed`` grounds nothing here (``c`` only counts subgraph nodes) and
    its ``force`` is never modelled by this analysis either way; (b) a
    block that is both fully grounded (``fixed`` on all three axes) and
    carries a nonzero external ``force`` — the force does nothing to a
    node this model holds motionless."""
    in_scope = set(node_names)
    out_of_scope: list[str] = []
    fixed_and_forced: list[str] = []
    for name, block in sorted(tree.blocks.items()):
        obj = block.objectives
        if not obj:
            continue
        fixed = obj.get("fixed")
        fixed_axes = set(fixed) if isinstance(fixed, list) else set()
        force = obj.get("force")
        forced = isinstance(force, list) and any(float(x) != 0.0 for x in force)
        if (fixed_axes or forced) and name not in in_scope:
            out_of_scope.append(name)
        if fixed_axes == {"x", "y", "z"} and forced:
            fixed_and_forced.append(name)
    notes: list[str] = []
    if out_of_scope:
        notes.append(
            f"loads on {len(out_of_scope)} block(s) outside the analysed "
            f"axial subgraph — not checked: {', '.join(out_of_scope)}"
        )
    for name in fixed_and_forced:
        notes.append(
            f"{name}: external force on a fully grounded node has no "
            "effect in this model"
        )
    return notes


def classify(tree: SeTree) -> StabilityReport:
    """Run the counting + the second-order test over ``tree``'s axial
    subgraph. Pure over the tree; no store access. See the module
    docstring for the model, the counting and the tripwire contract."""
    system = _assemble(tree)
    members, live, notes = system.members, system.live, list(system.notes)
    node_names = system.node_names
    notes.extend(_scope_honesty_notes(tree, node_names))
    rigid = _rigid_connects(tree)
    if rigid:
        notes.insert(
            0,
            f"PARTIAL: {len(rigid)} rigid connect(s) are not in the "
            "equilibrium matrix (no moment DOFs; gr334788) — the counts and "
            "verdict cover the axial subgraph only and say nothing about "
            f"what these hold together: {', '.join(rigid)}",
        )
    j, b = len(node_names), len(live)
    if b == 0:
        return StabilityReport(
            j=j,
            b=b,
            c=0,
            rank=0,
            m=0,
            rb_dim=0,
            m_internal=0,
            s=0,
            verdict="no axial members — stability analysis does not apply"
            + (" (rigid connects are not modelled)" if rigid else ""),
            members=members,
            notes=notes,
            unmodelled_connects=rigid,
        )

    coords = system.coords
    fixed_mask = system.fixed_mask
    c = int(fixed_mask.sum())
    free = ~fixed_mask
    n_free = int(free.sum())
    a_free = system.equilibrium[free, :]

    u_svd, svals, vt = np.linalg.svd(a_free)
    smax = float(svals[0]) if svals.size else 0.0
    rank = int(np.sum(svals > _RANK_RTOL * smax)) if smax > 0.0 else 0
    s = b - rank
    m = n_free - rank

    # Rigid-body modes the supports leave free: the span of the six
    # generators (3 translations, 3 rotations about the centroid),
    # restricted to motions that hold every fixed DOF at zero. A
    # rigid-body node also turns by the generator's rotation.
    rigid_full = np.zeros((system.n, 6))
    centroid = coords.mean(axis=0)
    for i in range(j):
        r0 = int(system.start[i])
        rigid_full[r0 : r0 + 3, 0:3] = np.eye(3)
        rigid_full[r0 : r0 + 3, 3:6] = _cross_matrix(coords[i] - centroid)
        if system.dof[i] == 6:
            rigid_full[r0 + 3 : r0 + 6, 3:6] = np.eye(3)
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
        system,
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
        unmodelled_connects=rigid,
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
    system: _System,
    free: np.ndarray,
    mech_basis: np.ndarray,
    rigid_free: np.ndarray,
    self_stress_basis: np.ndarray,
    *,
    m_internal: int,
    s: int,
    notes: list[str],
) -> str:
    live = system.live
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
        stress = _stress_matrix(system, state)[free, :][:, free]
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


def _stress_matrix(system: _System, state: np.ndarray) -> np.ndarray:
    """The geometric-stiffness (stress) matrix of ``state``: the quadratic
    form of the second variation of ``Σ t·L``. Per member, ``q |Δδ|²``
    (``q = t/L``, ``Δδ`` the relative first-order displacement of the two
    attachment points — for pin nodes exactly the force-density
    Laplacian Kronecker ``I₃``) plus, for a rotating end with arm ``r``,
    the second-order arc term ``±t·((e rᵀ + r eᵀ)/2 − (e·r) I)`` on its
    rotation block."""
    n = system.n
    out = np.zeros((n, n))
    for k, row in enumerate(system.live):
        t = float(state[k])
        q = t / float(system.lengths[k])
        ia, ib = system.index[row.a_block], system.index[row.b_block]
        ra, rb = system.arms[k]
        assert row.a_point is not None and row.b_point is not None
        e = (row.b_point - row.a_point) / float(system.lengths[k])
        d = np.zeros((3, n))
        for node, arm, sign in ((ia, ra, -1.0), (ib, rb, 1.0)):
            r0 = int(system.start[node])
            d[:, r0 : r0 + 3] += sign * np.eye(3)
            if system.dof[node] == 6:
                d[:, r0 + 3 : r0 + 6] += sign * -_skew(arm)
                m = 0.5 * (np.outer(e, arm) + np.outer(arm, e)) - float(
                    e @ arm
                ) * np.eye(3)
                out[r0 + 3 : r0 + 6, r0 + 3 : r0 + 6] += sign * t * m
        out += q * (d.T @ d)
    return out


# ── prestress: declared preloads vs the self-stress space ────────────────
# (docs/backlog/structural-solution-space.md rung 5's null-space DRC,
# shipped as the same doc's slice 3)

#: Acceptance for the null-space residual: max nodal out-of-balance ≤
#: rtol × max|declared preload|. Declared preloads are hand-entered
#: engineering numbers rounded to a couple of significant digits, so a
#: state within 1 % of balance is rounding, not a wrong state; a wrong
#: ratio is typically tens of percent out. The residual is always
#: reported in the view — the tolerance decides the finding, never what
#: is shown.
_PRESTRESS_RTOL = 1e-2


@dataclass
class PrestressRow:
    """One axial member's place in the declared prestress state."""

    subject: str
    role: str
    declared: float | None  # joint param 'preload', N, tension-positive
    implied: float | None = None  # completed self-stress force, N
    skipped: str | None = None
    #: Short per-row marker for a role-sign or capacity violation on this
    #: row's implied force (gripe 334780) — set alongside the matching
    #: ``findings`` entry so a renderer that only surfaces per-row notes
    #: (not the findings list) still shows the violation.
    flag: str | None = None


@dataclass
class PrestressReport:
    declared_count: int
    residual: float | None  # N — max nodal out-of-balance over free DOFs
    tolerance: float | None  # N — the acceptance threshold used
    compatible: bool | None  # None = no analysable system to check against
    worst_node: str | None
    unique: bool  # completion pinned uniquely by the declared preloads
    rows: list[PrestressRow] = field(default_factory=list)
    findings: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: True when every declared preload on a live (analysable) member is
    #: exactly 0 N — the null-space tolerance scales with the declared
    #: magnitude (``_PRESTRESS_RTOL × scale``), so an all-zero declaration
    #: would otherwise pass at tolerance 0 N against residual 0 N: true,
    #: but vacuously — nothing was actually checked (gripe 334781). The
    #: renderer reports "nothing to verify" instead of a pass when this is
    #: set; ``compatible`` is ``None`` alongside it (no verdict to give).
    nothing_to_verify: bool = False


def prestress_report(tree: SeTree) -> PrestressReport | None:
    """Check the declared member ``preload``s against the self-stress
    space: a prestress state is member forces that balance at every free
    node with no external load (``A_free · t ≈ 0``), so declared preloads
    that leave a net force at a free node cannot exist in the geometry as
    built. Undeclared members are unknowns, completed by least squares
    (minimum-magnitude when not pinned uniquely); the implied forces are
    then vetted against each member's role sign and capacity pair whether
    or not the declared set turned out compatible — an incompatible set
    still produces a least-squares completion, and a sign/capacity
    violation in it is worth a finding (gripe 334780), phrased
    conditionally to say so. Returns ``None`` when no member declares a
    preload — absent prestress is not a finding. Pure over the tree."""
    system = _assemble(tree)
    rows: list[PrestressRow] = []
    findings: list[tuple[str, str]] = []
    notes: list[str] = []
    for row in system.members:
        preload = row.params.get("preload")
        if row.skipped is not None and preload is not None:
            findings.append(
                (
                    row.subject,
                    f"preload {preload:g} N on a member that cannot be "
                    f"analysed ({row.skipped}) — the prestress check does "
                    "not cover it",
                )
            )
        rows.append(
            PrestressRow(
                subject=row.subject,
                role=row.role,
                declared=preload,
                skipped=row.skipped,
            )
        )
    declared_count = sum(1 for pr in rows if pr.declared is not None)
    if declared_count == 0:
        return None

    live = system.live
    if not live:
        notes.append("no analysable axial system — the null-space check cannot run")
        return PrestressReport(
            declared_count=declared_count,
            residual=None,
            tolerance=None,
            compatible=None,
            worst_node=None,
            unique=True,
            rows=rows,
            findings=findings,
            notes=notes,
        )

    live_rows = [pr for pr in rows if pr.skipped is None]
    b = len(live)
    free = ~system.fixed_mask
    a_free = system.equilibrium[free, :]

    declared_idx = [
        k for k, row in enumerate(live) if row.params.get("preload") is not None
    ]
    unknown_idx = [k for k in range(b) if k not in set(declared_idx)]
    t = np.zeros(b)
    for k in declared_idx:
        t[k] = float(live[k].params["preload"])
    # A zero-norm declared vector (every live member's preload is
    # literally 0 N, or no LIVE member declares one at all — only a
    # skipped one did, already flagged above) makes the tolerance scale
    # to 0 N too: residual 0 ≤ tolerance 0 always "passes", telling the
    # agent nothing. Report it as unchecked rather than a self-stress
    # state (gripe 334781) — no magic epsilon, a literal zero vector is
    # the case this guards.
    if float(np.linalg.norm(t[declared_idx])) == 0.0:
        notes.append(
            "every declared preload is 0 N — there is nothing for the "
            "null-space check to verify"
        )
        return PrestressReport(
            declared_count=declared_count,
            residual=None,
            tolerance=None,
            compatible=None,
            worst_node=None,
            unique=True,
            rows=rows,
            findings=findings,
            notes=notes,
            nothing_to_verify=True,
        )
    unique = True
    if unknown_idx:
        a_unknown = a_free[:, unknown_idx]
        if a_unknown.shape[0] == 0:
            t[unknown_idx] = 0.0
        else:
            rhs = -a_free[:, declared_idx] @ t[declared_idx]
            solution, *_ = np.linalg.lstsq(a_unknown, rhs, rcond=None)
            t[unknown_idx] = solution
        unique = _rank(a_unknown) == len(unknown_idx)
        if not unique:
            notes.append(
                "implied forces are the minimum-magnitude completion — "
                "the declared preloads do not pin them uniquely"
            )
    for k, pr in enumerate(live_rows):
        pr.implied = float(t[k])

    scale = max((abs(float(t[k])) for k in declared_idx), default=0.0)
    tolerance = _PRESTRESS_RTOL * scale
    residual_field = system.equilibrium @ t
    residual_field[system.fixed_mask] = 0.0  # reactions carry fixed DOFs
    # Per node: force out-of-balance (N); a rigid body's moment
    # out-of-balance (N·m) joins as a force at the mean member length.
    lref = float(np.mean(system.lengths))
    per_node = np.empty(len(system.node_names))
    for i in range(len(per_node)):
        r0 = int(system.start[i])
        f = float(np.linalg.norm(residual_field[r0 : r0 + 3]))
        mom = (
            float(np.linalg.norm(residual_field[r0 + 3 : r0 + 6])) / lref
            if system.dof[i] == 6
            else 0.0
        )
        per_node[i] = float(np.hypot(f, mom))
    worst = int(np.argmax(per_node))
    residual = float(per_node[worst])
    worst_node = system.node_names[worst]
    compatible = bool(residual <= tolerance)

    if not compatible:
        cause = (
            " — the geometry admits no self-stress at all (s = 0): every "
            "member force must be zero without external load"
            if b - _rank(a_free) == 0
            else ""
        )
        findings.append(
            (
                worst_node,
                "declared preload(s) are not a self-stress state: "
                f"out-of-balance force {residual:g} N at node "
                f"'{worst_node}' exceeds the {tolerance:g} N tolerance"
                f"{cause}; the members cannot hold these preloads without "
                "an external load — fix the preload ratios or the "
                "geometry (view='stability' reports the feasible state)",
            )
        )

    # Vet the implied forces on the undeclared members against role sign
    # and capacity, regardless of `compatible` (gripe 334780): an
    # incompatible declared set still produces a least-squares completion,
    # and a sign/capacity violation in that completion — e.g. tension in a
    # tension_capacity: 0.0 strut — is worth a finding even though the
    # underlying state "cannot be realised" for the separate reason of
    # being out of equilibrium. Declared preloads vs their own member's
    # capacities are already capacity_findings' job.
    def _sign_phrase(detail: str) -> str:
        if compatible:
            return (
                f"the declared preloads imply {detail} — the state cannot be realised"
            )
        return (
            "on the least-squares completion of an incompatible preload "
            f"set, {detail} is implied — the state cannot be realised"
        )

    def _capacity_phrase(detail: str) -> str:
        if compatible:
            return f"the declared preloads imply {detail}"
        return (
            "on the least-squares completion of an incompatible preload "
            f"set, {detail} is implied"
        )

    for k in unknown_idx:
        row = live[k]
        pr = live_rows[k]
        force = float(t[k])
        sign_violated = False
        if row.role == "tie" and force < -tolerance:
            sign_violated = True
            findings.append(
                (
                    row.subject,
                    _sign_phrase(
                        f"{-force:g} N compression in this tension-only member (tie)"
                    ),
                )
            )
            pr.flag = "sign violated (tie in compression)"
        elif row.role == "strut" and force > tolerance:
            sign_violated = True
            findings.append(
                (
                    row.subject,
                    _sign_phrase(
                        f"{force:g} N tension in this compression-only member (strut)"
                    ),
                )
            )
            pr.flag = "sign violated (strut in tension)"
        if sign_violated:
            continue
        tension = row.params.get("tension_capacity")
        compression = row.params.get("compression_capacity")
        if tension is not None and force > tension:
            findings.append(
                (
                    row.subject,
                    _capacity_phrase(
                        f"{force:g} N tension against this member's "
                        f"{tension:g} N tension capacity"
                    ),
                )
            )
            pr.flag = pr.flag or "capacity exceeded (tension)"
        if compression is not None and -force > compression:
            findings.append(
                (
                    row.subject,
                    _capacity_phrase(
                        f"{-force:g} N compression against this member's "
                        f"{compression:g} N buckling/crush ceiling"
                    ),
                )
            )
            pr.flag = pr.flag or "capacity exceeded (compression)"

    return PrestressReport(
        declared_count=declared_count,
        residual=residual,
        tolerance=tolerance,
        compatible=compatible,
        worst_node=worst_node,
        unique=unique,
        rows=rows,
        findings=findings,
        notes=notes,
    )


# ── preload tensioning consistency (rented by precis_se.drc) ─────────────
# (gripe 334782: free_length/rate/preload must cross-check against the
# installed geometry)

#: Relative tolerance for the free_length/rate/preload cross-check — same
#: rationale as _PRESTRESS_RTOL: declared numbers are hand-entered
#: engineering values rounded to a couple of significant digits, so 1 % is
#: rounding noise and a real ratio mismatch is typically tens of percent
#: out.
_PRELOAD_TRIPLE_RTOL = 1e-2


def preload_findings(tree: SeTree) -> list[tuple[str, str]]:
    """Warn-tier ``(subject, detail)`` pairs about an axial member's
    tensioning declaration (gripe 334782). Physically,
    ``preload = rate × (L_installed − L_free)``: a nonzero ``preload``
    with no ``free_length`` and/or no ``rate`` has no tensioning
    instruction behind it — nothing says how the member gets from its
    free length to the installed one. When all three are declared, they
    are cross-checked against the installed length — the endpoint
    port-to-port distance stability already computes (block pose ∘ port
    pose; a pose-less port sits at the block origin)."""
    findings: list[tuple[str, str]] = []
    for row in _axial_members(tree):
        if row.skipped is not None:
            continue
        preload = row.params.get("preload")
        if preload is None or preload == 0.0:
            continue
        free_length = row.params.get("free_length")
        rate = row.params.get("rate")
        if free_length is None or rate is None:
            missing = [
                name
                for name, val in (("free_length", free_length), ("rate", rate))
                if val is None
            ]
            findings.append(
                (
                    row.subject,
                    f"preload {preload:g} N declared but no "
                    f"{' and '.join(missing)} — preload = rate × "
                    "(installed length − free length), so nothing says how "
                    "this member gets tensioned to that number (set_joint "
                    "params)",
                )
            )
            continue
        installed = row.length
        if installed is None:
            continue
        implied = rate * (installed - free_length)
        tol = _PRELOAD_TRIPLE_RTOL * abs(preload)
        if abs(implied - preload) > tol:
            findings.append(
                (
                    row.subject,
                    f"declared preload {preload:g} N disagrees with rate × "
                    f"(installed − free length) = {rate:g} × "
                    f"({installed:g} − {free_length:g}) = {implied:g} N "
                    "(attachment points = block pose ∘ port pose) — "
                    "reconcile the free_length/rate/preload triple",
                )
            )
    return findings


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
    if row.a_point is None or row.b_point is None:
        return None
    u = row.b_point - row.a_point
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
