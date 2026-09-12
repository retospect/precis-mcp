"""Pure-function coverage for :mod:`precis_web.blocktree_3d` — the
three-cad-viewer scene projection behind the round-2a 3D reader (gr335242
comment 5), independent of any store/handler. Mirrors
``test_blocktree_svg.py``'s style: bare :class:`Tree`/:class:`BlockNode`
fixtures, no DB."""

from __future__ import annotations

import numpy as np
import pytest

from precis.blocktree.types import BlockNode, Connect, Tree
from precis_web.blocktree_3d import (
    Assembly3D,
    build_scene,
    build_shapes_node,
    connectivity_lines,
    explode_offsets,
    feature_edges,
    group_planar_faces,
    mermaid_topology,
    pose_spread,
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
    graph = mermaid_topology(plan, _IDS, lines)
    assert graph.startswith("graph LR")
    assert 'B1["hub"]' in graph
    assert 'B2["rim"]' in graph
    assert "axial" in graph and "B1 --" in graph and "--- B2" in graph


# ── build_scene end to end ─────────────────────────────────────────────────


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
