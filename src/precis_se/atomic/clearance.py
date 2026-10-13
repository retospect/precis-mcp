"""The **atomic** ``view='clearance'`` path — a van der Waals gap between two
structure-bound blocks, swept over rotor angle, with pockets.

The rotary ratchet valve's first design tool (``docs/backlog/
rotary-ratchet-valve.md`` "Design tool set" 1–2, hexfold spec §28.8): an
outer shell and an inner rotor whose in-between space is what the chemistry
happens in, measured before step 5's smooth mesh exists — the doc's own
stub: ``stick`` atoms plus van der Waals radii. The world-placement
convention is ``view='clearance'``'s own v1 one: a block's ``pose``/``rot``
ARE its world placement (:func:`precis_se.atomic.validate.
_posed_atom_cloud`, reused here rather than re-derived), atoms in Å.

**Gap.** Per-atom van der Waals radius (Bondi, :data:`VDW_RADII_A`); a
pair's gap is centre distance minus both radii. Each side's per-atom
nearest cross-block gap comes from a ``scipy.spatial.cKDTree`` over the
other cloud — scipy is installed in every venv this code runs in (``ase``
is a core dep and requires it); a structure-free fallback would just
re-derive :func:`precis_se.atomic.validate._cross_pairs_within`'s chunked
broad phase, which is the same answer slower.

**Pockets** (the metering number): the set of atoms on either block whose
nearest cross-block gap is under a threshold (default
:data:`POCKET_GAP_DEFAULT_A`), clustered by connectivity — atoms within
:data:`POCKET_LINK_A` of each other in the same block, unioned across the
pair through the near-contact partners that flagged them. One connected
component = one enclosed void = one metering pocket; the count at the
loading angle is the count the valve doc asks for. Each pocket reports
per-block atom counts, its centroid as ``(z, θ)`` about the sweep axis,
its angular extent, and an approximate volume (convex hull of the pocket
atoms' van der Waals centres — labelled approximate because the void, not
the centres, is the metered space).

**Sweep.** ``args={'a','b','sweep':{...}}`` rotates b's posed cloud
rigidly about the resolved axis and re-measures per angle — read-only,
never a pose write (the whole point of computing over a loaded scene
rather than re-posing the block). Axis resolution — an explicit
``[point, direction]`` (metres, world frame) wins when given; omitted,
a declared revolute ``joint.axis`` between the pair wins by default
(a declared axis is design intent; the joint carries a direction only,
so the line is anchored at b's posed origin — stated on the report's
``axis:`` line); else b's own envelope axis —
``cyl``/``cone``/``tcone``/``frustum``/``hex``/``ngon``/``pyramid`` are all
z-axis bodies (:mod:`precis.cad.primitives`), so the axis is the block's
local +z rotated by its world ``rot``, through its world ``pose``. A
non-axis envelope with no joint and no explicit axis is a loud
:class:`~precis.errors.BadInput`, never a guessed axis.

The verdict vocabulary is deliberately different from the envelope
path's: atoms are exact, not an SDF solve, so ``interpenetrating``
(vdW overlap — a steric clash, not "within resolution"), ``touching``
(0 ≤ min gap < :data:`FREE_GAP_A`), ``free``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np
from numpy.typing import NDArray

from precis.cad import dsl as cad_dsl
from precis.cad.vec import as_vec3 as cad_as_vec3
from precis.cad.vec import pose as cad_pose
from precis.errors import BadInput
from precis.format import render_agent_table
from precis.structure import Scene as StructScene
from precis_se.atomic import validate as se_atomic_validate
from precis_se.ops import SeBlock, SeTree, effective_envelope

if TYPE_CHECKING:  # pragma: no cover - typing only
    from precis.store import Store

#: Bondi van der Waals radii (Å) — the valve doc's gap definition. One
#: table, here, for both the gap and the pocket radius; elements absent
#: from it use :data:`VDW_DEFAULT_A`.
VDW_RADII_A: dict[str, float] = {
    "H": 1.20,
    "C": 1.70,
    "N": 1.55,
    "O": 1.52,
    "F": 1.47,
    "P": 1.80,
    "S": 1.80,
    "Cl": 1.75,
}
VDW_DEFAULT_A = 1.80

#: ``verdict``: minimum gap at or above this is ``free`` — a rotor that
#: turns without its van der Waals spheres grazing (valve doc's
#: 3–4 Å gaps are engineered, but sub-Å is already clear of contact).
FREE_GAP_A = 0.5

#: Pockets: an atom joins the pocket set when its nearest cross-block gap
#: is under this (Å); overridable as ``args.pocket_gap_A``.
POCKET_GAP_DEFAULT_A = 3.5

#: Intra-block connectivity cutoff for pocket clustering (Å) — generous
#: over a bond length so a bonded sheet never fragments, tight enough that
#: two distinct voids on the same surface stay distinct.
POCKET_LINK_A = 3.0

#: cad-DSL aliases whose primitive is a z-axis body (cylinder and every
#: prism/frustum/cone variant) — ``b``'s envelope axis is its local +z.
_Z_AXIS_SHAPES = frozenset(
    {"cyl", "cone", "tcone", "frustum", "hex", "ngon", "pyramid"}
)

_A_TO_M = se_atomic_validate._A_TO_M
_M_TO_A = se_atomic_validate._M_TO_A


@dataclass
class AtomCloud:
    """One block's posed atoms: labels/elements in scene order plus the
    world-frame positions (Å) and each atom's vdW radius."""

    name: str
    labels: list[str]
    elements: list[str]
    pts_A: NDArray[np.float64]
    radii_A: NDArray[np.float64]

    def atom_ref(self, i: int) -> str:
        """``'C#137 (aC138)'`` — element + ordinal (the doc's report
        fields) plus the citable label."""
        return f"{self.elements[i]}#{i} ({self.labels[i]})"

    def atom_ref_short(self, i: int) -> str:
        return f"{self.elements[i]}#{i}"


def atom_cloud(node: SeBlock, scene: StructScene) -> AtomCloud:
    """``node``'s bound ``scene`` posed into the world frame — the same
    placement :func:`precis_se.atomic.validate._atom_clash_findings`
    measures on, not re-derived."""
    labels, pts = se_atomic_validate._posed_atom_cloud(node, scene)
    elements = [scene.atoms[label].element for label in labels]
    radii = np.asarray([VDW_RADII_A.get(e, VDW_DEFAULT_A) for e in elements])
    return AtomCloud(
        name=node.name, labels=labels, elements=elements, pts_A=pts, radii_A=radii
    )


@dataclass
class GapField:
    """Per-atom nearest cross-block gap (Å) and the partner atom's index,
    both directions — the shared payload every report and the pocket set
    are reduced from."""

    gaps_a: NDArray[np.float64]
    near_a: NDArray[np.int64]
    gaps_b: NDArray[np.float64]
    near_b: NDArray[np.int64]

    @property
    def all_gaps(self) -> NDArray[np.float64]:
        return np.concatenate([self.gaps_a, self.gaps_b])


def _gap_nearest(
    query_pts: NDArray[np.float64],
    query_r: NDArray[np.float64],
    host_tree: Any,
    host_pts: NDArray[np.float64],
    host_r: NDArray[np.float64],
    dist_seed: NDArray[np.float64],
) -> tuple[NDArray[np.float64], NDArray[np.int64]]:
    """The true gap-minimal partner per query atom — NOT the
    centre-nearest atom. With mixed radii those differ: a C half an
    Ångström farther than an H still has the smaller vdW gap. The centre-
    nearest distance ``dist_seed`` widened by the host cloud's radius
    spread is the provably sufficient ball: the minimizer j* satisfies
    ``d(j*) - r(j*) <= d_min - r(cn)`` (the centre-nearest is itself a
    candidate), so ``d(j*) <= d_min + r(j*) - r(cn) <= d_min + spread``.
    For uniform radii the spread is 0 and the ball shrinks to the
    centre-nearest — same answer, same cost. The ``+1e-9`` padding is
    load-bearing: ``query_ball_point`` can EXCLUDE a point sitting
    exactly on the radius (squared-distance float roundoff), and the
    centre-nearest sits exactly on it."""
    spread = float(host_r.max() - host_r.min())
    balls = host_tree.query_ball_point(query_pts, r=dist_seed + spread + 1e-9)
    gaps = np.empty(len(query_pts))
    near = np.empty(len(query_pts), dtype=np.int64)
    for i, cand in enumerate(balls):
        cand = np.asarray(cand, dtype=np.int64)
        d = np.linalg.norm(host_pts[cand] - query_pts[i], axis=1)
        g = d - query_r[i] - host_r[cand]
        j = int(np.argmin(g))
        gaps[i] = g[j]
        near[i] = cand[j]
    return gaps, near


def gap_field(cloud_a: AtomCloud, cloud_b: AtomCloud) -> GapField:
    """Nearest cross-block GAP per atom, both directions — needed in both
    directions because the global minimum is a property of the pair, not
    of either cloud's query (a's closest b may not be b's closest a).
    Partner selection is by gap, not by centre distance — see
    :func:`_gap_nearest`."""
    from scipy.spatial import cKDTree

    tree_b = cKDTree(cloud_b.pts_A)
    dist_a, _ = tree_b.query(cloud_a.pts_A)
    gaps_a, near_a = _gap_nearest(
        cloud_a.pts_A, cloud_a.radii_A, tree_b, cloud_b.pts_A, cloud_b.radii_A, dist_a
    )
    tree_a = cKDTree(cloud_a.pts_A)
    dist_b, _ = tree_a.query(cloud_b.pts_A)
    gaps_b, near_b = _gap_nearest(
        cloud_b.pts_A, cloud_b.radii_A, tree_a, cloud_a.pts_A, cloud_a.radii_A, dist_b
    )
    return GapField(gaps_a=gaps_a, near_a=near_a, gaps_b=gaps_b, near_b=near_b)


@dataclass
class GapReport:
    """The reduced numbers a gap field reports: minimum, the pair that
    makes it, percentiles, and the interpenetration/near-contact census."""

    min_gap_A: float
    worst_a: int
    worst_b: int
    p05_A: float
    p50_A: float
    n_under_0: int
    n_under_1: int
    n_under_2: int

    @property
    def verdict(self) -> str:
        if self.min_gap_A < 0.0:
            return "interpenetrating"
        if self.min_gap_A < FREE_GAP_A:
            return "touching"
        return "free"


def gap_report(field: GapField) -> GapReport:
    ia = int(np.argmin(field.gaps_a))
    jb = int(np.argmin(field.gaps_b))
    if field.gaps_a[ia] <= field.gaps_b[jb]:
        worst_a, worst_b = ia, int(field.near_a[ia])
    else:
        worst_a, worst_b = int(field.near_b[jb]), jb
    gaps = field.all_gaps
    return GapReport(
        min_gap_A=float(gaps.min()),
        worst_a=worst_a,
        worst_b=worst_b,
        p05_A=float(np.percentile(gaps, 5)),
        p50_A=float(np.percentile(gaps, 50)),
        n_under_0=int((gaps < 0.0).sum()),
        n_under_1=int((gaps < 1.0).sum()),
        n_under_2=int((gaps < 2.0).sum()),
    )


# ── pockets ───────────────────────────────────────────────────────────


class _UnionFind:
    def __init__(self) -> None:
        self._parent: dict[Any, Any] = {}

    def find(self, x: Any) -> Any:
        parent = self._parent
        root = x
        while parent.get(root, root) != root:
            root = parent[root]
        while parent.get(x, x) != x:
            parent[x], x = root, parent[x]
        return root

    def union(self, x: Any, y: Any) -> None:
        rx, ry = self.find(x), self.find(y)
        if rx != ry:
            self._parent[ry] = rx


def _block_components(pts: NDArray[np.float64]) -> list[int]:
    """Component id per atom of ONE block's flagged set — union-find over
    pairs within :data:`POCKET_LINK_A`, via a KD-tree ``query_pairs``."""
    from scipy.spatial import cKDTree

    n = len(pts)
    uf = _UnionFind()
    for i, j in cKDTree(pts).query_pairs(POCKET_LINK_A):
        uf.union(int(i), int(j))
    comp: dict[Any, int] = {}
    out = []
    for i in range(n):
        root = uf.find(i)
        if root not in comp:
            comp[root] = len(comp)
        out.append(comp[root])
    return out


@dataclass
class Pocket:
    """One connected near-contact region — the flagged atoms of each
    block in it, plus its centroid-as-``(z, θ)``, angular extent and the
    approximate convex-hull volume (all Å / degrees). ``z_A``/``theta_deg``/
    ``extent_deg`` are ``None`` when no axis is resolvable;
    ``volume_A3`` is ``None`` when the centres can't hull (fewer than 4
    non-coplanar atoms)."""

    atoms_a: list[int]
    atoms_b: list[int]
    centroid_A: NDArray[np.float64]
    z_A: float | None
    theta_deg: float | None
    extent_deg: float | None
    volume_A3: float | None


def _axis_basis(
    axis_dir: NDArray[np.float64],
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """An orthonormal ``(v1, v2)`` spanning the plane normal to the axis —
    the θ=0 reference and its quarter-turn, deterministic off a fixed
    world axis (never the data, so the same pocket reports the same θ)."""
    ref = np.array([0.0, 0.0, 1.0])
    if abs(float(np.dot(axis_dir, ref))) > 0.9:
        ref = np.array([1.0, 0.0, 0.0])
    v1 = ref - float(np.dot(ref, axis_dir)) * axis_dir
    v1 /= np.linalg.norm(v1)
    return v1, np.cross(axis_dir, v1)


def pockets(
    cloud_a: AtomCloud,
    cloud_b: AtomCloud,
    field: GapField,
    threshold_A: float = POCKET_GAP_DEFAULT_A,
    axis: SweepAxis | None = None,
) -> list[Pocket]:
    """The pocket set at this pose: flag every atom whose nearest
    cross-block gap is under ``threshold_A``; cluster per block by
    :data:`POCKET_LINK_A` connectivity; union an a-component and a
    b-component whenever one contains an atom whose nearest cross-block
    partner (the contact that flagged it) sits in the other. Flagging is
    symmetric by construction — a's nearest b has no farther gap than the
    pair's, so a flagged atom's partner is always flagged too."""
    flagged_a = np.flatnonzero(field.gaps_a < threshold_A)
    flagged_b = np.flatnonzero(field.gaps_b < threshold_A)
    if not len(flagged_a) or not len(flagged_b):
        return []
    comp_a = _block_components(cloud_a.pts_A[flagged_a])
    comp_b = _block_components(cloud_b.pts_A[flagged_b])
    pos_b = {int(atom): k for k, atom in enumerate(flagged_b)}
    pos_a = {int(atom): k for k, atom in enumerate(flagged_a)}
    uf = _UnionFind()
    for k, atom in enumerate(flagged_a):
        partner = pos_b.get(int(field.near_a[atom]))
        if partner is not None:
            uf.union(("a", comp_a[k]), ("b", comp_b[partner]))
    for k, atom in enumerate(flagged_b):
        partner = pos_a.get(int(field.near_b[atom]))
        if partner is not None:
            uf.union(("b", comp_b[k]), ("a", comp_a[partner]))
    groups: dict[Any, dict[str, set[int]]] = {}
    for k, atom in enumerate(flagged_a):
        root = uf.find(("a", comp_a[k]))
        groups.setdefault(root, {"a": set(), "b": set()})["a"].add(int(atom))
    for k, atom in enumerate(flagged_b):
        root = uf.find(("b", comp_b[k]))
        groups.setdefault(root, {"a": set(), "b": set()})["b"].add(int(atom))
    basis = _axis_basis(axis.dir_A) if axis is not None else None
    out: list[Pocket] = []
    for members in groups.values():
        ia = sorted(members["a"])
        ib = sorted(members["b"])
        pts = np.concatenate([cloud_a.pts_A[ia], cloud_b.pts_A[ib]])
        centroid = pts.mean(axis=0)
        z_A: float | None = None
        theta_deg: float | None = None
        extent_deg: float | None = None
        if basis is not None and axis is not None:
            v1, v2 = basis
            rel = pts - axis.point_A
            thetas = np.degrees(np.arctan2(rel @ v2, rel @ v1)) % 360.0
            c_rel = centroid - axis.point_A
            z_A = float(np.dot(c_rel, axis.dir_A))
            theta_deg = float(
                np.degrees(
                    math.atan2(float(np.dot(c_rel, v2)), float(np.dot(c_rel, v1)))
                )
                % 360.0
            )
            # angular extent = 360° minus the largest gap between
            # consecutive sorted azimuths — the wrap-safe span.
            order = np.sort(thetas)
            gaps = np.diff(np.concatenate([order, order[:1] + 360.0]))
            extent_deg = float(360.0 - gaps.max())
        volume: float | None = None
        if len(pts) >= 4:
            from scipy.spatial import ConvexHull, QhullError

            try:
                volume = float(ConvexHull(pts).volume)
            except QhullError:
                volume = None
        out.append(
            Pocket(
                atoms_a=ia,
                atoms_b=ib,
                centroid_A=centroid,
                z_A=z_A,
                theta_deg=theta_deg,
                extent_deg=extent_deg,
                volume_A3=volume,
            )
        )
    return out


# ── the sweep axis ────────────────────────────────────────────────────


@dataclass
class SweepAxis:
    """A world-frame axis line (``point_A`` on it, unit ``dir_A``) plus
    the provenance line the report shows."""

    point_A: NDArray[np.float64]
    dir_A: NDArray[np.float64]
    note: str


def _envelope_axis(tree: SeTree, node: SeBlock) -> SweepAxis | None:
    """``node``'s own axis: its z-axis envelope's local +z rotated into
    the world frame, through its world pose — ``None`` when the effective
    envelope is absent, unparseable, or not a z-axis body."""
    env = effective_envelope(tree, node)
    if not env:
        return None
    try:
        spec = cad_dsl.parse(env)
    except (cad_dsl.DslError, ValueError):
        return None
    if spec.alias not in _Z_AXIS_SHAPES:
        return None
    placement = cad_pose(cad_as_vec3(node.pose), cad_as_vec3(node.rot))
    direction = np.asarray(placement.apply_dir(np.array([0.0, 0.0, 1.0])), dtype=float)
    return SweepAxis(
        point_A=np.asarray(node.pose, dtype=float) * _M_TO_A,
        dir_A=direction,
        note=f"{node.name}'s {spec.alias} envelope axis, through {node.name}'s posed origin",
    )


def _joint_axis(
    tree: SeTree, a_name: str, b_name: str, mover: SeBlock
) -> SweepAxis | None:
    """The declared ``revolute`` joint axis between the pair, if any —
    world-frame direction (:mod:`precis_se.joints`), the line taken
    through ``mover``'s posed origin (a joint carries a direction, not a
    point — the rotor's own origin is the natural anchor)."""
    pair = frozenset((a_name, b_name))
    for conn in tree.connects:
        if frozenset((conn.a_block, conn.b_block)) != pair:
            continue
        joint = getattr(conn, "joint", None)
        if not joint or joint.get("class") != "revolute":
            continue
        axis = joint.get("axis")
        if not axis:
            continue
        direction = np.asarray([float(v) for v in axis], dtype=float)
        norm = float(np.linalg.norm(direction))
        if norm < 1e-12:
            continue
        return SweepAxis(
            point_A=np.asarray(mover.pose, dtype=float) * _M_TO_A,
            dir_A=direction / norm,
            note=(
                f"declared revolute joint axis ({conn.a_block}—"
                f"{conn.b_block}), anchored at {mover.name}'s posed origin"
            ),
        )
    return None


def resolve_axis(
    tree: SeTree, a_node: SeBlock, b_node: SeBlock, axis_arg: Any
) -> SweepAxis:
    """The sweep axis: explicit ``[point, direction]`` (metres, world
    frame); ``'a'``/``'b'`` that block's envelope axis; omitted — the
    declared revolute joint axis when one exists between the pair (the
    joint wins over b's envelope by default: a declared axis is design
    intent, and it carries a direction only — the returned line is
    anchored at b's posed origin), else b's own envelope axis. Raises
    :class:`BadInput` naming what was tried when nothing resolves."""
    if isinstance(axis_arg, str):
        which = axis_arg.strip().lower()
        if which not in ("a", "b"):
            raise BadInput(
                f"clearance sweep axis must be 'a', 'b' or [point, direction]; "
                f"got {axis_arg!r}"
            )
        node = a_node if which == "a" else b_node
        axis = _envelope_axis(tree, node)
        if axis is not None:
            return axis
        raise BadInput(
            f"block {node.name!r} has no axis-bearing envelope (cyl/cone/"
            "tcone/frustum/hex/ngon/pyramid) to take a sweep axis from — "
            "pass sweep.axis=[point, direction] (metres) or declare a "
            "revolute joint with an axis between the pair"
        )
    if axis_arg is not None:
        try:
            point, direction = axis_arg
            point_v = np.asarray([float(v) for v in point], dtype=float)
            dir_v = np.asarray([float(v) for v in direction], dtype=float)
        except (TypeError, ValueError) as exc:
            raise BadInput(
                "clearance sweep axis must be 'a', 'b' or "
                "[point, direction] — two 3-vectors; got "
                f"{axis_arg!r}"
            ) from exc
        norm = float(np.linalg.norm(dir_v))
        if not point_v.shape == (3,) or not dir_v.shape == (3,) or norm < 1e-12:
            raise BadInput(
                f"clearance sweep axis [point, direction] needs two "
                f"3-vectors with a nonzero direction; got {axis_arg!r}"
            )
        return SweepAxis(
            point_A=point_v * _M_TO_A,
            dir_A=dir_v / norm,
            note=(
                "explicit axis, anchored at "
                + ", ".join(f"{v * _M_TO_A:g}" for v in point_v)
                + " Å"
            ),
        )
    joint = _joint_axis(tree, a_node.name, b_node.name, b_node)
    if joint is not None:
        return joint
    axis = _envelope_axis(tree, b_node)
    if axis is not None:
        return axis
    raise BadInput(
        f"no revolute joint between {a_node.name!r} and {b_node.name!r} "
        f"and {b_node.name!r} has no axis-bearing envelope — pass "
        "sweep.axis=[point, direction] (metres) or declare the joint"
    )


# ── the sweep ─────────────────────────────────────────────────────────


def _rotate_about(
    pts: NDArray[np.float64],
    point: NDArray[np.float64],
    direction: NDArray[np.float64],
    angle_rad: float,
) -> NDArray[np.float64]:
    """Rigidly rotate ``pts`` about the axis line — Rodrigues, over the
    posed cloud only; the block's stored pose is never touched."""
    c, s = math.cos(angle_rad), math.sin(angle_rad)
    u = direction
    K = np.array([[0.0, -u[2], u[1]], [u[2], 0.0, -u[0]], [-u[1], u[0], 0.0]])
    R = np.eye(3) * c + (1.0 - c) * np.outer(u, u) + s * K
    return point + (pts - point) @ R.T


def sweep_angles(step_deg: float, range_deg: float) -> list[float]:
    """Every ``k * step_deg`` below ``range_deg``, then the endpoint when
    the range is partial — floor, not round: a 150° step over 360° yields
    [0, 150, 300], not [0, 150]. A full turn samples ``[0, 360)`` — 360°
    is 0° re-measured."""
    n = max(1, math.floor((range_deg - 1e-9) / step_deg) + 1)
    angles = [k * step_deg for k in range(n)]
    if range_deg < 360.0 and not math.isclose(angles[-1], range_deg):
        angles.append(range_deg)
    return angles


@dataclass
class SweepRow:
    """One sweep angle's full payload — the report, the pockets (kept, not
    just counted: the complementarity stub scores them per angle), and
    b's rotated cloud for any per-angle pair reads."""

    angle_deg: float
    report: GapReport
    n_pockets: int
    pockets: list[Pocket]
    cloud_b: AtomCloud


def run_sweep(
    cloud_a: AtomCloud,
    cloud_b: AtomCloud,
    axis: SweepAxis,
    step_deg: float,
    range_deg: float,
    pocket_gap_A: float,
) -> list[SweepRow]:
    """``cloud_b`` rigidly rotated about ``axis`` per angle; per-angle gap
    field → report + pockets."""
    rows: list[SweepRow] = []
    for angle in sweep_angles(step_deg, range_deg):
        rotated = AtomCloud(
            name=cloud_b.name,
            labels=cloud_b.labels,
            elements=cloud_b.elements,
            pts_A=_rotate_about(
                cloud_b.pts_A, axis.point_A, axis.dir_A, math.radians(angle)
            ),
            radii_A=cloud_b.radii_A,
        )
        field = gap_field(cloud_a, rotated)
        pocket_list = pockets(cloud_a, rotated, field, pocket_gap_A, axis=axis)
        rows.append(
            SweepRow(
                angle_deg=angle,
                report=gap_report(field),
                n_pockets=len(pocket_list),
                pockets=pocket_list,
                cloud_b=rotated,
            )
        )
    return rows


# ── store hydration + render ──────────────────────────────────────────


def _load_scene(store: Store, slug: str) -> StructScene | None:
    ref = store.get_ref(kind="structure", id=slug)
    if ref is None:
        return None
    scene, _handles = store.structure_load(ref.id)
    return scene


def _rung_of(store: Store, node: SeBlock) -> str:
    """The bound structure's ``meta['last_relax']['rung']`` — the same
    read :func:`precis_se.atomic.join._rung_of` makes; ``stick`` when
    never relaxed past generator preview geometry."""
    if node.bound_kind != "structure" or not node.bound:
        return "—"
    ref = store.get_ref(kind="structure", id=node.bound)
    if ref is None:
        return "missing"
    return str(((ref.meta or {}).get("last_relax") or {}).get("rung") or "stick")


def is_atomic_pair(a_node: SeBlock, b_node: SeBlock) -> bool:
    """Both blocks bound to real chemistry — the condition under which
    ``view='clearance'`` takes this path instead of the cad kernel."""
    return (
        a_node.bound_kind == "structure"
        and bool(a_node.bound)
        and b_node.bound_kind == "structure"
        and bool(b_node.bound)
    )


def _vet_sweep_args(raw: Any) -> tuple[Any, float, float]:
    """``args['sweep']`` → ``(axis_arg, step_deg, range_deg)`` vetted."""
    if not isinstance(raw, dict):
        raise BadInput(
            "clearance 'sweep' must be an object like "
            "{'axis': 'b' | [point, direction], 'step_deg': 15, 'range_deg': 360}; "
            f"got {raw!r}"
        )
    unknown = sorted(set(raw) - {"axis", "step_deg", "range_deg"})
    if unknown:
        raise BadInput(
            f"unknown sweep key(s) {unknown}; accepted: axis, step_deg, range_deg"
        )
    try:
        step_deg = float(raw.get("step_deg", 15.0))
        range_deg = float(raw.get("range_deg", 360.0))
    except (TypeError, ValueError) as exc:
        raise BadInput(
            f"sweep step_deg/range_deg must be numbers; got {raw!r}"
        ) from exc
    if not (0.0 < step_deg <= 360.0):
        raise BadInput(f"sweep step_deg must be in (0, 360]; got {step_deg}")
    if not (0.0 < range_deg <= 360.0):
        raise BadInput(f"sweep range_deg must be in (0, 360]; got {range_deg}")
    return raw.get("axis"), step_deg, range_deg


def _pocket_gap_A(args: dict[str, Any]) -> float:
    raw = args.get("pocket_gap_A", POCKET_GAP_DEFAULT_A)
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise BadInput(
            f"clearance 'pocket_gap_A' must be a number (Å); got {raw!r}"
        ) from exc
    if not (0.0 < value <= 20.0):
        raise BadInput(f"clearance 'pocket_gap_A' must be in (0, 20] Å; got {value}")
    return value


def _fmt_pockets(
    pocket_list: list[Pocket],
    a_name: str,
    b_name: str,
    threshold_A: float,
) -> list[str]:
    lines = [
        f"pockets (nearest cross-block gap < {threshold_A:g} Å): {len(pocket_list)}"
    ]
    if not pocket_list:
        return lines
    has_axis = all(p.z_A is not None for p in pocket_list)
    schema = ["pocket", a_name, b_name]
    if has_axis:
        schema += ["centroid z (Å)", "θ", "extent"]
    schema += ["~volume (Å³)"]
    rows = []
    for i, p in enumerate(pocket_list, 1):
        row: dict[str, Any] = {
            "pocket": i,
            a_name: len(p.atoms_a),
            b_name: len(p.atoms_b),
        }
        if has_axis:
            row["centroid z (Å)"] = f"{p.z_A:.2f}"
            row["θ"] = f"{p.theta_deg:.1f}°"
            row["extent"] = f"{p.extent_deg:.1f}°"
        row["~volume (Å³)"] = f"{p.volume_A3:.2f}" if p.volume_A3 is not None else "—"
        rows.append(row)
    lines.append(render_agent_table(rows, schema=schema))
    if has_axis:
        lines.append(
            "z is the coordinate along the world-frame axis measured from "
            "the anchor named on the 'axis:' line above; θ is the azimuth "
            "about it off a fixed world basis"
        )
    return lines


def render_atomic_clearance(
    store: Store, tree: SeTree, a_name: str, b_name: str, args: dict[str, Any]
) -> str:
    """``view='clearance'`` on an atomic pair — the vdW gap report, the
    pockets at the as-posed (loading) angle, and the rotor-angle sweep
    table when ``args['sweep']`` is given."""
    a_node, b_node = tree.blocks[a_name], tree.blocks[b_name]
    scene_a = _load_scene(store, a_node.bound or "")
    scene_b = _load_scene(store, b_node.bound or "")
    for name, node, scene in (
        (a_name, a_node, scene_a),
        (b_name, b_node, scene_b),
    ):
        if scene is None:
            raise BadInput(
                f"block {name!r} is bound to structure {node.bound!r}, which "
                "no longer resolves — the binding is dangling (see "
                "view='validate' dangling_binding)"
            )
        if not scene.atoms:
            raise BadInput(
                f"block {name!r}'s bound structure {node.bound!r} has no "
                "atoms — nothing to measure a gap over"
            )
    assert scene_a is not None and scene_b is not None
    cloud_a = atom_cloud(a_node, scene_a)
    cloud_b = atom_cloud(b_node, scene_b)
    threshold_A = _pocket_gap_A(args or {})
    sweep_raw = (args or {}).get("sweep")

    lines = [f"# atomic clearance: {a_name!r} vs {b_name!r}"]
    lines.append(
        f"rungs: {a_name}={_rung_of(store, a_node)}, "
        f"{b_name}={_rung_of(store, b_node)} — "
        "stick/preview geometry unless the bound structures were relaxed"
    )
    lines.append(
        "vdW (Bondi Å): "
        + ", ".join(f"{el} {r:g}" for el, r in sorted(VDW_RADII_A.items()))
        + f"; other {VDW_DEFAULT_A:g}"
    )

    # The static report — the loading angle.
    field = gap_field(cloud_a, cloud_b)
    report = gap_report(field)
    axis: SweepAxis | None = None
    axis_err: str | None = None
    try:
        axis = resolve_axis(tree, a_node, b_node, None)
    except BadInput as exc:
        axis_err = str(exc)
    pocket_list = pockets(cloud_a, cloud_b, field, threshold_A, axis=axis)
    lines.append(f"gap: {report.min_gap_A:.2f} Å  ({report.verdict})")
    lines.append(
        f"distribution: p05 {report.p05_A:.2f} Å · p50 {report.p50_A:.2f} Å — "
        "each atom's nearest cross-block gap"
    )
    lines.append(
        f"under: <0 Å {report.n_under_0} · <1 Å {report.n_under_1} · "
        f"<2 Å {report.n_under_2}"
    )
    lines.append(
        f"worst pair: {a_name}:{cloud_a.atom_ref(report.worst_a)} — "
        f"{b_name}:{cloud_b.atom_ref(report.worst_b)}"
    )
    if axis is not None:
        lines.append(f"axis: {axis.note}")
    elif axis_err:
        lines.append(f"axis: unresolved ({axis_err}) — pockets omit z/θ")
    lines += _fmt_pockets(pocket_list, a_name, b_name, threshold_A)

    lining = (args or {}).get("lining")
    if sweep_raw is not None or lining is not None:
        from precis_se.atomic import sites as se_atomic_sites

        polarity: dict[str, dict[int, str]] | None = None
        cargo_A3 = 0.0
        if lining is not None:
            # a bad lining fails BEFORE the sweep runs — the validation
            # is the bond-graph pass, not a by-product of scoring.
            polarity = se_atomic_sites.build_polarity(
                cloud_a, cloud_b, scene_a, scene_b, lining
            )
            cargo_A3 = se_atomic_sites.vet_cargo_A3(args or {})
        axis_arg, step_deg, range_deg = _vet_sweep_args(sweep_raw or {})
        axis = resolve_axis(tree, a_node, b_node, axis_arg)
        rows = run_sweep(cloud_a, cloud_b, axis, step_deg, range_deg, threshold_A)
        lines.append(
            f"\nsweep: {len(rows)} angle(s) of {range_deg:g}° at {step_deg:g}° "
            f"steps, axis: {axis.note} — read-only, no pose written"
        )
        table_rows = [
            {
                "angle": f"{r.angle_deg:g}°",
                "min gap (Å)": f"{r.report.min_gap_A:.2f}",
                "worst pair": (
                    f"{cloud_a.atom_ref_short(r.report.worst_a)}—"
                    f"{cloud_b.atom_ref_short(r.report.worst_b)}"
                ),
                "n<1Å": r.report.n_under_1,
                "pockets": r.n_pockets,
            }
            for r in rows
        ]
        lines.append(
            render_agent_table(
                table_rows,
                schema=["angle", "min gap (Å)", "worst pair", "n<1Å", "pockets"],
            )
        )
        worst = min(rows, key=lambda r: r.report.min_gap_A)
        best = max(rows, key=lambda r: r.report.min_gap_A)
        lines.append(
            f"worst angle: {worst.angle_deg:g}° ({worst.report.min_gap_A:.2f} Å) · "
            f"best angle: {best.angle_deg:g}° ({best.report.min_gap_A:.2f} Å)"
        )
        verdict = worst.report.verdict
        detail = (
            f" — {verdict} at {worst.angle_deg:g}°"
            if verdict != "free"
            else f" — min gap {worst.report.min_gap_A:.2f} Å over the sweep"
        )
        lines.append(f"verdict: {verdict}{detail}")
        if polarity is not None:
            lines += se_atomic_sites.render_complementarity(
                cloud_a,
                cloud_b,
                rows,
                polarity,
                cargo_A3,
                threshold_A,
                range_deg,
            )
    else:
        lines.append(f"verdict: {report.verdict} (min gap {report.min_gap_A:.2f} Å)")
    return "\n".join(lines) + "\n"


def pair_gap_m(
    a_node: SeBlock,
    b_node: SeBlock,
    scenes: Mapping[str, StructScene],
) -> tuple[float, str, list[float]] | None:
    """``(gap_m, verdict, witness_m)`` for the all-pairs digest, or
    ``None`` when the pair can't be measured this way (a bound structure
    missing or empty — the digest renders the skip note from the
    ``_ClearanceUnavailable`` the caller raises)."""
    scene_a = scenes.get(a_node.bound or "")
    scene_b = scenes.get(b_node.bound or "")
    if scene_a is None or not scene_a.atoms or scene_b is None or not scene_b.atoms:
        return None
    cloud_a = atom_cloud(a_node, scene_a)
    cloud_b = atom_cloud(b_node, scene_b)
    field = gap_field(cloud_a, cloud_b)
    report = gap_report(field)
    witness_A = (cloud_a.pts_A[report.worst_a] + cloud_b.pts_A[report.worst_b]) / 2.0
    return (
        report.min_gap_A * _A_TO_M,
        report.verdict,
        [float(v) * _A_TO_M for v in witness_A],
    )
