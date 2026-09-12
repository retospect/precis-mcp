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
* **Connectivity link geometry.** A connect's two endpoints are drawn as
  a straight line between the two blocks' own POSE points (not a solved
  contact point) — the same "one representative point" convention
  :mod:`precis_web.blocktree_svg`'s member overlay uses for axial
  members — nudged sideways by a small, fixed fraction of the design's
  own pose-point spread so it reads as a separate link rather than
  tunnelling through solid geometry. This is a legibility heuristic, not
  a verified clearance: the line can still graze a solid on an unlucky
  orientation.
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
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.typing import NDArray

from precis.blocktree.types import BlockNode, Connect, Tree
from precis.cad.tessellate import apply_rigid, mesh_config
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
) -> dict[str, Any] | None:
    """Recursively build the ``Shapes`` node for ``name`` (module
    docstring: leaf id ends in the DB block id; a node with both its own
    geometry and visible children doubles its last segment). Records
    ``name``'s own primary path into ``assembly.primary_path`` as a side
    effect. Returns ``None`` when there is nothing to draw (bad/absent
    envelope, no descendant geometry either) — dropped by the caller,
    matching the SVG projector's own honest-absence convention.

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
        all_pts = np.concatenate(pts, axis=0)
        mn, mx = all_pts.min(axis=0), all_pts.max(axis=0)
        lo: Vec3f = (float(mn[0]), float(mn[1]), float(mn[2]))
        hi: Vec3f = (float(mx[0]), float(mx[1]), float(mx[2]))
        assembly.primary_path[name] = own_path
        return _shape_leaf(own_path, name, _box_mesh(lo, hi), _BOX_COLOUR)

    # kind == "shape"
    visible_kids = sorted(k for k in kids.get(name, []) if k in plan.shown)
    env = effective_envelope(tree, node)
    mesh = world_mesh(env, node.pose, node.rot) if env else None

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
            tree, effective_envelope, kids, plan, id_by_name, k, own_path, assembly, seen
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


def connectivity_lines(
    tree: Tree[BlockNode, Connect],
    plan: VisiblePlan,
    primary_path: dict[str, str],
    label_fn: Any,
    colour_fn: Any,
    group_path: str,
) -> list[ConnLine]:
    """One :class:`ConnLine` per distinct visible-endpoint pair among
    ``tree.connects`` (module docstring: DRAWN connectivity, resolved
    through collapsed boxes/isolation to the nearest visible
    representative — two connects that both resolve to the same pair, or
    to the same block on both ends, contribute at most one line)."""
    seen: set[tuple[str, str]] = set()
    lines: list[ConnLine] = []
    i = 0
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
        lines.append(
            ConnLine(
                path=f"{group_path}/c{i}",
                a_name=a_vis,
                b_name=b_vis,
                a_path=a_path,
                b_path=b_path,
                label=label_fn(c),
                colour=colour_fn(c),
            )
        )
        i += 1
    return lines


def _offset_vector(a: Vec3f, b: Vec3f, up: Vec3f = (0.0, 0.0, 1.0)) -> Vec3f:
    """A sideways nudge off the straight ``a``→``b`` line — module
    docstring's "connectivity link geometry" simplification."""
    d = np.array(b) - np.array(a)
    n = np.cross(d, np.array(up))
    if float(np.linalg.norm(n)) < 1e-9:
        n = np.cross(d, np.array([1.0, 0.0, 0.0]))
    norm = float(np.linalg.norm(n))
    if norm < 1e-9:
        return (0.0, 0.0, 0.0)
    u = n / norm
    return (float(u[0]), float(u[1]), float(u[2]))


def connectivity_leaf(
    line: ConnLine, a_pose: Vec3f, b_pose: Vec3f, offset_len: float
) -> dict[str, Any]:
    """The drawn link as an ``type: "edges"`` leaf (no solid geometry) —
    two endpoints, offset sideways so the line reads as a separate
    overlay rather than tunnelling through the blocks it connects."""
    ov = _offset_vector(a_pose, b_pose)
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
    connects at all, so it still moves somewhere)."""
    by_path: dict[str, list[NDArray[np.float64]]] = {}
    for line in lines:
        a = np.array(tree.blocks[line.a_name].pose, dtype=np.float64)
        b = np.array(tree.blocks[line.b_name].pose, dtype=np.float64)
        d = b - a
        norm = float(np.linalg.norm(d))
        if norm < 1e-9:
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
            direction = total / norm if norm > 1e-9 else np.zeros(3)
        else:
            pose = np.array(tree.blocks[name].pose, dtype=np.float64)
            d = pose - centroid
            norm = float(np.linalg.norm(d))
            direction = d / norm if norm > 1e-9 else np.zeros(3)
        scaled = direction * magnitude
        offsets[path] = (float(scaled[0]), float(scaled[1]), float(scaled[2]))
    return offsets


def pose_spread(tree: Tree[BlockNode, Connect]) -> float:
    """The diagonal of the bounding box of every block's own pose point —
    the scale reference :func:`connectivity_leaf`'s offset and
    :func:`explode_offsets`'s magnitude are a fixed fraction of, so both
    stay legible whether the design is millimetre- or metre-scaled."""
    poses = [node.pose for node in tree.blocks.values()]
    if not poses:
        return 1.0
    arr = np.array(poses, dtype=np.float64)
    lo = arr.min(axis=0)
    hi = arr.max(axis=0)
    diag = float(np.linalg.norm(hi - lo))
    return diag if diag > 1e-9 else 1.0


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
    for r in plan.render_roots:
        node = build_shapes_node(
            tree, effective_envelope, kids, plan, id_by_name, r, root_id, assembly, seen
        )
        if node is not None:
            parts.append(node)

    diag = pose_spread(tree)
    conn_group_path = f"{root_id}/_connections"
    lines = connectivity_lines(
        tree, plan, assembly.primary_path, label_fn, colour_fn, conn_group_path
    )
    offset_len = 0.05 * diag
    conn_parts = []
    for line in lines:
        a_pose = _as_vec3f(tree.blocks[line.a_name].pose)
        b_pose = _as_vec3f(tree.blocks[line.b_name].pose)
        conn_parts.append(connectivity_leaf(line, a_pose, b_pose, offset_len))
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
