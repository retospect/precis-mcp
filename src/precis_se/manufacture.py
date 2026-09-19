"""``intent='manufacture'`` — print-in-place: one mixed analytic+field
root per print group (docs/backlog/structural-solution-space.md §Slice 4
bridge, the print ``intent`` table's second row; round B2, the mixed
root of 2026-09-19).

A ``model`` group (:mod:`precis_se.printgroup`) prints every member as
its own object and every bought part as a stand-in. A **manufacture**
group is the real part: its printed members become ONE cad design in the
group root's frame whose root is an ordinary CSG expression — every
member's own node tree placed at its pose, analytic and exact, with a
sampled ``field:`` leaf ONLY where a field op is required — built by
CSG/field ops only; no mesh is ever operated on (Reto's rules of
2026-09-18/19, in force here: analytic geometry stays analytic wherever
no field op is needed; offsets only on a re-distanced field; no work on
the mesh, ever):

- **Joint classification** (:func:`classify_joints`). Every connect
  between two members of this group (B1's nesting rule decides
  membership) is *rigid* — its kinematic class keeps no DOF
  (:data:`RIGID_CLASSES`: ``rigid``/``captive``/``axial``, and a connect
  with no ``joint`` at all, which is fused and said so) — or *DOF*
  (:data:`precis_se.drc._MOVING_CLASSES`). Rigid connects between two
  printed members put them in one **fused component** (union-find);
  a connect to a bought member fuses nothing.
- **The root expression** (:func:`solve_manufacture`). A printed member
  with no DOF partner contributes its bound design's OWN node tree,
  placed in the root frame at its world pose (:func:`precis.cad.scene.
  placed_nodes` — the ``use`` instancing mechanism on its own, nodes
  ``<member>.<node>`` in components ``<member>.<component>``): its holes,
  their printed-hole compensation and every other sub-pitch feature are
  exactly what the store holds — ``analytic``. Only a member that needs
  a field op becomes a ``field:`` leaf (below). The design's per-
  component fold is a chain (``add``/``cut``/``intersect`` in node order,
  no nesting), so how members share components decides the semantics:
- **Fusion.** With ``blend=0`` (the default, a plain ``min``) every
  member keeps its own component(s) and the design's whole is their hard
  union — exactly the min-union of the member solids, cuts included.
  With ``blend>0`` (metres — :func:`precis.cad.fold.smooth_min_np`, the
  DSL's ``blend:<len>``, a fillet-*like* seam, not an exact radius) the
  members of ONE fused component chain into one cad component named
  ``<a>+<b>+…`` and each member after the first enters on its base
  node's ``blend:``; the chain fold means a later member's ``cut`` nodes
  also cut earlier members where the tools overlap them (the DSL has no
  nested union), which the report states. Different fused components
  are always different cad components (hard min).
- **In-place gap — seam-local by construction, one field per eroded
  member.** For each DOF pair of printed members ``(A, B)``: ``A' = A \\
  dilate(B, gap/2)`` and ``B' = B \\ dilate(A, gap/2)`` — computed on a
  grid sized to A's OWN root-frame AABB (+ the dilation radius + a
  margin) at the group pitch, its origin snapped onto the export lattice
  (below): A's exact SDF sampled there, the dilation :func:`precis.cad.
  fieldops.offset` on the PARTNER's re-distanced sample (never the mesh,
  never the raw CSG values), the subtraction a max on the grid; the
  result is stored with ``put_field`` (provenance names the member) and
  enters the root as ONE ``field:<sha>`` leaf for that member. Nothing
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
- **Cavities.** A bought member stays bought: its stand-in solid (B1's
  :func:`precis_se.printgroup.stand_in_for`, catalog part or spec dims)
  is subtracted from every component whose box it meets. ``fit=``
  (metres — required whenever the group has a cavity to cut, no
  default) is the radial clearance: ``fit == 0`` with a stand-in made of
  ``add`` nodes only cuts those nodes analytically at their pose (the
  exact stand-in); ``fit > 0`` is a field op — the stand-in sampled on
  its own lattice-snapped grid, re-distanced, dilated by ``fit`` (+ half
  a pitch), stored, and cut as one ``field:`` leaf (growing a box's or a
  cylinder's parameters by ``fit`` is NOT its Minkowski dilation, so
  that shortcut is never taken); a ``fit == 0`` stand-in that carries its
  own ``cut``/``intersect`` nodes (a bearing's bore) is a field leaf too
  — the bare sample, no offset — because a chain of cuts cannot express
  a composite tool. The report says which cavities are field-backed and
  why. No insertion-path search: the report names each cavity's
  **top-layer height** in the chosen build frame as the mid-print
  **pause** height — from the cavity's own field leaf, or the analytic
  stand-in sampled on the export lattice (grid-exact either way) — the
  ``bambuuzle`` rung of the hand-off inserts the part there. A bought
  member with no stand-in is a ``cavity_missing`` error on top of B1's
  ``no_stand_in``.
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
  suffix on collision), the mixed root above in the root frame (metres;
  every ``field:`` leaf a chunk on the se ref, several per ref), bound to
  the ROOT block the way :func:`precis_se.simp_bridge.realize_simp`
  binds — inside the per-ref :func:`~precis_se.persist.tree_mutation`
  lock, on a fresh tree, after the inputs hash (:func:`inputs_sha`)
  proves nothing moved; the run summary on the se ref's
  ``meta.manufacture`` (``last`` + ``runs``) and on the cad design's own
  ``meta.se_manufacture`` (so ``view='print'`` needs no se-ref lookup —
  a root with no field leaf has no provenance header to carry it); each
  minted design linked ``derived-from`` the se design; a re-run mints a
  sibling and switches the root's one binding. The root's own solid, if
  it had one, is refused — a manufacture root holds the fused result
  (move the solid to a child member).
- **Export lattice and objects.** The ONE group-wide grid left is the
  EXPORT lattice — :func:`precis.cad.fieldmesh.field_grid` over the
  printed members' root-frame box (+ ``blend/2``) at the group pitch,
  the grid the narrow-band marching cubes every field-backed design
  exports through would use. At realize time the whole root's exact SDF
  is sampled there once (:func:`~precis.cad.fieldmesh.sample_grid`) for
  ONE purpose: the sign field is labelled (:func:`precis.cad.fieldops.
  label_components`, 6-connectivity) into **connected components** —
  the objects — each named by the members whose material it holds (a
  DOF-separated pair is two objects, a fused pair one). It is a
  geometry-free grid: analytic members cost export cells only. Every
  per-member/per-cavity field grid snaps onto this lattice, so a leaf's
  stored samples ARE the export's vertex values. **Each object is then
  meshed as the exact fold of its OWN components** — its holders' node
  trees and leaves, every cavity cut inside them — on its own box snapped
  to the lattice (:func:`precis.cad.export.object_meshes`, the same call
  the generic cad 3MF export of the ``-mfg`` design makes: the split is
  a property of the design, ``meta.export_objects``). Objects are
  disjoint by construction, so the union of the per-object folds is the
  root and no vertex value is ever masked or invented; an analytic
  member's mesh vertices interpolate its EXACT distances, so a hole's
  compensated diameter survives a pitch far coarser than the feature. A
  member the cavities cut into several pieces is a ``member_split``
  error (the per-object fold cannot keep one piece). The measured
  **gap** per DOF joint is the minimum over the object's mesh vertices of
  the OTHER side's re-distanced eroded leaf (exact outside), measured at
  realize time on the same meshes the export writes.

**Op shape**: ``realize(block=<group root>, strategy='manufacture',
gap=?, fit=?, blend=?, pitch=?)``. ``pitch`` (metres) defaults to the
root mode's house ``layer_height`` — the group's export pitch. Cells are
counted as the export grid plus every per-member and per-cavity field
grid. The op runs **synchronously** when the group has no SIMP-realized
member and that total is under :data:`SYNC_CELL_CAP`; otherwise it
enqueues an ``se_manufacture`` job (:mod:`precis_se.manufacture_job` —
a job type of its own rather than a mode on ``se_simp``, whose params
schema is closed and SIMP-shaped). An export grid above
:data:`MAX_CELLS` is refused with the count and the pitch that would
fit.

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
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from precis.cad import bulk as cad_bulk
from precis.cad import dsl as cad_dsl
from precis.cad import fieldops
from precis.cad import printability as cad_printability
from precis.cad.export import (
    _MM_PER_M,
    EXPORT_LATTICE_KEY,
    EXPORT_OBJECTS_KEY,
    ExportError,
    _write_3mf,
    manifold_available,
    object_meshes,
)
from precis.cad.fieldmesh import FieldGrid, FieldMeshError, field_grid, sample_grid
from precis.cad.graph import Design as CadDesign
from precis.cad.primitives import Field
from precis.cad.printability import rotate_all_to_frame
from precis.cad.relate import component_sdf_np
from precis.cad.scene import (
    NodeSpec,
    SceneError,
    SceneSpec,
    build_design,
    placed_nodes,
)
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
#: Grid cells (the export grid plus every per-member/per-cavity field
#: grid) under which the op fuses inline — a re-distance per offset
#: member is three vectorised EDT passes over its grid, and at this size
#: the whole fuse is seconds, not the minutes an MCP call must never
#: block on. Above it the op enqueues.
SYNC_CELL_CAP = 500_000
#: Export-grid cells the job will take at all; beyond it the op refuses
#: with the count and the pitch that would fit (memory: several float64
#: arrays of this size live at once while sampling and labelling).
MAX_CELLS = 8_000_000
#: Voxels of empty margin around a per-member / per-cavity field grid,
#: beyond the dilation radius it must contain.
_GRID_MARGIN_PITCHES = 2
#: What a printed member or a cavity contributes to the root expression
#: — the ``form`` column of every render (module docstring).
FORM_ANALYTIC = "analytic"
FORM_GAP = "field (gap)"
FORM_CAVITY_FIT = "field (cavity fit)"
FORM_CAVITY_SHAPE = "field (cavity shape)"
#: The cad ref ``meta`` key an output design carries its run under —
#: ``{'source': JOB_TYPE, 'se_design', 'root', 'summary'}``.
CAD_META_KEY = "se_manufacture"
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
    fused root belongs to a group that no longer exists in that form.
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

    @property
    def to_root(self) -> Transform:
        """Member-local → root frame: the pose its nodes are placed under."""
        return self.to_local.inverse()

    @property
    def spec(self) -> SceneSpec | None:
        """The member's node tree (a printed member's cut solid, a bought
        member's stand-in) — what :func:`~precis.cad.scene.placed_nodes`
        inlines into the root."""
        m = self.member
        if m.role == PRINTED and m.printed is not None:
            return m.printed.spec
        return m.spec

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

    def printed_box(self) -> tuple[np.ndarray, np.ndarray] | None:
        """The root-frame box of the PRINTED members alone — what the
        export grid covers (cavities only remove material)."""
        solids = [self.solids[n] for n in self.printed]
        if not solids:
            return None
        lo = np.min([s.lo for s in solids], axis=0)
        hi = np.max([s.hi for s in solids], axis=0)
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
    """A root-frame sample grid: vertex-centred like :class:`Field`."""

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


@dataclass(frozen=True)
class _Lattice:
    """The export grid (:func:`precis.cad.fieldmesh.field_grid` over
    ``[lo, hi]``, root frame) — the one group-wide grid. Every per-member
    and per-cavity field grid snaps its origin onto this lattice
    (:func:`_grid_for`) so a stored leaf's samples are exactly the export's
    fine-vertex values: no trilinear resample between the two."""

    grid: FieldGrid
    lo: np.ndarray
    hi: np.ndarray

    @property
    def pitch(self) -> float:
        return self.grid.pitch


def _export_box(
    geo: GroupGeometry, blend: float
) -> tuple[np.ndarray, np.ndarray] | None:
    """The printed members' root-frame box padded by ``blend/2`` (a
    smooth-min adds material only inside the seam, never more than ``k/4``
    beyond the hard union — the same pad :mod:`precis.cad.export` uses)."""
    box = geo.printed_box()
    if box is None:
        return None
    pad = 0.5 * blend
    return box[0] - pad, box[1] + pad


def _lattice(geo: GroupGeometry, pitch: float, blend: float) -> _Lattice:
    box = _export_box(geo, blend)
    if box is None:
        raise ManufactureError(
            f"realize(manufacture): print group {geo.root!r} has no printed member "
            "to export (stand-ins alone are cavities in nothing)"
        )
    try:
        grid = field_grid(box[0], box[1], pitch, name=f"group {geo.root!r}")
    except FieldMeshError as exc:
        raise ManufactureError(f"realize(manufacture): {exc}") from exc
    return _Lattice(grid=grid, lo=box[0], hi=box[1])


def _grid_for(
    lo: np.ndarray, hi: np.ndarray, margin: float, lattice: _Lattice
) -> _Grid:
    """A grid covering ``[lo - margin, hi + margin]`` at the lattice pitch,
    its origin on the lattice (``anchor + k · pitch``, ``k`` integer)."""
    pitch = lattice.pitch
    anchor = np.asarray(lattice.grid.origin, dtype=float)
    origin = anchor + np.floor((lo - margin - anchor) / pitch + 1e-9) * pitch
    span = (hi + margin) - origin
    n = np.maximum(np.ceil(span / pitch - 1e-9).astype(int) + 1, 2)
    return _Grid(origin=origin, shape=(int(n[0]), int(n[1]), int(n[2])), pitch=pitch)


def _fit_pitch(pitch: float, cells: int) -> float:
    """The pitch that brings a ``cells``-cell grid under :data:`MAX_CELLS`."""
    return pitch * (cells / MAX_CELLS) ** (1.0 / 3.0) * 1.02


# --------------------------------------------------------------------------
# the parts of the root: analytic node trees and the field leaves
# --------------------------------------------------------------------------


@dataclass
class _Part:
    """One printed member's or one cavity's contribution to the root
    expression: its nodes (an analytic member's placed tree; a cut per
    stand-in node; or the single ``field:<sha>`` node) and, for a field
    form, the grid to store. ``sha`` is the content address the store
    will return — computed here from the same payload, so the solve can
    write the node tree before anything is stored."""

    block: str
    kind: str  # "member" | "cavity"
    form: str  # FORM_*
    nodes: list[NodeSpec]
    fld: Field | None = None
    sha: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)
    grid: tuple[int, int, int] | None = None
    #: Field-grid cells (0 for an analytic part).
    cells: int = 0
    #: Root-frame box the part occupies (a cavity's, padded by its fit).
    lo: np.ndarray | None = None
    hi: np.ndarray | None = None

    def row(self) -> dict[str, Any]:
        return {
            "block": self.block,
            "kind": self.kind,
            "form": self.form,
            "sha": self.sha,
            "grid": list(self.grid) if self.grid else None,
            "cells": self.cells,
            "nodes": [n.name for n in self.nodes],
        }


#: A stored leaf sample that is exactly 0 (a lattice vertex ON the carve
#: surface — ``offset`` lands there whenever the partner is a whole number
#: of voxels away) is stored as this many pitches outside instead: the
#: mesher's own convention for an exact zero (:mod:`precis.cad.fieldmesh`
#: step 3), applied at storage so the sign is the same however a query
#: lands relative to the lattice — the generic mm export of the design
#: reads the same leaf through a scaled frame.
_ON_SURFACE_PITCHES = 1e-6


def _leaf(arr: np.ndarray, grid: _Grid) -> Field:
    """A stored field leaf from root-frame samples on ``grid``."""
    vals = np.where(arr == 0.0, _ON_SURFACE_PITCHES * grid.pitch, arr)
    return Field(grid=vals.astype(np.float32), pitch=grid.pitch, origin=grid.origin)


def _field_sha(fld: Field, provenance: dict[str, Any]) -> str:
    """What ``Store.put_field(fld, provenance=)`` will return — the sha256
    of the same encoded payload (content-addressed, provenance included)."""
    return fieldops.payload_sha256(fieldops.encode_field(fld, provenance))


def _provenance(root: str, block: str, form: str) -> dict[str, Any]:
    return {"source": JOB_TYPE, "root": root, "block": block, "form": form}


def _dof_partners(plan: JointPlan) -> dict[str, list[str]]:
    """``{eroded member: [its DOF partners]}`` over the printed DOF edges."""
    out: dict[str, list[str]] = {}
    for e in plan.printed_dof_edges:
        out.setdefault(e.a, []).append(e.b)
        out.setdefault(e.b, []).append(e.a)
    return {k: sorted(set(v)) for k, v in out.items()}


def _erode_radius(gap: float, pitch: float) -> float:
    """``gap/2`` plus the half pitch the binarised re-distance needs."""
    return 0.5 * gap + 0.5 * pitch


def _member_grid(solid: _Solid, radius: float, lattice: _Lattice) -> _Grid:
    """An eroded member's own grid: its root-frame AABB plus the dilation
    radius (so every partner sample that can reach it is inside) plus the
    margin, on the lattice."""
    return _grid_for(
        solid.lo, solid.hi, radius + _GRID_MARGIN_PITCHES * lattice.pitch, lattice
    )


def _eroded_field(
    geo: GroupGeometry,
    name: str,
    partners: list[str],
    gap: float,
    lattice: _Lattice,
) -> tuple[Field, _Grid]:
    """``A' = A \\ dilate(B, gap/2 + pitch/2)`` for every DOF partner ``B``
    of member ``A``, on A's own grid (module docstring): A's exact SDF
    sampled, each partner's sample re-distanced and dilated, the
    subtraction a max. A partner whose material does not reach A's grid
    carves nothing."""
    pitch = lattice.pitch
    radius = _erode_radius(gap, pitch)
    solid = geo.solids[name]
    grid = _member_grid(solid, radius, lattice)
    pts = grid.points()
    arr = solid.sample(pts).reshape(grid.shape)
    for other in partners:
        inside = geo.solids[other].sample(pts).reshape(grid.shape) <= 0.0
        if not inside.any():
            continue
        base = _redistanced(inside, grid, f"member {other!r}")
        dilated = np.asarray(fieldops.offset(base, -radius).grid, dtype=np.float64)
        arr = np.maximum(arr, -dilated)
    if not np.any(arr <= 0.0):
        raise ManufactureError(
            f"realize(manufacture): carving {name!r} back by gap/2 = "
            f"{0.5 * gap:g} m (+ half a pitch) from its DOF partner(s) "
            "erased it — the member lies wholly inside the gap"
        )
    fld = _leaf(arr, grid)
    return fld, grid


def _placed(solid: _Solid, prefix: str) -> list[NodeSpec]:
    spec = solid.spec
    if spec is None:
        raise ManufactureError(
            f"realize(manufacture): member {solid.member.block!r} has no node tree"
        )
    try:
        return placed_nodes(spec, solid.to_root, prefix)
    except SceneError as exc:
        raise ManufactureError(
            f"realize(manufacture): cannot place {solid.member.block!r}'s design in "
            f"the root frame — {exc}"
        ) from exc


def _cavity_is_analytic(placed: list[NodeSpec], fit: float) -> bool:
    """``fit == 0`` and a stand-in of plain ``add`` nodes only (no cut, no
    intersect, no ``blend:``): the exact stand-in is a chain of cuts
    (module docstring). ``placed`` is the stand-in's placed node list."""
    if fit > 0.0:
        return False
    return all(n.op == "add" and n.blend == 0.0 for n in placed)


def _cavity_margin(fit: float, pitch: float) -> float:
    return (fit + 0.5 * pitch if fit > 0.0 else 0.0) + _GRID_MARGIN_PITCHES * pitch


def _cavity_grid(solid: _Solid, fit: float, lattice: _Lattice) -> _Grid:
    return _grid_for(solid.lo, solid.hi, _cavity_margin(fit, lattice.pitch), lattice)


def _cavity_part(
    geo: GroupGeometry,
    name: str,
    fit: float,
    lattice: _Lattice,
    placed: list[NodeSpec] | None = None,
) -> _Part:
    """The cut a bought member makes (module docstring's cavity rule):
    analytic ``cut`` nodes for a plain-add stand-in at ``fit == 0``, else
    one ``field:`` leaf — offset on the re-distanced sample for ``fit >
    0``, the bare sample for a composite stand-in at ``fit == 0``.
    ``placed`` is the stand-in's placed node list when the caller already
    has it (:func:`_cell_plan` places once per cavity)."""
    solid = geo.solids[name]
    pitch = lattice.pitch
    pad = fit + 0.5 * pitch if fit > 0.0 else 0.0
    lo, hi = solid.lo - pad, solid.hi + pad
    if placed is None:
        placed = _placed(solid, name)
    if _cavity_is_analytic(placed, fit):
        nodes = [replace(n, op="cut", component="") for n in placed]
        return _Part(
            block=name, kind="cavity", form=FORM_ANALYTIC, nodes=nodes, lo=lo, hi=hi
        )
    grid = _cavity_grid(solid, fit, lattice)
    pts = grid.points()
    raw = solid.sample(pts).reshape(grid.shape)
    if fit > 0.0:
        base = _redistanced(raw <= 0.0, grid, f"stand-in {name!r}")
        cav = np.asarray(fieldops.offset(base, -(fit + 0.5 * pitch)).grid, dtype=float)
        form = FORM_CAVITY_FIT
    else:
        cav = raw
        form = FORM_CAVITY_SHAPE
    fld = _leaf(cav, grid)
    prov = _provenance(geo.root, name, form)
    sha = _field_sha(fld, prov)
    node = NodeSpec(
        name=f"{name}.cavity", op="cut", config=f"field:{sha}", component=""
    )
    return _Part(
        block=name,
        kind="cavity",
        form=form,
        nodes=[node],
        fld=fld,
        sha=sha,
        provenance=prov,
        grid=grid.shape,
        cells=grid.cells,
        lo=lo,
        hi=hi,
    )


@dataclass(frozen=True)
class _CellPlan:
    """The grids a run will allocate, sized without sampling anything —
    :func:`prepare_manufacture`'s cell accounting and the echo."""

    lattice: _Lattice
    member_grids: dict[str, _Grid]
    cavity_grids: dict[str, _Grid]
    #: Each cavity's stand-in placed in the root frame — placed once here,
    #: handed to :func:`_cavity_part`.
    placed: dict[str, list[NodeSpec]] = field(default_factory=dict)

    @property
    def field_cells(self) -> int:
        return sum(g.cells for g in self.member_grids.values()) + sum(
            g.cells for g in self.cavity_grids.values()
        )

    @property
    def total(self) -> int:
        return self.lattice.grid.cells + self.field_cells


def _cell_plan(
    geo: GroupGeometry,
    *,
    pitch: float,
    gap: float | None,
    fit: float | None,
    blend: float,
    cavities: list[str],
) -> _CellPlan:
    lattice = _lattice(geo, pitch, blend)
    member_grids: dict[str, _Grid] = {}
    if geo.plan.eroded:
        assert gap is not None
        radius = _erode_radius(gap, pitch)
        for name in sorted(geo.plan.eroded):
            member_grids[name] = _member_grid(geo.solids[name], radius, lattice)
    cavity_grids: dict[str, _Grid] = {}
    placed: dict[str, list[NodeSpec]] = {}
    for name in cavities:
        f = fit or 0.0
        placed[name] = _placed(geo.solids[name], name)
        if not _cavity_is_analytic(placed[name], f):
            cavity_grids[name] = _cavity_grid(geo.solids[name], f, lattice)
    return _CellPlan(
        lattice=lattice,
        member_grids=member_grids,
        cavity_grids=cavity_grids,
        placed=placed,
    )


def _refuse_over_budget(plan: _CellPlan, pitch: float) -> None:
    grid = plan.lattice.grid
    if grid.cells > MAX_CELLS:
        raise ManufactureError(
            f"realize(manufacture): pitch {pitch:g} m over the group box gives a "
            f"{grid.nv[0]}x{grid.nv[1]}x{grid.nv[2]} = {grid.cells} cell export "
            f"grid, above the {MAX_CELLS} budget — coarsen pitch= (>= "
            f"{_fit_pitch(pitch, grid.cells):.3g} m fits)"
        )
    for what, grids in (("member", plan.member_grids), ("cavity", plan.cavity_grids)):
        for name, g in grids.items():
            if g.cells > MAX_CELLS:
                raise ManufactureError(
                    f"realize(manufacture): pitch {pitch:g} m gives {what} {name!r} a "
                    f"{g.shape[0]}x{g.shape[1]}x{g.shape[2]} = {g.cells} cell field "
                    f"grid, above the {MAX_CELLS} budget — coarsen pitch= (>= "
                    f"{_fit_pitch(pitch, g.cells):.3g} m fits)"
                )


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
    cells = _cell_plan(
        geo, pitch=pitch, gap=gap, fit=fit, blend=blend, cavities=cavities
    )
    _refuse_over_budget(cells, pitch)
    grid = cells.lattice.grid
    simp_members = [
        m.block
        for m in geo.members
        if (tree.blocks[m.block].build_frame or {}).get("origin") == "simp"
    ]
    sync = not simp_members and cells.total <= SYNC_CELL_CAP
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
        f"export grid {grid.nv[0]}x{grid.nv[1]}x{grid.nv[2]} = {grid.cells} cells",
        f"{len(cells.member_grids)} eroded member field(s) + "
        f"{len(cells.cavity_grids)} cavity field(s) = {cells.field_cells} cells",
        f"{len(geo.printed) - len(cells.member_grids)} analytic member(s)",
        f"total {cells.total} cells",
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
                else f"{cells.total} cells > {SYNC_CELL_CAP}"
            )
            + f"); {key!r} keeps its previous binding until it lands"
        )
    )
    echo = f"realize({key!r}, strategy='manufacture'): {how} — " + ", ".join(bits)
    return echo, request


def _is_fused_output(cad_store_reader: Store, cad_slug: str, root: str) -> bool:
    """True when ``cad_slug`` is a design this module minted for ``root``
    (its ``meta.se_manufacture`` says so)."""
    loaded = _fused_spec(cad_store_reader, cad_slug)
    return loaded is not None and str(loaded[1].get("root") or "") == root


def _fused_spec(
    cad_store_reader: Store, cad_slug: str
) -> tuple[SceneSpec, dict[str, Any]] | None:
    """``(spec, meta.se_manufacture)`` of a design this module minted, else
    ``None`` (unknown slug, a foreign design, an unreadable spec)."""
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
    run = spec.meta.get(CAD_META_KEY)
    if not isinstance(run, dict) or run.get("source") != JOB_TYPE:
        return None
    return spec, dict(run)


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
    """:func:`solve_manufacture`'s answer: the root expression as a flat
    node list (root frame, metres) with its component order, the parts it
    is made of (the field leaves still to store ride on them), the
    per-object meshes it labels, and the JSON-safe summary."""

    nodes: list[NodeSpec]
    components: list[str]
    parts: list[_Part]
    summary: dict[str, Any]
    findings: list[ValidationIssue]
    objects: list[tuple[str, np.ndarray, np.ndarray]]

    @property
    def fields(self) -> list[_Part]:
        return [p for p in self.parts if p.fld is not None]

    def spec(self, meta: dict[str, Any] | None = None) -> SceneSpec:
        """The output design's spec; ``meta`` rides on the cad ref. Always
        carries the cad-level object split (``meta.export_objects`` — which
        components print as one object — and the lattice they were built
        on), so the generic cad 3MF export of this design writes the same
        objects ``view='print'`` does."""
        s = self.summary
        return SceneSpec(
            nodes=list(self.nodes),
            components=list(self.components),
            meta={
                "units": "mm",
                EXPORT_OBJECTS_KEY: dict(s["export_objects"]),
                EXPORT_LATTICE_KEY: {
                    "origin": list(s["export"]["origin"]),
                    "pitch": float(s["pitch_m"]),
                },
                **(meta or {}),
            },
        )


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


def _redistanced(binary: np.ndarray, grid: _Grid, what: str) -> Field:
    try:
        return fieldops.redistance(binary, grid.pitch, grid.origin, pad=0)
    except ValueError as exc:
        raise ManufactureError(
            f"realize(manufacture): {what} voxelises to {'nothing' if not binary.any() else 'the whole box'} "
            f"at pitch {grid.pitch:g} m — {exc}"
        ) from exc


def label_objects(samples: np.ndarray) -> tuple[np.ndarray, int]:
    """The connected components (6-connectivity) of a sampled sign field
    — the root's export-grid samples."""
    return fieldops.label_components(np.asarray(samples) <= 0.0)


SdfFn = Callable[[np.ndarray], np.ndarray]


def _root_sdf(design: CadDesign) -> SdfFn:
    whole = design.whole()

    def sdf(pts: np.ndarray) -> np.ndarray:
        return np.asarray(component_sdf_np(design, whole, pts), dtype=np.float64)

    return sdf


def _object_meshes(
    design: CadDesign,
    objects: dict[str, list[str]],
    pitch: float,
    origin: np.ndarray,
) -> list[tuple[str, np.ndarray, np.ndarray]]:
    """:func:`precis.cad.export.object_meshes` in the root frame (metres),
    every object's box snapped onto the export lattice — the SAME per-
    object fold the generic cad 3MF export of the ``-mfg`` design writes
    (``meta.export_objects``): each object is its own components' exact
    fold, no vertex value is ever invented."""
    try:
        return object_meshes(design, objects, pitch, origin=origin)
    except ExportError as exc:
        raise ManufactureError(f"realize(manufacture): {exc}") from exc


def _object_components(
    holders: list[str], member_parts: dict[str, _Part], chain_of: dict[str, str]
) -> list[str]:
    """The cad components an object is the fold of: its holders' own
    components (a chained member's is the chain's), in first-seen order."""
    out: list[str] = []
    for m in holders:
        for n in member_parts[m].nodes:
            c = chain_of.get(m, n.component)
            if c not in out:
                out.append(c)
    return out


def _overlaps(
    a: tuple[np.ndarray, np.ndarray], b: tuple[np.ndarray, np.ndarray]
) -> bool:
    return bool(np.all(a[0] <= b[1]) and np.all(a[1] >= b[0]))


def _field_loader_for(
    parts: list[_Part], cad_store_reader: Store
) -> Callable[[str], Field]:
    """Resolves this solve's not-yet-stored leaves from memory and every
    other ``field:`` (a SIMP-realized member's own root) through the
    store's loader."""
    local = {p.sha: p.fld for p in parts if p.fld is not None and p.sha}
    store_loader = getattr(cad_store_reader, "field_loader", None)
    fallback = store_loader() if callable(store_loader) else None

    def load(ref: str) -> Field:
        key = ref.lower()
        hit = next((f for sha, f in local.items() if sha and sha.startswith(key)), None)
        if hit is not None:
            return hit
        if fallback is None:
            raise LookupError(f"no field loader for {ref!r}")
        return fallback(ref)

    return load


def solve_manufacture(
    tree: SeTree, req: ManufactureRequest, *, cad_store_reader: Store
) -> ManufactureSolve:
    """Build the root expression — the members' placed node trees, one
    field leaf per DOF-eroded member, the cavities — then sample it on
    the export grid, label the objects, mesh each and measure the gaps:
    the store-free half of :func:`run_manufacture` (module docstring).
    Raises :class:`ManufactureError` for what :func:`prepare_manufacture`
    could not see (a member that voxelises to nothing at this pitch)."""
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
    elided = geo.elided
    cavity_names = [n for n in geo.stand_ins if n not in elided]
    fit = req.fit or 0.0
    cells = _cell_plan(
        geo,
        pitch=pitch,
        gap=req.gap,
        fit=req.fit,
        blend=req.blend,
        cavities=cavity_names,
    )
    _refuse_over_budget(cells, pitch)
    lattice = cells.lattice

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

    # --- the members: an analytic node tree each, or ONE field leaf for a
    # DOF-eroded member (module docstring: the in-place gap, seam-local by
    # construction, computed on that member's own lattice-snapped grid).
    partners = _dof_partners(plan)
    member_parts: dict[str, _Part] = {}
    eroded_fields: dict[str, Field] = {}
    for name in geo.printed:
        solid = geo.solids[name]
        if name in plan.eroded:
            assert gap is not None
            fld, grid = _eroded_field(geo, name, partners.get(name, []), gap, lattice)
            prov = _provenance(root, name, FORM_GAP)
            sha = _field_sha(fld, prov)
            eroded_fields[name] = fld
            member_parts[name] = _Part(
                block=name,
                kind="member",
                form=FORM_GAP,
                nodes=[
                    NodeSpec(
                        name=f"{name}.eroded",
                        op="add",
                        config=f"field:{sha}",
                        component=name,
                    )
                ],
                fld=fld,
                sha=sha,
                provenance=prov,
                grid=grid.shape,
                cells=grid.cells,
                lo=solid.lo,
                hi=solid.hi,
            )
        else:
            member_parts[name] = _Part(
                block=name,
                kind="member",
                form=FORM_ANALYTIC,
                nodes=_placed(solid, name),
                lo=solid.lo,
                hi=solid.hi,
            )

    # --- the root's components: blend=0 keeps every member's own
    # component(s) (whole() = the hard min-union, cuts included); blend>0
    # chains the members of one fused component into one cad component,
    # each member after the first entering on its base node's blend.
    nodes: list[NodeSpec] = []
    comp_order: list[str] = []
    comp_box: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    #: ``{chained member: its chain's component}`` — the object split
    #: reads a chained member's component from here.
    chain_of: dict[str, str] = {}
    #: ``{chain component: [later members carrying a cut/intersect]}`` —
    #: the members whose tools reach back along the chain (blend_chain).
    leaking: dict[str, list[str]] = {}
    for members in plan.components():
        chain = req.blend > 0.0 and len(members) > 1
        chain_name = "+".join(members)
        for i, name in enumerate(members):
            part = member_parts[name]
            solid = geo.solids[name]
            mnodes = list(part.nodes)
            if chain:
                chain_of[name] = chain_name
                mnodes = [replace(n, component=chain_name) for n in mnodes]
                if i > 0 and mnodes and mnodes[0].op == "add":
                    mnodes[0] = replace(mnodes[0], blend=req.blend)
                if i > 0 and any(n.op in ("cut", "intersect") for n in mnodes):
                    leaking.setdefault(chain_name, []).append(name)
            for n in mnodes:
                if n.component not in comp_box:
                    comp_order.append(n.component)
                    comp_box[n.component] = (solid.lo.copy(), solid.hi.copy())
                else:
                    lo, hi = comp_box[n.component]
                    comp_box[n.component] = (
                        np.minimum(lo, solid.lo),
                        np.maximum(hi, solid.hi),
                    )
            nodes.extend(mnodes)
    if not nodes:
        raise ManufactureError(
            f"realize(manufacture): print group {root!r} has no printed member to "
            "fuse (stand-ins alone are cavities in nothing)"
        )
    for chain_name, later in leaking.items():
        findings.append(
            ValidationIssue(
                rule="blend_chain",
                subject=chain_name,
                detail=(
                    f"blend {req.blend * 1000:.3g} mm chains the members of "
                    f"{chain_name} into one cad component (the DSL's blend: is a "
                    "union-only option on a per-component chain, no nested "
                    f"union): the cut/intersect nodes of {', '.join(later)} also "
                    "cut the members before them in the chain where the tools "
                    "overlap them"
                ),
                severity="info",
            )
        )

    # --- cavities: cut from every component whose box the stand-in meets
    cavity_parts: list[_Part] = []
    cavities: list[dict[str, Any]] = []
    for name in cavity_names:
        part = _cavity_part(geo, name, fit, lattice, cells.placed.get(name))
        cavity_parts.append(part)
        assert part.lo is not None and part.hi is not None
        targets = [c for c in comp_order if _overlaps(comp_box[c], (part.lo, part.hi))]
        for c in targets:
            for n in part.nodes:
                nodes.append(
                    replace(
                        n,
                        component=c,
                        name=n.name if len(targets) == 1 else f"{n.name}~{c}",
                    )
                )
        member = geo.solids[name].member
        cavities.append(
            {
                "block": name,
                "source": member.stand_in.source if member.stand_in else "",
                "fit_m": fit,
                "form": part.form,
                "sha": part.sha,
                "components": targets,
            }
        )
        if not targets:
            findings.append(
                ValidationIssue(
                    rule="cavity_outside",
                    subject=name,
                    detail=(
                        f"bought member {name!r}'s stand-in meets no printed member "
                        "of the group — no cavity was cut (its pause height is "
                        "still reported)"
                    ),
                    severity="info",
                )
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
    parts = [*(member_parts[n] for n in geo.printed), *cavity_parts]

    # --- the root, sampled ONCE on the export grid ------------------------
    spec = SceneSpec(nodes=list(nodes), components=list(comp_order))
    spec.field_loader = _field_loader_for(parts, cad_store_reader)
    try:
        design = build_design(spec)
    except (SceneError, cad_dsl.DslError) as exc:
        raise ManufactureError(
            f"realize(manufacture): the root expression does not build — {exc}"
        ) from exc
    sdf = _root_sdf(design)
    export = lattice.grid
    samples = sample_grid(sdf, export)
    inside = samples <= 0.0
    if not inside.any():
        raise ManufactureError(
            "realize(manufacture): the cavities consumed every printed member — "
            "nothing left to print"
        )

    # --- objects: one per connected component -----------------------------
    labels, count = label_objects(samples)
    pts = export.points()
    member_labels: dict[str, set[int]] = {}
    for name in geo.printed:
        if name in eroded_fields:
            m_in = eroded_fields[name].distance_local_np(pts) <= 0.0
        else:
            m_in = geo.solids[name].sample(pts) <= 0.0
        m_in = m_in.reshape(export.nv)
        member_labels[name] = {int(v) for v in np.unique(labels[m_in & (labels >= 0)])}
    del pts
    # A member the cavities cut into several pieces holds several labels;
    # each of those objects is then the fold of the WHOLE member (the
    # per-object fold has no way to keep one piece), so they overlap in
    # the file — an error, said per member, never a silent duplicate.
    for name, ls in member_labels.items():
        if len(ls) > 1:
            findings.append(
                ValidationIssue(
                    rule="member_split",
                    subject=name,
                    detail=(
                        f"printed member {name!r} comes out in {len(ls)} "
                        "disconnected pieces after the cavities are cut — every "
                        "object holding one of them exports the member's whole "
                        "fold, so those objects overlap; re-pose or resize the "
                        "bought part(s) cutting it"
                    ),
                    severity="error",
                )
            )
    object_rows: list[dict[str, Any]] = []
    object_comps: dict[str, list[str]] = {}
    for label in range(count):
        holders = sorted(n for n, ls in member_labels.items() if label in ls)
        if not holders:
            continue  # material of no member: a blend seam sliver — no object
        base_name = "+".join(holders)
        name = base_name
        k = 2
        while any(o["name"] == name for o in object_rows):
            name = f"{base_name}-{k}"
            k += 1
        object_comps[name] = _object_components(holders, member_parts, chain_of)
        object_rows.append(
            {
                "name": name,
                "label": label,
                "members": holders,
                "components": object_comps[name],
                "voxels": int(np.count_nonzero(labels == label)),
            }
        )
    # Name order, not label order (a label is the lowest voxel index —
    # whichever body happens to sit at the grid's min corner).
    object_rows.sort(key=lambda o: str(o["name"]))
    object_comps = {str(o["name"]): list(o["components"]) for o in object_rows}
    # Each object meshed as its own components' exact fold, on the lattice
    # (the same call the generic cad 3MF export of this design makes).
    objects = _object_meshes(design, object_comps, pitch, export.origin)

    # --- gaps: min of the other side's re-distanced eroded leaf (exact
    # outside its binarised surface) over this side's object vertices ----
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
                try:
                    side_field[other] = fieldops.redistance(eroded_fields[other])
                except ValueError as exc:
                    raise ManufactureError(
                        f"realize(manufacture): eroded member {other!r} — {exc}"
                    ) from exc
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
        "grid": list(export.nv),
        "cells": cells.total,
        "export": {
            "lo": [float(x) for x in lattice.lo],
            "hi": [float(x) for x in lattice.hi],
            "origin": [float(x) for x in export.origin],
            "shape": list(export.nv),
            "cells": export.cells,
        },
        "field_cells": cells.field_cells,
        "parts": [p.row() for p in parts],
        "components": list(comp_order),
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
        "export_objects": object_comps,
        "gaps": gaps,
        "volume_m3": float(np.count_nonzero(inside)) * pitch**3,
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
        nodes=nodes,
        components=comp_order,
        parts=parts,
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
            "the fused root is stale and was NOT bound; re-run realize"
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
    """Store every field leaf (each provenance names its member/cavity),
    mint the cad design whose root is the solve's node tree (the run
    summary on its ``meta.se_manufacture``), then — inside the design's
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

    field_shas: dict[str, str] = {}
    for part in solve.fields:
        assert part.fld is not None and part.sha is not None
        sha = store.put_field(ref_id, part.fld, provenance=part.provenance)
        if sha != part.sha:  # pragma: no cover — the payload codec is deterministic
            raise ManufactureError(
                f"realize(manufacture): stored field for {part.block!r} came back as "
                f"{sha[:12]}, the root expression names {part.sha[:12]}"
            )
        field_shas[part.block] = sha
    slug, note = _unique_cad_slug(store, f"{design_slug}-{req.block}-{CAD_SUFFIX}")
    title = f"{req.block} ({design_slug} realize manufacture)"
    s = solve.summary
    spec = solve.spec(
        {
            CAD_META_KEY: {
                "source": JOB_TYPE,
                "se_design": design_slug,
                "root": req.block,
                "summary": s,
            }
        }
    )
    n_analytic = sum(
        1 for p in solve.parts if p.kind == "member" and p.form == FORM_ANALYTIC
    )
    card_text = (
        f"{title}: print-in-place root of {n_analytic} analytic member tree(s) and "
        f"{len(solve.fields)} field leaf(ves) at {req.pitch:g} m pitch, "
        f"{len(s['objects'])} object(s), {len(s['cavities'])} cavity(ies), "
        f"{len(s['elided'])} fastener(s) elided"
    )
    cad_ref, _created, _n = store.cad_save(
        slug=slug, title=title, spec=spec, card_text=card_text
    )
    summary = dict(solve.summary)
    summary["cad"] = slug
    summary["field_shas"] = field_shas
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
        f"{slug!r} (root: {n_analytic} analytic member tree(s) + "
        f"{len(solve.fields)} field leaf(ves) — "
        + ", ".join(f"{p.block} {p.form}" for p in solve.parts)
        + f"; export grid {s['grid'][0]}x{s['grid'][1]}x{s['grid'][2]} at "
        f"{req.pitch:g} m); {len(s['objects'])} object(s): "
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


@dataclass
class StoredRoot:
    """A fused output design loaded back: its spec (field leaves resolved
    through the store's loader) and the live design. ``distance_local_np``
    is the whole root's exact SDF in the ROOT frame (metres) — an
    analytic member answers analytically, a leaf by its grid."""

    spec: SceneSpec
    design: CadDesign

    def distance_local_np(self, pts: np.ndarray) -> np.ndarray:
        return _root_sdf(self.design)(np.asarray(pts, dtype=np.float64))


def _stored_root(
    cad_store_reader: Store, root_node: SeBlock, root: str
) -> tuple[str, dict[str, Any], StoredRoot] | None:
    """``(cad slug, summary, root)`` when the root block is bound to a
    fused output of this group, else ``None``."""
    if root_node.bound_kind != "cad" or not root_node.bound:
        return None
    loaded = _fused_spec(cad_store_reader, str(root_node.bound))
    if loaded is None:
        return None
    spec, run = loaded
    if run.get("root") != root:
        return None
    try:
        design = build_design(spec)
    except (SceneError, cad_dsl.DslError):
        return None
    return (
        str(root_node.bound),
        dict(run.get("summary") or {}),
        StoredRoot(spec, design),
    )


def _pause_heights(
    geo: GroupGeometry,
    summary: dict[str, Any],
    lattice: _Lattice,
    xf: Transform,
    d: np.ndarray,
    floor: float,
    cad_store_reader: Store,
) -> list[tuple[str, float, str]]:
    """Each cavity's top layer above the bed along ``-down``: the highest
    void vertex of the cavity's own field leaf (a field-backed cavity) or
    of the analytic stand-in sampled on the export lattice (``fit == 0``),
    plus half a pitch — exact to the grid in any frame; an AABB would
    over-estimate a rotated member and fire the pause after the cavity
    has closed."""
    pitch = lattice.pitch
    out: list[tuple[str, float, str]] = []
    for cav in summary.get("cavities") or []:
        block = str(cav["block"])
        solid = geo.solids.get(block)
        sha = cav.get("sha")
        if sha:
            try:
                _h, fld = cad_store_reader.get_field(str(sha))
            except Exception:
                continue
            grid = _Grid(
                origin=np.asarray(fld.origin), shape=fld.shape, pitch=fld.pitch
            )
            void = np.asarray(fld.grid) < 0.0
        elif solid is not None:
            grid = _cavity_grid(solid, 0.0, lattice)
            void = solid.sample(grid.points()).reshape(grid.shape) < 0.0
        else:
            continue
        if not void.any():
            continue
        world_pts = grid.points() @ xf.R.T + xf.t
        height = floor - (world_pts @ d)  # per vertex, above the bed
        top = float(height[void.ravel()].max()) + 0.5 * pitch
        out.append((block, top, str(cav.get("source") or "")))
    return out


def report_for(tree: SeTree, root: str, *, cad_store_reader: Store) -> GroupPrintReport:
    """The manufacture group's report: B1's member classification, the
    joint plan, and — once realized — the stored fused root's objects at
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
    stored = _stored_root(cad_store_reader, root_node, root)

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
    # The objects the solve labelled, each meshed as its own components'
    # exact fold on the stored lattice (the same call the generic cad 3MF
    # export of the design makes) — no re-sampling of the whole root here.
    export = dict(summary.get("export") or {})
    try:
        origin = np.asarray([float(x) for x in export["origin"]], dtype=float)
        lattice = _Lattice(
            grid=FieldGrid(
                origin=origin,
                nv=tuple(int(n) for n in export["shape"]),  # type: ignore[arg-type]
                pitch=req.pitch,
                factor=1,
            ),
            lo=np.asarray([float(x) for x in export["lo"]], dtype=float),
            hi=np.asarray([float(x) for x in export["hi"]], dtype=float),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ManufactureError(
            f"print group {root!r}: the stored run summary on {cad_slug!r} has no "
            f"export lattice ({exc}) — re-run realize(strategy='manufacture')"
        ) from exc
    object_comps: dict[str, list[str]] = {
        str(r["name"]): [str(c) for c in (r.get("components") or [])]
        for r in summary.get("objects") or []
    }
    xf = geo.root_xform
    objects = [
        (name, verts @ xf.R.T + xf.t, tris)
        for name, verts, tris in _object_meshes(
            fused.design, object_comps, req.pitch, origin
        )
    ]
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
        pauses = _pause_heights(geo, summary, lattice, xf, d, floor, cad_store_reader)
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
    """One 3MF, one object per connected component of the fused root
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
    "CAD_META_KEY",
    "CAD_SUFFIX",
    "CLEARANCE_FIELD",
    "FORM_ANALYTIC",
    "FORM_CAVITY_FIT",
    "FORM_CAVITY_SHAPE",
    "FORM_GAP",
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
    "StoredRoot",
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
