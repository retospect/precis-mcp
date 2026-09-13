"""three-cad-viewer (bernhard-42/three-cad-viewer) scene projection over a
:mod:`precis.blocktree` design — round 2a's 3D counterpart to
:mod:`precis_web.blocktree_svg` (gr335242 comment 5 / docs/backlog/
multiscale-design-system-spec.md §5.8). Kind-agnostic on purpose, same
convention as the SVG projector: a caller supplies the domain's own
``effective_envelope``/``pose``/``rot`` triple, this module never reaches
for the DB or re-derives it. It reuses :mod:`precis_web.blocktree_svg`'s
tree walk (``children_map``/``plan_visibility``/``group_of``) so the level
ladder, part isolation, and the round-2a per-subtree override behave
IDENTICALLY between the SVG and 3D readers — one plan, two renderers.

**Data format.** ``Shapes`` — the vendored viewer's own hierarchical JSON
tree (``static/three-cad-viewer/``, ``Data Format.md`` upstream) — is a
tree of ``group`` (has ``parts``) / ``leaf`` (has ``shape``) nodes
addressed by a slash path. Per the spec's "DB-minted block ids as leaf
names... a pick returns the id with no lookup table": every LEAF's path
ends in the block's own DB row id (``se_blocks``/``nm_blocks``.``id`` —
the caller passes ``id_by_name``, since that mapping needs a store round
trip this module never makes). A block that has both its own geometry
AND visible children (a real assembly node, not just a container) can't
be represented by one ``Shapes`` node (group XOR leaf) — it becomes a
GROUP at ``.../<id>`` whose own shape lives one level deeper at
``.../<id>/<id>`` (the id repeated), the same convention CadQuery's own
`ocp_vscode`/jupyter-cadquery viewers use for the same shape XOR
children constraint. The pick handler reads the LAST path segment as the
id either way, so "no lookup table" still holds for that doubled leaf.

**Known simplifications** (this module's own honesty-header entries,
alongside the SVG projector's convex-hull-for-concave-envelopes one):

* **Face picking granularity.** :func:`group_planar_faces` reconstructs
  coarse analytic faces from the flat triangle soup ``mesh_config``
  returns by merging edge-adjacent triangles whose flat normals agree —
  exact for flat primitives (box, ngon ends), but a curved primitive's
  tessellated surface (cyl/cone side, sphere, torus) still fragments into
  one "face" per near-planar triangle strip, since the frozen
  ``precis.cad.tessellate.Mesh`` carries no analytic per-facet id to
  recover the true single curved B-rep face. BLOCKER for a cleaner fix:
  ``precis.cad.tessellate`` would need to return a per-triangle facet-id
  array (e.g. ``mesh_config(config: str) -> tuple[Mesh, NDArray[np.int64]]``
  with one facet id per triangle) — not added here, per this round's
  frozen-``precis.cad`` scope.
* **Feature edges** are the boundary between two reconstructed face
  groups (or a genuine mesh boundary) — each rendered as ONE straight
  segment, never a multi-segment analytic curve loop (the mesh has no
  memory of which straight sub-segments belong to the same curved edge).
* **Connectivity link geometry.** A connect's line is anchored at the
  cad kernel's own closest-point WITNESS between the connect's literal
  ``a_block``/``b_block`` envelopes (:func:`_witness_point`,
  :func:`~precis.cad.relate.clearance` — gr337917), half the actual
  surface gap each way (floored to a small visible stub for a touching/
  interfering pair), rather than the two blocks' bare POSE points — a
  base-at-pose envelope's pose can be a corner, not the part's centre,
  so a pose-to-pose line could float through empty space nowhere near
  the real joint. Falls back to the HISTORICAL pose-to-pose segment,
  nudged sideways by a small fixed fraction of the design's own
  pose-point spread, only when the witness query fails (an absent/
  unparseable envelope, or a degenerate SDF query) — this is still a
  legibility heuristic, not a verified clearance render: the line can
  still graze a solid on an unlucky orientation.
* **Exploded view** moves each visible top node along the SUM of its own
  unit vectors away from every block it's DRAWN-connected to (the same
  connect data the link overlay draws) — a real "pull apart along
  attachment directions" per the spec, not the vendored viewer's own
  built-in ``explode()`` (which pushes radially from the whole model's
  bounding-box centre — a fine default for an unconnected pile of parts,
  but not what "along attachment directions" asks for here). A node with
  no drawn connects at all falls back to the radial-from-centroid
  direction so it still moves somewhere.
"""

from __future__ import annotations

import math
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.typing import NDArray

from precis.blocktree.types import BlockNode, Connect, Tree
from precis.cad import dsl as cad_dsl
from precis.cad import relate as cad_relate
from precis.cad.graph import Design as CadDesign
from precis.cad.tessellate import apply_rigid, mesh_config
from precis.cad.vec import LINEAR_REL_EPS
from precis.cad.vec import as_vec3 as cad_as_vec3
from precis.cad.vec import pose as cad_pose
from precis_web.blocktree_svg import EffectiveEnvelopeFn, VisiblePlan

#: Two triangles sharing an edge merge into the same reconstructed face
#: when their flat normals agree within this angle (radians) — small
#: enough that a genuine box corner (90°) never merges, generous enough to
#: absorb float noise from the rigid-transform pipeline.
_FACE_ANGLE_EPS = 1e-4
_FACE_COS_EPS = math.cos(_FACE_ANGLE_EPS)

Vec3f = tuple[float, float, float]


def _as_vec3f(v: list[float]) -> Vec3f:
    return (float(v[0]), float(v[1]), float(v[2]))


# ── mesh geometry ────────────────────────────────────────────────────────


def world_mesh(
    envelope: str, pose: list[float], rot: list[float]
) -> tuple[NDArray[np.float64], NDArray[np.int64]] | None:
    """The block's own posed mesh in WORLD coordinates (verts, tris) — the
    3D analogue of :func:`~precis_web.blocktree_svg.envelope_polygon`
    (same honest-absence-on-bad-config convention: ``None``, never a
    raised error)."""
    try:
        verts, tris = mesh_config(envelope)
    except ValueError:
        return None
    xf = cad_pose(cad_as_vec3(list(pose)), cad_as_vec3(list(rot)))
    world = apply_rigid(xf, verts)
    return world, tris


def _tri_normal(
    verts: NDArray[np.float64], tri: NDArray[np.int64]
) -> NDArray[np.float64]:
    a, b, c = verts[tri[0]], verts[tri[1]], verts[tri[2]]
    n = np.cross(b - a, c - a)
    norm = float(np.linalg.norm(n))
    if norm < 1e-12:
        return np.zeros(3)
    return n / norm


def group_planar_faces(
    verts: NDArray[np.float64], tris: NDArray[np.int64]
) -> list[int]:
    """One reconstructed-face id per triangle (0..F-1) — union-find over
    edge-adjacent triangles whose flat normals coincide (module
    docstring's "face picking granularity" simplification)."""
    n = len(tris)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    normals = [_tri_normal(verts, t) for t in tris]
    edge_tris: dict[tuple[int, int], list[int]] = {}
    for i, t in enumerate(tris):
        for a, b in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0])):
            key = (int(a), int(b)) if a < b else (int(b), int(a))
            edge_tris.setdefault(key, []).append(i)
    for idxs in edge_tris.values():
        if len(idxs) != 2:
            continue
        i, j = idxs
        if float(np.dot(normals[i], normals[j])) >= _FACE_COS_EPS:
            union(i, j)
    roots = [find(i) for i in range(n)]
    remap = {r: k for k, r in enumerate(sorted(set(roots)))}
    return [remap[r] for r in roots]


def feature_edges(
    verts: NDArray[np.float64], tris: NDArray[np.int64], face_of: list[int]
) -> list[tuple[Vec3f, Vec3f]]:
    """One segment per mesh edge that sits on a reconstructed-face
    boundary (or the mesh's own boundary, for an open/non-manifold input)
    — the module docstring's "feature edges" simplification."""
    edge_info: dict[tuple[int, int], tuple[set[int], int]] = {}
    for i, t in enumerate(tris):
        for a, b in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0])):
            key = (int(a), int(b)) if a < b else (int(b), int(a))
            faces, count = edge_info.get(key, (set(), 0))
            faces.add(face_of[i])
            edge_info[key] = (faces, count + 1)
    segments: list[tuple[Vec3f, Vec3f]] = []
    for (a, b), (faces, count) in edge_info.items():
        if count == 1 or len(faces) > 1:
            pa = (float(verts[a][0]), float(verts[a][1]), float(verts[a][2]))
            pb = (float(verts[b][0]), float(verts[b][1]), float(verts[b][2]))
            segments.append((pa, pb))
    return segments


def shape_json(verts: NDArray[np.float64], tris: NDArray[np.int64]) -> dict[str, Any]:
    """A ``type: "shapes", subtype: "solid"`` leaf's ``shape`` payload
    (three-cad-viewer's ``Data Format.md``) — flat-shaded (each triangle
    gets its own 3 vertex/normal entries, grouped contiguously per
    reconstructed face so ``triangles_per_face`` stays a plain count
    list, the flat serialization format the upstream doc documents)."""
    face_of = group_planar_faces(verts, tris)
    order = sorted(range(len(tris)), key=lambda i: face_of[i])
    flat_vertices: list[float] = []
    flat_normals: list[float] = []
    flat_triangles: list[int] = []
    triangles_per_face: list[int] = []
    cur_face: int | None = None
    cur_count = 0
    vidx = 0
    for i in order:
        f = face_of[i]
        if f != cur_face:
            if cur_face is not None:
                triangles_per_face.append(cur_count)
            cur_face = f
            cur_count = 0
        tri = tris[i]
        n = _tri_normal(verts, tri)
        for vi in tri:
            flat_vertices.extend(float(c) for c in verts[vi])
            flat_normals.extend(float(c) for c in n)
        flat_triangles.extend([vidx, vidx + 1, vidx + 2])
        vidx += 3
        cur_count += 1
    if cur_face is not None:
        triangles_per_face.append(cur_count)

    edges = feature_edges(verts, tris, face_of)
    edges_flat: list[float] = []
    for a, b in edges:
        edges_flat.extend(a)
        edges_flat.extend(b)
    segments_per_edge = [1] * len(edges)

    obj_vertices = [float(c) for v in verts for c in v]
    return {
        "vertices": flat_vertices,
        "normals": flat_normals,
        "triangles": flat_triangles,
        "triangles_per_face": triangles_per_face,
        "edges": edges_flat,
        "segments_per_edge": segments_per_edge,
        "obj_vertices": obj_vertices,
        "face_types": [0] * len(triangles_per_face),
        "edge_types": [0] * len(segments_per_edge),
    }


def _box_mesh(lo: Vec3f, hi: Vec3f) -> tuple[NDArray[np.float64], NDArray[np.int64]]:
    """An axis-aligned box mesh spanning ``[lo, hi]`` — the 3D analogue of
    :func:`~precis_web.blocktree_svg.bbox_polygon` for a collapsed
    subtree's stand-in solid."""
    x0, y0, z0 = lo
    x1, y1, z1 = hi
    corners = np.array(
        [
            [x0, y0, z0],
            [x1, y0, z0],
            [x1, y1, z0],
            [x0, y1, z0],
            [x0, y0, z1],
            [x1, y0, z1],
            [x1, y1, z1],
            [x0, y1, z1],
        ],
        dtype=np.float64,
    )
    tris = np.array(
        [
            [0, 1, 2],
            [0, 2, 3],  # bottom
            [4, 6, 5],
            [4, 7, 6],  # top
            [0, 5, 1],
            [0, 4, 5],  # front (y0)
            [1, 6, 2],
            [1, 5, 6],  # right (x1)
            [2, 7, 3],
            [2, 6, 7],  # back (y1)
            [3, 4, 0],
            [3, 7, 4],  # left (x0)
        ],
        dtype=np.int64,
    )
    return corners, tris


# ── display scale (gr337751) ────────────────────────────────────────────
#
# The DB stores every pose/envelope in raw SI metres; a nanometre-scale
# design's own bounding-box diagonal (~1e-9) sits outside three.js's
# camera near/far-plane working range, rendering a blank canvas even
# though the scene is otherwise correct. This is a DISPLAY-ONLY fix:
# :func:`scene_scale` picks a power-of-ten multiplier and
# :func:`build_scene`/:func:`build_shapes_node` apply it to every
# emitted vertex/edge/connectivity coordinate — the store round trip
# (``adapter.load_tree``) and everything this module reads off ``tree``
# stay SI throughout.


def _overall_bounds(
    tree: Tree[BlockNode, Connect], effective_envelope: EffectiveEnvelopeFn
) -> tuple[NDArray[np.float64], NDArray[np.float64]] | None:
    """Axis-aligned bounds spanning every block's own pose point AND its
    effective-envelope world mesh — :func:`scene_scale`'s "overall
    bounds" input (poses alone would miss a block whose envelope extends
    far past its own pose point; meshes alone would miss a bare-pose
    block whose envelope is absent/invalid). ``None`` for an empty tree."""
    pts: list[NDArray[np.float64]] = []
    for node in tree.blocks.values():
        pts.append(np.array(node.pose, dtype=np.float64))
        env = effective_envelope(tree, node)
        mesh = world_mesh(env, node.pose, node.rot) if env else None
        if mesh is not None:
            pts.append(mesh[0].min(axis=0))
            pts.append(mesh[0].max(axis=0))
    if not pts:
        return None
    arr = np.array(pts, dtype=np.float64)
    return arr.min(axis=0), arr.max(axis=0)


def scene_scale(
    tree: Tree[BlockNode, Connect], effective_envelope: EffectiveEnvelopeFn
) -> float:
    """A per-scene power-of-ten display multiplier bringing the whole
    design's overall bounding-box diagonal into three.js's comfortable
    working range (roughly O(1-1000)) — see the module-level note above.
    ``1.0`` (no-op) for an empty/degenerate tree, so an already-legible
    metre-scale design is left alone rather than nudged for no reason."""
    bounds = _overall_bounds(tree, effective_envelope)
    if bounds is None:
        return 1.0
    lo, hi = bounds
    diag = float(np.linalg.norm(hi - lo))
    if diag <= 0.0 or not math.isfinite(diag):
        return 1.0
    # Power-of-ten factor centring the scaled diagonal near the middle of
    # the target decade band (10**1.5 ≈ 32) rather than an edge, so a
    # design already inside the working range still gets at most a
    # modest nudge instead of drifting all the way to one boundary.
    exponent = round(1.5 - math.log10(diag))
    return 10.0**exponent


# ── leaf/group construction ────────────────────────────────────────────


@dataclass
class Assembly3D:
    """The side table :func:`build_shapes_node` fills in as it walks the
    plan — not part of the vendored viewer's own ``Shapes`` schema."""

    #: block name -> its own primary node path (the group path if it has
    #: rendered children, else the leaf path) — connectivity/mermaid/
    #: explode all key off this, not the doubled self-leaf path.
    primary_path: dict[str, str] = field(default_factory=dict)


def _shape_leaf(
    path: str,
    name: str,
    mesh: tuple[NDArray[np.float64], NDArray[np.int64]],
    colour: str,
) -> dict[str, Any]:
    verts, tris = mesh
    return {
        "version": 3,
        "id": path,
        "name": name,
        "type": "shapes",
        "subtype": "solid",
        "state": [1, 1],
        "color": colour,
        "alpha": 1.0,
        "renderback": False,
        "texture": None,
        "accuracy": None,
        "bb": None,
        "loc": None,
        "shape": shape_json(verts, tris),
    }


_SHAPE_COLOUR = "#8a9bb0"
_BOX_COLOUR = "#cbd5e1"


def build_shapes_node(
    tree: Tree[BlockNode, Connect],
    effective_envelope: EffectiveEnvelopeFn,
    kids: dict[str, list[str]],
    plan: VisiblePlan,
    id_by_name: dict[str, int],
    name: str,
    path_prefix: str,
    assembly: Assembly3D,
    seen: set[str] | None = None,
    scale: float = 1.0,
) -> dict[str, Any] | None:
    """Recursively build the ``Shapes`` node for ``name`` (module
    docstring: leaf id ends in the DB block id; a node with both its own
    geometry and visible children doubles its last segment). Records
    ``name``'s own primary path into ``assembly.primary_path`` as a side
    effect. Returns ``None`` when there is nothing to draw (bad/absent
    envelope, no descendant geometry either) — dropped by the caller,
    matching the SVG projector's own honest-absence convention.

    ``scale`` (default ``1.0``, a no-op) is :func:`scene_scale`'s
    display-only multiplier (gr337751) — applied to every emitted
    vertex, never to anything read back off ``tree``.

    ``seen`` guards against a stored parent cycle: ``plan_visibility``
    legitimately marks every node of a cyclic ``parent`` chain "shape"
    (reachable only via an ``isolate=`` that names a cycle member — a
    cyclic pair has no ``parent is None`` root of its own, so the cycle
    only bites when entered explicitly), so without a visited guard the
    ``visible_kids`` recursion below would walk it forever. ONE mutable
    set for the whole walk (mutated in place, never copied per branch) —
    "a block can only ever have one parent, so a non-cyclic tree visits
    each name exactly once" — same global-dedup convention
    :func:`~precis_web.blocktree_svg.plan_visibility`'s own ``visited``
    set uses, not a per-path copy."""
    if seen is None:
        seen = set()
    if name in seen:
        return None
    seen.add(name)
    kind = plan.shown.get(name)
    if kind is None:
        return None
    block_id = id_by_name.get(name)
    if block_id is None:
        return None
    node = tree.blocks[name]
    own_path = f"{path_prefix}/{block_id}"

    if kind == "box":
        pts: list[NDArray[np.float64]] = []
        for desc in [name, *_descendants_of(name, kids)]:
            dnode = tree.blocks.get(desc)
            if dnode is None:
                continue
            env = effective_envelope(tree, dnode)
            mesh = world_mesh(env, dnode.pose, dnode.rot) if env else None
            if mesh is not None:
                pts.append(mesh[0])
        if not pts:
            return None
        all_pts = np.concatenate(pts, axis=0) * scale
        mn, mx = all_pts.min(axis=0), all_pts.max(axis=0)
        lo: Vec3f = (float(mn[0]), float(mn[1]), float(mn[2]))
        hi: Vec3f = (float(mx[0]), float(mx[1]), float(mx[2]))
        assembly.primary_path[name] = own_path
        return _shape_leaf(own_path, name, _box_mesh(lo, hi), _BOX_COLOUR)

    # kind == "shape"
    visible_kids = sorted(k for k in kids.get(name, []) if k in plan.shown)
    env = effective_envelope(tree, node)
    raw_mesh = world_mesh(env, node.pose, node.rot) if env else None
    mesh = (raw_mesh[0] * scale, raw_mesh[1]) if raw_mesh is not None else None

    if not visible_kids:
        if mesh is None:
            return None
        assembly.primary_path[name] = own_path
        return _shape_leaf(own_path, name, mesh, _SHAPE_COLOUR)

    parts: list[dict[str, Any]] = []
    if mesh is not None:
        parts.append(_shape_leaf(f"{own_path}/{block_id}", name, mesh, _SHAPE_COLOUR))
    for k in visible_kids:
        child = build_shapes_node(
            tree,
            effective_envelope,
            kids,
            plan,
            id_by_name,
            k,
            own_path,
            assembly,
            seen,
            scale=scale,
        )
        if child is not None:
            parts.append(child)
    assembly.primary_path[name] = own_path
    if not parts:
        return None
    return {
        "version": 3,
        "id": own_path,
        "name": name,
        "loc": None,
        "parts": parts,
    }


def _descendants_of(name: str, kids: dict[str, list[str]]) -> list[str]:
    out: list[str] = []
    seen = {name}
    stack = list(kids.get(name, []))
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        out.append(n)
        stack.extend(kids.get(n, []))
    return out


# ── connectivity (drawn, not inferred) ──────────────────────────────────


def _visible_ancestor(
    tree: Tree[BlockNode, Connect], name: str, shown: Mapping[str, str]
) -> str | None:
    """Walk from ``name`` up through ``parent`` until hitting a name
    present in ``shown`` (the current render plan) — a connect endpoint
    that's collapsed inside a box, or hidden by isolation, resolves to
    whatever visible ancestor stands in for it. ``None`` when nothing on
    the chain is visible (the endpoint is entirely outside this render)."""
    cur: str | None = name
    seen: set[str] = set()
    while cur is not None and cur not in seen:
        if cur in shown:
            return cur
        seen.add(cur)
        node = tree.blocks.get(cur)
        cur = node.parent if node is not None else None
    return None


@dataclass
class ConnLine:
    path: str
    a_name: str
    b_name: str
    a_path: str
    b_path: str
    label: str
    colour: str
    #: gr337917 — the kernel's own closest-point witness between the
    #: connect's LITERAL ``a_block``/``b_block`` envelopes (never the
    #: possibly-collapsed ancestor ``a_path``/``b_path`` resolve to), in
    #: the SAME display-scaled coordinates :func:`build_scene` emits
    #: everywhere else. ``None`` when it couldn't be computed (an
    #: absent/unparseable envelope, or the SDF query itself failing) —
    #: :func:`connectivity_leaf` then falls back to the historical
    #: pose-to-pose segment.
    witness: Vec3f | None = None
    witness_gap: float = 0.0


#: DSL param keys that are not lengths (counts/angles) — mirrors
#: :mod:`precis_se.validate`'s own ``_UNSCALED_KEYS``: everything else a
#: parsed envelope carries is a length in the design's own metres, so
#: only these skip :func:`_witness_point`'s scale multiply.
_UNSCALED_KEYS = frozenset({"n", "angle"})


#: gr337917 perf follow-up (adjacent to the gr337045 >120s validate
#: hang): ``cad_relate.clearance`` is a full multi-seed SDF search
#: (relate.py's own coarse grid + closest-point seeds + several
#: 80-iteration descents per call) — expensive enough that calling it
#: uncached, once per connect, on every ``scene3d.json`` request would
#: reproduce that hang on a design with many connects. Bounded LRU
#: keyed on the full witness identity (both envelopes/poses/rots plus
#: the display scale — any of those changing is a genuinely different
#: query); a module-level cache (not per-request) since a design's own
#: geometry changes far less often than its scene gets re-rendered, and
#: a size cap (not a TTL) is enough — stale entries simply age out via
#: eviction, no explicit invalidation needed.
_WITNESS_CACHE_MAX = 1024
_witness_cache: OrderedDict[tuple[Any, ...], tuple[Vec3f, float] | None] = OrderedDict()

#: Hard cap on ACTUAL witness computations (cache hits don't count —
#: they're cheap) per :func:`connectivity_lines` call — independent of
#: the cache above: a pathological design with hundreds of distinct
#: connects must not multiply the per-request cost unboundedly even on
#: an all-cache-miss first render. Lines beyond the budget fall back to
#: the historical pose-to-pose segment, same as any other
#: witness-computation failure.
_WITNESS_BUDGET_PER_SCENE = 64


def _witness_point(
    tree: Tree[BlockNode, Connect],
    effective_envelope: EffectiveEnvelopeFn,
    a_block: str,
    b_block: str,
    scale: float,
) -> tuple[Vec3f, float] | None:
    """The actual contact anchor for a drawn connect (gr337917) — the
    cad kernel's own closest-point witness
    (:func:`precis.cad.relate.clearance`) between ``a_block``'s and
    ``b_block``'s EFFECTIVE envelopes, so the connect line lands where
    the parts actually meet instead of floating between two bare pose
    points that may be corners, not centres.

    Builds a throwaway 2-component :class:`~precis.cad.graph.Design`
    with both envelope lengths and pose scaled by the SAME ``scale``
    :func:`build_scene` already applies to every other emitted
    coordinate (gr337751's :func:`scene_scale`) — the returned
    point/gap come back already in DISPLAY units, no separate unscale
    step needed. ``None`` on ANY failure (a missing/unparseable
    envelope, a degenerate SDF query, ...) — the caller falls back to
    the historical pose-to-pose segment rather than ever raising into a
    scene build.

    Memoized in :data:`_witness_cache` (module docstring above) — a
    repeat query for the SAME two envelopes at the SAME poses/rots/scale
    returns the cached answer (including a cached ``None`` failure)
    without re-running the SDF search."""
    a_node = tree.blocks.get(a_block)
    b_node = tree.blocks.get(b_block)
    if a_node is None or b_node is None:
        return None
    a_env = effective_envelope(tree, a_node)
    b_env = effective_envelope(tree, b_node)
    if not a_env or not b_env:
        return None
    key = (
        a_env,
        tuple(float(c) for c in a_node.pose),
        tuple(float(c) for c in a_node.rot),
        b_env,
        tuple(float(c) for c in b_node.pose),
        tuple(float(c) for c in b_node.rot),
        float(scale),
    )
    if key in _witness_cache:
        _witness_cache.move_to_end(key)
        return _witness_cache[key]
    result = _compute_witness_point(
        a_block, a_env, a_node, b_block, b_env, b_node, scale
    )
    _witness_cache[key] = result
    if len(_witness_cache) > _WITNESS_CACHE_MAX:
        _witness_cache.popitem(last=False)  # evict the least-recently-used entry
    return result


def _compute_witness_point(
    a_block: str,
    a_env: str,
    a_node: BlockNode,
    b_block: str,
    b_env: str,
    b_node: BlockNode,
    scale: float,
) -> tuple[Vec3f, float] | None:
    """The uncached SDF search behind :func:`_witness_point` — split out
    purely so the cache wrapper above stays readable; never call this
    directly outside that wrapper."""
    try:
        design = CadDesign()
        for name, env, node in ((a_block, a_env, a_node), (b_block, b_env, b_node)):
            spec = cad_dsl.parse(env)
            if scale != 1.0:
                spec = cad_dsl.ShapeSpec(
                    spec.alias,
                    {
                        k: (v if k in _UNSCALED_KEYS else v * scale)
                        for k, v in spec.params.items()
                    },
                )
            prim = cad_dsl.build(spec)
            pose_scaled = cad_as_vec3([float(c) * scale for c in node.pose])
            xform = cad_pose(pose_scaled, cad_as_vec3(node.rot))
            design.add_component(name, design.prim(name, prim, xform))
        result = cad_relate.clearance(design, a_block, b_block)
    except Exception:
        # Best-effort — see connectivity_leaf's own pose-to-pose fallback.
        return None
    p = result.point
    return (float(p[0]), float(p[1]), float(p[2])), float(result.gap)


def connectivity_lines(
    tree: Tree[BlockNode, Connect],
    plan: VisiblePlan,
    primary_path: dict[str, str],
    label_fn: Any,
    colour_fn: Any,
    group_path: str,
    *,
    effective_envelope: EffectiveEnvelopeFn | None = None,
    scale: float = 1.0,
) -> list[ConnLine]:
    """One :class:`ConnLine` per distinct visible-endpoint pair among
    ``tree.connects`` (module docstring: DRAWN connectivity, resolved
    through collapsed boxes/isolation to the nearest visible
    representative — two connects that both resolve to the same pair, or
    to the same block on both ends, contribute at most one line).

    ``effective_envelope``/``scale`` (both optional; omitting either
    leaves every line witness-less — the historical pose-to-pose-only
    shape older callers/fixtures still get) feed :func:`_witness_point`
    per connect, anchoring the drawn line at the kernel's own closest-
    point witness between the connect's LITERAL ``a_block``/``b_block``
    envelopes (gr337917) rather than their bare pose points, up to
    :data:`_WITNESS_BUDGET_PER_SCENE` witness computations per call —
    a design with more distinct connects than that just gets the
    historical pose-to-pose segment for the overflow, same as any other
    witness-computation failure, rather than letting one pathological
    design multiply the per-request SDF-search cost unboundedly. Each
    line's own ``label`` is now ``"a.port—b.port (joint class)"`` (the
    stability report's own ``subject`` convention,
    :mod:`precis_se.stability`) — the tree row/mermaid edge previously
    showed only the bare joint class, unidentifiable among several
    connects to the same block."""
    seen: set[tuple[str, str]] = set()
    lines: list[ConnLine] = []
    i = 0
    witness_budget = _WITNESS_BUDGET_PER_SCENE
    for c in tree.connects:
        a_vis = _visible_ancestor(tree, c.a_block, plan.shown)
        b_vis = _visible_ancestor(tree, c.b_block, plan.shown)
        if a_vis is None or b_vis is None or a_vis == b_vis:
            continue
        key = (a_vis, b_vis) if a_vis < b_vis else (b_vis, a_vis)
        if key in seen:
            continue
        seen.add(key)
        a_path = primary_path.get(a_vis)
        b_path = primary_path.get(b_vis)
        if a_path is None or b_path is None:
            continue
        witness: Vec3f | None = None
        witness_gap = 0.0
        if effective_envelope is not None and witness_budget > 0:
            witness_budget -= 1
            found = _witness_point(
                tree, effective_envelope, c.a_block, c.b_block, scale
            )
            if found is not None:
                witness, witness_gap = found
        subject = f"{c.a_block}.{c.a_port}—{c.b_block}.{c.b_port}"
        lines.append(
            ConnLine(
                path=f"{group_path}/c{i}",
                a_name=a_vis,
                b_name=b_vis,
                a_path=a_path,
                b_path=b_path,
                label=f"{subject} ({label_fn(c)})",
                colour=colour_fn(c),
                witness=witness,
                witness_gap=witness_gap,
            )
        )
        i += 1
    return lines


def _offset_vector(
    a: Vec3f, b: Vec3f, up: Vec3f = (0.0, 0.0, 1.0), *, eps: float
) -> Vec3f:
    """A sideways nudge off the straight ``a``→``b`` line — module
    docstring's "connectivity link geometry" simplification. ``eps`` is
    the caller's own scale-relative "meaningfully zero" tolerance
    (gr337751 — a flat ``1e-9`` absolute floor degenerates every offset
    to zero at nanometre scale, since ``a``/``b`` themselves sit near
    that magnitude there)."""
    d = np.array(b) - np.array(a)
    n = np.cross(d, np.array(up))
    if float(np.linalg.norm(n)) < eps:
        n = np.cross(d, np.array([1.0, 0.0, 0.0]))
    norm = float(np.linalg.norm(n))
    if norm < eps:
        return (0.0, 0.0, 0.0)
    u = n / norm
    return (float(u[0]), float(u[1]), float(u[2]))


def connectivity_leaf(
    line: ConnLine,
    a_pose: Vec3f,
    b_pose: Vec3f,
    offset_len: float,
    *,
    eps: float,
    min_stub: float,
) -> dict[str, Any]:
    """The drawn link as an ``type: "edges"`` leaf (no solid geometry).

    gr337917: when the connect's own kernel witness point is available
    (``line.witness``), the segment runs THROUGH it along the a→b pose
    direction, half the ACTUAL surface gap each way (the witness is the
    midpoint between the two nearest surface points, so ± half the gap
    recovers roughly where each surface is) — floored to ``min_stub`` so
    a touching/interfering pair (gap ~0) still draws a visible marker,
    per the gripe's own "single witness point ± a short stub" fallback
    shape. Falls back to the HISTORICAL pose-to-pose segment, offset
    sideways so it reads as a separate overlay rather than tunnelling
    through solid geometry (module docstring's "connectivity link
    geometry" simplification), only when no witness could be computed."""
    if line.witness is not None:
        centre = np.array(line.witness, dtype=np.float64)
        d = np.array(b_pose) - np.array(a_pose)
        norm = float(np.linalg.norm(d))
        unit = d / norm if norm > eps else np.array([1.0, 0.0, 0.0])
        half = max(abs(line.witness_gap) / 2.0, min_stub)
        pa_arr = centre - unit * half
        pb_arr = centre + unit * half
        flat = [float(c) for c in (*pa_arr, *pb_arr)]
    else:
        ov = _offset_vector(a_pose, b_pose, eps=eps)
        off: Vec3f = (ov[0] * offset_len, ov[1] * offset_len, ov[2] * offset_len)
        pa: Vec3f = (a_pose[0] + off[0], a_pose[1] + off[1], a_pose[2] + off[2])
        pb: Vec3f = (b_pose[0] + off[0], b_pose[1] + off[1], b_pose[2] + off[2])
        flat = [float(c) for c in (*pa, *pb)]
    return {
        "version": 3,
        "id": line.path,
        "name": line.label,
        "type": "edges",
        "state": [3, 1],
        "color": line.colour,
        "alpha": 1.0,
        "width": 3,
        "loc": None,
        "shape": {
            "vertices": [],
            "normals": [],
            "triangles": [],
            "triangles_per_face": [],
            "edges": flat,
            "segments_per_edge": [1],
            "obj_vertices": [],
            "face_types": [],
            "edge_types": [],
        },
    }


# ── explode (along drawn attachment directions) ─────────────────────────


def explode_offsets(
    tree: Tree[BlockNode, Connect],
    lines: list[ConnLine],
    primary_path: dict[str, str],
    magnitude: float,
) -> dict[str, Vec3f]:
    """``path -> [dx, dy, dz]`` per visible top node — module docstring's
    "along attachment directions" explode: each node moves along the sum
    of its own unit vectors away from every block it is DRAWN-connected
    to (falling back to away-from-centroid when a node has no drawn
    connects at all, so it still moves somewhere).

    "Meaningfully zero" (a genuinely coincident pair, or a genuinely
    self-cancelling sum) is judged against a tolerance RELATIVE to the
    whole tree's own pose spread (:func:`pose_spread`), not a flat
    absolute constant (gr337751 — a nanometre-scale design's own pose
    deltas sit at/under a flat ``1e-9`` floor, so every explode offset
    silently zeroed; same ``LINEAR_REL_EPS`` relative-tolerance pattern
    as :mod:`precis.cad.primitives`'s ``_linear_eps``)."""
    eps = LINEAR_REL_EPS * pose_spread(tree)
    by_path: dict[str, list[NDArray[np.float64]]] = {}
    for line in lines:
        a = np.array(tree.blocks[line.a_name].pose, dtype=np.float64)
        b = np.array(tree.blocks[line.b_name].pose, dtype=np.float64)
        d = b - a
        norm = float(np.linalg.norm(d))
        if norm < eps:
            continue
        unit = d / norm
        by_path.setdefault(line.a_path, []).append(-unit)
        by_path.setdefault(line.b_path, []).append(unit)

    all_poses = [
        np.array(node.pose, dtype=np.float64)
        for name, node in tree.blocks.items()
        if name in primary_path
    ]
    centroid = np.mean(all_poses, axis=0) if all_poses else np.zeros(3)

    offsets: dict[str, Vec3f] = {}
    for name, path in primary_path.items():
        vecs = by_path.get(path)
        if vecs:
            total = np.sum(vecs, axis=0)
            norm = float(np.linalg.norm(total))
            direction = total / norm if norm > eps else np.zeros(3)
        else:
            pose = np.array(tree.blocks[name].pose, dtype=np.float64)
            d = pose - centroid
            norm = float(np.linalg.norm(d))
            direction = d / norm if norm > eps else np.zeros(3)
        scaled = direction * magnitude
        offsets[path] = (float(scaled[0]), float(scaled[1]), float(scaled[2]))
    return offsets


def pose_spread(tree: Tree[BlockNode, Connect]) -> float:
    """The diagonal of the bounding box of every block's own pose point —
    the scale reference :func:`connectivity_leaf`'s offset and
    :func:`explode_offsets`'s magnitude are a fixed fraction of, so both
    stay legible whether the design is millimetre- or metre-scaled.

    The empty/degenerate-spread fallback (``1.0``, a dimensionless
    sentinel — every block coincides, or there's only one) tests the
    diagonal against exact zero, never an absolute epsilon (gr337751): a
    fixed ``1e-9`` cutoff would itself misfire on a genuinely tiny but
    REAL nanometre-scale spread, the same absolute-epsilon-at-the-wrong-
    magnitude bug this whole function exists to avoid for its callers."""
    poses = [node.pose for node in tree.blocks.values()]
    if not poses:
        return 1.0
    arr = np.array(poses, dtype=np.float64)
    lo = arr.min(axis=0)
    hi = arr.max(axis=0)
    diag = float(np.linalg.norm(hi - lo))
    return diag if diag > 0.0 else 1.0


# ── mermaid topology (linked selection with the 3D view) ────────────────


def _mermaid_escape(text: str) -> str:
    return text.replace('"', "'").replace("[", "(").replace("]", ")")


@dataclass
class Scene3D:
    """The full bundle a ``scene3d.json`` response hands the client: the
    vendored viewer's own ``shapes`` tree, plus the side data (module
    docstring) the client's own JS glue needs — connectivity metadata for
    the linked-selection recolour and the mermaid<->3D id correspondence,
    per-path explode offsets, and the mermaid source itself."""

    shapes: dict[str, Any]
    connections: list[ConnLine]
    explode: dict[str, Vec3f]
    mermaid: str


def build_scene(
    tree: Tree[BlockNode, Connect],
    effective_envelope: EffectiveEnvelopeFn,
    kids: dict[str, list[str]],
    plan: VisiblePlan,
    id_by_name: dict[str, int],
    *,
    root_id: str,
    root_name: str,
    label_fn: Any,
    colour_fn: Any,
) -> Scene3D:
    """Assemble the whole round-2a bundle for one render pass — the
    ``Shapes`` tree (assembly + a ``_connections`` sibling group carrying
    the drawn links), the connectivity metadata, the explode-along-
    attachment offsets, and the linked mermaid topology graph."""
    assembly = Assembly3D()
    parts: list[dict[str, Any]] = []
    # ONE cycle-guard set shared across every render root — mirrors
    # plan_visibility's own single ``visited`` set spanning its whole
    # ``for r in roots`` loop (build_shapes_node's own docstring).
    seen: set[str] = set()
    # gr337751 — every emitted coordinate below is multiplied by this
    # display-only factor; the tree/poses read off ``tree`` stay SI.
    scale = scene_scale(tree, effective_envelope)
    for r in plan.render_roots:
        node = build_shapes_node(
            tree,
            effective_envelope,
            kids,
            plan,
            id_by_name,
            r,
            root_id,
            assembly,
            seen,
            scale=scale,
        )
        if node is not None:
            parts.append(node)

    diag = pose_spread(tree) * scale
    conn_group_path = f"{root_id}/_connections"
    lines = connectivity_lines(
        tree,
        plan,
        assembly.primary_path,
        label_fn,
        colour_fn,
        conn_group_path,
        effective_envelope=effective_envelope,
        scale=scale,
    )
    offset_len = 0.05 * diag
    offset_eps = LINEAR_REL_EPS * diag
    # gr337917 — a touching/interfering pair's own gap collapses toward
    # zero, so the witness-anchored stub still needs a legible minimum
    # size (connectivity_leaf's own docstring).
    min_stub = 0.01 * diag
    conn_parts = []
    for line in lines:
        a_pose = _as_vec3f([c * scale for c in tree.blocks[line.a_name].pose])
        b_pose = _as_vec3f([c * scale for c in tree.blocks[line.b_name].pose])
        conn_parts.append(
            connectivity_leaf(
                line, a_pose, b_pose, offset_len, eps=offset_eps, min_stub=min_stub
            )
        )
    if conn_parts:
        parts.append(
            {
                "version": 3,
                "id": conn_group_path,
                "name": "connections",
                "loc": None,
                "parts": conn_parts,
            }
        )

    shapes = {
        "version": 3,
        "id": root_id,
        "name": root_name,
        "loc": None,
        "normal_len": 0,
        "bb": None,
        "parts": parts,
    }
    offsets = explode_offsets(tree, lines, assembly.primary_path, magnitude=0.3 * diag)
    mermaid = mermaid_topology(plan, id_by_name, lines)
    return Scene3D(shapes=shapes, connections=lines, explode=offsets, mermaid=mermaid)


def mermaid_topology(
    plan: VisiblePlan,
    id_by_name: dict[str, int],
    lines: list[ConnLine],
) -> str:
    """``graph LR`` source for the visible blocks + their drawn connects,
    edges labelled by joint/bond kind (spec §5.8 comment 5(c)). Node ids
    are ``B<block_id>`` — the SAME DB id the 3D leaf paths end in, so the
    client's mermaid<->3D selection link needs no separate lookup table
    either."""
    lines_out = ["graph LR"]
    for name in sorted(plan.shown):
        bid = id_by_name.get(name)
        if bid is None:
            continue
        lines_out.append(f'  B{bid}["{_mermaid_escape(name)}"]')
    for conn in lines:
        a_id = id_by_name.get(conn.a_name)
        b_id = id_by_name.get(conn.b_name)
        if a_id is None or b_id is None:
            continue
        lines_out.append(f'  B{a_id} -- "{_mermaid_escape(conn.label)}" --- B{b_id}')
    return "\n".join(lines_out)
