"""Slice 2 (phase 1): the CDT mesh — every pad a vertex, every
courtyard/outline edge a present constraint, Euler V−E+T=1 (footprint
interiors triangulated, no holes), crossing constraints refused, plus
the ``design_from_ir`` slice-2 fields (part outlines, per-net layer
force, pin extents). Wire/colour/certificate tests land with 2b/2c."""

from __future__ import annotations

import numpy as np
import pytest

import precis_geom.pcb as pcb
from precis.pcb import local as kernel
from precis.pcb.topoplace import design_from_ir
from precis.pcb.topoplace.design import (
    LAYER_FORCE_FREE,
    LAYER_FORCE_INNER,
    ObjectiveShares,
)
from precis.pcb.topoplace.mesh import build_mesh
from precis.pcb.topoplace.proxy import all_pin_world
from tests.fixtures.pcb.topo_example_a import example_a
from tests.fixtures.pcb.topo_synth import synth_design


def _referenced(mesh) -> np.ndarray:
    return np.unique(mesh.tris.ravel())


def _edge_set(mesh) -> set[tuple[int, ...]]:
    return {tuple(int(x) for x in e) for e in mesh.edges}


def _ring_edges_xy(
    mesh, ring_xy: np.ndarray
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """World-coords consecutive pairs of a placed ring, for set compare."""
    pts = ring_xy[:-1] if np.allclose(ring_xy[0], ring_xy[-1]) else ring_xy
    return [(tuple(pts[i]), tuple(pts[(i + 1) % len(pts)])) for i in range(len(pts))]


def test_every_pad_is_a_vertex_at_its_world_position() -> None:
    d = example_a()
    mesh = build_mesh(d, d.pose0)
    pw = all_pin_world(d, d.pose0)
    for pin in range(len(d.pin_xy)):
        part = int(d.pin_part[pin])
        v = mesh.pad_vertex[(part, pin)]
        assert mesh.pin_vertex[pin] == v
        np.testing.assert_allclose(mesh.verts[v], pw[pin], atol=1e-9)


def test_every_courtyard_and_outline_edge_is_a_present_constraint() -> None:
    d = example_a()
    mesh = build_mesh(d, d.pose0)
    edges = _edge_set(mesh)
    vid_of = {
        (round(float(x), 9), round(float(y), 9)): i
        for i, (x, y) in enumerate(mesh.verts)
    }
    constrained = _edge_set(mesh) & {
        tuple(int(x) for x in mesh.edges[i]) for i in np.flatnonzero(mesh.constrained)
    }
    for part in range(len(d.parts)):
        ring = d.part_outline[part]
        assert len(ring) >= 4
        x, y, rot_q, side = d.pose0[part]
        assert rot_q == 0 and side == 0
        world = ring[:-1] + np.asarray([x, y])
        for pa, pb in _ring_edges_xy(mesh, np.vstack([world, world[0]])):
            e = tuple(
                sorted((vid_of[tuple(np.round(pa, 9))], vid_of[tuple(np.round(pb, 9))]))
            )
            assert e in edges, (pa, pb)
            assert e in constrained, (pa, pb)
    outline = np.asarray(d.outline)
    for pa, pb in _ring_edges_xy(mesh, np.vstack([outline, outline[0]])):
        e = tuple(
            sorted((vid_of[tuple(np.round(pa, 9))], vid_of[tuple(np.round(pb, 9))]))
        )
        assert e in edges
        assert e in constrained


def test_euler_no_holes_example_a_and_100p_synth() -> None:
    """V − E + T = 1 over referenced vertices — footprint interiors are
    triangulated, so the courtyards punch no holes."""
    for d in (example_a(), synth_design(seed=0)[0]):
        mesh = build_mesh(d, d.pose0)
        v_ref = len(_referenced(mesh))
        assert v_ref - len(mesh.edges) + len(mesh.tris) == 1


def test_courtyard_overlap_meshes_and_records_the_conflict() -> None:
    """Overlapping courtyards are legal on real poses (advisory margins):
    both rings keep their edges through shared crossing vertices, and
    ``conflicts`` carries the part pair with exact area and side."""
    d = example_a()
    pose = d.pose0.copy()
    pose[1, 0] = pose[0, 0] + 0.6  # A2's courtyard now overlaps A1's
    pose[1, 1] = pose[0, 1] + 0.6
    mesh = build_mesh(d, pose)
    hit = [c for c in mesh.conflicts if (c.a, c.b) == (0, 1)]
    assert len(hit) == 1 and hit[0].same_side and hit[0].side == 0
    assert hit[0].area_mm2 == pytest.approx(0.04, abs=1e-6)
    # the crossing minted vertices: both rings' split sub-edges are
    # present and constrained, and Euler still holds
    assert len(_referenced(mesh)) - len(mesh.edges) + len(mesh.tris) == 1
    n_cross_verts = len(mesh.verts) - (len(d.pin_xy) + 4 * len(d.parts) + 4)
    assert n_cross_verts == 2


def test_fully_stacked_rings_record_a_conflict() -> None:
    """Coincident rings (two parts stacked exactly) never properly CROSS
    — the conflict is caught by the area pass (containment), and the
    shared edge carries both owners."""
    d = example_a()
    pose = d.pose0.copy()
    pose[1] = pose[0]  # A2 sits exactly on A1
    mesh = build_mesh(d, pose)
    hit = [c for c in mesh.conflicts if (c.a, c.b) == (0, 1)]
    assert len(hit) == 1 and hit[0].area_mm2 == pytest.approx(0.64, abs=1e-6)
    owners = {tuple(int(x) for x in row) for row in mesh.edge_owner}
    assert (0, 1) in owners or (1, 0) in owners


def test_opposite_side_overlap_is_not_a_violation() -> None:
    """A bottom-mounted part under a top-side one is facing, not
    colliding — the DRC ``bottom_by_refdes`` split, carried as
    ``side=2`` / ``same_side=False``."""
    d = example_a()
    pose = d.pose0.copy()
    pose[1] = pose[0].copy()
    pose[1, 3] = 1.0  # A2 stacked exactly on A1 but on the bottom
    mesh = build_mesh(d, pose)
    hit = [c for c in mesh.conflicts if (c.a, c.b) == (0, 1)]
    assert len(hit) == 1 and hit[0].side == 2 and not hit[0].same_side


def test_pad_outside_outline_refused() -> None:
    """A pad vertex in no triangle (pad over the board edge) refuses
    with a fix hint rather than stranding the wire builder."""
    from precis.pcb.topoplace.mesh import MeshConflictError

    d = example_a()
    pose = d.pose0.copy()
    pose[1, 0] = 6.2  # A2's pad at x 6.2, outline ends at x 6
    with pytest.raises(MeshConflictError, match="no triangle"):
        build_mesh(d, pose)


def test_edge_owner_and_side() -> None:
    """Every courtyard edge is owned by its part (a wire may leave its
    own part); the board outline owns -1; an SMD part blocks only its
    mount-side outer layer, a TH part all layers."""
    from precis.pcb.topoplace.mesh import (
        OWNER_BOARD,
        SIDE_ALL,
        edge_blocks_layer,
    )

    d = example_a()
    pose = d.pose0.copy()
    pose[1, 3] = 1.0  # A2 to the bottom
    mesh = build_mesh(d, pose)
    owned = mesh.edge_owner[mesh.constrained]
    assert set(int(o) for o in owned[:, 0]) == {-1, 0, 1, 2, 3, 4, 5}
    assert OWNER_BOARD in owned[:, 0]
    # A2's ring edges block only the bottom layer
    a2_edges = np.flatnonzero(mesh.edge_owner[:, 0] == 1)
    assert len(a2_edges) >= 4
    assert all(not edge_blocks_layer(mesh, d, int(e), 0, 4) for e in a2_edges)
    assert all(edge_blocks_layer(mesh, d, int(e), 3, 4) for e in a2_edges)
    # board outline blocks every layer
    b_edges = np.flatnonzero(mesh.edge_owner[:, 0] == OWNER_BOARD)
    assert all(
        edge_blocks_layer(mesh, d, int(e), ly, 4) for e in b_edges for ly in range(4)
    )
    d_th = example_a(pin_side=2)
    mesh_th = build_mesh(d_th, d_th.pose0)
    th_edges = np.flatnonzero(mesh_th.edge_owner[:, 0] == 0)
    assert all(int(mesh_th.edge_side[e, 0]) == SIDE_ALL for e in th_edges)


def test_zones_constrain_and_report() -> None:
    """A keepout zone gets constrained edges under owner ``n_parts + z``
    (layer-scoped → SIDE_ZONE); a part on it lands in
    ``zone_conflicts``."""
    import dataclasses

    from precis.pcb.topoplace.design import TopoZone
    from precis.pcb.topoplace.mesh import SIDE_ZONE

    d = example_a()
    ring = np.asarray([[2.0, -1.0], [4.0, -1.0], [4.0, 1.0], [2.0, 1.0], [2.0, -1.0]])
    zones = (TopoZone(kind="keepout", ring=ring, layers=(0,), name="k"),)
    d = dataclasses.replace(d, zones=zones)
    mesh = build_mesh(d, d.pose0)
    n = len(d.parts)
    z_edges = np.flatnonzero(mesh.edge_owner[:, 0] == n)
    assert len(z_edges) >= 4  # ring crossings split edges into sub-segments
    assert all(int(mesh.edge_side[e, 0]) == SIDE_ZONE for e in z_edges)
    assert all(mesh.constrained[e] for e in z_edges)
    # B1 sits at (2,0) — inside the keepout
    assert mesh.zone_conflicts and mesh.zone_conflicts[0][0] == 2


def test_outline_escape_is_separated() -> None:
    """A part whose courtyard spills over the board edge reports on
    ``outline_conflicts``, not as a part-pair conflict."""
    d = example_a()
    pose = d.pose0.copy()
    pose[1, 0] = 5.9  # ring reaches past outline x=6, pads still inside
    mesh = build_mesh(d, pose)
    assert mesh.outline_conflicts and mesh.outline_conflicts[0][0] == 1
    assert mesh.outline_conflicts[0][1] > 0.0


def test_legalize_separates_same_side_overlap() -> None:
    """The post-pass drives same-side courtyard overlap to zero: the
    worst pair separates along its MTV, fixed parts never move, and the
    report marks the pose resolved."""
    import dataclasses

    from precis.pcb.topoplace.legalize import legalize_pose
    from precis.pcb.topoplace.proxy import overlap_area

    d = example_a()
    pose = d.pose0.copy()
    pose[1] = pose[0].copy()  # A2 stacked exactly on A1
    assert overlap_area(d, pose) > 0.0
    res = legalize_pose(d, pose)
    assert res.resolved and overlap_area(d, res.pose) == pytest.approx(0.0)
    assert res.moved_mm > 0.4  # the stacked pair separated, minimally

    d2 = dataclasses.replace(d, fixed=np.asarray([True] + [False] * (len(d.parts) - 1)))
    res2 = legalize_pose(d2, pose)
    np.testing.assert_allclose(res2.pose[0], pose[0])
    assert res2.resolved


def test_legalize_th_part_collides_across_sides() -> None:
    """A through-hole part's pads exist on EVERY copper layer, so an
    opposite-side stack with a TH part is NOT a legal facing — legalize
    must separate it (the taz15 regression: a flipped TH header left a
    top SMD on its barrel)."""
    from precis.pcb.topoplace.legalize import legalize_pose
    from precis.pcb.topoplace.proxy import overlap_area

    d = example_a(pin_side=2)
    pose = d.pose0.copy()
    pose[1] = pose[0].copy()
    pose[1, 3] = 1.0  # A2 stacked on A1 but flipped to the bottom
    assert overlap_area(d, pose) > 0.0  # priced despite the side flip
    res = legalize_pose(d, pose)
    assert res.resolved
    assert overlap_area(d, res.pose) == pytest.approx(0.0)


def test_recovery_stress_overlapping_poses() -> None:
    """Grid-snapped poses with overlapping courtyards, random rotation
    and side: Euler holds, no pad is orphaned, no conflict raises
    (slice-2a seam item 7 — exercises flips/cavity paths for real)."""
    rng = np.random.default_rng(7)
    d, _inst, _nets = synth_design(seed=0, n_parts=60, side=30.0)
    for trial in range(40):
        pose = d.pose0.copy()
        pose[:, 0] += rng.normal(0.0, 2.0, len(pose))
        pose[:, 1] += rng.normal(0.0, 2.0, len(pose))
        pose[:, 2] = rng.integers(0, 4, len(pose)).astype(np.float64)
        pose[:, 3] = rng.integers(0, 2, len(pose)).astype(np.float64)
        pose[:, 0] = np.clip(pose[:, 0], 1.0, 29.0)
        pose[:, 1] = np.clip(pose[:, 1], 1.0, 29.0)
        mesh = build_mesh(d, pose)
        ref = np.unique(mesh.tris.ravel())
        assert len(ref) - len(mesh.edges) + len(mesh.tris) == 1
        assert set(int(v) for v in mesh.pin_vertex) <= set(int(v) for v in ref)


def test_nonconvex_outline_drops_exterior_tris() -> None:
    """A concave outline keeps every emitted triangle inside it."""
    import dataclasses

    from precis.pcb.geom import point_in_polygon

    d = example_a()
    outline = np.asarray(
        [[-2, -2], [6, -2], [6, 2], [5, 2], [5, 6], [-2, 6]],
        dtype=np.float64,
    )
    d = dataclasses.replace(d, outline=outline)
    mesh = build_mesh(d, d.pose0)
    poly = [tuple(p) for p in outline]
    assert len(mesh.tris) > 0
    for tri in mesh.tris:
        centroid = tuple(mesh.verts[tri].mean(axis=0))
        assert point_in_polygon(centroid, poly)
    assert len(_referenced(mesh)) - len(mesh.edges) + len(mesh.tris) == 1


def test_mesh_is_deterministic() -> None:
    d, _inst, _nets = synth_design(seed=3)
    a, b = build_mesh(d, d.pose0), build_mesh(d, d.pose0)
    assert np.array_equal(a.verts, b.verts)
    assert np.array_equal(a.tris, b.tris)
    assert np.array_equal(a.edges, b.edges)
    assert np.array_equal(a.constrained, b.constrained)


def test_unplaced_pose_refused() -> None:
    d = example_a()
    pose = d.pose0.copy()
    pose[2, 0] = np.nan
    with pytest.raises(ValueError, match="unplaced"):
        build_mesh(d, pose)


def _tiny_board() -> pcb.Board:
    fp = pcb.Footprint(
        "SIP2",
        [
            pcb.pad("1", "rect", -0.8, 0.0, w=0.9, h=1.0),
            pcb.pad("2", "rect", 0.8, 0.0, w=0.9, h=1.0),
        ],
    )
    b = pcb.Board("tiny", stackup=4, outline=pcb.rect(20.0, 15.0))
    f = b.footprint(fp)
    b.net_class("bot", layers=["B.Cu"])
    b.net_class("inner", layers=["In1.Cu"])
    b.net("BOTONLY", cls="bot")
    b.net("INNERONLY", cls="inner")
    j1 = b.part("J1", footprint=f, pins=["1", "2"]).at(5.0, 7.5)
    j2 = b.part("J2", footprint=f, pins=["1", "2"]).at(15.0, 7.5)
    j3 = b.part("J3", footprint=f, pins=["1", "2"]).at(10.0, 12.0)
    j4 = b.part("J4", footprint=f, pins=["1", "2"]).at(10.0, 3.0, side="bottom")
    j5 = b.part("J5", footprint=f, pins=["1", "2"]).at(12.0, 3.0, side="bottom")
    j6 = b.part("J6", footprint=f, pins=["1", "2"]).at(14.0, 3.0)
    b.wire(j1["1"], j2["1"], name="FREE")
    b.wire(j1["2"], j2["2"], name="BOTONLY")
    b.wire(j3["1"], j3["2"], name="INNERONLY")
    b.wire(j4["1"], j5["1"], name="BOTPAD")
    b.wire(j4["2"], j6["1"], name="MIXED")
    return b


def test_design_from_ir_net_layer_force_and_slice2_fields() -> None:
    """A class ``layers`` rule projects onto the two outer layers:
    one outer → that index, none → INNER, no/both → FREE. Courtyard
    rings, pin extents and net names populate alongside."""
    board = _tiny_board()
    design = kernel._design(board, None)
    ir = kernel._build_ir(design)
    footprints = kernel._footprints(design, ir)
    td = design_from_ir(ir, footprints, net_classes=design.graph.get("net_classes"))
    force = dict(zip(td.net_name, td.net_layer_force.tolist()))
    n_outer = ir.n_layers - 1
    assert force["BOTONLY"] == n_outer
    assert force["INNERONLY"] == LAYER_FORCE_INNER
    assert force["FREE"] == LAYER_FORCE_FREE
    assert len(td.part_outline) == len(td.parts)
    assert all(len(r) >= 4 for r in td.part_outline)
    assert td.pin_wh.shape == (len(td.pin_xy), 2)
    assert (td.pin_wh[:, 0] > 0).all()
    # pin-side forces: all-bottom SMD net -> bottom outer, mixed -> free,
    # all-top -> top
    pin_force = dict(zip(td.net_name, td.net_pin_force.tolist()))
    assert pin_force["BOTPAD"] == n_outer
    assert pin_force["MIXED"] == LAYER_FORCE_FREE
    assert pin_force["FREE"] == 0
    # and the mesh reads what design_from_ir wrote
    mesh = build_mesh(td, td.pose0)
    assert len(mesh.pad_vertex) == len(td.pin_xy)


def test_unresolvable_layer_rule_refused() -> None:
    """A class ``layers`` name outside the stackup refuses — never the
    silent 'inner-only' read that would fabricate a lock (item 8)."""
    from precis.errors import BadInput

    board = _tiny_board()
    design = kernel._design(board, None)
    ir = kernel._build_ir(design)
    footprints = kernel._footprints(design, ir)
    classes = dict(design.graph.get("net_classes") or {})
    classes["bot"] = {"layers": ["Nope.Cu"]}
    with pytest.raises(BadInput, match="Nope.Cu"):
        design_from_ir(ir, footprints, net_classes=classes)


def test_overlap_proxy_term() -> None:
    """The placer prices same-side courtyard overlap between rung-2
    evals: ``overlap_area`` agrees with the mesh conflict data, and a
    staged move that resolves the overlap drops the term."""
    from precis.pcb.topoplace.proxy import ProxyState, overlap_area

    d = example_a()
    pose = d.pose0.copy()
    pose[1, 0] = pose[0, 0] + 0.6
    pose[1, 1] = pose[0, 1] + 0.6
    mesh = build_mesh(d, pose)
    expected = sum(c.area_mm2 for c in mesh.conflicts if c.same_side)
    assert overlap_area(d, pose) == pytest.approx(expected)
    w = ObjectiveShares(crossings=0.5, ratsnest=0.1, overlap=0.4)
    proxy = ProxyState(d, pose, w)
    assert proxy.overlap == pytest.approx(expected)
    row = pose[1].copy()
    row[0] = 4.0
    row[1] = 2.0  # A2 back to its authored spot — overlap gone
    proxy.try_move(1, row)
    assert proxy.overlap == pytest.approx(0.0)
    proxy.commit()
    assert proxy.overlap == pytest.approx(overlap_area(d, proxy.pose))
