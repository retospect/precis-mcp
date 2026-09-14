"""Pure-function coverage for :mod:`precis_web.blocktree_3d` — the
three-cad-viewer scene projection behind the round-2a 3D reader (gr335242
comment 5), independent of any store/handler. Mirrors
``test_blocktree_svg.py``'s style: bare :class:`Tree`/:class:`BlockNode`
fixtures, no DB."""

from __future__ import annotations

import math

import numpy as np
import pytest

from precis.blocktree.types import BlockNode, Connect, Tree
from precis_web import blocktree_3d
from precis_web.blocktree_3d import (
    Assembly3D,
    ConnLine,
    _witness_point,
    build_scene,
    build_shapes_node,
    connectivity_leaf,
    connectivity_lines,
    explode_offsets,
    feature_edges,
    group_planar_faces,
    mermaid_topology,
    pose_spread,
    scene_scale,
    shape_json,
    world_mesh,
)
from precis_web.blocktree_svg import children_map, plan_visibility


def _tree(**blocks: BlockNode) -> Tree[BlockNode, Connect]:
    t: Tree[BlockNode, Connect] = Tree()
    t.blocks = dict(blocks)
    return t


def _effective_envelope(_tree: object, node: BlockNode) -> str | None:
    return node.envelope


def _fork_tree() -> Tree[BlockNode, Connect]:
    return _tree(
        hub=BlockNode(name="hub", pose=[0, 0, 0], envelope="cyl:r0.02h0.05"),
        rim=BlockNode(name="rim", pose=[0, 0, 0.3], envelope="torus:R0.3r0.01"),
        fork=BlockNode(name="fork", pose=[0, 0, -0.1], envelope="box:w0.04d0.02h0.08"),
        fork_arm=BlockNode(
            name="fork_arm",
            parent="fork",
            pose=[0, 0, -0.15],
            envelope="box:w0.01d0.01h0.05",
        ),
        fork_tip=BlockNode(
            name="fork_tip",
            parent="fork_arm",
            pose=[0, 0, -0.18],
            envelope="sphere:r0.005",
        ),
    )


_IDS = {"hub": 1, "rim": 2, "fork": 3, "fork_arm": 4, "fork_tip": 5}


# ── mesh geometry ────────────────────────────────────────────────────────


def test_world_mesh_box_is_posed_in_world_coords() -> None:
    mesh = world_mesh("box:w1d1h1", pose=[10.0, 0.0, 0.0], rot=[0.0, 0.0, 0.0])
    assert mesh is not None
    verts, _tris = mesh
    xs = verts[:, 0]
    assert xs.min() == pytest.approx(9.5) and xs.max() == pytest.approx(10.5)


def test_world_mesh_honest_absence_on_bad_config() -> None:
    assert world_mesh("not-a-shape", pose=[0, 0, 0], rot=[0, 0, 0]) is None
    assert world_mesh("chamfer:1x45", pose=[0, 0, 0], rot=[0, 0, 0]) is None


def test_group_planar_faces_box_recovers_six_faces() -> None:
    verts, tris = world_mesh("box:w1d1h1", pose=[0, 0, 0], rot=[0, 0, 0])  # type: ignore[misc]
    face_of = group_planar_faces(verts, tris)
    assert len(set(face_of)) == 6
    # every triangle on the same reconstructed face keeps the same id.
    assert len(face_of) == len(tris)


def test_feature_edges_box_has_twelve_edges() -> None:
    verts, tris = world_mesh("box:w1d1h1", pose=[0, 0, 0], rot=[0, 0, 0])  # type: ignore[misc]
    face_of = group_planar_faces(verts, tris)
    edges = feature_edges(verts, tris, face_of)
    assert len(edges) == 12


def test_shape_json_arrays_are_internally_consistent() -> None:
    verts, tris = world_mesh("box:w1d1h1", pose=[0, 0, 0], rot=[0, 0, 0])  # type: ignore[misc]
    payload = shape_json(verts, tris)
    assert len(payload["vertices"]) == len(payload["normals"])
    assert sum(payload["triangles_per_face"]) * 3 == len(payload["triangles"])
    assert len(payload["face_types"]) == len(payload["triangles_per_face"])
    assert len(payload["edge_types"]) == len(payload["segments_per_edge"])
    assert len(payload["edges"]) == sum(payload["segments_per_edge"]) * 6


# ── shapes tree construction ─────────────────────────────────────────────


def test_build_shapes_node_leaf_path_ends_in_the_db_block_id() -> None:
    tree = _fork_tree()
    kids = children_map(tree)
    plan = plan_visibility(tree, kids, level="refined", isolate=None)
    assembly = Assembly3D()
    node = build_shapes_node(
        tree, _effective_envelope, kids, plan, _IDS, "hub", "/se-x", assembly
    )
    assert node is not None
    assert node["id"] == "/se-x/1"  # hub's own db id, no lookup table needed
    assert node["type"] == "shapes" and node["subtype"] == "solid"


def test_build_shapes_node_doubles_id_when_a_block_has_geometry_and_children() -> None:
    tree = _fork_tree()
    kids = children_map(tree)
    plan = plan_visibility(tree, kids, level="refined", isolate=None)
    assembly = Assembly3D()
    node = build_shapes_node(
        tree, _effective_envelope, kids, plan, _IDS, "fork", "/se-x", assembly
    )
    assert node is not None
    assert node["id"] == "/se-x/3"  # the group
    assert "parts" in node
    self_leaf = next(p for p in node["parts"] if p["id"] == "/se-x/3/3")
    assert self_leaf["type"] == "shapes"
    child = next(p for p in node["parts"] if p["id"] == "/se-x/3/4")
    assert child["name"] == "fork_arm"
    assert (
        assembly.primary_path["fork"] == "/se-x/3"
    )  # group path, not the doubled leaf


def test_build_shapes_node_envelope_level_collapses_fork_to_a_box_leaf() -> None:
    tree = _fork_tree()
    kids = children_map(tree)
    plan = plan_visibility(tree, kids, level="envelope", isolate=None)
    assembly = Assembly3D()
    node = build_shapes_node(
        tree, _effective_envelope, kids, plan, _IDS, "fork", "/se-x", assembly
    )
    assert node is not None
    assert node["id"] == "/se-x/3"
    assert node["type"] == "shapes"  # a leaf, not a group -- no 'parts'
    assert "parts" not in node
    # the box must at least cover fork's own footprint, per the box-covers-
    # hidden-extent convention shared with the SVG projector.
    verts = node["shape"]["obj_vertices"]
    xs = verts[0::3]
    assert max(xs) - min(xs) >= 0.04 - 1e-9


def test_build_shapes_node_stops_on_a_stored_parent_cycle() -> None:
    """A two-block mutual-parent cycle: ``plan_visibility`` legitimately
    marks BOTH members "shape" (test_blocktree_svg.py's own
    ``test_plan_visibility_stops_on_a_stored_parent_cycle`` proves
    ``plan.shown == {"a": "shape", "b": "shape"}``) — the SVG route
    renders this design fine (it never recurses on ``kids``, only reads
    ``plan.shown``), so the 3D builder's OWN recursion through ``kids``
    must be guarded the same way, or this is an infinite-recursion
    500 where the SVG route is a clean 200."""
    tree = _tree(
        a=BlockNode(name="a", parent="b", envelope="cyl:r1h1"),
        b=BlockNode(name="b", parent="a", envelope="cyl:r1h1"),
    )
    kids = children_map(tree)
    plan = plan_visibility(tree, kids, level="refined", isolate="a")
    assert plan.shown == {"a": "shape", "b": "shape"}  # the shared-plan claim

    assembly = Assembly3D()
    node = build_shapes_node(
        tree, _effective_envelope, kids, plan, {"a": 1, "b": 2}, "a", "/se-x", assembly
    )
    assert node is not None  # renders rather than raising RecursionError

    seen_ids: set[str] = set()

    def _walk(n: dict) -> None:
        assert n["id"] not in seen_ids  # each id appears exactly once
        seen_ids.add(n["id"])
        for p in n.get("parts", []):
            _walk(p)

    _walk(node)
    assert "/se-x/1" in seen_ids and "/se-x/1/2" in seen_ids


def test_build_scene_stops_on_a_stored_parent_cycle() -> None:
    """End-to-end: ``build_scene`` (what the ``scene3d.json`` route
    calls) must also survive the same cyclic design, not just the bare
    node builder — a regression here is the actual RecursionError the
    live route would hit."""
    tree = _tree(
        a=BlockNode(name="a", parent="b", envelope="cyl:r1h1"),
        b=BlockNode(name="b", parent="a", envelope="cyl:r1h1"),
    )
    kids = children_map(tree)
    plan = plan_visibility(tree, kids, level="refined", isolate="a")
    scene = build_scene(
        tree,
        _effective_envelope,
        kids,
        plan,
        {"a": 1, "b": 2},
        root_id="/se-x",
        root_name="x",
        label_fn=lambda c: "tie",
        colour_fn=lambda c: "#16a34a",
    )
    assert scene.shapes["id"] == "/se-x"


# ── connectivity + explode ────────────────────────────────────────────────


def _connected_tree() -> Tree[BlockNode, Connect]:
    tree = _tree(
        hub=BlockNode(name="hub", pose=[0, 0, 0], envelope="cyl:r0.02h0.05"),
        rim=BlockNode(name="rim", pose=[0, 0, 0.3], envelope="torus:R0.3r0.01"),
    )
    tree.connects = [Connect(a_block="hub", a_port="pin", b_block="rim", b_port="pin")]
    return tree


def test_connectivity_lines_resolves_through_a_collapsed_box() -> None:
    tree = _fork_tree()
    tree.connects = [
        Connect(a_block="hub", a_port="pin", b_block="fork_tip", b_port="pin")
    ]
    kids = children_map(tree)
    plan = plan_visibility(tree, kids, level="envelope", isolate=None)
    assembly = Assembly3D()
    for r in plan.render_roots:
        build_shapes_node(
            tree, _effective_envelope, kids, plan, _IDS, r, "/se-x", assembly
        )
    lines = connectivity_lines(
        tree, plan, assembly.primary_path, lambda c: "tie", lambda c: "#16a34a", "/g"
    )
    assert len(lines) == 1
    # fork_tip is collapsed inside 'fork's box -> the line's far endpoint
    # resolves to 'fork', not the hidden 'fork_tip'.
    assert lines[0].a_name == "hub" and lines[0].b_name == "fork"


def test_connectivity_lines_dedupes_and_drops_self_loops() -> None:
    tree = _connected_tree()
    tree.connects.append(
        Connect(a_block="hub", a_port="pin2", b_block="rim", b_port="pin2")
    )
    kids = children_map(tree)
    plan = plan_visibility(tree, kids, level="refined", isolate=None)
    assembly = Assembly3D()
    for r in plan.render_roots:
        build_shapes_node(
            tree, _effective_envelope, kids, plan, _IDS, r, "/se-x", assembly
        )
    lines = connectivity_lines(
        tree, plan, assembly.primary_path, lambda c: "tie", lambda c: "#16a34a", "/g"
    )
    assert len(lines) == 1  # both connects resolve to the same (hub, rim) pair


# ── witness-anchored connect geometry (gr337917) ──────────────────────────
#
# The connect overlay used to be a straight pose-to-pose segment nudged
# sideways by a fixed fraction of the design's diagonal — with
# base-at-pose envelope conventions the pose point can be a corner, not
# a part's centre, so the offset segment could float through empty
# space nowhere near the actual joint (user report, se:unicycle-mk2).
# Spheres are pose-CENTRED regardless of any base-vs-centre convention
# debate (only ``box`` has one), so these fixtures isolate the witness
# math from that question entirely.


def _overlapping_pair() -> Tree[BlockNode, Connect]:
    tree = _tree(
        a=BlockNode(name="a", pose=[0.0, 0.0, 0.0], envelope="sphere:r0.6"),
        b=BlockNode(name="b", pose=[0.5, 0.0, 0.0], envelope="sphere:r0.6"),
    )
    tree.connects = [Connect(a_block="a", a_port="p", b_block="b", b_port="p")]
    return tree


def _touching_pair() -> Tree[BlockNode, Connect]:
    tree = _tree(
        a=BlockNode(name="a", pose=[0.0, 0.0, 0.0], envelope="sphere:r0.5"),
        b=BlockNode(name="b", pose=[1.0, 0.0, 0.0], envelope="sphere:r0.5"),
    )
    tree.connects = [Connect(a_block="a", a_port="p", b_block="b", b_port="p")]
    return tree


def test_witness_point_overlapping_pair_is_interfering() -> None:
    tree = _overlapping_pair()
    found = _witness_point(tree, _effective_envelope, "a", "b", scale=1.0)
    assert found is not None
    point, gap = found
    assert gap < 0.0  # the two spheres genuinely interpenetrate
    # the witness sits between the two centres (0.0 and 0.5 on x), never
    # off in some unrelated direction.
    assert -0.1 < point[0] < 0.6


def test_witness_point_touching_pair_gap_near_zero() -> None:
    tree = _touching_pair()
    found = _witness_point(tree, _effective_envelope, "a", "b", scale=1.0)
    assert found is not None
    _point, gap = found
    assert abs(gap) < 1e-2  # touching, within the kernel's own resolution


def test_witness_point_none_on_unparseable_envelope() -> None:
    """Fallback path, part 1: a bad envelope must never raise into a
    scene build — ``None``, for the caller to degrade on."""
    tree = _tree(
        a=BlockNode(name="a", pose=[0.0, 0.0, 0.0], envelope="not-a-shape"),
        b=BlockNode(name="b", pose=[1.0, 0.0, 0.0], envelope="sphere:r0.5"),
    )
    assert _witness_point(tree, _effective_envelope, "a", "b", scale=1.0) is None


def test_witness_point_none_on_absent_envelope() -> None:
    tree = _tree(
        a=BlockNode(name="a", pose=[0.0, 0.0, 0.0], envelope=None),
        b=BlockNode(name="b", pose=[1.0, 0.0, 0.0], envelope="sphere:r0.5"),
    )
    assert _witness_point(tree, _effective_envelope, "a", "b", scale=1.0) is None


def test_connectivity_lines_sets_witness_when_effective_envelope_given() -> None:
    tree = _overlapping_pair()
    kids = children_map(tree)
    plan = plan_visibility(tree, kids, level="refined", isolate=None)
    assembly = Assembly3D()
    for r in plan.render_roots:
        build_shapes_node(
            tree,
            _effective_envelope,
            kids,
            plan,
            {"a": 1, "b": 2},
            r,
            "/se-x",
            assembly,
        )
    lines = connectivity_lines(
        tree,
        plan,
        assembly.primary_path,
        lambda c: "tie",
        lambda c: "#16a34a",
        "/g",
        effective_envelope=_effective_envelope,
        scale=1.0,
    )
    assert len(lines) == 1
    assert lines[0].witness is not None
    assert lines[0].witness_gap < 0.0
    # the "a.port—b.port (joint class)" label (gr337917's own hover/tree-
    # row identification ask) — the stability report's own convention.
    assert lines[0].label == "a.p—b.p (tie)"


def test_connectivity_lines_witness_is_none_without_effective_envelope() -> None:
    """Omitting ``effective_envelope``/``scale`` keeps every line
    witness-less — the historical pose-to-pose-only shape older callers/
    fixtures still get (backward compatible default)."""
    tree = _overlapping_pair()
    kids = children_map(tree)
    plan = plan_visibility(tree, kids, level="refined", isolate=None)
    assembly = Assembly3D()
    for r in plan.render_roots:
        build_shapes_node(
            tree,
            _effective_envelope,
            kids,
            plan,
            {"a": 1, "b": 2},
            r,
            "/se-x",
            assembly,
        )
    lines = connectivity_lines(
        tree, plan, assembly.primary_path, lambda c: "tie", lambda c: "#16a34a", "/g"
    )
    assert lines[0].witness is None


def test_connectivity_leaf_anchors_at_the_witness_point_when_available() -> None:
    line = ConnLine(
        path="/g/c0",
        a_name="a",
        b_name="b",
        a_path="/x/1",
        b_path="/x/2",
        label="a.p—b.p (tie)",
        colour="#16a34a",
        witness=(5.0, 0.0, 0.0),
        witness_gap=0.4,
    )
    leaf = connectivity_leaf(
        line,
        a_pose=(0.0, 0.0, 0.0),
        b_pose=(10.0, 0.0, 0.0),
        offset_len=1.0,
        eps=1e-6,
        min_stub=0.1,
    )
    edges = leaf["shape"]["edges"]
    pa, pb = np.array(edges[:3]), np.array(edges[3:])
    assert np.allclose(0.5 * (pa + pb), [5.0, 0.0, 0.0])  # through the witness
    assert np.allclose(pb - pa, [0.4, 0.0, 0.0])  # half the ACTUAL gap each way


def test_connectivity_leaf_floors_stub_length_for_a_touching_pair() -> None:
    line = ConnLine(
        path="/g/c0",
        a_name="a",
        b_name="b",
        a_path="/x/1",
        b_path="/x/2",
        label="a.p—b.p (tie)",
        colour="#16a34a",
        witness=(5.0, 0.0, 0.0),
        witness_gap=0.0,
    )
    leaf = connectivity_leaf(
        line,
        a_pose=(0.0, 0.0, 0.0),
        b_pose=(10.0, 0.0, 0.0),
        offset_len=1.0,
        eps=1e-6,
        min_stub=0.3,
    )
    edges = leaf["shape"]["edges"]
    pa, pb = np.array(edges[:3]), np.array(edges[3:])
    assert np.linalg.norm(pb - pa) == pytest.approx(0.6)  # 2 * min_stub


def test_connectivity_leaf_falls_back_to_pose_offset_when_no_witness() -> None:
    """Fallback path, part 2: ``line.witness is None`` (the historical
    shape) must still draw the pose-to-pose-plus-sideways-offset
    segment, unchanged."""
    line = ConnLine(
        path="/g/c0",
        a_name="a",
        b_name="b",
        a_path="/x/1",
        b_path="/x/2",
        label="a.p—b.p (tie)",
        colour="#16a34a",
    )
    leaf = connectivity_leaf(
        line,
        a_pose=(0.0, 0.0, 0.0),
        b_pose=(1.0, 0.0, 0.0),
        offset_len=0.1,
        eps=1e-9,
        min_stub=0.02,
    )
    edges = leaf["shape"]["edges"]
    pa, pb = np.array(edges[:3]), np.array(edges[3:])
    # offset sideways off the pose-to-pose line, not collapsed onto it.
    assert pa[1] != 0.0 or pa[2] != 0.0


def test_witness_point_respects_rotation_for_tipped_cylinders() -> None:
    """Test-gap follow-up (re-review): every witness test above uses
    identity-rot spheres, so ``cad_as_vec3(node.rot)`` is never actually
    exercised there — a dropped/swapped rot term would pass all of them
    while misanchoring the common real case (the unicycle's own
    ``rot=[0, pi/2, 0]`` strut cylinders). Two short, wide cylinders
    (``cyl:r0.5h0.2`` — the local-Z axis, base-at-origin per
    ``CircularFrustum``, is the SHORT 0.2 dimension) tipped 90° about Y
    so local Z maps to world X (:func:`precis.cad.vec.rotation`'s own
    ``Rz@Ry@Rx`` convention: ``Ry(90°)·(0,0,1) = (1,0,0)``) and offset
    0.3 apart along X: the facing END disks sit at world x=0.2 (a) and
    x=0.3 (b) — a real 0.1 gap on-axis. A dropped rot would instead
    leave both as UPRIGHT 0.5-radius pucks 0.3 apart, deeply
    interfering — nowhere near this answer."""
    tree = _tree(
        a=BlockNode(
            name="a",
            pose=[0.0, 0.0, 0.0],
            rot=[0.0, math.pi / 2, 0.0],
            envelope="cyl:r0.5h0.2",
        ),
        b=BlockNode(
            name="b",
            pose=[0.3, 0.0, 0.0],
            rot=[0.0, math.pi / 2, 0.0],
            envelope="cyl:r0.5h0.2",
        ),
    )
    found = _witness_point(tree, _effective_envelope, "a", "b", scale=1.0)
    assert found is not None
    point, gap = found
    assert gap == pytest.approx(0.1, abs=0.02)
    # between the facing end faces, on the rotated axis (x) ...
    assert 0.15 < point[0] < 0.35
    # ... and ON-axis (y/z ~ 0), not off at the disk's 0.5 radius, which
    # is where an unrotated (upright) pair's witness would land instead.
    assert abs(point[1]) < 0.1
    assert abs(point[2]) < 0.1


def test_witness_point_cache_avoids_recomputing_clearance(monkeypatch) -> None:
    """gr337917 perf follow-up: a repeat query for the SAME witness
    identity must hit :data:`blocktree_3d._witness_cache` instead of
    re-running the multi-seed SDF search — proven by counting the
    underlying ``cad_relate.clearance`` calls directly."""
    blocktree_3d._witness_cache.clear()
    calls = {"n": 0}
    real_clearance = blocktree_3d.cad_relate.clearance

    def counting_clearance(design, a, b):
        calls["n"] += 1
        return real_clearance(design, a, b)

    monkeypatch.setattr(blocktree_3d.cad_relate, "clearance", counting_clearance)
    tree = _overlapping_pair()

    first = _witness_point(tree, _effective_envelope, "a", "b", scale=1.0)
    second = _witness_point(tree, _effective_envelope, "a", "b", scale=1.0)

    assert calls["n"] == 1  # the second call hit the cache
    assert first == second


def test_witness_point_cache_distinguishes_different_queries(monkeypatch) -> None:
    """The cache key carries the full witness identity — a DIFFERENT
    pose must still trigger a real (uncached) computation, not
    accidentally reuse an unrelated pair's answer."""
    blocktree_3d._witness_cache.clear()
    calls = {"n": 0}
    real_clearance = blocktree_3d.cad_relate.clearance

    def counting_clearance(design, a, b):
        calls["n"] += 1
        return real_clearance(design, a, b)

    monkeypatch.setattr(blocktree_3d.cad_relate, "clearance", counting_clearance)
    tree_a = _overlapping_pair()
    tree_b = _touching_pair()

    _witness_point(tree_a, _effective_envelope, "a", "b", scale=1.0)
    _witness_point(tree_b, _effective_envelope, "a", "b", scale=1.0)

    assert calls["n"] == 2  # two distinct queries, no false cache hit


def test_connectivity_lines_witness_budget_caps_computations_per_scene(
    monkeypatch,
) -> None:
    """gr337917 perf follow-up: a design with more distinct connects than
    the per-scene witness budget must fall back to the pose-to-pose
    segment for the overflow rather than growing the per-request SDF-
    search cost unboundedly. Budget patched down to 2 (real geometry,
    kept small so the test itself stays fast) over 3 distinct
    (uncached-on-purpose) overlapping pairs."""
    blocktree_3d._witness_cache.clear()
    monkeypatch.setattr(blocktree_3d, "_WITNESS_BUDGET_PER_SCENE", 2)

    blocks: dict[str, BlockNode] = {}
    connects: list[Connect] = []
    for i in range(3):
        blocks[f"a{i}"] = BlockNode(
            name=f"a{i}", pose=[float(i) * 10, 0.0, 0.0], envelope="sphere:r0.6"
        )
        blocks[f"b{i}"] = BlockNode(
            name=f"b{i}", pose=[float(i) * 10 + 0.5, 0.0, 0.0], envelope="sphere:r0.6"
        )
        connects.append(
            Connect(a_block=f"a{i}", a_port="p", b_block=f"b{i}", b_port="p")
        )
    tree = _tree(**blocks)
    tree.connects = connects
    kids = children_map(tree)
    plan = plan_visibility(tree, kids, level="refined", isolate=None)
    assembly = Assembly3D()
    id_by_name = {name: idx + 1 for idx, name in enumerate(sorted(blocks))}
    for r in plan.render_roots:
        build_shapes_node(
            tree,
            _effective_envelope,
            kids,
            plan,
            id_by_name,
            r,
            "/se-x",
            assembly,
        )
    lines = connectivity_lines(
        tree,
        plan,
        assembly.primary_path,
        lambda c: "tie",
        lambda c: "#16a34a",
        "/g",
        effective_envelope=_effective_envelope,
        scale=1.0,
    )
    assert len(lines) == 3
    with_witness = sum(1 for line in lines if line.witness is not None)
    assert with_witness == 2  # budget capped it — the 3rd falls back


def test_explode_offsets_pull_connected_blocks_apart() -> None:
    tree = _connected_tree()
    kids = children_map(tree)
    plan = plan_visibility(tree, kids, level="refined", isolate=None)
    assembly = Assembly3D()
    for r in plan.render_roots:
        build_shapes_node(
            tree, _effective_envelope, kids, plan, _IDS, r, "/se-x", assembly
        )
    lines = connectivity_lines(
        tree, plan, assembly.primary_path, lambda c: "tie", lambda c: "#16a34a", "/g"
    )
    offsets = explode_offsets(tree, lines, assembly.primary_path, magnitude=1.0)
    hub_off = np.array(offsets[assembly.primary_path["hub"]])
    rim_off = np.array(offsets[assembly.primary_path["rim"]])
    # hub sits below rim (z=0 vs z=0.3) -- explode must push them further
    # apart along that SAME axis, not some unrelated direction.
    assert hub_off[2] < 0
    assert rim_off[2] > 0


def test_explode_offsets_fallback_when_nothing_is_connected() -> None:
    tree = _tree(
        a=BlockNode(name="a", pose=[-1, 0, 0], envelope="box:w0.1d0.1h0.1"),
        b=BlockNode(name="b", pose=[1, 0, 0], envelope="box:w0.1d0.1h0.1"),
    )
    kids = children_map(tree)
    plan = plan_visibility(tree, kids, level="refined", isolate=None)
    assembly = Assembly3D()
    for r in plan.render_roots:
        build_shapes_node(
            tree, _effective_envelope, kids, plan, {"a": 1, "b": 2}, r, "/x", assembly
        )
    offsets = explode_offsets(tree, [], assembly.primary_path, magnitude=1.0)
    a_off = offsets[assembly.primary_path["a"]]
    b_off = offsets[assembly.primary_path["b"]]
    assert a_off[0] < 0 and b_off[0] > 0  # away from the shared centroid


def test_pose_spread_empty_tree_is_never_zero() -> None:
    assert pose_spread(_tree()) == 1.0


# ── mermaid topology ─────────────────────────────────────────────────────


def test_mermaid_topology_labels_edges_with_joint_kind() -> None:
    tree = _connected_tree()
    kids = children_map(tree)
    plan = plan_visibility(tree, kids, level="refined", isolate=None)
    assembly = Assembly3D()
    for r in plan.render_roots:
        build_shapes_node(
            tree, _effective_envelope, kids, plan, _IDS, r, "/se-x", assembly
        )
    lines = connectivity_lines(
        tree, plan, assembly.primary_path, lambda c: "axial", lambda c: "#16a34a", "/g"
    )
    graph = mermaid_topology(plan, _IDS, lines, kids)
    assert graph.startswith("graph LR")
    assert 'B1["hub"]' in graph
    assert 'B2["rim"]' in graph
    assert "axial" in graph and "B1 --" in graph and "--- B2" in graph
    # flat design (no parent links) — no subgraph wrapper appears
    assert "subgraph" not in graph


def test_mermaid_topology_nests_opened_parents_as_subgraphs() -> None:
    tree = _fork_tree()
    tree.connects = [
        Connect(a_block="hub", a_port="pin", b_block="fork_tip", b_port="pin")
    ]
    kids = children_map(tree)
    plan = plan_visibility(tree, kids, level="refined", isolate=None)
    assembly = Assembly3D()
    for r in plan.render_roots:
        build_shapes_node(
            tree, _effective_envelope, kids, plan, _IDS, r, "/se-x", assembly
        )
    lines = connectivity_lines(
        tree, plan, assembly.primary_path, lambda c: "tie", lambda c: "#16a34a", "/g"
    )
    graph = mermaid_topology(plan, _IDS, lines, kids)
    rows = graph.splitlines()
    # fork (opened, has shown children) wraps fork_arm wraps fork_tip
    i_fork = rows.index('  subgraph B3["fork"]')
    i_arm = rows.index('    subgraph B4["fork_arm"]')
    i_tip = rows.index('      B5["fork_tip"]')
    assert i_fork < i_arm < i_tip
    assert rows.count("    end") == 1 and rows.count("  end") == 1
    # leaves outside the subtree stay plain nodes; the edge still targets
    # the B<id> node scheme
    assert '  B1["hub"]' in rows
    assert any(r.startswith('  B1 -- "') and r.endswith("--- B5") for r in rows)


def test_mermaid_topology_collapsed_parent_stays_a_plain_node() -> None:
    tree = _fork_tree()
    kids = children_map(tree)
    plan = plan_visibility(tree, kids, level="envelope", isolate=None)
    graph = mermaid_topology(plan, _IDS, [], kids)
    # 'fork' collapses to a box at envelope level -> its children are not
    # shown, so it renders as an ordinary node, not an empty subgraph
    assert 'B3["fork"]' in graph
    assert "subgraph" not in graph


# ── build_scene end to end ─────────────────────────────────────────────────


# ── display scale (gr337751) ─────────────────────────────────────────────


def _hub_rim_tree(pose_scale: float) -> Tree[BlockNode, Connect]:
    """The same hub/rim design at two different SI magnitudes — metre
    scale (the unicycle fixture's own numbers) and nanometre scale
    (boxel-3nm's own report: a whole bb diagonal ~4e-9 m)."""
    tree = _tree(
        hub=BlockNode(
            name="hub",
            pose=[0, 0, 0],
            envelope=f"cyl:r{0.02 * pose_scale}h{0.05 * pose_scale}",
        ),
        rim=BlockNode(
            name="rim",
            pose=[0, 0, 0.3 * pose_scale],
            envelope=f"torus:R{0.3 * pose_scale}r{0.01 * pose_scale}",
        ),
    )
    tree.connects = [Connect(a_block="hub", a_port="pin", b_block="rim", b_port="pin")]
    return tree


def _hub_rim_scene(pose_scale: float):
    tree = _hub_rim_tree(pose_scale)
    kids = children_map(tree)
    plan = plan_visibility(tree, kids, level="refined", isolate=None)
    return build_scene(
        tree,
        _effective_envelope,
        kids,
        plan,
        {"hub": 1, "rim": 2},
        root_id="/se-x",
        root_name="x",
        label_fn=lambda c: "tie",
        colour_fn=lambda c: "#16a34a",
    )


def _scene_verts(scene) -> np.ndarray:
    out: list[float] = []

    def _walk(n: dict) -> None:
        if "shape" in n:
            out.extend(n["shape"]["obj_vertices"])
        for p in n.get("parts", []):
            _walk(p)

    _walk(scene.shapes)
    return np.array(out, dtype=np.float64).reshape(-1, 3)


def test_scene_scale_brings_nm_scale_bounds_into_the_working_range() -> None:
    nm_tree = _hub_rim_tree(1e-9)
    scale = scene_scale(nm_tree, _effective_envelope)
    assert scale >= 1e6  # a flat 1.0 (no-op) would leave it at ~1e-9


def test_scene_scale_is_a_near_noop_for_an_already_legible_metre_design() -> None:
    metre_tree = _hub_rim_tree(1.0)
    scale = scene_scale(metre_tree, _effective_envelope)
    assert 1.0 <= scale <= 1000.0


def test_scene_scale_uses_extent_not_midpoint_for_a_symmetric_design() -> None:
    # Two spheres symmetric about the origin: hi + lo cancels to ~0 while
    # hi - lo is the real ~1.2e-8 extent — a diag computed from the
    # midpoint would collapse to the degenerate 1.0 fallback instead of
    # the nm-scale boost.
    tree = _tree(
        a=BlockNode(name="a", pose=[-5e-9, 0, 0], envelope="sphere:r1e-9"),
        b=BlockNode(name="b", pose=[5e-9, 0, 0], envelope="sphere:r1e-9"),
    )
    assert scene_scale(tree, _effective_envelope) == 1e9


def test_scene_scale_falls_back_to_noop_for_a_single_point_design() -> None:
    # One bare-pose block: bounds collapse to a point — diag is exactly
    # 0.0 yet finite, so only the zero half of the degenerate guard can
    # catch it before log10(0) blows up.
    tree = _tree(a=BlockNode(name="a", pose=[0, 0, 0], envelope=None))
    assert scene_scale(tree, _effective_envelope) == 1.0


def test_scene_scale_includes_envelope_extent_beyond_the_pose_points() -> None:
    # A single origin-posed block whose envelope supplies ALL the extent:
    # pose points alone give a zero-size bb (scale 1.0); only the mesh
    # bounds produce the nm-scale factor.
    tree = _tree(a=BlockNode(name="a", pose=[0, 0, 0], envelope="sphere:r5e-9"))
    assert scene_scale(tree, _effective_envelope) == 1e9


def test_build_scene_nm_and_metre_scale_share_topology_and_land_in_working_range() -> (
    None
):
    """gr337751: raw SI metres put a nanometre-scale design's own bounds
    outside three.js's near/far-plane working range (a blank canvas),
    and the module's own absolute epsilon floors degenerated its explode
    offsets to zero on top of that. A metre-scale and an otherwise-
    identical nanometre-scale design must produce the SAME topology,
    coordinates landing in roughly O(1-1000) regardless of the DB's own
    SI units, and NONZERO explode offsets for both."""
    metre_scene = _hub_rim_scene(1.0)
    nm_scene = _hub_rim_scene(1e-9)

    def _ids(scene) -> set[str]:
        out: set[str] = set()

        def _walk(n: dict) -> None:
            out.add(n["id"])
            for p in n.get("parts", []):
                _walk(p)

        _walk(scene.shapes)
        return out

    assert _ids(metre_scene) == _ids(nm_scene)

    for scene in (metre_scene, nm_scene):
        verts = _scene_verts(scene)
        diag = float(np.linalg.norm(verts.max(axis=0) - verts.min(axis=0)))
        assert 1.0 <= diag <= 1000.0

    # the actual bug report: nm-scale explode offsets used to floor to
    # zero for every path (the norm<1e-9 absolute cutoffs).
    assert nm_scene.explode
    assert all(any(c != 0.0 for c in v) for v in nm_scene.explode.values())
    assert metre_scene.explode
    assert all(any(c != 0.0 for c in v) for v in metre_scene.explode.values())


def test_build_scene_bundles_shapes_connections_explode_and_mermaid() -> None:
    tree = _connected_tree()
    kids = children_map(tree)
    plan = plan_visibility(tree, kids, level="refined", isolate=None)
    scene = build_scene(
        tree,
        _effective_envelope,
        kids,
        plan,
        _IDS,
        root_id="/se-x",
        root_name="x",
        label_fn=lambda c: "tie",
        colour_fn=lambda c: "#16a34a",
    )
    assert scene.shapes["id"] == "/se-x"
    top_ids = {p["id"] for p in scene.shapes["parts"]}
    assert "/se-x/1" in top_ids and "/se-x/2" in top_ids
    connections_group = next(
        p for p in scene.shapes["parts"] if p["id"] == "/se-x/_connections"
    )
    assert len(connections_group["parts"]) == 1
    assert connections_group["parts"][0]["type"] == "edges"
    assert len(scene.connections) == 1
    assert set(scene.explode) == {"/se-x/1", "/se-x/2"}
    assert "graph LR" in scene.mermaid
