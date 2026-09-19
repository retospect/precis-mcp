"""``intent='manufacture'`` — print-in-place: one fused field per print
group (docs/backlog/structural-solution-space.md §Slice 4 bridge, the
print ``intent`` table's second row; round B2).

A ``model`` group (:mod:`precis_se.printgroup`) prints every member as
its own object and every bought part as a stand-in. A **manufacture**
group is the real part: its printed members become ONE sampled-field
solid in the group root's frame, built by field/CSG ops only — no mesh
is ever operated on (Reto's rules of 2026-09-18, in force here):

- **Joint classification** (:func:`classify_joints`). Every connect
  between two members of this group (B1's nesting rule decides
  membership) is *rigid* — its kinematic class keeps no DOF
  (:data:`RIGID_CLASSES`: ``rigid``/``captive``/``axial``, and a connect
  with no ``joint`` at all, which is fused and said so) — or *DOF*
  (:data:`precis_se.drc._MOVING_CLASSES`). Rigid connects between two
  printed members put them in one **fused component** (union-find);
  a connect to a bought member fuses nothing.
- **Fusion** = min-union of the member SDFs, with :func:`precis.cad.fold.
  smooth_min_np` of width ``blend=`` (metres, default ``0`` = a plain
  ``min`` — the DSL's ``blend:<len>`` semantics, a fillet-*like* seam,
  not an exact radius) between members of one fused component; a hard
  ``min`` between components.
- **In-place gap — seam-local by construction.** For each DOF pair of
  printed members ``(A, B)``: ``A' = A \\ dilate(B, gap/2)`` and ``B' = B
  \\ dilate(A, gap/2)`` — the dilation is :func:`precis.cad.fieldops.
  offset` on the PARTNER's re-distanced sampled field (never the mesh,
  never the raw CSG values), the subtraction a max on the grid. Nothing
  else about ``A`` or ``B`` moves, so a rigid seam ``A`` shares with a
  third member stays exactly as sampled (a whole-member erosion would
  open it). Guarantee: face-to-face and pin-in-bore separations come out
  at ``gap``; at a re-entrant corner where both carves meet the worst
  case is ``gap/2`` — the report always quotes the measured minimum.
  Every offset here carries **an extra half pitch**, because the
  kernel's re-distance binarises (its surface sits half a voxel from the
  last inside sample, so it can lie up to ``pitch/2`` outside the true
  one) — the declared ``gap``/``fit`` are therefore FLOORS. ``gap=`` is
  required unless the house ``min_clearance`` capability resolves
  (``se_capabilities.json`` — null in every fdm row today; a
  ``set_process_override(field='min_clearance')`` on the root supplies
  one), in which case it is the default. A ``gap`` below the floor is an
  ``in_place_clearance`` **error** finding; a null floor is an info
  finding saying the floor is uncalibrated and the gap was taken as
  declared. A DOF pair whose two sides a rigid path joins anyway
  (union-find over the rigid edges) is ``dof_bridged`` — an error, and
  the export is refused: whatever gap the joint keeps, the two sides are
  one body.
- **Every member is re-sampled at the pitch.** The fused root is ONE
  ``field:`` leaf: sub-pitch features of a member's design (a hole's
  printed-hole compensation, a seat) are not preserved below the grid.
  The follow-up is a mixed root — the fused ``field:`` leaf plus the
  members' analytic ``add``/``cut``/``blend:`` nodes, which the DSL
  already expresses; the report says so on every render.
- **Cavities.** A bought member stays bought: its stand-in solid (B1's
  :func:`precis_se.printgroup.stand_in_for`, catalog part or spec dims)
  is re-distanced, **dilated by ``fit=``** (metres — required whenever
  the group has a cavity to cut, no default; ``0`` subtracts the exact
  analytic stand-in) and subtracted. No insertion-path search: the
  report names each cavity's **top-layer height** in the chosen build
  frame as the mid-print **pause** height — the ``bambuuzle`` rung of the
  hand-off inserts the part there. A bought member with no stand-in is a
  ``cavity_missing`` error on top of B1's ``no_stand_in``.
- **Fastener elision.** A bought fastener (catalog form ``screw``) whose
  grip stack (:func:`precis_se.fasten.fasten`'s members — the blocks its
  axis passes through) holds two or more printed members that are all
  in ONE fused component is **elided**: no cavity, an info finding
  ``joint fused, <fastener> not needed``, and the nuts/washers in the
  same stack go with it. The ``screw`` mechanism demand
  (:func:`precis_se.fasten.abstract_joints`) is satisfied by fusion —
  for this intent only; ``model`` still prints every fastener. A
  fastener across a DOF joint, or one whose stack leaves the group,
  stays a cavity.
- **Teardrop bore.** No new primitive: a DOF joint of an axis class
  whose declared axis lies within 45° of the build plane in the chosen
  frame gets an ``overhang`` finding naming the bore (both members, the
  connect, the axis and its angle from the plate) — a horizontal bore
  under the 45° rule wants a teardrop/diamond section the kernel does not
  draw yet. A DOF joint without an axis gets an info finding saying the
  check could not run.
- **Output**: ONE cad design per group, ``<design>-<root>-mfg`` (numeric
  suffix on collision), rooted at ``field:<sha>`` (the fused grid, root
  frame, metres), bound to the ROOT block the way :func:`precis_se.
  simp_bridge.realize_simp` binds — inside the per-ref
  :func:`~precis_se.persist.tree_mutation` lock, on a fresh tree, after
  the inputs hash (:func:`inputs_sha`) proves nothing moved; the run
  summary on the se ref's ``meta.manufacture`` (``last`` + ``runs``) and
  in the field's own provenance header (so ``view='print'`` needs no
  ref lookup); each minted design linked ``derived-from`` the se design;
  a re-run mints a sibling and switches the root's one binding. The
  root's own solid, if it had one, is refused — a manufacture root holds
  the fused result (move the solid to a child member).
- **Objects**: ``fmt='3mf'`` writes one object per **connected
  component** of the fused field (:func:`precis.cad.fieldops.
  label_components`, 6-connectivity) — a DOF-separated pair is two
  objects, a fused pair one. Each object is named by the members whose
  material it holds. The measured **gap** per DOF joint is the minimum
  over the object's mesh vertices of the OTHER side's eroded SDF (exact
  outside an eroded body), measured at realize time on the same meshes
  the export writes.

**Op shape**: ``realize(block=<group root>, strategy='manufacture',
gap=?, fit=?, blend=?, pitch=?)``. ``pitch`` (metres) defaults to the
root mode's house ``layer_height`` — the group's export pitch. The op
runs **synchronously** when the group has no SIMP-realized member and
the grid is under :data:`SYNC_CELL_CAP` cells; otherwise it enqueues an
``se_manufacture`` job (:mod:`precis_se.manufacture_job` — a job type of
its own rather than a mode on ``se_simp``, whose params schema is closed
and SIMP-shaped). Above :data:`MAX_CELLS` the op refuses with the count.

**Fused SIMP domain**: ``realize(block=<root>, strategy='simp', ...)`` on
a manufacture root solves the group as one body — :func:`simp_domain`
is the domain-builder hook :func:`precis_se.simp_bridge.solve_simp`
takes: the union of the member envelopes in the root frame, minus the
stand-in cavities (dilated by the simp op's optional ``fit=``); loads and
supports are the ROOT's ``objectives`` at ``load_at``/``fixed_at`` (a
face of the group box or a root port). A group with a DOF joint between
printed members is refused there: one solve is one body.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from precis.cad import bulk as cad_bulk
from precis.cad import dsl as cad_dsl
from precis.cad import fieldops
from precis.cad import printability as cad_printability
from precis.cad.export import _MM_PER_M, ExportError, _write_3mf, manifold_available
from precis.cad.fieldmesh import FieldMeshError, field_mesh
from precis.cad.fold import smooth_min_np
from precis.cad.graph import Design as CadDesign
from precis.cad.primitives import Field
from precis.cad.printability import rotate_all_to_frame
from precis.cad.relate import component_sdf_np
from precis.cad.scene import NodeSpec, SceneError, SceneSpec, build_design
from precis.cad.vec import Transform, Vec3, as_vec3
from precis_se import capabilities as se_caps
from precis_se import fasten as se_fasten
from precis_se import persist
from precis_se.drc import _MOVING_CLASSES
from precis_se.joints import AXIS_CLASSES, KINEMATIC_CLASSES
from precis_se.ops import (
    ConnectSpec,
    OpError,
    SeBlock,
    SeTree,
    apply_ops,
    effective_envelope,
    print_intent,
)
from precis_se.printgroup import (
    MANUFACTURE,
    NESTED,
    PRINTED,
    STAND_IN,
    UNREALIZED,
    GroupMember,
    GroupPrintReport,
    _member,
    _walk,
    _world,
    choose_frame,
    group_loads,
    is_group_root,
)
from precis_se.printing import (
    PrintUnsupported,
    _rules_for,
    format_down,
    frame_findings,
    frame_free_findings,
)
from precis_se.realize import _unique_cad_slug, resolve_realize_target
from precis_se.validate import ValidationIssue

if TYPE_CHECKING:  # pragma: no cover - typing only
    from precis.store import Store

log = logging.getLogger(__name__)

#: The job type the handler enqueues for a group too big to fuse inline.
JOB_TYPE = "se_manufacture"
#: The se ref's ``meta`` key the run summaries live under.
META_KEY = "manufacture"
#: The capability field the in-place gap floor is read from (millimetres).
CLEARANCE_FIELD = "min_clearance"
#: The cad slug suffix a fused design carries: ``<design>-<root>-mfg``.
CAD_SUFFIX = "mfg"
#: Grid cells (samples) under which the op fuses inline — a re-distance
#: per offset member is three vectorised EDT passes over the grid, and at
#: this size the whole fuse is seconds, not the minutes an MCP call must
#: never block on. Above it the op enqueues.
SYNC_CELL_CAP = 500_000
#: Grid cells the job will take at all; beyond it the op refuses with the
#: count (memory: several float64 grids of this size live at once).
MAX_CELLS = 8_000_000
#: Voxels of empty margin around the padded group box.
_GRID_MARGIN_PITCHES = 2
#: A DOF axis closer than this to the build plane is a horizontal bore.
TEARDROP_ANGLE_DEG = 45.0
#: Kinematic classes that keep no DOF — a connect of one of these between
#: two printed members fuses them (module docstring).
RIGID_CLASSES: frozenset[str] = frozenset(KINEMATIC_CLASSES) - _MOVING_CLASSES
#: Catalog fastener forms that ride along with an elided screw.
_STACK_FORMS: frozenset[str] = frozenset({"nut", "washer"})


class ManufactureError(OpError):
    """A refusal from this module — an :class:`~precis_se.ops.OpError`, so
    the op walker maps it to ``BadInput`` like every other op refusal."""


class ManufactureStale(ManufactureError):
    """The group changed between the fuse's snapshot and the bind — the
    fused field belongs to a group that no longer exists in that form.
    Nothing is bound; the caller re-runs ``realize``."""


# --------------------------------------------------------------------------
# the request
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ManufactureRequest:
    """One validated ``realize(strategy='manufacture')`` — JSON-round-
    trippable because it travels as the ``se_manufacture`` job's params.
    Lengths in metres. ``sync`` is the op-time decision (module docstring)
    and is not a job param."""

    block: str
    mode: str
    pitch: float
    pitch_source: str
    gap: float | None
    gap_source: str
    gap_floor_mm: float | None
    fit: float | None
    blend: float
    sync: bool = False

    def to_params(self) -> dict[str, Any]:
        return {
            "block": self.block,
            "mode": self.mode,
            "pitch": self.pitch,
            "pitch_source": self.pitch_source,
            "gap": self.gap,
            "gap_source": self.gap_source,
            "gap_floor_mm": self.gap_floor_mm,
            "fit": self.fit,
            "blend": self.blend,
        }

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> ManufactureRequest:
        def _opt(key: str) -> float | None:
            v = params.get(key)
            return None if v is None else float(v)

        return cls(
            block=str(params["block"]),
            mode=str(params["mode"]),
            pitch=float(params["pitch"]),
            pitch_source=str(params.get("pitch_source") or "given"),
            gap=_opt("gap"),
            gap_source=str(params.get("gap_source") or "none"),
            gap_floor_mm=_opt("gap_floor_mm"),
            fit=_opt("fit"),
            blend=float(params.get("blend") or 0.0),
        )


# --------------------------------------------------------------------------
# joint classification
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class JointEdge:
    """One connect between two members of the group, classified."""

    a: str
    b: str
    subject: str
    klass: str | None
    mechanism: str | None
    axis: list[float] | None
    dof: bool

    @property
    def declared(self) -> bool:
        return self.klass is not None


@dataclass
class JointPlan:
    """:func:`classify_joints`' answer. ``fused_id`` maps every printed
    member to its fused-component index; ``eroded`` names the printed
    members that carry a DOF connect to another printed member (each is
    carved back from its partner by ``gap/2``); ``bridged`` are the DOF
    pairs whose two sides a rigid path joins anyway — not print-in-place,
    an error finding, export refused."""

    edges: list[JointEdge]
    fused_id: dict[str, int]
    eroded: set[str]
    findings: list[ValidationIssue] = field(default_factory=list)
    bridged: list[JointEdge] = field(default_factory=list)

    @property
    def dof_edges(self) -> list[JointEdge]:
        return [e for e in self.edges if e.dof]

    @property
    def printed_dof_edges(self) -> list[JointEdge]:
        """DOF edges between two PRINTED members — the ones that get a gap."""
        return [
            e
            for e in self.edges
            if e.dof and e.a in self.fused_id and e.b in self.fused_id
        ]

    @property
    def rigid_edges(self) -> list[JointEdge]:
        return [e for e in self.edges if not e.dof]

    def components(self) -> list[list[str]]:
        """The fused components as sorted member lists, in index order."""
        by_id: dict[int, list[str]] = {}
        for name, cid in self.fused_id.items():
            by_id.setdefault(cid, []).append(name)
        return [sorted(by_id[k]) for k in sorted(by_id)]

    def fused(self, a: str, b: str) -> bool:
        """The predicate :func:`precis_se.fasten.abstract_joints` takes."""
        return a in self.fused_id and self.fused_id.get(a) == self.fused_id.get(b)


def _joint_subject(c: ConnectSpec) -> str:
    return f"{c.a_block}.{c.a_port}—{c.b_block}.{c.b_port}"


def classify_joints(
    tree: SeTree, printed: list[str], stand_ins: list[str]
) -> JointPlan:
    """Classify every connect between two members (``printed`` ∪
    ``stand_ins``) as rigid or DOF and union-find the printed members
    over the rigid edges (module docstring). A connect with no ``joint``
    is fused and reported (``joint_undeclared``, info)."""
    members = set(printed) | set(stand_ins)
    printed_set = set(printed)
    parent: dict[str, str] = {m: m for m in printed}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    edges: list[JointEdge] = []
    findings: list[ValidationIssue] = []
    for c in sorted(tree.connects, key=lambda c: (c.a_block, c.a_port, c.b_block)):
        if c.a_block not in members or c.b_block not in members:
            continue
        if c.a_block == c.b_block or c.kind is not None:
            continue  # a self-connect or an atomic bond says nothing here
        joint = c.joint or {}
        klass = joint.get("class") if isinstance(joint, dict) else None
        klass = str(klass) if klass else None
        mech = joint.get("mechanism") if isinstance(joint, dict) else None
        raw_axis = joint.get("axis") if isinstance(joint, dict) else None
        axis = (
            [float(x) for x in raw_axis]
            if isinstance(raw_axis, list) and len(raw_axis) == 3
            else None
        )
        dof = klass is not None and klass in _MOVING_CLASSES
        edge = JointEdge(
            a=c.a_block,
            b=c.b_block,
            subject=_joint_subject(c),
            klass=klass,
            mechanism=str(mech) if mech else None,
            axis=axis,
            dof=dof,
        )
        edges.append(edge)
        both_printed = c.a_block in printed_set and c.b_block in printed_set
        if klass is None and both_printed:
            findings.append(
                ValidationIssue(
                    rule="joint_undeclared",
                    subject=edge.subject,
                    detail=(
                        f"connect {edge.subject} declares no joint class; the "
                        f"manufacture fuse treats it as rigid ({c.a_block!r} and "
                        f"{c.b_block!r} become one body)"
                    ),
                    severity="info",
                    suggested_fix=(
                        "set_joint(a=..., b=..., joint={'class': 'revolute', "
                        "'axis': [...]}) to keep the DOF, or 'rigid' to say so"
                    ),
                )
            )
        if not dof and both_printed:
            parent[find(c.a_block)] = find(c.b_block)
    roots = sorted({find(m) for m in printed})
    index = {r: i for i, r in enumerate(roots)}
    fused_id = {m: index[find(m)] for m in printed}
    eroded = {
        m
        for e in edges
        if e.dof and e.a in printed_set and e.b in printed_set
        for m in (e.a, e.b)
    }
    bridged: list[JointEdge] = []
    for e in edges:
        if not (e.dof and e.a in printed_set and e.b in printed_set):
            continue
        if fused_id[e.a] != fused_id[e.b]:
            continue
        bridged.append(e)
        via = sorted(
            m
            for m, cid in fused_id.items()
            if cid == fused_id[e.a] and m not in (e.a, e.b)
        )
        findings.append(
            ValidationIssue(
                rule="dof_bridged",
                subject=e.subject,
                detail=(
                    f"{e.a}–{e.b} {e.klass} is bridged by a rigid path through "
                    f"{', '.join(via) if via else 'a rigid connect of its own'}: "
                    "not print-in-place — the two sides are one body whatever "
                    "gap the joint keeps"
                ),
                severity="error",
                suggested_fix=(
                    "break the rigid path (set_joint one of its connects to a DOF "
                    "class, or move a member out of the group)"
                ),
            )
        )
    return JointPlan(
        edges=edges,
        fused_id=fused_id,
        eroded=eroded,
        findings=findings,
        bridged=bridged,
    )


# --------------------------------------------------------------------------
# group geometry: members in the root frame, the grid, the samples
# --------------------------------------------------------------------------


@dataclass
class _Solid:
    """One member's evaluable solid: the live design (already cut for a
    printed member; the stand-in for a bought one) and the transform
    taking a ROOT-frame point into the member's local frame."""

    member: GroupMember
    design: CadDesign
    to_local: Transform
    lo: np.ndarray  # root-frame AABB
    hi: np.ndarray

    def sample(self, pts_root: np.ndarray) -> np.ndarray:
        local = pts_root @ self.to_local.R.T + self.to_local.t
        return np.asarray(component_sdf_np(self.design, self.design.whole(), local))


@dataclass
class GroupGeometry:
    """Everything the fuse and the SIMP domain read off the tree once:
    the classified members, their solids in the root frame, and the joint
    plan. ``root_xform`` maps root-local → world."""

    root: str
    root_xform: Transform
    members: list[GroupMember]
    solids: dict[str, _Solid]
    plan: JointPlan
    findings: list[ValidationIssue]
    #: ``{elided bought member: {'stack', 'subject', 'via'}}`` — decided
    #: on the first pass; the printed members were then rebuilt with those
    #: fasteners' holes excluded (:func:`group_geometry`).
    elided: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def printed(self) -> list[str]:
        return [m.block for m in self.members if m.role == PRINTED]

    @property
    def stand_ins(self) -> list[str]:
        return [m.block for m in self.members if m.role == STAND_IN]

    def box(self) -> tuple[np.ndarray, np.ndarray] | None:
        if not self.solids:
            return None
        lo = np.min([s.lo for s in self.solids.values()], axis=0)
        hi = np.max([s.hi for s in self.solids.values()], axis=0)
        return lo, hi


def _corners(lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
    return np.array(
        [
            [x, y, z]
            for x in (lo[0], hi[0])
            for y in (lo[1], hi[1])
            for z in (lo[2], hi[2])
        ],
        dtype=float,
    )


def _solid_for(member: GroupMember, root_xform: Transform) -> _Solid | None:
    if member.role == PRINTED and member.printed is not None:
        design = member.printed.design
    elif member.role == STAND_IN and member.spec is not None:
        try:
            design = build_design(member.spec)
        except SceneError:
            return None
    else:
        return None
    if not design.components:
        return None
    # root-local -> world -> member-local
    to_local = member.xform.inverse().compose(root_xform)
    lo_l, hi_l = (
        np.asarray(v, dtype=float) for v in cad_bulk.expr_aabb(design, design.whole())
    )
    back = to_local.inverse()
    corners = _corners(lo_l, hi_l) @ back.R.T + back.t
    return _Solid(
        member=member,
        design=design,
        to_local=to_local,
        lo=corners.min(axis=0),
        hi=corners.max(axis=0),
    )


def group_geometry(
    tree: SeTree, root: str, *, cad_store_reader: Store
) -> GroupGeometry:
    """Classify the group's members (B1's :func:`~precis_se.printgroup.
    _member`, root excluded — a manufacture root holds the result), build
    each placeable member's solid in the root frame, classify the joints,
    decide the elisions — then, when anything was elided, rebuild the
    printed members with those fasteners' holes excluded
    (:func:`precis_se.printsolid.printed_solid`'s ``exclude``): a fused
    body keeps no clearance hole for a screw it does not need. Two passes
    because the elision rule reads the joint plan, which reads the member
    roster, which the holes never change."""
    geo = _group_geometry(tree, root, cad_store_reader=cad_store_reader)
    elided = _elisions(tree, geo)
    if elided:
        geo = _group_geometry(
            tree, root, cad_store_reader=cad_store_reader, exclude=frozenset(elided)
        )
    geo.elided = elided
    return geo


def _group_geometry(
    tree: SeTree,
    root: str,
    *,
    cad_store_reader: Store,
    exclude: frozenset[str] = frozenset(),
) -> GroupGeometry:
    root_node = tree.blocks[root]
    root_xform = _world(root_node)
    names, nested = _walk(tree, root)
    members = [
        _member(tree, n, cad_store_reader=cad_store_reader, exclude=exclude)
        for n in names
    ]
    members.extend(
        GroupMember(
            block=n,
            role=NESTED,
            xform=_world(tree.blocks[n]),
            note=f"nested group {n!r} — printed separately, see its own row",
        )
        for n in nested
    )
    findings: list[ValidationIssue] = []
    for m in members:
        findings.extend(m.findings)
    solids: dict[str, _Solid] = {}
    for m in members:
        solid = _solid_for(m, root_xform)
        if solid is not None:
            solids[m.block] = solid
    printed = [m.block for m in members if m.role == PRINTED and m.block in solids]
    stand_ins = [m.block for m in members if m.role == STAND_IN and m.block in solids]
    plan = classify_joints(tree, printed, stand_ins)
    findings.extend(plan.findings)
    return GroupGeometry(
        root=root,
        root_xform=root_xform,
        members=members,
        solids=solids,
        plan=plan,
        findings=findings,
    )


@dataclass(frozen=True)
class _Grid:
    """The root-frame sample grid: vertex-centred like :class:`Field`."""

    origin: np.ndarray
    shape: tuple[int, int, int]
    pitch: float

    @property
    def cells(self) -> int:
        return self.shape[0] * self.shape[1] * self.shape[2]

    def points(self) -> np.ndarray:
        ii, jj, kk = np.meshgrid(*(np.arange(n) for n in self.shape), indexing="ij")
        idx = np.stack([ii.ravel(), jj.ravel(), kk.ravel()], axis=1).astype(float)
        return self.origin[None, :] + idx * self.pitch


def _grid_for(lo: np.ndarray, hi: np.ndarray, pitch: float, pad: float) -> _Grid:
    margin = pad + _GRID_MARGIN_PITCHES * pitch
    origin = lo - margin
    span = (hi + margin) - origin
    n = np.maximum(np.ceil(span / pitch - 1e-9).astype(int) + 1, 2)
    return _Grid(origin=origin, shape=(int(n[0]), int(n[1]), int(n[2])), pitch=pitch)


# --------------------------------------------------------------------------
# the op's pure half
# --------------------------------------------------------------------------


def _length(op: dict[str, Any], key: str, *, allow_zero: bool) -> float | None:
    raw = op.get(key)
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError) as exc:
        raise ManufactureError(
            f"realize(manufacture): {key} must be a length in metres, got {raw!r}"
        ) from exc
    if not math.isfinite(v) or v < 0.0 or (v == 0.0 and not allow_zero):
        bound = ">= 0" if allow_zero else "> 0"
        raise ManufactureError(
            f"realize(manufacture): {key} must be {bound} m, got {raw!r}"
        )
    return v


def is_manufacture_root(tree: SeTree, name: str) -> bool:
    node = tree.blocks.get(name)
    return (
        node is not None
        and is_group_root(tree, name)
        and print_intent(node) == MANUFACTURE
    )


def _resolve_gap(
    tree: SeTree, root_node: SeBlock, op: dict[str, Any], *, needed: bool
) -> tuple[float | None, str, float | None]:
    """``(gap_m, source, floor_mm)`` — the declared ``gap=`` or the house
    ``min_clearance`` default; refused when neither exists and a DOF
    joint needs one."""
    resolved = se_caps.resolve(tree, root_node, CLEARANCE_FIELD)
    floor_mm = None if resolved is None else float(resolved.value)
    given = _length(op, "gap", allow_zero=False)
    if given is not None:
        return given, "given", floor_mm
    if floor_mm is not None:
        return floor_mm / 1000.0, "house", floor_mm
    if needed:
        raise ManufactureError(
            f"realize(manufacture) needs gap= (metres): the group has a DOF "
            f"joint between printed members and the house {CLEARANCE_FIELD} "
            f"figure for {root_node.mode!r} is null (uncalibrated — "
            "se_capabilities.json says why); pass gap=, or "
            f"set_process_override(block={root_node.name!r}, "
            f"field={CLEARANCE_FIELD!r}, value=<mm>)"
        )
    return None, "none", floor_mm


def _resolve_pitch(
    tree: SeTree, root_node: SeBlock, op: dict[str, Any]
) -> tuple[float, str]:
    given = _length(op, "pitch", allow_zero=False)
    if given is not None:
        return given, "given"
    rules = _rules_for(tree, root_node)
    layer = rules.get("layer_height")
    if layer is None:
        raise ManufactureError(
            f"realize(manufacture) needs pitch= (metres): {root_node.mode!r} "
            "resolves no house layer_height to sample the group at"
        )
    return float(layer), "layer_height"


def prepare_manufacture(
    tree: SeTree, op: dict[str, Any], *, cad_store_reader: Store
) -> tuple[str, ManufactureRequest]:
    """Validate ``{"op": "realize", "strategy": "manufacture", "block",
    "gap"?, "fit"?, "blend"?, "pitch"?}`` against the in-memory tree and
    return ``(echo, request)`` — ``request.sync`` says whether the
    handler fuses inline or enqueues. Pure over the store (reads only:
    member designs, component rows, a previous run's cad slugs).

    Refusals (all :class:`ManufactureError`): not a manufacture group
    root, a root bound to a solid of its own, no placeable member, a
    needed ``gap``/``fit`` missing, a grid beyond :data:`MAX_CELLS`."""
    key, node = resolve_realize_target(tree, op)
    if not is_manufacture_root(tree, key):
        raise ManufactureError(
            f"realize(manufacture): {key!r} is not a print group root with "
            f"intent 'manufacture' — set_mode(block={key!r}, mode='fdm/<m>', "
            "intent='manufacture') first (a group is an fdm ancestor block; its "
            "members are the blocks below it)"
        )
    mode = str(node.mode)
    raw_mode = op.get("mode")
    if raw_mode is not None and str(raw_mode).strip() != mode:
        raise ManufactureError(
            f"realize(manufacture): mode={str(raw_mode).strip()!r} disagrees with "
            f"the group root's mode {mode!r} — the root names the process; "
            "set_mode it first, or drop mode= from the realize op"
        )
    if node.bound_kind not in (None, "cad"):
        raise ManufactureError(
            f"realize(manufacture): root {key!r} is bound to a {node.bound_kind} "
            f"({node.bound!r}) — a manufacture root holds the fused result; "
            "set_binding(clear=true) first"
        )
    if (
        node.bound_kind == "cad"
        and node.bound
        and not _is_fused_output(cad_store_reader, node.bound, key)
    ):
        raise ManufactureError(
            f"realize(manufacture): root {key!r} is bound to cad design "
            f"{node.bound!r}, which is not a fused output of this group — a "
            "manufacture root holds the fused result; move the root's own solid "
            "to a child member (add_block parent=…, set_binding) and clear it"
        )
    geo = group_geometry(tree, key, cad_store_reader=cad_store_reader)
    if not geo.solids:
        raise ManufactureError(
            f"realize(manufacture): print group {key!r} places nothing — no "
            "realized fdm member and no stand-in (view='print' "
            f"args={{'block': {key!r}}} says why per member)"
        )
    pitch, pitch_source = _resolve_pitch(tree, node, op)
    dof_printed = bool(geo.plan.eroded)
    gap, gap_source, floor_mm = _resolve_gap(tree, node, op, needed=dof_printed)
    fit = _length(op, "fit", allow_zero=True)
    elided = geo.elided
    cavities = [n for n in geo.stand_ins if n not in elided]
    if cavities and fit is None:
        raise ManufactureError(
            f"realize(manufacture) needs fit= (metres, the radial clearance "
            f"around a bought part's cavity; 0 = the exact stand-in): the group "
            f"has {len(cavities)} bought member(s) to cut a cavity for "
            f"({', '.join(cavities)}) and nothing is defaulted in code"
        )
    blend = _length(op, "blend", allow_zero=True) or 0.0
    if blend and blend < pitch:
        raise ManufactureError(
            f"realize(manufacture): blend={blend:g} m is below the pitch "
            f"({pitch:g} m) — the grid cannot resolve the seam; 0 = plain min"
        )
    box = geo.box()
    assert box is not None
    grid = _grid_for(box[0], box[1], pitch, (fit or 0.0) + 0.5 * pitch)
    if grid.cells > MAX_CELLS:
        raise ManufactureError(
            f"realize(manufacture): pitch {pitch:g} m over the group box gives a "
            f"{grid.shape[0]}x{grid.shape[1]}x{grid.shape[2]} = {grid.cells} "
            f"cell grid, above the {MAX_CELLS} budget — coarsen pitch="
        )
    simp_members = [
        m.block
        for m in geo.members
        if (tree.blocks[m.block].build_frame or {}).get("origin") == "simp"
    ]
    sync = not simp_members and grid.cells <= SYNC_CELL_CAP
    request = ManufactureRequest(
        block=key,
        mode=mode,
        pitch=pitch,
        pitch_source=pitch_source,
        gap=gap,
        gap_source=gap_source,
        gap_floor_mm=floor_mm,
        fit=fit,
        blend=blend,
        sync=sync,
    )
    plan = geo.plan
    bits = [
        f"{len(geo.printed)} printed member(s) in {len(plan.components())} fused "
        f"component(s)",
        f"{len(plan.dof_edges)} DOF joint(s)",
        f"{len(cavities)} cavity(ies)",
        f"{len(elided)} fastener(s) elided",
        f"pitch {pitch:g} m ({pitch_source})",
        f"grid {grid.shape[0]}x{grid.shape[1]}x{grid.shape[2]} = {grid.cells} cells",
    ]
    if gap is not None:
        bits.append(f"gap {gap:g} m ({gap_source})")
    if fit is not None:
        bits.append(f"fit {fit:g} m")
    bits.append(f"blend {blend:g} m" if blend else "blend 0 (plain min)")
    how = (
        "fusing inline"
        if sync
        else (
            f"queued a {JOB_TYPE} job ("
            + (
                f"SIMP member(s) {', '.join(simp_members)}"
                if simp_members
                else f"{grid.cells} cells > {SYNC_CELL_CAP}"
            )
            + f"); {key!r} keeps its previous binding until it lands"
        )
    )
    echo = f"realize({key!r}, strategy='manufacture'): {how} — " + ", ".join(bits)
    return echo, request


def _is_fused_output(cad_store_reader: Store, cad_slug: str, root: str) -> bool:
    """True when ``cad_slug`` is a design this module minted for ``root``
    (its root node is a ``field:`` leaf whose provenance says so)."""
    header = _fused_header(cad_store_reader, cad_slug)
    return header is not None and str(header.get("root") or "") == root


def _fused_header(cad_store_reader: Store, cad_slug: str) -> dict[str, Any] | None:
    get_ref = getattr(cad_store_reader, "get_ref", None)
    if get_ref is None:
        return None
    ref = get_ref(kind="cad", id=cad_slug)
    if ref is None:
        return None
    try:
        spec, _handles = cad_store_reader.cad_load(ref.id)
    except Exception:
        return None
    if not spec.nodes:
        return None
    config = str(spec.nodes[0].config or "")
    if not config.startswith("field:"):
        return None
    try:
        header, _fld = cad_store_reader.get_field(config[len("field:") :])
    except Exception:
        return None
    prov = dict(header.get("provenance") or {})
    if prov.get("source") != JOB_TYPE:
        return None
    return prov


# --------------------------------------------------------------------------
# elision
# --------------------------------------------------------------------------


def _elisions(tree: SeTree, geo: GroupGeometry) -> dict[str, dict[str, Any]]:
    """``{elided bought member: {'stack': [...], 'subject': ..., 'via':
    <screw>}}`` — module docstring's rule."""
    out: dict[str, dict[str, Any]] = {}
    stand_ins = set(geo.stand_ins)
    if not stand_ins:
        return out
    plan = geo.plan
    for res in se_fasten.fasten(tree):
        if res.fastener is None or res.fastener not in stand_ins or res.why_not:
            continue
        printed_stack = [m.block for m in res.members if not m.bought]
        if len(printed_stack) < 2:
            continue
        ids = {plan.fused_id.get(b) for b in printed_stack}
        if None in ids or len(ids) != 1:
            continue
        out[res.fastener] = {
            "stack": printed_stack,
            "subject": res.subject,
            "via": res.fastener,
        }
        for m in res.members:
            if m.bought and m.block in stand_ins and m.form in _STACK_FORMS:
                out.setdefault(
                    m.block,
                    {
                        "stack": printed_stack,
                        "subject": res.subject,
                        "via": res.fastener,
                    },
                )
    return out


def _elision_finding(block: str, info: dict[str, Any]) -> ValidationIssue:
    stack = ", ".join(info["stack"])
    return ValidationIssue(
        rule="fastener_elided",
        subject=block,
        detail=(
            f"joint fused, {block} not needed — {stack} print as one body in "
            f"this group (connect {info['subject']}); the fastener is not exported"
        ),
        severity="info",
    )


# --------------------------------------------------------------------------
# the fuse (pure over the tree + store reads)
# --------------------------------------------------------------------------


@dataclass
class ManufactureSolve:
    """:func:`solve_manufacture`'s answer: the fused field (root frame),
    the per-object meshes it labels, and the JSON-safe summary."""

    field: Field
    summary: dict[str, Any]
    findings: list[ValidationIssue]
    objects: list[tuple[str, np.ndarray, np.ndarray]]


@dataclass
class ManufactureOutcome:
    echo: str
    cad_slug: str
    summary: dict[str, Any]


def group_signature(tree: SeTree, root: str) -> dict[str, Any]:
    """Everything a fuse (or a fused SIMP domain) reads off the tree for
    the group — every member's binding, mode, pose, envelope, build frame;
    every connect among members; the root's pose — as a JSON-safe dict.
    Shared by :func:`inputs_sha` and :func:`precis_se.simp_bridge.
    inputs_sha` (a manufacture root's solve hashes the group, not just
    the root)."""
    node = tree.blocks[root]
    names, nested = _walk(tree, root)
    members = set(names)
    return {
        "root": {"pose": node.pose, "rot": node.rot, "mode": node.mode},
        "members": {
            n: {
                "bound": [b.bound_kind, b.bound],
                "mode": b.mode,
                "pose": b.pose,
                "rot": b.rot,
                "envelope": effective_envelope(tree, b),
                "frame": b.build_frame,
                "template": b.template,
            }
            for n in names
            if (b := tree.blocks.get(n)) is not None
        },
        "nested": nested,
        "connects": sorted(
            [
                c.a_block,
                c.a_port,
                c.b_block,
                c.b_port,
                json.dumps(c.joint, sort_keys=True),
            ]
            for c in tree.connects
            if c.a_block in members and c.b_block in members
        ),
    }


def inputs_sha(tree: SeTree, req: ManufactureRequest) -> str | None:
    """Content hash of :func:`group_signature` plus the request. ONE
    function for the idem key, the staleness check and the report's
    stale-warning. ``None`` when the root is gone."""
    if req.block not in tree.blocks:
        return None
    payload = {"group": group_signature(tree, req.block), "request": req.to_params()}
    text = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _cavity_sdf(solid: _Solid, grid: _Grid, pts: np.ndarray, fit: float) -> np.ndarray:
    """The cavity a bought member cuts, on ``grid``: its stand-in sampled,
    re-distanced and dilated by ``fit`` + half a pitch (``fit == 0``: the
    exact analytic stand-in). Negative = void. One function for the fuse
    (subtraction) and the report (the pause height), so the two can never
    disagree about where the cavity is."""
    raw = solid.sample(pts).reshape(grid.shape)
    if fit <= 0.0:
        return raw
    base = _redistanced(raw <= 0.0, grid, f"stand-in {solid.member.block!r}")
    return np.asarray(
        fieldops.offset(base, -(fit + 0.5 * grid.pitch)).grid, dtype=np.float64
    )


def _redistanced(binary: np.ndarray, grid: _Grid, what: str) -> Field:
    try:
        return fieldops.redistance(binary, grid.pitch, grid.origin, pad=0)
    except ValueError as exc:
        raise ManufactureError(
            f"realize(manufacture): {what} voxelises to {'nothing' if not binary.any() else 'the whole box'} "
            f"at pitch {grid.pitch:g} m — {exc}"
        ) from exc


def _component_field(fused: Field, labels: np.ndarray, label: int) -> Field:
    """The fused field with every OTHER component's inside pushed
    outside: its zero set is this component's surface alone."""
    g = np.asarray(fused.grid, dtype=np.float64)
    other = (labels >= 0) & (labels != label)
    grid = np.where(other, np.maximum(np.abs(g), 0.5 * fused.pitch), g)
    return Field(grid=grid.astype(np.float32), pitch=fused.pitch, origin=fused.origin)


def _mesh_component(
    fused: Field, labels: np.ndarray, label: int, name: str
) -> tuple[np.ndarray, np.ndarray]:
    comp = _component_field(fused, labels, label)
    idx = np.argwhere(labels == label)
    lo = fused.origin + (idx.min(axis=0) - 2) * fused.pitch
    hi = fused.origin + (idx.max(axis=0) + 2) * fused.pitch
    try:
        return field_mesh(comp.distance_local_np, lo, hi, fused.pitch, name=name)
    except FieldMeshError as exc:
        raise ManufactureError(f"realize(manufacture): {exc}") from exc


def label_objects(fused: Field) -> tuple[np.ndarray, int]:
    """The fused field's connected components (6-connectivity)."""
    return fieldops.label_components(np.asarray(fused.grid) <= 0.0)


def solve_manufacture(
    tree: SeTree, req: ManufactureRequest, *, cad_store_reader: Store
) -> ManufactureSolve:
    """Sample, erode, fuse, cut the cavities, label the objects, measure
    the gaps — the store-free half of :func:`run_manufacture` (module
    docstring). Raises :class:`ManufactureError` for what
    :func:`prepare_manufacture` could not see (a member that voxelises
    to nothing at this pitch)."""
    root = req.block
    if not is_manufacture_root(tree, root):
        raise ManufactureError(
            f"realize(manufacture): {root!r} is no longer a manufacture group root"
        )
    geo = group_geometry(tree, root, cad_store_reader=cad_store_reader)
    if not geo.solids:
        raise ManufactureError(
            f"realize(manufacture): print group {root!r} places nothing"
        )
    # The fuse's OWN findings; the member/plan findings (geo.findings) are
    # re-derived by every report and would double up if stored.
    findings: list[ValidationIssue] = []
    plan = geo.plan
    pitch = req.pitch
    half = 0.5 * pitch
    box = geo.box()
    assert box is not None
    grid = _grid_for(box[0], box[1], pitch, (req.fit or 0.0) + half)
    pts = grid.points()

    elided = geo.elided
    for name in sorted(elided):
        findings.append(_elision_finding(name, elided[name]))

    gap = req.gap
    if plan.eroded:
        assert gap is not None
        if req.gap_floor_mm is None:
            findings.append(
                ValidationIssue(
                    rule="in_place_clearance",
                    subject=root,
                    detail=(
                        f"the house {CLEARANCE_FIELD} floor for {req.mode!r} is "
                        f"uncalibrated (null in se_capabilities.json); the gap "
                        f"{gap * 1000:.3g} mm is taken as declared, unchecked"
                    ),
                    severity="info",
                    suggested_fix=(
                        "print a gap ladder and set_process_override(block="
                        f"{root!r}, field={CLEARANCE_FIELD!r}, value=<mm>)"
                    ),
                )
            )
        elif gap * 1000.0 < req.gap_floor_mm - 1e-9:
            findings.append(
                ValidationIssue(
                    rule="in_place_clearance",
                    subject=root,
                    detail=(
                        f"gap {gap * 1000:.3g} mm is below the {CLEARANCE_FIELD} "
                        f"floor {req.gap_floor_mm:g} mm for {req.mode!r} — the "
                        "DOF joints will fuse in the print"
                    ),
                    severity="error",
                    measured=f"{gap * 1000:.3g} mm",
                    expected=f">= {req.gap_floor_mm:g} mm",
                    suggested_fix=f"realize(..., gap={req.gap_floor_mm / 1000.0:g})",
                )
            )

    # --- per printed member: the sampled SDF ------------------------------
    member_sdf: dict[str, np.ndarray] = {
        name: geo.solids[name].sample(pts).reshape(grid.shape) for name in geo.printed
    }
    # --- the in-place gap, seam-local by construction (module docstring):
    # for each DOF pair (A, B), A' = A \ dilate(B, gap/2) and B' = B \
    # dilate(A, gap/2) — the dilation an offset of the PARTNER's
    # re-distanced sample (+ half a pitch for the binarisation), the
    # subtraction a plain max on the grid. Nothing else about A or B moves,
    # so a rigid seam away from the partner stays exactly as sampled.
    # Dilations come from the ORIGINAL samples, applied after all are built.
    carve: dict[str, list[np.ndarray]] = {}
    if plan.printed_dof_edges:
        assert gap is not None
        dilated: dict[str, np.ndarray] = {}
        for name in plan.eroded:
            base = _redistanced(member_sdf[name] <= 0.0, grid, f"member {name!r}")
            dilated[name] = np.asarray(
                fieldops.offset(base, -(0.5 * gap + half)).grid, dtype=np.float64
            )
        for e in plan.printed_dof_edges:
            carve.setdefault(e.a, []).append(dilated[e.b])
            carve.setdefault(e.b, []).append(dilated[e.a])
        for name, partners in carve.items():
            arr = member_sdf[name]
            for d in partners:
                arr = np.maximum(arr, -d)
            if not np.any(arr <= 0.0):
                raise ManufactureError(
                    f"realize(manufacture): carving {name!r} back by gap/2 = "
                    f"{0.5 * gap:g} m (+ half a pitch) from its DOF partner(s) "
                    "erased it — the member lies wholly inside the gap"
                )
            member_sdf[name] = arr

    # --- fusion: smooth-min inside a fused component, hard min across ---
    fused_grid: np.ndarray | None = None
    for comp in plan.components():
        acc: np.ndarray | None = None
        for name in comp:
            acc = (
                member_sdf[name]
                if acc is None
                else smooth_min_np(acc, member_sdf[name], req.blend)
            )
        assert acc is not None
        fused_grid = acc if fused_grid is None else np.minimum(fused_grid, acc)
    if fused_grid is None:
        raise ManufactureError(
            f"realize(manufacture): print group {root!r} has no printed member to "
            "fuse (stand-ins alone are cavities in nothing)"
        )

    # --- cavities ---------------------------------------------------------
    cavities: list[dict[str, Any]] = []
    for name in geo.stand_ins:
        if name in elided:
            continue
        fit = req.fit or 0.0
        cav = _cavity_sdf(geo.solids[name], grid, pts, fit)
        fused_grid = np.maximum(fused_grid, -cav)
        member = geo.solids[name].member
        cavities.append(
            {
                "block": name,
                "source": member.stand_in.source if member.stand_in else "",
                "fit_m": fit,
            }
        )
    for m in geo.members:
        if m.role != STAND_IN and m.note == "no stand-in":
            findings.append(
                ValidationIssue(
                    rule="cavity_missing",
                    subject=m.block,
                    detail=(
                        f"bought member {m.block!r} has no stand-in, so the fused "
                        "part has no cavity for it — the print leaves no room "
                        "for the bought part"
                    ),
                    severity="error",
                )
            )

    fused = Field(grid=fused_grid.astype(np.float32), pitch=pitch, origin=grid.origin)
    if not np.any(fused_grid <= 0.0):
        raise ManufactureError(
            "realize(manufacture): the cavities consumed every printed member — "
            "nothing left to print"
        )

    # --- objects: one per connected component -----------------------------
    labels, count = label_objects(fused)
    inside = {n: member_sdf[n] <= 0.0 for n in geo.printed}
    member_labels: dict[str, set[int]] = {
        n: {int(v) for v in np.unique(labels[inside[n] & (labels >= 0)])}
        for n in geo.printed
    }
    objects: list[tuple[str, np.ndarray, np.ndarray]] = []
    object_rows: list[dict[str, Any]] = []
    for label in range(count):
        holders = sorted(n for n, ls in member_labels.items() if label in ls)
        base_name = "+".join(holders) if holders else f"{root}-{label + 1}"
        name = base_name
        k = 2
        while any(o["name"] == name for o in object_rows):
            name = f"{base_name}-{k}"
            k += 1
        verts, tris = _mesh_component(fused, labels, label, name)
        objects.append((name, verts, tris))
        object_rows.append(
            {
                "name": name,
                "label": label,
                "members": holders,
                "voxels": int(np.count_nonzero(labels == label)),
            }
        )
    # Name order, not label order (a label is the lowest voxel index —
    # whichever body happens to sit at the grid's min corner).
    order = sorted(range(len(object_rows)), key=lambda i: object_rows[i]["name"])
    objects = [objects[i] for i in order]
    object_rows = [object_rows[i] for i in order]

    # --- gaps: min of the other side's re-distanced SDF (exact outside its
    # binarised surface) over this side's object vertices ------------------
    gaps: list[dict[str, Any]] = []
    side_field: dict[str, Field] = {}
    bridged = {e.subject for e in plan.bridged}
    for e in plan.printed_dof_edges:
        if e.subject in bridged:
            continue  # one body by a rigid path: dof_bridged already says so
        shared = member_labels[e.a] & member_labels[e.b]
        if shared:
            gaps.append({"subject": e.subject, "a": e.a, "b": e.b, "measured_m": 0.0})
            findings.append(
                ValidationIssue(
                    rule="in_place_fused",
                    subject=e.subject,
                    detail=(
                        f"{e.a!r} and {e.b!r} ({e.klass}) came out as ONE object "
                        "after erosion — they overlap by more than the gap; "
                        "re-pose or resize one of them"
                    ),
                    severity="error",
                )
            )
            continue
        measured = math.inf
        for this, other in ((e.a, e.b), (e.b, e.a)):
            if other not in side_field:
                side_field[other] = _redistanced(
                    member_sdf[other] <= 0.0, grid, f"member {other!r}"
                )
            other_field = side_field[other]
            for name, verts, _t in objects:
                if any(name == o["name"] and this in o["members"] for o in object_rows):
                    d = other_field.distance_local_np(verts)
                    measured = min(measured, float(d.min()))
        if not math.isfinite(measured):
            continue
        gaps.append({"subject": e.subject, "a": e.a, "b": e.b, "measured_m": measured})
        assert gap is not None
        if measured < gap - 1e-6:
            findings.append(
                ValidationIssue(
                    rule="in_place_gap_short",
                    subject=e.subject,
                    detail=(
                        f"measured separation between {e.a!r} and {e.b!r} is "
                        f"{measured * 1000:.3g} mm, below the declared gap "
                        f"{gap * 1000:.3g} mm"
                    ),
                    severity="warn",
                    measured=f"{measured * 1000:.3g} mm",
                    expected=f">= {gap * 1000:.3g} mm",
                )
            )

    summary: dict[str, Any] = {
        "root": root,
        "mode": req.mode,
        "inputs_sha": inputs_sha(tree, req),
        "pitch_m": pitch,
        "pitch_source": req.pitch_source,
        "gap_m": gap,
        "gap_source": req.gap_source,
        "gap_floor_mm": req.gap_floor_mm,
        "fit_m": req.fit,
        "blend_m": req.blend,
        "grid": list(grid.shape),
        "cells": grid.cells,
        "printed": geo.printed,
        "stand_ins": geo.stand_ins,
        "fused_components": plan.components(),
        "eroded": sorted(plan.eroded),
        "joints": [
            {
                "subject": e.subject,
                "a": e.a,
                "b": e.b,
                "class": e.klass,
                "mechanism": e.mechanism,
                "axis": e.axis,
                "dof": e.dof,
            }
            for e in plan.edges
        ],
        "cavities": cavities,
        "elided": [{"block": n, **elided[n]} for n in sorted(elided)],
        "objects": object_rows,
        "gaps": gaps,
        "volume_m3": float(np.count_nonzero(fused_grid <= 0.0)) * pitch**3,
        "findings": [
            {
                "rule": f.rule,
                "subject": f.subject,
                "detail": f.detail,
                "severity": f.severity,
                "measured": f.measured,
                "expected": f.expected,
                "suggested_fix": f.suggested_fix,
            }
            for f in findings
        ],
        "ran_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    return ManufactureSolve(
        field=fused,
        summary=summary,
        findings=[*geo.findings, *findings],
        objects=objects,
    )


# --------------------------------------------------------------------------
# the store half + the whole run
# --------------------------------------------------------------------------


def _load_live(store: Store, ref_id: int, slug: str) -> SeTree:
    tree = persist.load_tree(store, ref_id)
    tree.own_slug = slug
    tree.foreign = persist.foreign_resolver(store)
    return tree


def _require_fresh(
    tree: SeTree, req: ManufactureRequest, solve: ManufactureSolve, when: str
) -> None:
    now = inputs_sha(tree, req)
    if now is None:
        raise ManufactureStale(
            f"realize(manufacture): root {req.block!r} no longer exists ({when})"
        )
    if now != solve.summary["inputs_sha"]:
        raise ManufactureStale(
            f"realize(manufacture): design changed while fusing ({when}: a member "
            f"of {req.block!r} moved, re-bound or re-jointed since the snapshot) — "
            "the fused field is stale and was NOT bound; re-run realize"
        )


def _record_failure(
    store: Store, ref_id: int, *, cad_slug: str, retired: bool, reason: str
) -> None:
    try:
        with persist.tree_mutation(store, ref_id) as conn:
            row = conn.execute(
                "SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)
            ).fetchone()
            existing = dict(((row[0] if row else None) or {}).get(META_KEY) or {})
            existing["failed"] = {
                "cad": cad_slug,
                "retired": retired,
                "reason": reason[:500],
                "at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
            store.stamp_ref_meta(ref_id, {META_KEY: existing}, conn=conn)
    except Exception:  # pragma: no cover — defensive
        log.warning("se_manufacture: could not record the failed run on ref %s", ref_id)


def realize_manufacture(
    store: Store, ref_id: int, req: ManufactureRequest, solve: ManufactureSolve
) -> ManufactureOutcome:
    """Store the fused field (summary in its provenance), mint the cad
    design rooted at it, then — inside the design's
    :func:`~precis_se.persist.tree_mutation` lock, on a FRESH tree —
    verify nothing moved, bind the ROOT, save, stamp ``meta.manufacture``.
    The same durability order as :func:`precis_se.simp_bridge.
    realize_simp`: a failure after the mint retires the minted design and
    records it under ``meta.manufacture.failed``."""
    from precis_se.handler import _card_text  # cycle: handler -> apply -> here

    ref = store.get_ref(kind="se", id=ref_id)
    if ref is None:
        raise ManufactureError(
            f"realize(manufacture): se design ref {ref_id} not found"
        )
    design_slug = str(ref.slug)
    _require_fresh(_load_live(store, ref_id, design_slug), req, solve, "pre-mint check")

    sha = store.put_field(
        ref_id,
        solve.field,
        provenance={
            "source": JOB_TYPE,
            "se_design": design_slug,
            "root": req.block,
            "summary": solve.summary,
        },
    )
    slug, note = _unique_cad_slug(store, f"{design_slug}-{req.block}-{CAD_SUFFIX}")
    title = f"{req.block} ({design_slug} realize manufacture)"
    spec = SceneSpec(
        nodes=[
            NodeSpec(name="body", op="add", config=f"field:{sha}", component="part")
        ],
        components=["part"],
    )
    s = solve.summary
    card_text = (
        f"{title}: print-in-place fused field at {req.pitch:g} m pitch, "
        f"{len(s['objects'])} object(s), {len(s['cavities'])} cavity(ies), "
        f"{len(s['elided'])} fastener(s) elided"
    )
    cad_ref, _created, _n = store.cad_save(
        slug=slug, title=title, spec=spec, card_text=card_text
    )
    summary = dict(solve.summary)
    summary["cad"] = slug
    summary["field_sha"] = sha
    try:
        with persist.tree_mutation(store, ref_id) as conn:
            tree = _load_live(store, ref_id, design_slug)
            _require_fresh(tree, req, solve, "at bind time")
            node = tree.blocks[req.block]
            previous = node.bound if node.bound_kind == "cad" else None
            apply_ops(
                tree,
                [
                    {
                        "op": "set_binding",
                        "block": req.block,
                        "kind": "cad",
                        "design": slug,
                    }
                ],
            )
            row = conn.execute(
                "SELECT title, meta FROM refs WHERE ref_id = %s", (ref_id,)
            ).fetchone()
            meta_now = dict((row[1] if row else None) or {})
            title_now = (row[0] if row else None) or design_slug
            description = str(meta_now.get("description") or "").strip()
            persist.save_tree(
                store,
                ref_id=ref_id,
                tree=tree,
                card_text=_card_text(title_now, description, tree),
                conn=conn,
            )
            summary["previous_cad"] = previous
            existing = dict(meta_now.get(META_KEY) or {})
            runs = [r for r in (existing.get("runs") or []) if isinstance(r, dict)]
            runs.append(summary)
            existing.pop("failed", None)
            existing.update({"last": summary, "runs": runs})
            store.stamp_ref_meta(ref_id, {META_KEY: existing}, conn=conn)
    except Exception as exc:
        retired = False
        try:
            store.cad_delete(int(cad_ref.id))
            retired = True
        except Exception:  # pragma: no cover — defensive
            log.warning("se_manufacture: could not retire orphaned cad design %s", slug)
        _record_failure(store, ref_id, cad_slug=slug, retired=retired, reason=str(exc))
        raise

    persist.sync_realized_by(store, ref_id, tree)
    try:
        store.add_link(
            src_ref_id=int(cad_ref.id),
            dst_ref_id=int(ref_id),
            relation="derived-from",
            meta={
                "se_manufacture": True,
                "block": req.block,
                "inputs_sha": summary["inputs_sha"],
            },
        )
    except Exception:  # pragma: no cover — defensive, mirrors cad's sync
        log.warning("se_manufacture: derived-from link failed for cad %s", slug)

    errors = [f for f in solve.findings if f.severity == "error"]
    echo = (
        f"realize({req.block!r}, strategy='manufacture'): bound to new cad design "
        f"{slug!r} (root field:{sha[:12]}, {s['grid'][0]}x{s['grid'][1]}x"
        f"{s['grid'][2]} grid at {req.pitch:g} m); {len(s['objects'])} object(s): "
        + ", ".join(o["name"] for o in s["objects"])
        + f"; {len(s['cavities'])} cavity(ies); {len(s['elided'])} fastener(s) elided"
    )
    if s["gaps"]:
        echo += "; gaps " + ", ".join(
            f"{g['a']}–{g['b']} {g['measured_m'] * 1000:.3g} mm" for g in s["gaps"]
        )
    if note:
        echo += f" ({note})"
    if previous:
        echo += (
            f"; previous realization {previous!r} left in place (meta.manufacture.runs)"
        )
    if errors:
        echo += "; ERROR finding(s): " + "; ".join(
            f"{f.rule}: {f.detail}" for f in errors
        )
    return ManufactureOutcome(echo=echo, cad_slug=slug, summary=summary)


def run_manufacture(
    store: Store, ref_id: int, req: ManufactureRequest
) -> ManufactureOutcome:
    """The whole run, callable in-process (the handler's synchronous path
    and the job body alike): snapshot the live tree, fuse OUTSIDE any
    lock, then bind under the per-ref lock after re-checking the inputs."""
    ref = store.get_ref(kind="se", id=ref_id)
    if ref is None:
        raise ManufactureError(
            f"realize(manufacture): se design ref {ref_id} not found"
        )
    tree = _load_live(store, ref_id, str(ref.slug))
    solve = solve_manufacture(tree, req, cad_store_reader=store)
    return realize_manufacture(store, ref_id, req, solve)


# --------------------------------------------------------------------------
# the fused SIMP domain (the simp_bridge hook)
# --------------------------------------------------------------------------


@dataclass
class _KeepIn:
    """The fused group's keep-in before sampling: the member envelopes
    posed in the root frame as one design, its box, and the geometry."""

    geo: GroupGeometry
    design: CadDesign
    lo: np.ndarray
    hi: np.ndarray
    n_envelopes: int


def _keep_in(tree: SeTree, root: str, *, cad_store_reader: Store) -> _KeepIn:
    geo = group_geometry(tree, root, cad_store_reader=cad_store_reader)
    if geo.plan.eroded:
        pairs = ", ".join(e.subject for e in geo.plan.dof_edges)
        raise ManufactureError(
            f"realize(simp) on manufacture root {root!r}: the group keeps a DOF "
            f"between printed members ({pairs}); one SIMP solve is one body — "
            "split them into their own groups, or set the joint rigid"
        )
    root_xform = geo.root_xform
    design = CadDesign()
    los: list[np.ndarray] = []
    his: list[np.ndarray] = []
    for m in geo.members:
        if m.role not in (PRINTED, UNREALIZED):
            continue  # the keep-in is the printed members' envelopes
        env = effective_envelope(tree, tree.blocks[m.block])
        if not env:
            continue
        try:
            prim = cad_dsl.build_config(env)
        except cad_dsl.DslError:
            continue
        to_root = root_xform.inverse().compose(m.xform)
        expr = design.prim(m.block, prim, to_root)
        design.add_component(m.block, expr)
        lo, hi = (np.asarray(v, dtype=float) for v in cad_bulk.expr_aabb(design, expr))
        los.append(lo)
        his.append(hi)
    if not los:
        raise ManufactureError(
            f"realize(simp) on manufacture root {root!r}: no printed member has an "
            "envelope — set_envelope the members (the union is the keep-in)"
        )
    return _KeepIn(
        geo=geo,
        design=design,
        lo=np.min(los, axis=0),
        hi=np.max(his, axis=0),
        n_envelopes=len(los),
    )


def simp_box(
    tree: SeTree, root: str, *, cad_store_reader: Store
) -> tuple[np.ndarray, np.ndarray]:
    """The fused keep-in's root-frame box — what
    :func:`precis_se.simp_bridge.prepare_simp` sizes the element budget
    from without sampling anything. Same refusals as :func:`simp_domain`."""
    keep = _keep_in(tree, root, cad_store_reader=cad_store_reader)
    return keep.lo, keep.hi


def simp_domain(
    tree: SeTree,
    root: str,
    pitch: float,
    *,
    cad_store_reader: Store,
    fit: float | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, str]:
    """``(domain, origin, lo, hi, note)`` — the fused group's keep-in for
    :func:`precis_se.simp_bridge.solve_simp`: the union of the member
    ENVELOPES in the root frame (element-centred at ``pitch``), minus
    each stand-in cavity (re-distanced, dilated by ``fit`` + half a
    pitch when ``fit`` is given; the bare stand-in otherwise, said so).
    Refuses a group with a DOF joint between printed members: one solve
    is one body."""
    keep = _keep_in(tree, root, cad_store_reader=cad_store_reader)
    geo, design, lo, hi = keep.geo, keep.design, keep.lo, keep.hi
    ext = np.maximum(hi - lo, 0.0)
    shape = tuple(
        int(x) for x in np.maximum(np.ceil(ext / pitch - 1e-9), 1).astype(int)
    )
    origin = lo + 0.5 * pitch
    ii, jj, kk = np.meshgrid(*(np.arange(n) for n in shape), indexing="ij")
    centres = (
        origin[None, :]
        + np.stack([ii.ravel(), jj.ravel(), kk.ravel()], axis=1).astype(float) * pitch
    )
    whole = design.whole()
    domain = (np.asarray(component_sdf_np(design, whole, centres)) <= 0.0).reshape(
        shape
    )
    notes = [
        f"keep-in = union of {keep.n_envelopes} member envelope(s) in {root!r}'s frame"
    ]
    n_cav = 0
    for name in geo.stand_ins:
        solid = geo.solids[name]
        raw = solid.sample(centres).reshape(shape)
        if fit is not None and fit > 0.0:
            try:
                base = fieldops.redistance(raw <= 0.0, pitch, origin, pad=0)
            except ValueError:
                continue
            cav = np.asarray(fieldops.offset(base, -(fit + 0.5 * pitch)).grid) <= 0.0
        else:
            cav = raw <= 0.0
        domain &= ~cav
        n_cav += 1
    if n_cav:
        notes.append(
            f"minus {n_cav} stand-in cavity(ies)"
            + (
                f" dilated by fit {fit:g} m (+ half a pitch)"
                if fit
                else " (bare stand-in solids — no fit= given)"
            )
        )
    return domain, origin, lo, hi, "; ".join(notes)


# --------------------------------------------------------------------------
# the report (view='print' / 'fab') + export
# --------------------------------------------------------------------------


@dataclass
class ManufactureDetail:
    """The ``manufacture`` half of a :class:`~precis_se.printgroup.
    GroupPrintReport` — rendered by the handler, exported by
    :func:`write_mesh`."""

    realized: bool
    cad_slug: str | None
    summary: dict[str, Any]
    plan: JointPlan
    #: ``(name, verts, tris)`` per object, WORLD frame, metres.
    objects: list[tuple[str, np.ndarray, np.ndarray]]
    #: ``(block, pause height m above the bed, source)`` per cavity, in the
    #: chosen frame; empty until a frame is chosen.
    pauses: list[tuple[str, float, str]]
    stale: bool = False

    @property
    def elided(self) -> list[str]:
        return [str(e["block"]) for e in self.summary.get("elided") or []]

    @property
    def cavities(self) -> list[dict[str, Any]]:
        return list(self.summary.get("cavities") or [])

    @property
    def gaps(self) -> list[dict[str, Any]]:
        return list(self.summary.get("gaps") or [])


def _issue_from(row: dict[str, Any]) -> ValidationIssue:
    return ValidationIssue(
        rule=str(row.get("rule") or ""),
        subject=str(row.get("subject") or ""),
        detail=str(row.get("detail") or ""),
        severity=str(row.get("severity") or "info"),
        measured=row.get("measured"),
        expected=row.get("expected"),
        suggested_fix=row.get("suggested_fix"),
    )


def _teardrop_findings(plan: JointPlan, down: Vec3, root: str) -> list[ValidationIssue]:
    """The horizontal-bore rule (module docstring)."""
    out: list[ValidationIssue] = []
    d = as_vec3(down)
    d = d / (np.linalg.norm(d) or 1.0)
    for e in plan.dof_edges:
        if e.klass not in AXIS_CLASSES:
            continue
        if e.axis is None:
            out.append(
                ValidationIssue(
                    rule="overhang",
                    subject=e.subject,
                    detail=(
                        f"{e.klass} joint between {e.a!r} and {e.b!r} declares no "
                        "axis — whether its bore prints horizontal (a ceiling "
                        "under the 45° rule) cannot be judged"
                    ),
                    severity="info",
                    suggested_fix="set_joint(..., joint={'class': ..., 'axis': [...]})",
                )
            )
            continue
        a = np.asarray(e.axis, dtype=float)
        n = float(np.linalg.norm(a))
        if n == 0.0:
            continue
        a = a / n
        from_plate = math.degrees(math.asin(min(1.0, abs(float(a @ d)))))
        if from_plate <= TEARDROP_ANGLE_DEG + 1e-9:
            out.append(
                ValidationIssue(
                    rule="overhang",
                    subject=e.subject,
                    detail=(
                        f"the {e.klass} bore between {e.a!r} and {e.b!r} (axis "
                        f"{format_down(as_vec3(e.axis))}) lies {from_plate:.0f}° "
                        f"from the build plate in group {root!r}'s frame — its "
                        "ceiling is an unsupported overhang; the kernel has no "
                        "teardrop/diamond section yet, so this is reported, not fixed"
                    ),
                    severity="warn",
                    measured=f"{from_plate:.0f}° from the plate",
                    expected=(
                        f"> {TEARDROP_ANGLE_DEG:g}° (a vertical bore), or a "
                        "teardrop section"
                    ),
                    suggested_fix=(
                        f"set_build_frame(block={root!r}, down=…) with the bore "
                        "axis vertical, or bridge-close the bore in the cad design"
                    ),
                )
            )
    return out


def _stored_field(
    cad_store_reader: Store, root_node: SeBlock, root: str
) -> tuple[str, dict[str, Any], Field] | None:
    """``(cad slug, summary, field)`` when the root is bound to a fused
    output of this group, else ``None``."""
    if root_node.bound_kind != "cad" or not root_node.bound:
        return None
    ref = cad_store_reader.get_ref(kind="cad", id=root_node.bound)
    if ref is None:
        return None
    try:
        spec, _h = cad_store_reader.cad_load(ref.id)
    except Exception:
        return None
    if not spec.nodes or not str(spec.nodes[0].config or "").startswith("field:"):
        return None
    try:
        header, fld = cad_store_reader.get_field(str(spec.nodes[0].config)[6:])
    except Exception:
        return None
    prov = dict(header.get("provenance") or {})
    if prov.get("source") != JOB_TYPE or prov.get("root") != root:
        return None
    return str(root_node.bound), dict(prov.get("summary") or {}), fld


def report_for(tree: SeTree, root: str, *, cad_store_reader: Store) -> GroupPrintReport:
    """The manufacture group's report: B1's member classification, the
    joint plan, and — once realized — the stored fused field's objects at
    the group frame (root pin > SIMP member > search on the fused mesh),
    the per-object frame findings, the teardrop rule, the cavity pause
    heights, the measured gaps and the elisions (module docstring)."""
    root_node = tree.blocks[root]
    mode = root_node.mode or ""
    geo = group_geometry(tree, root, cad_store_reader=cad_store_reader)
    findings = list(geo.findings)
    plan = geo.plan
    rules = _rules_for(tree, root_node)
    policy = se_caps.orientation_policy(mode) or {}
    pitch = rules.get("layer_height")
    stored = _stored_field(cad_store_reader, root_node, root)

    def _report(
        detail: ManufactureDetail,
        *,
        candidates: list[Any] | None = None,
        chosen_down: Vec3 | None = None,
        chosen_score: Any = None,
        pinned: bool = False,
        best_other: str | None = None,
        search_skipped: str | None = None,
    ) -> GroupPrintReport:
        return GroupPrintReport(
            root=root,
            intent=MANUFACTURE,
            mode=mode,
            members=geo.members,
            candidates=list(candidates or []),
            chosen_down=chosen_down,
            chosen_score=chosen_score,
            pinned=pinned,
            best_other=best_other,
            search_skipped=search_skipped,
            pitch=pitch,
            findings=findings,
            manufacture=detail,
        )

    elided = geo.elided
    plan_summary: dict[str, Any] = {
        "fused_components": plan.components(),
        "joints": [
            {"subject": e.subject, "a": e.a, "b": e.b, "class": e.klass, "dof": e.dof}
            for e in plan.edges
        ],
        "eroded": sorted(plan.eroded),
        "cavities": [
            {"block": n, "source": geo.solids[n].member.note}
            for n in geo.stand_ins
            if n not in elided
        ],
        "elided": [{"block": n, **info} for n, info in sorted(elided.items())],
    }
    if stored is None:
        findings.append(
            ValidationIssue(
                rule="manufacture_unrealized",
                subject=root,
                detail=(
                    f"print group {root!r} (intent manufacture) has no fused "
                    "realization yet — the plan above is what realize will do"
                ),
                severity="info",
                suggested_fix=(
                    f"realize(block={root!r}, strategy='manufacture', gap=<m>, "
                    "fit=<m>, blend=<m>?)"
                ),
            )
        )
        return _report(
            ManufactureDetail(
                realized=False,
                cad_slug=None,
                summary=plan_summary,
                plan=plan,
                objects=[],
                pauses=[],
            )
        )

    cad_slug, summary, fused = stored
    for row in summary.get("findings") or []:
        findings.append(_issue_from(row))
    stale = False
    req = ManufactureRequest(
        block=root,
        mode=str(summary.get("mode") or mode),
        pitch=float(summary.get("pitch_m") or (pitch or 0.0) or 1.0),
        pitch_source=str(summary.get("pitch_source") or "given"),
        gap=summary.get("gap_m"),
        gap_source=str(summary.get("gap_source") or "none"),
        gap_floor_mm=summary.get("gap_floor_mm"),
        fit=summary.get("fit_m"),
        blend=float(summary.get("blend_m") or 0.0),
    )
    if inputs_sha(tree, req) != summary.get("inputs_sha"):
        stale = True
        findings.append(
            ValidationIssue(
                rule="manufacture_stale",
                subject=root,
                detail=(
                    f"the fused design {cad_slug!r} was realized from a different "
                    f"group (a member of {root!r} moved, re-bound or re-jointed "
                    "since) — the export below is the OLD fuse"
                ),
                severity="warn",
                suggested_fix=f"realize(block={root!r}, strategy='manufacture', ...)",
            )
        )
    if not manifold_available():
        raise PrintUnsupported(
            "view='print' on a group needs the manifold3d backend (core "
            "dependency — a broken venv?)"
        )
    labels, count = label_objects(fused)
    rows = list(summary.get("objects") or [])
    named = {int(r["label"]): str(r["name"]) for r in rows if "label" in r}
    objects: list[tuple[str, np.ndarray, np.ndarray]] = []
    xf = geo.root_xform
    for label in range(count):
        name = named.get(label, f"{root}-{label + 1}")
        verts, tris = _mesh_component(fused, labels, label, name)
        objects.append((name, verts @ xf.R.T + xf.t, tris))
    objects.sort(key=lambda o: o[0])
    union = (
        np.vstack([v for _n, v, _t in objects]),
        np.vstack(
            [
                t + off
                for (_n, _v, t), off in zip(
                    objects,
                    np.cumsum([0, *[len(v) for _n, v, _t in objects[:-1]]]),
                    strict=True,
                )
            ]
        ),
    )
    loads = group_loads(tree, geo.members)
    choice = choose_frame(
        tree, root_node, geo.members, union, rules, policy, loads, findings
    )
    down = choice.chosen_down
    pauses: list[tuple[str, float, str]] = []
    if down is not None:
        for name, verts, tris in objects:
            score = (
                cad_printability.score((verts, tris), down, rules, policy, loads)
                if policy
                else None
            )
            findings.extend(
                frame_findings(
                    name,
                    (verts, tris),
                    down,
                    rules,
                    score=score,
                    best_other=choice.best_other if name == objects[0][0] else None,
                )
            )
        findings.extend(_teardrop_findings(plan, down, root))
        # Heights the way the export lays the objects down
        # (rotate_all_to_frame): the most-down point of the union is the
        # bed, height = distance above it along -down.
        d = as_vec3(down)
        d = d / (np.linalg.norm(d) or 1.0)
        floor = max(float((v @ d).max()) for _n, v, _t in objects)
        cav_rows = list(summary.get("cavities") or [])
        if cav_rows:
            # The cavity's top from the SAMPLED cavity field on the stored
            # grid (the same _cavity_sdf the fuse subtracted): the highest
            # void cell centre along -down, plus half a pitch — exact to the
            # grid in any frame; an AABB would over-estimate a rotated
            # member and fire the pause after the cavity has closed.
            grid = _Grid(
                origin=np.asarray(fused.origin), shape=fused.shape, pitch=fused.pitch
            )
            pts = grid.points()
            world_pts = pts @ xf.R.T + xf.t
            height = floor - (world_pts @ d)  # per cell, above the bed
            for cav in cav_rows:
                block = str(cav["block"])
                solid = geo.solids.get(block)
                if solid is None:
                    continue
                void = (
                    _cavity_sdf(solid, grid, pts, float(cav.get("fit_m") or 0.0)) < 0.0
                )
                if not void.any():
                    continue
                top = float(height[void.ravel()].max()) + 0.5 * fused.pitch
                pauses.append((block, top, str(cav.get("source") or "")))
    for m in geo.members:
        if m.role == PRINTED and m.printed is not None:
            findings.extend(
                frame_free_findings(
                    tree,
                    tree.blocks[m.block],
                    m.block,
                    m.printed,
                    rules,
                    fused=plan.fused,
                )
            )
    detail = ManufactureDetail(
        realized=True,
        cad_slug=cad_slug,
        summary=summary,
        plan=plan,
        objects=objects,
        pauses=pauses,
        stale=stale,
    )
    return _report(
        detail,
        candidates=choice.candidates,
        chosen_down=down,
        chosen_score=choice.chosen_score,
        pinned=choice.pinned,
        best_other=choice.best_other,
        search_skipped=choice.search_skipped,
    )


def write_mesh(report: GroupPrintReport, out_path: str | Path) -> Path:
    """One 3MF, one object per connected component of the fused field
    (world pose, rotated so the group's build-down is ``-z``, one shared
    bed offset, millimetres)."""
    detail: ManufactureDetail | None = report.manufacture
    if detail is None or not detail.realized or not detail.objects:
        raise ExportError(
            f"print group {report.root!r} (intent manufacture) has no fused "
            f"realization to export — realize(block={report.root!r}, "
            "strategy='manufacture', ...) first"
        )
    if report.chosen_down is None:
        raise ExportError(
            f"print group {report.root!r} has no build frame to export in"
        )
    if detail.plan.bridged:
        pairs = ", ".join(f"{e.a}–{e.b} ({e.klass})" for e in detail.plan.bridged)
        raise ExportError(
            f"print group {report.root!r} is not print-in-place: {pairs} bridged "
            "by a rigid path (dof_bridged) — the export is refused until the path "
            "is broken; view='print' args={'block': ...} has the finding"
        )
    names = [n for n, _v, _t in detail.objects]
    verts = [v * _MM_PER_M for _n, v, _t in detail.objects]
    tris = [t for _n, _v, t in detail.objects]
    framed = rotate_all_to_frame(verts, report.chosen_down)
    out = Path(out_path)
    _write_3mf(out, list(zip(names, framed, tris, strict=True)))
    return out


__all__ = [
    "CAD_SUFFIX",
    "CLEARANCE_FIELD",
    "JOB_TYPE",
    "MAX_CELLS",
    "META_KEY",
    "RIGID_CLASSES",
    "SYNC_CELL_CAP",
    "TEARDROP_ANGLE_DEG",
    "GroupGeometry",
    "JointEdge",
    "JointPlan",
    "ManufactureDetail",
    "ManufactureError",
    "ManufactureOutcome",
    "ManufactureRequest",
    "ManufactureSolve",
    "ManufactureStale",
    "classify_joints",
    "group_geometry",
    "inputs_sha",
    "is_manufacture_root",
    "label_objects",
    "prepare_manufacture",
    "realize_manufacture",
    "report_for",
    "run_manufacture",
    "simp_domain",
    "solve_manufacture",
    "write_mesh",
]
