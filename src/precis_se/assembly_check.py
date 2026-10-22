"""Macro relax v1 — ``method: 'assembly'`` as check-and-report
(docs/backlog/structure-workspaces.md §Relax, slice 5; ruling W4: no mate
solver, settle or FEA yet). One call, :func:`assembly_check`, runs the
existing macro checkers over a loaded :class:`~precis_se.ops.SeTree` and
returns ONE findings list plus the two analyses this slice adds, in the
spec's result shape ``{geometry, findings[], method, seed, constraints}``:

- **overconstrained** — :func:`redundancy`, new: a rigid-body mate
  Jacobian over the connect graph, its rank, and the redundant set (below);
  the word-level checks stay :mod:`precis_se.drc`'s (folded in).
- **underconstrained** — :mod:`precis_se.freedom`'s inventory (moving
  joints, unconnected bodies) and the Jacobian's mobility count, as one
  ``assembly.underconstrained`` info row; the sign-aware axial
  completeness (``check_completeness``) is already a drc finding.
- **interference** — :func:`precis_se.validate.envelope_overlaps` over
  every block pair (the scan behind validate's
  ``undeclared_interpenetration``), depth reported for every
  interpenetrating pair: ``warn`` when no connect sanctions the contact,
  ``info`` when a connect does (a shaft in its bore overlaps by
  construction; the depth is still worth reading).
- **tolerance stack-up** — :func:`precis_se.measures.stackup` worst-case
  (drc's ``tolerance_mismatch``, replaced here by ``stackup.worst_case``
  carrying the chain) and, new, RSS (``stackup.rss``,
  :func:`precis_se.measures.rss_problem`). A chain that fails both is
  over-tight; one that fails RSS only is a chain whose worst case happens
  to be inside the band while its statistical spread is not — both rows
  name the chain source-ward.
- **mold pull** — :func:`precis_se.mold.findings` (``mold.undercut``).
- **geometry solve, where axial** — :func:`precis_se.formfind.solve` run
  read-only with ``move='all'``: every axial node's distance from
  force-density equilibrium is a ``formfind.off_equilibrium`` warn, the
  solved coordinates are the result's ``geometry`` and the stored poses
  its ``seed``. The op that writes them back (``formfind``) exists; this
  check never mutates. No axial members → nothing is said; a refusal
  (undeclared roles, collapse, all anchors) → one ``formfind.skipped``
  info row carrying the solver's own reason.

Entry point today: ``get(kind='se', id=…, view='assembly_check')`` over a
stored design (slice 0's importer and its manifest ``relax:`` key are not
on main; when they land, ``relax: {method: 'assembly'}`` calls
:func:`assembly_check` — one implementation). Store access is limited to
what :mod:`precis_se.mold` needs (the bound cad designs); everything else
is pure over the tree.

Redundancy analysis — the method
--------------------------------

Every non-template block is a rigid body with six velocity coordinates
``(v, ω)``: ``v`` the velocity of the body point coincident with the world
origin, ``ω`` its angular velocity, so a body point ``p`` moves at
``v + ω × p``. Each connect with a validated joint contributes the rows of
motion its class *forbids* between its two bodies, written at the joint
point ``p`` (the two ports' world positions, :func:`precis_se.stability.
port_world`, averaged when they differ):

- translation along unit ``n`` at ``p``: ``n·(Δv + Δω × p) = 0`` →
  body a ``[n, p × n]``, body b ``[−n, −(p × n)]``;
- rotation about ``n``: ``n·Δω = 0`` → body a ``[0, n]``, body b ``[0, −n]``.

Rows per class (6 − the class's freedoms): ``rigid`` 3 + 3; ``revolute``
3 translations + 2 rotations ⊥ axis; ``prismatic`` 2 translations ⊥ axis
+ 3 rotations; ``cylindrical`` 2 + 2; ``screw`` 2 + 2 plus the lead
coupling ``n·Δv − (lead/2π)·n·Δω = 0`` when ``params.lead`` is declared
(else it is modelled as ``cylindrical`` and the roster says so);
``planar`` 1 translation along the normal + 2 rotations ⊥ it; ``ball`` 3
translations; ``axial`` one row, the endpoints' relative velocity along
the member line (its two attachment points differ, so each side carries
its own ``p``). ``compliant`` and ``captive`` constrain no rigid motion
(stiffness, topology) and a connect without a joint says nothing about
motion: those are listed as *unmodelled*, never guessed. A support
(``objectives.fixed``) pins its block's origin along each listed axis
(``x/y/z`` translations, ``rx/ry/rz`` rotations) as rows against ground.
A parent–child nesting is a frame, not a constraint — two siblings under
one group may well move relative to each other.

Angular columns are scaled by the characteristic length ``L`` (the spread
of every body origin and joint point, 1 when degenerate) so every entry
is O(1) whatever the design's unit scale, and the Jacobian's rank is
counted with :data:`RANK_RTOL` relative to its largest singular value. A
cut of ``1e-6`` is a judgment: two hinge axes coaxial to within a
micro-radian are, for any manufacturing process, the same axis — the
assembly binds exactly as if they were — so they must read as redundant
rather than as independent by a rounding hair; genuine dependencies sit
at ``1e-16``, so the band between is empty in practice.

*Redundant* = rows − rank: the number of constraint equations the others
already imply, i.e. mates that over-determine the assembly and can only be
satisfied by tolerance, deformation or luck. The *redundant set* names who
takes part: each left-null vector ``y`` of the Jacobian (``Jᵀy = 0``) has
its non-zero entries exactly on the rows of one dependency, and the
connects (or supports) owning those rows form one dependency group;
groups sharing a member merge. Removing any one member of a group breaks
that dependency, so the report names the group, not a victim — which mate
to loosen is the designer's call. *Mobility* = ``6·bodies − rank``
(rigid-body DOF the constraints leave, including the whole assembly's six
when nothing is fixed); envelopes and contacts are not consulted, so a
pin that a shoulder stops still counts as free here — the DOF probe in
drc owns that reading.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from precis.format import render_agent_table
from precis.utils.units import format_quantity
from precis_se import drc as se_drc
from precis_se import formfind as se_formfind
from precis_se import freedom as se_freedom
from precis_se import joints as se_joints
from precis_se import mold as se_mold
from precis_se import stability as se_stability
from precis_se.measures import MeasureSpec, rss_problem, stackup
from precis_se.ops import OpError, SeTree
from precis_se.validate import _OVERLAP_BUDGET_S, ValidationIssue, envelope_overlaps

__all__ = [
    "METHOD",
    "RANK_RTOL",
    "AssemblyCheck",
    "RedundancyReport",
    "StackRow",
    "assembly_check",
    "redundancy",
    "render",
]

METHOD = "assembly"

#: Singular values below this fraction of the largest count as zero when
#: the Jacobian's rank is taken (module docstring on why 1e-6, not 1e-9).
RANK_RTOL = 1e-6

#: A left-null-vector entry below this fraction of the vector's largest
#: entry is noise, not participation in the dependency.
_PARTICIPATION_RTOL = 1e-6

#: An axial node this fraction of the structure's extent (or closer) from
#: its force-density equilibrium is "at equilibrium" — the same rounding
#: band :mod:`precis_se.formfind` uses to leave a pose untouched.
_EQUILIBRIUM_RTOL = 1e-6

#: drc rules this module restates with more on the row — dropped from the
#: folded drc list so one problem is one finding (the chain-findings
#: ``supersedes`` posture in the handler's ``_render_drc``).
_SUPERSEDED_DRC_RULES = frozenset({"tolerance_mismatch"})


@dataclass
class Constraint:
    """One modelled constraint block — a connect's rows or a support's."""

    subject: str  # 'a.port—b.port' or '<block> fixed x,y,z'
    klass: str
    rows: int
    note: str | None = None  # e.g. "lead undeclared — modelled as cylindrical"


@dataclass
class RedundancyReport:
    bodies: list[str]
    constraints: list[Constraint]
    #: connects the model says nothing about, with why.
    unmodelled: list[tuple[str, str]]
    rows: int
    rank: int
    #: each group: the subjects taking part in one dependency (merged
    #: when groups overlap); ``redundant`` is rows − rank.
    groups: list[list[str]]
    grounded: bool

    @property
    def redundant(self) -> int:
        return self.rows - self.rank

    @property
    def mobility(self) -> int:
        return 6 * len(self.bodies) - self.rank


@dataclass
class StackRow:
    """One stack-up chain judged twice: ``wc`` is the worst-case problem
    (``None`` = agrees), ``rss`` the RSS one."""

    measure: str
    chain: list[str]
    tol_wc: float
    tol_rss: float
    unit: str
    wc: str | None
    rss: str | None


@dataclass
class AssemblyCheck:
    """The spec's result object at macro scale: ``findings`` is the one
    list; ``geometry``/``seed`` hold the formfind-solved and stored poses
    of every axial node the solve may move (empty when no axial
    subgraph); ``constraints`` the modelled mate roster."""

    findings: list[ValidationIssue]
    redundancy: RedundancyReport
    stack: list[StackRow]
    geometry: dict[str, list[float]] = field(default_factory=dict)
    seed: dict[str, list[float]] = field(default_factory=dict)
    formfind_note: str | None = None
    method: str = METHOD

    @property
    def constraints(self) -> list[str]:
        return [f"{c.subject} ({c.klass})" for c in self.redundancy.constraints]


# ── redundancy: the mate Jacobian ─────────────────────────────────────────


def _perp_basis(n: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Two unit vectors orthonormal to unit ``n``."""
    helper = np.array([1.0, 0.0, 0.0]) if abs(n[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    u = np.cross(n, helper)
    u /= np.linalg.norm(u)
    return u, np.cross(n, u)


class _Jacobian:
    def __init__(self, bodies: list[str], length: float) -> None:
        self.index = {name: i for i, name in enumerate(bodies)}
        self.n = len(bodies)
        self.length = length
        self.rows: list[np.ndarray] = []
        self.owner: list[int] = []  # row → constraint index

    def _row(self) -> np.ndarray:
        return np.zeros(6 * self.n)

    def _add(self, row: np.ndarray, owner: int) -> None:
        self.rows.append(row)
        self.owner.append(owner)

    def translation(
        self,
        n: np.ndarray,
        a: str | None,
        p_a: np.ndarray,
        b: str | None,
        p_b: np.ndarray,
        owner: int,
        *,
        coupling: float = 0.0,
    ) -> None:
        """``n·(v_a + ω_a × p_a) − n·(v_b + ω_b × p_b) − coupling·n·(ω_a − ω_b) = 0``;
        a ``None`` body is ground (its columns are absent)."""
        row = self._row()
        for name, p, sign in ((a, p_a, 1.0), (b, p_b, -1.0)):
            if name is None:
                continue
            i = self.index[name]
            row[6 * i : 6 * i + 3] += sign * n
            row[6 * i + 3 : 6 * i + 6] += (
                sign * (np.cross(p, n) - coupling * n) / self.length
            )
        self._add(row, owner)

    def rotation(self, n: np.ndarray, a: str | None, b: str | None, owner: int) -> None:
        row = self._row()
        for name, sign in ((a, 1.0), (b, -1.0)):
            if name is None:
                continue
            i = self.index[name]
            row[6 * i + 3 : 6 * i + 6] += sign * n
        self._add(row, owner)

    def matrix(self) -> np.ndarray:
        if not self.rows:
            return np.zeros((0, 6 * self.n))
        return np.vstack(self.rows)


_AXES = {
    "x": np.array([1.0, 0.0, 0.0]),
    "y": np.array([0.0, 1.0, 0.0]),
    "z": np.array([0.0, 0.0, 1.0]),
}


def _bodies(tree: SeTree) -> list[str]:
    templates_in_use = {
        node.template
        for node in tree.blocks.values()
        if node.template is not None and "#" not in node.template
    }
    return sorted(name for name in tree.blocks if name not in templates_in_use)


def _joint_rows(
    jac: _Jacobian,
    owner: int,
    klass: str,
    axis: np.ndarray | None,
    a: str,
    p_a: np.ndarray,
    b: str,
    p_b: np.ndarray,
    lead: float | None,
) -> tuple[int, str | None]:
    """Emit ``klass``'s forbidden motions; ``(rows emitted, note)``."""
    p = 0.5 * (p_a + p_b)
    before = len(jac.rows)
    note: str | None = None
    if klass == "axial":
        d = p_b - p_a
        length = float(np.linalg.norm(d))
        if length <= 0.0:
            return 0, "endpoints coincide — no line of action"
        jac.translation(d / length, a, p_a, b, p_b, owner)
        return 1, None
    if klass == "ball":
        for e in _AXES.values():
            jac.translation(e, a, p, b, p, owner)
        return len(jac.rows) - before, None
    if klass == "rigid":
        for e in _AXES.values():
            jac.translation(e, a, p, b, p, owner)
        for e in _AXES.values():
            jac.rotation(e, a, b, owner)
        return len(jac.rows) - before, None
    if axis is None:
        return 0, "no axis declared"
    u, w = _perp_basis(axis)
    if klass == "revolute":
        for e in _AXES.values():
            jac.translation(e, a, p, b, p, owner)
        for e in (u, w):
            jac.rotation(e, a, b, owner)
    elif klass == "prismatic":
        for e in (u, w):
            jac.translation(e, a, p, b, p, owner)
        for e in _AXES.values():
            jac.rotation(e, a, b, owner)
    elif klass in ("cylindrical", "screw"):
        for e in (u, w):
            jac.translation(e, a, p, b, p, owner)
        for e in (u, w):
            jac.rotation(e, a, b, owner)
        if klass == "screw":
            if lead is not None and lead > 0.0:
                jac.translation(
                    axis, a, p, b, p, owner, coupling=lead / (2.0 * math.pi)
                )
            else:
                note = "lead undeclared — modelled as cylindrical"
    elif klass == "planar":
        jac.translation(axis, a, p, b, p, owner)
        for e in (u, w):
            jac.rotation(e, a, b, owner)
    else:  # pragma: no cover — every class is enumerated above or unmodelled
        return 0, f"class {klass!r} not modelled"
    return len(jac.rows) - before, note


def _merge_groups(groups: list[set[str]]) -> list[list[str]]:
    merged: list[set[str]] = []
    for g in groups:
        g = set(g)
        keep: list[set[str]] = []
        for m in merged:
            if m & g:
                g |= m
            else:
                keep.append(m)
        keep.append(g)
        merged = keep
    return sorted((sorted(g) for g in merged), key=lambda g: (len(g), g))


def redundancy(tree: SeTree) -> RedundancyReport:
    """The mate-redundancy analysis (module docstring). Pure."""
    bodies = _bodies(tree)
    body_set = set(bodies)
    constraints: list[Constraint] = []
    unmodelled: list[tuple[str, str]] = []
    pending: list[
        tuple[
            str, str, np.ndarray | None, str, np.ndarray, str, np.ndarray, float | None
        ]
    ] = []
    points: list[np.ndarray] = [
        np.asarray(tree.blocks[n].pose, dtype=float) for n in bodies
    ]
    for c in tree.connects:
        subject = f"{c.a_block}.{c.a_port}—{c.b_block}.{c.b_port}"
        if c.joint is None:
            unmodelled.append((subject, "no joint — says nothing about motion"))
            continue
        try:
            joint = se_joints.validate_joint(c.joint)
        except se_joints.JointError:
            unmodelled.append((subject, "malformed joint (drc's malformed_joint)"))
            continue
        klass = joint["class"]
        if klass in ("compliant", "captive"):
            unmodelled.append(
                (subject, f"{klass}: stiffness/topology, not a rigid constraint")
            )
            continue
        if c.a_block == c.b_block:
            unmodelled.append((subject, "both ends on one block"))
            continue
        if c.a_block not in body_set or c.b_block not in body_set:
            unmodelled.append((subject, "an endpoint is a template or missing"))
            continue
        p_a = se_stability.port_world(tree, c.a_block, c.a_port)
        p_b = se_stability.port_world(tree, c.b_block, c.b_port)
        points.extend((p_a, p_b))
        axis_raw = joint.get("axis")
        axis = np.asarray(axis_raw, dtype=float) if axis_raw is not None else None
        params = joint.get("params") or {}
        lead_raw = params.get("lead")
        lead = float(lead_raw) if isinstance(lead_raw, (int, float)) else None
        pending.append((subject, klass, axis, c.a_block, p_a, c.b_block, p_b, lead))

    supports: list[tuple[str, list[str]]] = []
    for name in bodies:
        fixed = tree.blocks[name].objectives.get("fixed")
        axes = [str(a) for a in fixed] if isinstance(fixed, list) else []
        if axes:
            supports.append((name, axes))

    spread = 0.0
    if points:
        stack = np.vstack(points)
        spread = float(np.max(np.ptp(stack, axis=0)))
    length = spread if spread > 0.0 else 1.0
    jac = _Jacobian(bodies, length)

    for subject, klass, axis, a, p_a, b, p_b, lead in pending:
        owner = len(constraints)
        n_rows, note = _joint_rows(jac, owner, klass, axis, a, p_a, b, p_b, lead)
        if n_rows == 0:
            unmodelled.append((subject, note or "no rows"))
            continue
        constraints.append(
            Constraint(subject=subject, klass=klass, rows=n_rows, note=note)
        )
    for name, axes in supports:
        owner = len(constraints)
        origin = np.asarray(tree.blocks[name].pose, dtype=float)
        n_rows = 0
        for ax in axes:
            if ax in _AXES:
                jac.translation(_AXES[ax], name, origin, None, origin, owner)
                n_rows += 1
            elif ax.startswith("r") and ax[1:] in _AXES:
                jac.rotation(_AXES[ax[1:]], name, None, owner)
                n_rows += 1
        if n_rows:
            constraints.append(
                Constraint(
                    subject=f"{name} fixed {','.join(axes)}",
                    klass="support",
                    rows=n_rows,
                )
            )

    matrix = jac.matrix()
    rows = matrix.shape[0]
    groups: list[set[str]] = []
    rank = 0
    if rows and matrix.size:
        u_mat, svals, _vt = np.linalg.svd(matrix, full_matrices=True)
        if svals.size and svals[0] > 0.0:
            rank = int(np.sum(svals > RANK_RTOL * svals[0]))
        for k in range(rank, rows):
            y = u_mat[:, k]
            peak = float(np.max(np.abs(y)))
            if peak <= 0.0:
                continue
            members = {
                constraints[jac.owner[i]].subject
                for i in range(rows)
                if abs(y[i]) > _PARTICIPATION_RTOL * peak
            }
            if members:
                groups.append(members)
    return RedundancyReport(
        bodies=bodies,
        constraints=constraints,
        unmodelled=unmodelled,
        rows=rows,
        rank=rank,
        groups=_merge_groups(groups),
        grounded=bool(supports),
    )


# ── the fold ──────────────────────────────────────────────────────────────


def _quantity(v: float, unit: str) -> str:
    return format_quantity(v, "length") if unit == "m" else f"{v:g} {unit}".rstrip()


def _stack_rows(measures: list[MeasureSpec]) -> list[StackRow]:
    by_key = {f"{m.block}.{m.name}": m for m in measures}
    out: list[StackRow] = []
    for res in stackup(measures):
        m = by_key.get(res.measure)
        if m is None or (res.derived is None and res.derived_min is None):
            continue  # dangling/cycle/malformed rows stay drc's
        if res.problem_kind not in (None, "mismatch"):
            continue
        rss_detail, _kind = rss_problem(m, res)
        out.append(
            StackRow(
                measure=res.measure,
                chain=list(res.chain),
                tol_wc=res.tol_accum,
                tol_rss=res.tol_rss,
                unit=res.unit,
                wc=res.problem,
                rss=rss_detail,
            )
        )
    return out


def _stack_findings(rows: list[StackRow], tree: SeTree) -> list[ValidationIssue]:
    strength_of = {f"{m.block}.{m.name}": m.strength for m in tree.measures}
    out: list[ValidationIssue] = []
    for r in rows:
        if r.wc is None and r.rss is None:
            continue
        chain = " ← ".join(r.chain)
        base_sev = "error" if strength_of.get(r.measure) == "hard" else "warn"
        budget = (
            f"worst-case ±{_quantity(r.tol_wc, r.unit)}, "
            f"RSS ±{_quantity(r.tol_rss, r.unit)}"
        )
        if r.wc is not None:
            out.append(
                ValidationIssue(
                    rule="stackup.worst_case",
                    subject=r.measure,
                    detail=f"{r.wc}; chain {chain}; {budget} — over-tight under "
                    "both stacks: widen a tolerance in the chain or move the "
                    "declared value",
                    severity=base_sev,
                )
            )
        if r.rss is not None:
            detail = f"{r.rss}; chain {chain}; {budget}" + (
                ""
                if r.wc is not None
                else " — inside the worst-case band but outside the RSS "
                "band: the statistical stack does not carry this chain either"
            )
            out.append(
                ValidationIssue(
                    rule="stackup.rss",
                    subject=r.measure,
                    detail=detail,
                    severity=base_sev if r.wc is not None else "warn",
                )
            )
    return out


def _interference_findings(
    tree: SeTree, budget_s: float | None
) -> list[ValidationIssue]:
    connected: dict[frozenset[str], str | None] = {}
    for c in tree.connects:
        key = frozenset({c.a_block, c.b_block})
        klass = c.joint.get("class") if c.joint else None
        connected.setdefault(key, str(klass) if klass else None)
    overlaps, cross_scale, unchecked = envelope_overlaps(tree, budget_s=budget_s)
    out: list[ValidationIssue] = []
    for a, b, gap in sorted(overlaps, key=lambda t: t[2]):
        key = frozenset({a, b})
        depth = format_quantity(-gap, "length")
        if key in connected:
            klass = connected[key] or "no class"
            out.append(
                ValidationIssue(
                    rule="assembly.interference",
                    subject=f"{a}—{b}",
                    detail=f"posed envelopes interpenetrate by {depth} — "
                    f"sanctioned by a connect ({klass}); read the depth "
                    "against what the joint calls for",
                    severity="info",
                )
            )
        else:
            out.append(
                ValidationIssue(
                    rule="assembly.interference",
                    subject=f"{a}—{b}",
                    detail=f"posed envelopes interpenetrate by {depth} with no "
                    "connect between the two blocks — does not fit: re-pose, "
                    "or connect their ports if the contact is intended",
                    severity="warn",
                )
            )
    if cross_scale:
        shown = ", ".join(f"{a}—{b}" for a, b in cross_scale[:5])
        out.append(
            ValidationIssue(
                rule="assembly.interference_unchecked",
                subject=f"{len(cross_scale)} pair(s)",
                detail=f"{shown}: sizes too far apart for one SDF query — "
                "UNCHECKED, not clear",
                severity="warn",
            )
        )
    if unchecked:
        shown = ", ".join(f"{a}—{b}" for a, b in unchecked[:5])
        out.append(
            ValidationIssue(
                rule="assembly.interference_unchecked",
                subject=f"{len(unchecked)} pair(s)",
                detail=f"{shown}: the narrow-phase budget ran out — UNCHECKED",
                severity="warn",
            )
        )
    return out


def _redundancy_findings(
    rep: RedundancyReport, free: se_freedom.FreedomReport
) -> list[ValidationIssue]:
    out: list[ValidationIssue] = []
    if rep.redundant > 0:
        groups = (
            "; ".join("{" + ", ".join(g) + "}" for g in rep.groups) or "(unresolved)"
        )
        out.append(
            ValidationIssue(
                rule="assembly.overconstrained",
                subject=f"{rep.redundant} redundant of {rep.rows} constraint row(s)",
                detail=f"mate Jacobian rank {rep.rank} over {len(rep.bodies)} "
                f"bodies — redundant set {groups}: each group over-determines "
                "the assembly, which then fits only by tolerance or "
                "deformation; loosen one mate's class in each group (set_joint) "
                "or accept and declare the tolerance relation that absorbs it",
                severity="warn",
            )
        )
    for subject, why in rep.unmodelled:
        out.append(
            ValidationIssue(
                rule="assembly.unmodelled_mate",
                subject=subject,
                detail=f"not in the mate Jacobian: {why}",
                severity="info",
            )
        )
    if rep.bodies and (rep.mobility > 0 or free.motions or free.unconnected):
        parts: list[str] = [
            f"mobility {rep.mobility} rigid-body DOF over {len(rep.bodies)} "
            f"bodies (rank {rep.rank})"
        ]
        if not rep.grounded:
            parts.append("nothing fixed, so six of those are the whole assembly")
        if free.motions:
            parts.append(
                "declared moving joints: "
                + ", ".join(f"{m.subject} ({m.klass})" for m in free.motions)
            )
        if free.unconnected:
            parts.append("unconnected blocks: " + ", ".join(free.unconnected))
        out.append(
            ValidationIssue(
                rule="assembly.underconstrained",
                subject=f"{rep.mobility} DOF",
                detail="; ".join(parts) + " — envelopes/contacts not consulted "
                "(drc's DOF probe reads those)",
                severity="info",
            )
        )
    return out


def _formfind(tree: SeTree, check: AssemblyCheck) -> list[ValidationIssue]:
    if not se_stability.axial_connects(tree):
        return []
    try:
        solved = se_formfind.solve(tree, {"move": "all"})
    except OpError as exc:
        check.formfind_note = str(exc)
        return [
            ValidationIssue(
                rule="formfind.skipped",
                subject="axial subgraph",
                detail=str(exc),
                severity="info",
            )
        ]
    out: list[ValidationIssue] = []
    tol = _EQUILIBRIUM_RTOL * solved.extent
    for name in solved.node_names:
        check.seed[name] = [float(v) for v in tree.blocks[name].pose]
        check.geometry[name] = [float(v) for v in solved.coords[solved.index[name]]]
        if name not in solved.movable:
            continue
        d = solved.displacement(tree, name)
        if d <= tol:
            continue
        out.append(
            ValidationIssue(
                rule="formfind.off_equilibrium",
                subject=name,
                detail=f"sits {format_quantity(d, 'length')} from force-density "
                "equilibrium (tie +1, strut −1, rod +1; anchors from "
                "objectives.fixed) — the solved position is in this result's "
                "geometry; the formfind op writes it back",
                severity="warn",
            )
        )
    return out


def assembly_check(
    store: Any,
    tree: SeTree,
    ref_id: int,
    *,
    budget_s: float | None = _OVERLAP_BUDGET_S,
) -> AssemblyCheck:
    """Run every macro checker (module docstring) and fold the result.
    ``budget_s`` bounds the interference narrow phase
    (:func:`~precis_se.validate.envelope_overlaps`; ``None`` = unbounded,
    tests only)."""
    free = se_freedom.freedom(tree)
    rep = redundancy(tree)
    rows = _stack_rows(tree.measures)
    check = AssemblyCheck(findings=[], redundancy=rep, stack=rows)
    drc_report = se_drc.drc(tree)
    findings = [f for f in drc_report.findings if f.rule not in _SUPERSEDED_DRC_RULES]
    findings.extend(_redundancy_findings(rep, free))
    findings.extend(_interference_findings(tree, budget_s))
    findings.extend(_stack_findings(rows, tree))
    findings.extend(se_mold.findings(store, tree, ref_id))
    findings.extend(_formfind(tree, check))
    order = {"error": 0, "warn": 1, "info": 2}
    findings.sort(key=lambda f: (order.get(f.severity, 3), f.rule, f.subject))
    check.findings = findings
    return check


# ── render ────────────────────────────────────────────────────────────────


def render(check: AssemblyCheck) -> str:
    """``view='assembly_check'`` — the findings table, then the mate
    roster and the stack-up table (both stacks per chain)."""
    rep = check.redundancy
    n_err = sum(1 for f in check.findings if f.severity == "error")
    n_warn = sum(1 for f in check.findings if f.severity == "warn")
    n_info = len(check.findings) - n_err - n_warn
    lines = [
        f"# assembly check (method={check.method}) — {n_err} error(s), "
        f"{n_warn} warning(s), {n_info} info"
    ]
    lines.append(
        f"bodies {len(rep.bodies)} · constraint rows {rep.rows} · rank {rep.rank} · "
        f"redundant {rep.redundant} · mobility {rep.mobility}"
        + ("" if rep.grounded else " (ungrounded)")
    )
    if check.findings:
        lines.append("")
        lines.append(
            render_agent_table(
                [
                    {
                        "severity": f.severity,
                        "rule": f.rule,
                        "subject": f.subject,
                        "detail": f.detail,
                    }
                    for f in check.findings
                ],
                schema=["severity", "rule", "subject", "detail"],
            )
        )
    else:
        lines.append("✓ no findings — see view='validate' for the filled fraction")
    if rep.constraints:
        lines.append("")
        lines.append("## mate roster (rows each class forbids)")
        lines.append(
            render_agent_table(
                [
                    {
                        "constraint": c.subject,
                        "class": c.klass,
                        "rows": str(c.rows),
                        "note": c.note or "",
                    }
                    for c in rep.constraints
                ],
                schema=["constraint", "class", "rows", "note"],
            )
        )
    if check.stack:
        lines.append("")
        lines.append("## stack-up — worst-case and RSS per chain")
        lines.append(
            render_agent_table(
                [
                    {
                        "measure": r.measure,
                        "chain": " ← ".join(r.chain[1:]),
                        "worst-case": f"±{_quantity(r.tol_wc, r.unit)}"
                        + ("" if r.wc is None else " FAIL"),
                        "rss": f"±{_quantity(r.tol_rss, r.unit)}"
                        + ("" if r.rss is None else " FAIL"),
                    }
                    for r in check.stack
                ],
                schema=["measure", "chain", "worst-case", "rss"],
            )
        )
    if check.geometry:
        lines.append("")
        lines.append(
            f"formfind: {len(check.geometry)} axial node(s) solved read-only — "
            "geometry/seed in the result; nothing written"
        )
    return "\n".join(lines)
