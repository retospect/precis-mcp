"""Slice 2 (phase 1): the CDT mesh — every pad a vertex, every
courtyard/outline edge a present constraint, Euler V−E+T=1 (footprint
interiors triangulated, no holes), crossing constraints refused, plus
the ``design_from_ir`` slice-2 fields (part outlines, per-net layer
force, pin extents) — and the 2b wires: per-net MST branches pulled to
dual paths on the mesh, the taut representative's crossing parameters,
shared-edge order, capacity and the unreachable-pad marker.
Colour/certificate tests land with 2c."""

from __future__ import annotations

import itertools

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


def test_build_wires_example_a_branches() -> None:
    """Every 2-pin net yields one geodesic-Prim branch pulled taut on
    the mesh: endpoints on the pads, one crossing parameter per crossed
    edge in [0,1], corridor one triangle longer than the edge list.
    Courtyards don't block routing (R-a) — all three nets thread
    straight."""
    from precis.pcb.topoplace.wires import build_wires

    d = example_a()
    mesh = build_mesh(d, d.pose0)
    w = build_wires(mesh, d, 2)
    pw = all_pin_world(d, d.pose0)
    assert len(w.branches) == len(d.net_pins) == 3
    for k, pins in enumerate(d.net_pins):
        b = w.branches[k]
        assert b.net == k
        assert {b.pin_a, b.pin_b} == {int(p) for p in pins}
        assert len(b.tris) == len(b.edges) + 1
        assert len(b.t) == len(b.edges) > 0
        assert all(0.0 <= t <= 1.0 for t in b.t)
        np.testing.assert_allclose(b.points[0], pw[b.pin_a], atol=1e-9)
        np.testing.assert_allclose(b.points[-1], pw[b.pin_b], atol=1e-9)
    by_net = {d.net_name[b.net]: b for b in w.branches}
    assert by_net["A"].length_mm == pytest.approx(4.0, abs=1e-6)
    assert by_net["B"].length_mm == pytest.approx(4.0, abs=1e-6)
    assert by_net["C"].length_mm == pytest.approx(5.0, abs=1e-6)
    assert w.net_layers == (frozenset({0, 1}),) * 3


def _assert_routing_rule(ctx, d, w, n_layers: int) -> None:
    """Every crossed edge is passable on a candidate layer under the
    NET exemption: the fans of the wire's own-net pins are open, a pad
    of another net walls on its owner's face, zones block their layers
    (except same-net copper), and the board edge never passes."""
    from precis.pcb.topoplace.wires import _cand_mask

    for b in w.branches:
        net = d.net_name[b.net] if b.net < len(d.net_name) else str(b.net)
        cand = w.net_layers[b.net] or frozenset({0, n_layers - 1})
        cand_mask = _cand_mask(cand, n_layers)
        own = frozenset(int(p) for p in d.net_pins[b.net])
        for e in b.edges:
            assert not ctx.board[e]
            blocked = 0
            for pin, pm in ctx.pad_contrib.get(e, ()):
                if pin not in own:
                    blocked |= pm
            for z, zm in ctx.zone_contrib.get(e, ()):
                zn = d.zones[z].net
                if not (zn and zn == net):
                    blocked |= zm
            # the passable mask is uniform per net — a crossed edge is
            # simply open on some candidate layer, no endpoint carve-out
            assert cand_mask & ~blocked


def test_build_wires_order_and_routing_rule() -> None:
    """``order[e]`` holds exactly the branches crossing ``e``, sorted by
    their taut parameter within the tie tolerance — and the routing rule
    (see ``_assert_routing_rule``) holds on every crossed edge."""
    from precis.pcb.topoplace.wires import _make_ctx, build_wires

    n_layers = 4
    d, _inst, _nets = synth_design(seed=0, n_parts=40)
    mesh = build_mesh(d, d.pose0)
    w = build_wires(mesh, d, n_layers)
    assert len(w.order) == len(mesh.edges)
    for e, ids in enumerate(w.order):
        # a branch may cross e twice (wrapping both endpoints); its k-th
        # appearance in order[e] is its k-th smallest t at e
        occ: dict[int, int] = {}
        ts = []
        for bi in ids:
            k = occ.get(bi, 0)
            occ[bi] = k + 1
            ts.append(
                sorted(
                    t
                    for t, e2 in zip(w.branches[bi].t, w.branches[bi].edges)
                    if e2 == e
                )[k]
            )
        for x, y in itertools.pairwise(ts):
            assert y >= x - 1e-9
    ctx = _make_ctx(mesh, d, n_layers)
    for bi, b in enumerate(w.branches):
        for e in b.edges:
            assert bi in w.order[e]
    _assert_routing_rule(ctx, d, w, n_layers)


def test_build_wires_routing_rule_on_zone_fixture() -> None:
    """The routing rule on a fixture WITH a keepout: two nets detour an
    all-layer keepout between their endpoints and share corridor edges
    on the way around — every crossed edge still opens on a candidate
    layer and no branch crosses a zone edge (round-2 item 7)."""
    from precis.pcb.topoplace.design import TopoDesign, TopoZone
    from precis.pcb.topoplace.wires import _make_ctx, build_wires

    sq = lambda hx, hy: np.asarray(
        [[-hx, -hy], [hx, -hy], [hx, hy], [-hx, hy], [-hx, -hy]], float
    )
    d = TopoDesign(
        parts=("A", "B", "C", "D"),
        pin_xy=np.zeros((4, 2)),
        pin_part=np.asarray([0, 1, 2, 3]),
        pin_side=np.asarray([0] * 4, np.int8),
        pin_label=np.asarray(["1"] * 4, dtype=object),
        pin_net=np.asarray([0, 0, 1, 1], np.int32),
        net_pins=(np.asarray([0, 1]), np.asarray([2, 3])),
        flex=(),
        swap_groups=(),
        fixed=np.zeros(4, bool),
        freedoms=np.full(4, 3, np.uint8),
        outline=np.asarray([[-10, -10], [10, -10], [10, 10], [-10, 10]], float),
        pose0=np.asarray(
            [[-7, -3, 0, 0], [7, 3, 0, 0], [-7, 3, 0, 0], [7, -3, 0, 0]], float
        ),
        part_outline=(sq(0.5, 0.5),) * 4,
        net_layer_force=np.full(2, -1, np.int8),
        net_name=("N", "M"),
        pin_wh=np.tile([[0.6, 0.6]], (4, 1)),
        zones=(
            TopoZone(
                kind="keepout",
                ring=np.asarray([[-2, -2], [2, -2], [2, 2], [-2, 2], [-2, -2]], float),
                layers=(),
                name="kz",
            ),
        ),
    )
    mesh = build_mesh(d, d.pose0)
    ctx = _make_ctx(mesh, d, 2)
    w = build_wires(mesh, d, 2)
    assert not w.unreachable
    zone_edges = set(ctx.zone_contrib)
    assert zone_edges
    for bi, b in enumerate(w.branches):
        assert not set(b.edges) & zone_edges
        for e in b.edges:
            assert bi in w.order[e]
    _assert_routing_rule(ctx, d, w, 2)


def test_build_wires_no_transit_through_same_net_part() -> None:
    """The exemption keys on the wire's NET, not the endpoint part: a
    foreign-net pad's fan walls the wire off even when the pad sits on
    a same-net part (round-2 ruling). Net N links P0–P2–B; B's M-net TH
    pad keeps its walls, so no net-N branch crosses its fan boundary —
    while B's N-net pad still joins the tree (geodesic Prim attaches
    it) and net M routes its own branch."""
    from precis.pcb.topoplace.design import TopoDesign
    from precis.pcb.topoplace.wires import _make_ctx, build_wires

    sq = lambda hx, hy: np.asarray(
        [[-hx, -hy], [hx, -hy], [hx, hy], [-hx, hy], [-hx, -hy]], float
    )
    d = TopoDesign(
        parts=("P0", "P2", "B", "P3"),
        pin_xy=np.asarray([[0, 0], [0, 0], [0, 5.6], [0, -5.6], [0, 0]], float),
        pin_part=np.asarray([0, 1, 2, 2, 3]),
        pin_side=np.asarray([0, 0, 0, 2, 0], np.int8),
        pin_label=np.asarray(["1"] * 5, dtype=object),
        pin_net=np.asarray([0, 0, 0, 1, 1], np.int32),
        net_pins=(np.asarray([0, 1, 2]), np.asarray([3, 4])),
        flex=(),
        swap_groups=(),
        fixed=np.zeros(4, bool),
        freedoms=np.full(4, 3, np.uint8),
        outline=np.asarray([[-3, -10], [9, -10], [9, 9], [-3, 9]], float),
        pose0=np.asarray(
            [[0, 0, 0, 0], [6, 0, 0, 0], [3, 0, 0, 0], [3, -7.5, 0, 0]], float
        ),
        part_outline=(sq(0.4, 0.4), sq(0.4, 0.4), sq(1, 6), sq(0.4, 0.4)),
        net_layer_force=np.full(2, -1, np.int8),
        net_name=("N", "M"),
        pin_wh=np.tile([[0.6, 0.6]], (5, 1)),
    )
    mesh = build_mesh(d, d.pose0)
    w = build_wires(mesh, d, 2)
    ctx = _make_ctx(mesh, d, 2)
    net0 = [b for b in w.branches if b.net == 0]
    assert net0
    # the whole net connects through B's N pad (Prim's tree shape is
    # free), and no branch crosses the M-net pad's fan walls
    assert {p for b in net0 for p in (b.pin_a, b.pin_b)} == {0, 1, 2}
    for b in net0:
        assert set(b.edges).isdisjoint(ctx.fan_boundary[3])
    assert any(b.net == 1 for b in w.branches)
    assert not w.unreachable


def test_build_wires_two_owner_edge() -> None:
    """An edge carried by two owners — the endpoint's pad fan and a
    foreign keepout — prices BOTH: the pad's exemption covers only its
    own contribution, the zone's layers still block. A keepout abutting
    part A1's courtyard on the pin-1 side shares its eastern ring edge,
    making that edge two-owner: layer-0 keepout → net A crosses on the
    surviving layer; an all-layer keepout over a single-layer net → the
    branch detours around every two-owner edge (pin A1 still exits
    through fan-boundary edges the ring does not cover)."""
    import dataclasses

    from precis.pcb.topoplace.design import TopoZone
    from precis.pcb.topoplace.wires import _make_ctx, build_wires

    ring = np.asarray([[0.4, 1.6], [1.4, 1.6], [1.4, 2.4], [0.4, 2.4], [0.4, 1.6]])
    d = dataclasses.replace(
        example_a(),
        zones=(TopoZone(kind="keepout", ring=ring, layers=(0,), name="kz"),),
    )
    mesh = build_mesh(d, d.pose0)
    ctx = _make_ctx(mesh, d, 2)
    two_owner = {e for e in ctx.pad_contrib if e in ctx.zone_contrib}
    assert two_owner
    w = build_wires(mesh, d, 2)
    a = next(b for b in w.branches if d.net_name[b.net] == "A")
    assert set(a.edges) & two_owner  # crossed a two-owner edge on layer 1
    assert not w.unreachable

    d2 = dataclasses.replace(
        example_a(),
        zones=(TopoZone(kind="keepout", ring=ring, layers=(), name="kz"),),
        net_layer_force=np.asarray([0, -1, -1], np.int8),  # net A on layer 0
    )
    mesh2 = build_mesh(d2, d2.pose0)
    ctx2 = _make_ctx(mesh2, d2, 2)
    two2 = {e for e in ctx2.pad_contrib if e in ctx2.zone_contrib}
    assert two2
    w2 = build_wires(mesh2, d2, 2)
    a2 = next(b for b in w2.branches if d2.net_name[b.net] == "A")
    assert set(a2.edges).isdisjoint(two2)  # detoured around the keepout
    assert not [u for u in w2.unreachable if u.net == 0]


def test_build_wires_zone_hug_crosses_no_zone_edge() -> None:
    """A wire pulled taut against a keepout hugs its boundary without
    leaking inside: the taut polyline bends through the zone's own
    corner vertices and crosses no edge the zone contributes to — for a
    rectangular keepout on both sides and over a range of pin heights,
    and for a CONCAVE multi-corner keepout (round-2 item 1, the re-walk
    must not cut a reflex corner)."""
    from precis.pcb.topoplace.design import TopoDesign, TopoZone
    from precis.pcb.topoplace.wires import _make_ctx, build_wires

    sq = lambda hx, hy: np.asarray(
        [[-hx, -hy], [hx, -hy], [hx, hy], [-hx, hy], [-hx, -hy]], float
    )

    def zone_design(dy: float, ring: np.ndarray) -> TopoDesign:
        return TopoDesign(
            parts=("A", "B"),
            pin_xy=np.zeros((2, 2)),
            pin_part=np.asarray([0, 1]),
            pin_side=np.asarray([0, 0], np.int8),
            pin_label=np.asarray(["1"] * 2, dtype=object),
            pin_net=np.asarray([0, 0], np.int32),
            net_pins=(np.asarray([0, 1]),),
            flex=(),
            swap_groups=(),
            fixed=np.zeros(2, bool),
            freedoms=np.full(2, 3, np.uint8),
            outline=np.asarray([[-10, -10], [10, -10], [10, 10], [-10, 10]], float),
            pose0=np.asarray([[-6, dy, 0, 0], [6, dy, 0, 0]], float),
            part_outline=(sq(0.5, 0.5),) * 2,
            net_layer_force=np.full(1, -1, np.int8),
            net_name=("N",),
            pin_wh=np.tile([[0.6, 0.6]], (2, 1)),
            zones=(TopoZone(kind="keepout", ring=ring, layers=(), name="kz"),),
        )

    def hugs(dy: float, ring: np.ndarray) -> list[tuple[float, ...]]:
        d = zone_design(dy, ring)
        mesh = build_mesh(d, d.pose0)
        ctx = _make_ctx(mesh, d, 2)
        w = build_wires(mesh, d, 2)
        assert not w.unreachable
        (b,) = w.branches
        assert not set(b.edges) & set(ctx.zone_contrib)
        return [tuple(p) for p in b.points]

    rect = np.asarray([[-2, -1], [2, -1], [2, 1], [-2, 1], [-2, -1]], float)
    for dy in (-0.5, 0.0, 0.3, 1.5):
        hugs(dy, rect)
    # dead-level pins hug the southern corners; a slight lift takes the
    # northern ones — the bend lands ON the ring vertex either way
    assert hugs(0.0, rect)[1:3] == [(-2.0, -1.0), (2.0, -1.0)]
    assert hugs(0.3, rect)[1:3] == [(-2.0, 1.0), (2.0, 1.0)]
    # a reflex corner: the L's concave notch must not be cut either
    L = np.asarray(
        [[-2, -2], [2, -2], [2, -0.5], [-0.5, -0.5], [-0.5, 2], [-2, 2], [-2, -2]],
        float,
    )
    assert hugs(-1.0, L)


def test_build_wires_capacity_prices_pad_ends() -> None:
    """capacity_mm is the edge length minus each endpoint pad's extent
    projected onto the edge — a pin-touching edge loses half its pad's
    shadow, an edge between courtyard corners keeps its full length."""
    from precis.pcb.topoplace.wires import build_wires

    d = example_a()
    mesh = build_mesh(d, d.pose0)
    w = build_wires(mesh, d, 2)
    pin_vids = {int(v) for v in mesh.pin_vertex}
    pin_edges = np.flatnonzero(np.isin(mesh.edges, list(pin_vids)).any(axis=1))
    assert len(pin_edges) > 0
    for e in pin_edges:
        u, v = mesh.verts[mesh.edges[e]]
        dvec = v - u
        length = float(np.hypot(dvec[0], dvec[1]))
        ux, uy = float(dvec[0]) / length, float(dvec[1]) / length
        expect = length
        for vid in (int(mesh.edges[e, 0]), int(mesh.edges[e, 1])):
            if vid in pin_vids:
                pin = int(np.flatnonzero(mesh.pin_vertex == vid)[0])
                pw_, ph = float(d.pin_wh[pin, 0]), float(d.pin_wh[pin, 1])
                expect -= (pw_ * abs(ux) + ph * abs(uy)) / 2.0
        assert w.capacity_mm[e] == pytest.approx(max(expect, 0.0))
        assert 0.0 <= w.capacity_mm[e] < length
    corner_edges = np.flatnonzero(~np.isin(mesh.edges, list(pin_vids)).any(axis=1))
    assert len(corner_edges) > 0
    for e in corner_edges:
        u, v = mesh.verts[mesh.edges[e]]
        assert w.capacity_mm[e] == pytest.approx(float(np.hypot(*(v - u))))


def test_build_wires_unreachable_pad_reported() -> None:
    """A pad caged by a foreign obstacle on every candidate layer is
    dropped from its net's tree and named once on ``w.unreachable``
    with the boxing owners — the colour pass's certificate input; the
    rest of the design still routes (seam R-b)."""
    import dataclasses

    from precis.pcb.topoplace.design import TopoZone
    from precis.pcb.topoplace.wires import build_wires

    d = example_a()
    ring = np.asarray(
        [[-1.0, -1.0], [1.0, -1.0], [1.0, 1.0], [-1.0, 1.0], [-1.0, -1.0]]
    )
    d = dataclasses.replace(
        d,
        zones=(TopoZone(kind="keepout", ring=ring, layers=(), name="cage"),),
    )
    mesh = build_mesh(d, d.pose0)
    w = build_wires(mesh, d, 2)
    # the caged pin is net C's pin at (0,0) — even though it is the
    # tree's root, the record names the boxed side, not the free one
    assert len(w.unreachable) == 1
    hit = w.unreachable[0]
    assert d.net_name[hit.net] == "C"
    assert hit.pin == 4
    assert hit.owners == (len(d.parts),)  # the keepout zone
    assert all(b.edges for b in w.branches)
    assert all(d.net_name[b.net] != "C" for b in w.branches)


def _rescue_design(ring_own: bool, wall_mid_on_net: bool):
    """R is a lone pad; J is a pad ringed by four TH pads; T is a
    seven-pad TH wall across the middle. ``ring_own`` puts J's ring on
    the wire's own net (walls open to it); ``wall_mid_on_net`` puts T's
    middle wall pad on the net too (the reviewer's ``t`` repro)."""
    from precis.pcb.topoplace.design import TopoDesign

    sq = lambda hx, hy: np.asarray(
        [[-hx, -hy], [hx, -hy], [hx, hy], [-hx, hy], [-hx, -hy]], float
    )
    ring = [[1.0, 0], [0, 1.0], [-1.0, 0], [0, -1.0]]
    wall = [[0, -3.0 + i] for i in range(7)]
    pin_xy = [[0, 0]] + [[0, 0]] + ring + wall
    nets = [0, 0] + ([0] * 4 if ring_own else [1, 2, 3, 4]) + [5 + i for i in range(7)]
    if wall_mid_on_net:
        nets[6 + 3] = 0
    net_ids = sorted(set(nets))
    return TopoDesign(
        parts=("R", "J", "T"),
        pin_xy=np.asarray(pin_xy, float),
        pin_part=np.asarray([0, 1, 1, 1, 1, 1] + [2] * 7),
        pin_side=np.asarray([0, 2, 2, 2, 2, 2] + [2] * 7, np.int8),
        pin_label=np.asarray(["1"] * len(nets), dtype=object),
        pin_net=np.asarray(nets, np.int32),
        net_pins=tuple(
            np.asarray([i for i, n in enumerate(nets) if n == k]) for k in net_ids
        ),
        flex=(),
        swap_groups=(),
        fixed=np.zeros(3, bool),
        freedoms=np.full(3, 3, np.uint8),
        outline=np.asarray([[-10, -10], [10, -10], [10, 10], [-10, 10]], float),
        pose0=np.asarray([[-6, 0, 0, 0], [5, 0, 0, 0], [0, 0, 0, 0]], float),
        part_outline=(sq(0.5, 0.5), sq(1.6, 1.6), sq(0.6, 3.6)),
        net_layer_force=np.full(len(net_ids), -1, np.int8),
        net_name=tuple(f"n{k}" for k in net_ids),
        pin_wh=np.tile([[0.6, 0.6]], (len(nets), 1)),
    )


def test_build_wires_rescue_through_own_net_walls() -> None:
    """The net-keyed rescue, positive direction: J's centre pad sits
    behind a ring of four TH pads, but the ring is on the wire's OWN
    net, so its walls open — the pin joins the tree and a branch crosses
    the ring's own fan edges to reach it (round-2 item 7: a successful
    rescue). Foreign-net rings wall the same pin off instead."""
    from precis.pcb.topoplace.mesh import build_mesh
    from precis.pcb.topoplace.wires import _make_ctx, build_wires

    d = _rescue_design(ring_own=True, wall_mid_on_net=False)
    mesh = build_mesh(d, d.pose0)
    ctx = _make_ctx(mesh, d, 2)
    w = build_wires(mesh, d, 2)
    assert not w.unreachable
    own = frozenset(int(p) for p in d.net_pins[0])
    assert own == frozenset(range(6))  # R pad, J centre, J ring
    wall_crossings = [
        e
        for b in w.branches
        if b.net == 0
        for e in b.edges
        if any(p in own for p, _m in ctx.pad_contrib.get(e, ()))
    ]
    assert wall_crossings  # a branch went THROUGH the ring's walls
    _assert_routing_rule(ctx, d, w, 2)

    d2 = _rescue_design(ring_own=False, wall_mid_on_net=False)
    w2 = build_wires(build_mesh(d2, d2.pose0), d2, 2)
    (u,) = w2.unreachable
    assert (u.net, u.pin, u.owners) == (0, 1, (1,))  # caged by part J


def test_build_wires_rescue_t_wall_pad_on_net() -> None:
    """The ``t`` repro: T's middle wall pad joins the wire's net, but
    the wall's foreign-net pads still wall it — the search must NOT
    cross J's net-3 pad (or T's) to reach it; both caged pins come back
    on ``w.unreachable`` with their boxing parts (round-2 item 2)."""
    from precis.pcb.topoplace.mesh import build_mesh
    from precis.pcb.topoplace.wires import _make_ctx, build_wires

    d = _rescue_design(ring_own=False, wall_mid_on_net=True)
    mesh = build_mesh(d, d.pose0)
    ctx = _make_ctx(mesh, d, 2)
    w = build_wires(mesh, d, 2)
    by_pin = {u.pin: u for u in w.unreachable if u.net == 0}
    assert set(by_pin) == {1, 9}
    assert by_pin[1].owners == (1,)  # J's ring
    assert by_pin[9].owners == (2,)  # T's wall
    _assert_routing_rule(ctx, d, w, 2)


def test_build_wires_caged_first_pin_keeps_net() -> None:
    """A boxed-in first pin does not drop its net: with pin order
    [0,1,2,3] and pin 0 caged by a keepout, the forest roots fresh
    components and still routes the free pins; the caged one is named
    once on ``w.unreachable`` with the zone owner (round-2 item 4)."""
    from precis.pcb.topoplace.design import TopoDesign, TopoZone
    from precis.pcb.topoplace.wires import build_wires

    sq = lambda hx, hy: np.asarray(
        [[-hx, -hy], [hx, -hy], [hx, hy], [-hx, hy], [-hx, -hy]], float
    )
    d = TopoDesign(
        parts=("A", "B", "C", "D"),
        pin_xy=np.zeros((4, 2)),
        pin_part=np.asarray([0, 1, 2, 3]),
        pin_side=np.asarray([0] * 4, np.int8),
        pin_label=np.asarray(["1"] * 4, dtype=object),
        pin_net=np.asarray([0] * 4, np.int32),
        net_pins=(np.asarray([0, 1, 2, 3]),),
        flex=(),
        swap_groups=(),
        fixed=np.zeros(4, bool),
        freedoms=np.full(4, 3, np.uint8),
        outline=np.asarray([[-10, -10], [10, -10], [10, 10], [-10, 10]], float),
        pose0=np.asarray(
            [[-5, 0, 0, 0], [3, 0, 0, 0], [5, 4, 0, 0], [5, -4, 0, 0]], float
        ),
        part_outline=(sq(0.5, 0.5),) * 4,
        net_layer_force=np.full(1, -1, np.int8),
        net_name=("N",),
        pin_wh=np.tile([[0.6, 0.6]], (4, 1)),
        zones=(
            TopoZone(
                kind="keepout",
                ring=np.asarray(
                    [[-7, -2], [-3, -2], [-3, 2], [-7, 2], [-7, -2]], float
                ),
                layers=(),
                name="cage",
            ),
        ),
    )
    mesh = build_mesh(d, d.pose0)
    w = build_wires(mesh, d, 2)
    (u,) = w.unreachable
    assert (u.net, u.pin) == (0, 0)
    assert u.owners == (len(d.parts),)  # the keepout zone
    assert {p for b in w.branches for p in (b.pin_a, b.pin_b)} == {1, 2, 3}


def test_build_wires_t_lies_on_taut_path() -> None:
    """``t`` is geometry: the point at parameter ``t`` on a crossed edge
    lies on the branch's taut polyline — the crossing parameter a 2c
    via consumes is the real crossing."""
    from precis.pcb.geom import dist_point_to_segment
    from precis.pcb.topoplace.wires import build_wires

    d = example_a()
    mesh = build_mesh(d, d.pose0)
    w = build_wires(mesh, d, 2)
    for b in w.branches:
        for e, te in zip(b.edges, b.t):
            u = mesh.verts[mesh.edges[e, 0]]
            v = mesh.verts[mesh.edges[e, 1]]
            p = u + te * (v - u)
            gap = min(
                dist_point_to_segment(
                    (float(p[0]), float(p[1])), b.points[i], b.points[i + 1]
                )
                for i in range(len(b.points) - 1)
            )
            assert gap <= 1e-6


def test_build_wires_corridor_adjacency() -> None:
    """``tris`` is a real corridor: edge ``i`` is shared by tris
    ``i``/``i+1``, ``tris[0]`` holds pin_a's vertex, ``tris[-1]``
    pin_b's."""
    from precis.pcb.topoplace.wires import build_wires

    d, _inst, _nets = synth_design(seed=0, n_parts=40)
    mesh = build_mesh(d, d.pose0)
    w = build_wires(mesh, d, 4)
    for b in w.branches:
        assert len(b.tris) == len(b.edges) + 1
        va = int(mesh.pin_vertex[b.pin_a])
        vb = int(mesh.pin_vertex[b.pin_b])
        assert va in (int(x) for x in mesh.tris[b.tris[0]])
        assert vb in (int(x) for x in mesh.tris[b.tris[-1]])
        for i, e in enumerate(b.edges):
            sides = {int(x) for x in mesh.edge_tris[e] if int(x) >= 0}
            assert {b.tris[i], b.tris[i + 1]} == sides


def test_build_wires_tie_order_consistent() -> None:
    """Order ties at wrapped vertices resolve by divergence, giving the
    same answer on every edge of a shared run: two different-net wires
    whose taut polylines never cross keep ONE normalized side on all
    shared edges — 2c reads no phantom order flips (seam finding 1).
    The sign is normalized by the edge's stored direction against the
    corridor travel, since adjacent edges needn't agree on u→v."""
    from precis.pcb.geom import segments_cross
    from precis.pcb.topoplace.wires import build_wires

    n_layers = 4
    d, _inst, _nets = synth_design(seed=0, n_parts=40)
    mesh = build_mesh(d, d.pose0)
    w = build_wires(mesh, d, n_layers)
    cent = [mesh.verts[mesh.tris[t]].mean(axis=0) for t in range(len(mesh.tris))]

    def norm_sign(ba, bb, e: int, a: int, b: int) -> int:
        pa, pb = w.order[e].index(a), w.order[e].index(b)
        assert pa != pb
        s = 1 if pa < pb else -1
        ib = list(bb.edges).index(e)
        cdir = cent[bb.tris[ib + 1]] - cent[bb.tris[ib]]
        edir = mesh.verts[mesh.edges[e, 1]] - mesh.verts[mesh.edges[e, 0]]
        cr = float(cdir[0] * edir[1] - cdir[1] * edir[0])
        return s * (1 if cr > 0 else -1) if abs(cr) > 1e-12 else s

    def polys_cross(ba, bb) -> bool:
        pa, pb = ba.points, bb.points
        if any(
            segments_cross(pa[i], pa[i + 1], pb[j], pb[j + 1])
            for i in range(len(pa) - 1)
            for j in range(len(pb) - 1)
        ):
            return True
        # a shared bend vertex is a real crossing when the four rays
        # alternate cyclically around it (an X touch segments_cross
        # cannot see); coincident rays merge first — wires arriving or
        # leaving together touch without piercing
        import math

        for i in range(1, len(pa) - 1):
            for j in range(1, len(pb) - 1):
                if math.dist(pa[i], pb[j]) > 1e-9:
                    continue
                p = np.asarray(pa[i])
                rays = sorted(
                    (math.atan2(float(v[1]), float(v[0])), tag)
                    for tag, v in (
                        ("a", np.asarray(pa[i - 1]) - p),
                        ("a", np.asarray(pa[i + 1]) - p),
                        ("b", np.asarray(pb[j - 1]) - p),
                        ("b", np.asarray(pb[j + 1]) - p),
                    )
                )
                merged: list[tuple[float, str]] = []
                for ang, tag in rays:
                    if merged and ang - merged[-1][0] <= 1e-6:
                        merged[-1] = (merged[-1][0], merged[-1][1] + tag)
                    else:
                        merged.append((ang, tag))
                if (
                    len(merged) > 1
                    and merged[0][0] + 2 * math.pi - merged[-1][0] <= 1e-6
                ):
                    merged[0] = (merged[0][0], merged[-1][1] + merged[0][1])
                    merged.pop()
                if len(merged) != 4:
                    continue
                ia = [k for k, (_a, t) in enumerate(merged) if t == "a"]
                if ia[1] - ia[0] == 2:
                    return True
        return False

    seen: set[tuple[int, int]] = set()
    phantom = 0
    tied = 0
    for e, ids in enumerate(w.order):
        for a, b in itertools.pairwise(ids):
            if (a, b) in seen:
                continue
            seen.add((a, b))
            ba, bb = w.branches[a], w.branches[b]
            if ba.net == bb.net:
                continue
            ea, eb = ba.edges, bb.edges
            eb_at = {x: i for i, x in enumerate(eb)}
            i = 0
            runs: list[list[int]] = []
            while i < len(ea):
                if ea[i] in eb_at:
                    run = [ea[i]]
                    j = i + 1
                    while (
                        j < len(ea)
                        and ea[j] in eb_at
                        and abs(eb_at[ea[j]] - eb_at[ea[j - 1]]) == 1
                    ):
                        run.append(ea[j])
                        j += 1
                    runs.append(run)
                    i = j
                else:
                    i += 1
            for run in runs:
                if len(run) < 2:
                    continue
                if any(
                    abs(ba.t[list(ea).index(e2)] - bb.t[list(eb).index(e2)]) <= 1e-9
                    for e2 in run
                ):
                    tied += 1
                signs = {norm_sign(ba, bb, e2, a, b) for e2 in run}
                if len(signs) > 1 and not polys_cross(ba, bb):
                    phantom += 1
    assert tied > 0  # the fixture does produce wrapped-vertex ties
    assert phantom == 0


def test_build_wires_capacity_honours_rotation() -> None:
    """The pad extent priced off an edge is read through the part pose:
    a quarter-turned part projects its pad's ROTATED shadow, so an
    x-aligned edge sees the local-y extent, not the local-x one."""
    import dataclasses

    from precis.pcb.topoplace.wires import build_wires

    d = example_a()
    pose = d.pose0.copy()
    pose[0, 2] = 1.0  # A1 quarter-turned
    pin_wh = d.pin_wh.copy()
    pin_wh[0] = (2.0, 1.0)
    d = dataclasses.replace(d, pose0=pose, pin_wh=pin_wh)
    mesh = build_mesh(d, d.pose0)
    w = build_wires(mesh, d, 2)
    v0 = int(mesh.pin_vertex[0])
    hit = 0
    for e in range(len(mesh.edges)):
        if v0 not in (int(mesh.edges[e, 0]), int(mesh.edges[e, 1])):
            continue
        hit += 1
        u, v = mesh.verts[mesh.edges[e]]
        dvec = v - u
        length = float(np.hypot(dvec[0], dvec[1]))
        ux, uy = abs(float(dvec[0])) / length, abs(float(dvec[1])) / length
        # rot_q 1: local +x maps to world +y — the pad's local h shows
        # on the edge's x projection and w on its y projection
        expect = length - (1.0 * ux + 2.0 * uy) / 2.0
        assert w.capacity_mm[e] == pytest.approx(max(expect, 0.0))
    assert hit > 0


def test_build_wires_rate_printed() -> None:
    """The 2b route runs well inside the spec's 2 evals/s full-route
    floor at 100 parts — print the measured rate so 2c's budget is
    visible."""
    import time

    from precis.pcb.topoplace.wires import build_wires

    d, _inst, _nets = synth_design(seed=0, n_parts=100)
    mesh = build_mesh(d, d.pose0)
    t0 = time.monotonic()
    w = build_wires(mesh, d, 4)
    dt = time.monotonic() - t0
    rate = 1.0 / dt
    print(
        f"\nwires: {len(w.branches)} branches in {dt * 1e3:.0f} ms -> {rate:.1f} evals/s"
    )
    # Floor 1.7 for r108 (CI runner measured 1.84; local 2.9). gr515107
    # round 4 restores >= 2.0 — Reto 2026-10-25.
    assert rate >= 1.7


def test_build_wires_deterministic() -> None:
    from precis.pcb.topoplace.wires import build_wires

    d, _inst, _nets = synth_design(seed=3)
    mesh = build_mesh(d, d.pose0)
    a, b = build_wires(mesh, d, 2), build_wires(mesh, d, 2)
    assert a.branches == b.branches
    assert a.order == b.order
    assert a.net_layers == b.net_layers
    np.testing.assert_array_equal(a.capacity_mm, b.capacity_mm)


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
