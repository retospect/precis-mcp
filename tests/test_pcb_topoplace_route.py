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
    TopoDesign,
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


def test_th_header_t_junction_recovery_keeps_every_constraint() -> None:
    """The nano seed-3 failure (topo-s1): two 1x15 through-hole headers
    at a near-touching offset raised ``recovered edge ... vanished`` —
    an ear-clip sliver parked a pruned collinear vertex exactly ON an
    edge, and a later constraint ending there found no proper crossing
    to walk. The mesh must build, and every constraint edge —
    courtyard, board outline AND each pad's rect ring — must be
    present and constrained."""
    from precis.pcb.topoplace.mesh import _place

    pitch, pad, n_pin = 2.54, 1.7, 15
    cy_hx = (n_pin - 1) * pitch / 2 + pad / 2 + 0.4
    cy_hy = pad / 2 + 0.4
    ring = np.asarray(
        [
            [-cy_hx, -cy_hy],
            [cy_hx, -cy_hy],
            [cy_hx, cy_hy],
            [-cy_hx, cy_hy],
            [-cy_hx, -cy_hy],
        ]
    )
    # the failing offset the pose scan found (dx=2.54, dy=0.8)
    pose = np.asarray(
        [[20.0, 20.0, 1.0, 0.0], [22.54, 20.8, 1.0, 0.0]], dtype=np.float64
    )
    d = TopoDesign(
        parts=("J1", "J2"),
        pin_xy=np.asarray([[(i - 7) * pitch, 0.0] for i in range(n_pin)] * 2),
        pin_part=np.asarray([0] * n_pin + [1] * n_pin),
        pin_side=np.full(2 * n_pin, 2, dtype=np.int8),
        pin_label=np.asarray([str(i + 1) for i in range(n_pin)] * 2, dtype=object),
        pin_net=np.arange(2 * n_pin) % n_pin,
        net_pins=tuple(
            np.asarray([i, n_pin + i], dtype=np.int64) for i in range(n_pin)
        ),
        flex=(),
        swap_groups=(),
        fixed=np.zeros(2, dtype=bool),
        freedoms=np.full(2, 3, dtype=np.uint8),
        outline=np.asarray([[-2.0, -2.0], [42.0, -2.0], [42.0, 42.0], [-2.0, 42.0]]),
        pose0=pose,
        part_outline=(ring, ring),
        net_layer_force=np.full(n_pin, -1, dtype=np.int8),
        net_name=tuple(f"N{i + 1}" for i in range(n_pin)),
        pin_wh=np.tile(np.asarray([[pad, pad]]), (2 * n_pin, 1)),
    )
    mesh = build_mesh(d, pose)

    vid_of = {
        (round(float(x), 9), round(float(y), 9)): i
        for i, (x, y) in enumerate(mesh.verts)
    }
    edges = _edge_set(mesh)
    constrained = {
        tuple(int(x) for x in mesh.edges[i]) for i in np.flatnonzero(mesh.constrained)
    }

    def assert_edge(pa, pb) -> None:
        e = tuple(
            sorted((vid_of[tuple(np.round(pa, 9))], vid_of[tuple(np.round(pb, 9))]))
        )
        assert e in edges, (pa, pb)
        assert e in constrained, (pa, pb)

    # board outline
    outline = np.asarray(d.outline)
    for pa, pb in _ring_edges_xy(mesh, np.vstack([outline, outline[0]])):
        assert_edge(pa, pb)
    # courtyard rings at their world poses
    for part in range(len(d.parts)):
        world = _place(np.asarray(d.part_outline[part][:-1]), pose[part])
        for pa, pb in _ring_edges_xy(mesh, np.vstack([world, world[0]])):
            assert_edge(pa, pb)
    # every pad's rect ring — the ear-clip's T-junction victims
    for pin in range(len(d.pin_xy)):
        part = int(d.pin_part[pin])
        pw, ph = (float(v) for v in d.pin_wh[pin])
        cx, cy = (float(v) for v in d.pin_xy[pin])
        local = np.asarray(
            [
                [cx - pw / 2, cy - ph / 2],
                [cx + pw / 2, cy - ph / 2],
                [cx + pw / 2, cy + ph / 2],
                [cx - pw / 2, cy + ph / 2],
            ]
        )
        world = _place(local, pose[part])
        for pa, pb in _ring_edges_xy(mesh, np.vstack([world, world[0]])):
            assert_edge(pa, pb)


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
    from precis.pcb.topoplace.mesh import _place

    base: set[tuple[float, float]] = set()
    for pin in range(len(d.pin_xy)):
        part = int(d.pin_part[pin])
        pw, ph = d.pin_wh[pin]
        cx, cy = d.pin_xy[pin]
        pts = np.vstack(
            [
                _place(np.asarray([[float(cx), float(cy)]]), pose[part]),
                _place(
                    np.asarray(
                        [
                            [cx - pw / 2, cy - ph / 2],
                            [cx + pw / 2, cy - ph / 2],
                            [cx + pw / 2, cy + ph / 2],
                            [cx - pw / 2, cy + ph / 2],
                        ]
                    ),
                    pose[part],
                ),
                _place(np.asarray(d.part_outline[part]), pose[part]),
            ]
        )
        base |= {(round(float(p[0]), 9), round(float(p[1]), 9)) for p in pts}
    base |= {
        (round(float(p[0]), 9), round(float(p[1]), 9)) for p in np.asarray(d.outline)
    }
    # 2 courtyard×courtyard crossings + 4 pad-ring×courtyard crossings
    # from the moved part's pad rect clipping the other's walls
    n_cross_verts = len(mesh.verts) - len(base)
    assert n_cross_verts == 6


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


def test_zone_conflict_measures_the_part_body() -> None:
    """gr529963 (ruling Reto 2026-10-28): the part-on-zone hard gate
    (``courtyard_hole``) measures the part BODY against the zone polygon
    — the zone ring already carries its margin in its radius, so the
    courtyard's 0.4mm must not stack on top. Part-part conflicts and
    outline escapes stay on courtyard rings. A part with no ``part_body``
    ring falls back to its courtyard ring (``test_zones_constrain_and_report``
    above pins exactly that)."""
    import dataclasses

    from precis.pcb.topoplace.design import TopoZone

    ring = np.asarray([[2.0, -1.0], [4.0, -1.0], [4.0, 1.0], [2.0, 1.0], [2.0, -1.0]])
    zones = (TopoZone(kind="keepout", ring=ring, layers=(0,), name="k"),)
    pose = example_a().pose0.copy()
    pose[2, 0] = 1.7  # B1's 0.8mm courtyard spans x 1.3..2.1: on the zone

    # B1's body: a 0.3mm square — spans x 1.55..1.85, clear of the zone.
    # The other parts carry no body ring, so they fall back to their
    # courtyard rings in the zone test; all of them sit clear of the
    # zone anyway.
    def sq(half: float) -> np.ndarray:
        return np.asarray(
            [
                [-half, -half],
                [half, -half],
                [half, half],
                [-half, half],
                [-half, -half],
            ]
        )

    part_body = tuple(
        sq(0.15) if i == 2 else np.zeros((0, 2)) for i in range(len(example_a().parts))
    )
    d = dataclasses.replace(example_a(), zones=zones, part_body=part_body)
    mesh = build_mesh(d, pose)
    assert not mesh.zone_conflicts
    # Same pose WITHOUT body rings: the courtyard fallback flags B1 —
    # proving the clean verdict above came from the body measure.
    d_nobody = dataclasses.replace(example_a(), zones=zones)
    assert build_mesh(d_nobody, pose).zone_conflicts
    # And a body that DOES bite the zone still conflicts.
    d_hot = dataclasses.replace(
        example_a(),
        zones=zones,
        part_body=tuple(
            sq(0.5) if i == 2 else np.zeros((0, 2))
            for i in range(len(example_a().parts))
        ),
    )
    mesh_hot = build_mesh(d_hot, pose)
    assert mesh_hot.zone_conflicts and mesh_hot.zone_conflicts[0][0] == 2


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


def _assert_routing_rule(
    ctx, d, w, n_layers: int, rule: tuple[float, float] = (0.15, 0.15)
) -> None:
    """Check the routing rule with an oracle independent of
    ``_blocked_masks``. Fan walls and zone walls are recomputed from
    mesh/design data; for the gap rule each ringed pad's rectangle is
    placed from ``pin_xy``/``pin_wh`` and the pose, inflated by
    ``trace/2 + clearance``, and clipped against the crossed edge."""
    from precis.pcb.topoplace.mesh import _place

    trace_mm, clearance_mm = rule
    grow = trace_mm / 2.0 + clearance_mm
    all_mask = (1 << n_layers) - 1

    def copper(pin: int) -> int:
        side = int(d.pin_side[pin])
        if side == 2:
            return all_mask
        return 1 << (n_layers - 1 if side == 1 else 0)

    # Rebuild each pin's fan boundary rather than reuse ctx.fan_boundary.
    # ``add_vert`` stores vertices rounded to 9 decimals, so the
    # ``vid_of`` key is the 9-decimal value re-rounded to 6 — rounding
    # the raw pin position once can straddle the 6-decimal boundary.
    pin_xy = all_pin_world(d, d.pose0)
    fans: list[set[int]] = []
    for x, y in pin_xy:
        key = (
            round(round(float(x), 9), 6),
            round(round(float(y), 9), 6),
        )
        v = ctx.vid_of[key]
        fans.append(
            {
                int(e)
                for t in ctx.v2t[v]
                for e in ctx.tri_edge_list[t]
                if v not in ctx.edge_ends[e]
            }
        )

    # Place each ringed pad's four corners independently and retain its
    # world bounding box.
    pad_boxes: list[tuple[int, np.ndarray, np.ndarray]] = []
    for p in range(len(d.pin_xy)):
        if len(d.pin_wh) <= p:
            continue
        pw, ph = float(d.pin_wh[p, 0]), float(d.pin_wh[p, 1])
        if pw <= 0.0 or ph <= 0.0:
            continue
        cx, cy = (float(v) for v in d.pin_xy[p])
        local = np.asarray(
            [
                [cx - pw / 2.0, cy - ph / 2.0],
                [cx + pw / 2.0, cy - ph / 2.0],
                [cx + pw / 2.0, cy + ph / 2.0],
                [cx - pw / 2.0, cy + ph / 2.0],
            ]
        )
        world = _place(local, d.pose0[int(d.pin_part[p])])
        pad_boxes.append((p, world.min(axis=0) - grow, world.max(axis=0) + grow))

    def clip_interval(e: int, lo: np.ndarray, hi: np.ndarray):
        """Parameter interval of edge ``e`` inside box [lo,hi]."""
        p0, p1 = ctx.seg_u[e], ctx.seg_v[e]
        ent, ext = 0.0, 1.0
        for ax in range(2):
            delta = float(p1[ax] - p0[ax])
            pos = float(p0[ax])
            if abs(delta) <= 1e-12:
                if pos < lo[ax] or pos > hi[ax]:
                    return None
                continue
            t0 = (float(lo[ax]) - pos) / delta
            t1 = (float(hi[ax]) - pos) / delta
            ent = max(ent, min(t0, t1))
            ext = min(ext, max(t0, t1))
            if ent >= ext:
                return None
        return (ent, ext)

    for b in w.branches:
        net = d.net_name[b.net] if b.net < len(d.net_name) else str(b.net)
        cand = w.net_layers[b.net] or frozenset({0, n_layers - 1})
        cand_mask = sum(1 << ly for ly in cand)
        own = frozenset(int(p) for p in d.net_pins[b.net])
        for e in b.edges:
            assert not ctx.board[e]
            blocked = 0
            for pin in range(len(d.pin_xy)):
                if pin not in own and e in fans[pin]:
                    blocked |= copper(pin)
            for z, zm in ctx.zone_contrib.get(e, ()):
                zone_net = d.zones[z].net
                if not (zone_net and zone_net == net):
                    blocked |= zm
            intervals: list[list[tuple[float, float]]] = [[] for _ in range(n_layers)]
            for pin, lo, hi in pad_boxes:
                if pin in own:
                    continue
                iv = clip_interval(int(e), lo, hi)
                if iv is None:
                    continue
                for ly in range(n_layers):
                    if copper(pin) & (1 << ly):
                        intervals[ly].append(iv)
            for ly in range(n_layers):
                ints = sorted(intervals[ly])
                covered = 0.0
                cur_lo, cur_hi = -1.0, -1.0
                for a0, b0 in ints:
                    if a0 <= cur_hi:
                        cur_hi = max(cur_hi, b0)
                    else:
                        covered += max(0.0, cur_hi - cur_lo)
                        cur_lo, cur_hi = a0, b0
                covered += max(0.0, cur_hi - cur_lo)
                free = float(ctx.seg_len[e]) - covered * float(ctx.seg_len[e])
                if free <= 1e-9:
                    blocked |= 1 << ly
            assert cand_mask & ~blocked


def test_routing_rule_oracle_catches_a_gap_leak(monkeypatch) -> None:
    """The oracle must stay independent of ``_blocked_masks`` (round-5
    must-fix 2): patch a gap leak into the implementation — the
    wrapper re-opens every edge ``_pad_eval`` closed — and the wire
    threads the closed 0.25 mm channel, where
    ``_assert_routing_rule``'s own coverage recompute has to refuse."""
    from precis.pcb.topoplace import wires as wires_mod

    d = _pad_row_design(0.75, 0.5, split=True)  # channel 0.25 < 0.45
    mesh = build_mesh(d, d.pose0)
    ctx = wires_mod._make_ctx(mesh, d, 2)
    real = wires_mod._blocked_masks

    def leaky(ctx_, d_, cand_mask, net, own, trace_mm, clearance_mm):
        out = real(ctx_, d_, cand_mask, net, own, trace_mm, clearance_mm).copy()
        blocked_gap, _free, _ep, _pe = wires_mod._pad_eval(
            ctx_, trace_mm / 2.0 + clearance_mm
        )
        out[blocked_gap != 0] = True  # the leak: closed channels pass
        return out

    monkeypatch.setattr(wires_mod, "_blocked_masks", leaky)
    w = wires_mod.build_wires(mesh, d, 2)
    assert not w.unreachable
    (b,) = [bb for bb in w.branches if bb.net == 0]
    # the leak did bite: the wire takes the closed channel, not the
    # detour past the grown row top
    assert any(abs(y) <= 3 * 0.75 + 0.25 for y in _crossing_ys(b))
    with pytest.raises(AssertionError):
        _assert_routing_rule(ctx, d, w, 2)


def test_build_wires_order_and_routing_rule() -> None:
    """``order[e]`` holds exactly the branches crossing ``e``, sorted by
    their taut parameter within the tie tolerance — and the routing rule
    (see ``_assert_routing_rule``) holds on every crossed edge."""
    from precis.pcb.topoplace.wires import _make_ctx, build_wires

    n_layers = 4
    d, _inst, _nets = synth_design(seed=0, n_parts=40, pad_mm=0.5)
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

    ring = np.asarray([[0.3, 1.7], [1.4, 1.7], [1.4, 2.3], [0.3, 2.3], [0.3, 1.7]])
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


def _ring_cage_design(ring_own: bool, wall_mid_on_net: bool):
    """R is a lone pad; J is a pad ringed by four TH pads big enough to
    seal (1.2 mm rings meet the 1.6 mm courtyard and leave <0.3 mm
    diagonals); T is a seven-pad TH wall across the middle.
    ``ring_own`` puts J's ring on the wire's own net (walls open to
    it); ``wall_mid_on_net`` puts T's middle wall pad on the net too
    (the reviewer's ``t`` repro)."""
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
    pin_wh = np.tile([[0.6, 0.6]], (len(nets), 1)).astype(float)
    pin_wh[2:6] = 1.2  # J's ring pads — sealed cage, not a sliver wall
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
        pin_wh=pin_wh,
    )


def test_build_wires_own_net_ring_opens() -> None:
    """Own-net ring opens: J's centre pad sits behind a ring of four TH
    pads on the wire's OWN net — the walls are exempt by net and the
    own-flanked gaps are open, so the pin joins the tree and a branch
    crosses the ring's own fan edges to reach it (round-2 item 7)."""
    from precis.pcb.topoplace.mesh import build_mesh
    from precis.pcb.topoplace.wires import _make_ctx, build_wires

    d = _ring_cage_design(ring_own=True, wall_mid_on_net=False)
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


def test_build_wires_foreign_net_ring_cages() -> None:
    """Foreign-net ring cages: with J's ring on other nets the pin is
    genuinely boxed — the sub-clearance diagonal gaps and the pad
    walls are all foreign — and ``unreachable`` names it with J as
    owner. The ``t`` variant (T's middle wall pad on the net) still
    routes: every gap it escapes through is flanked by its own pad
    (round-2 item 2; the pad-wall mesh makes that escape real)."""
    from precis.pcb.topoplace.mesh import build_mesh
    from precis.pcb.topoplace.wires import _make_ctx, build_wires

    d = _ring_cage_design(ring_own=False, wall_mid_on_net=False)
    w = build_wires(build_mesh(d, d.pose0), d, 2)
    (u,) = w.unreachable
    assert (u.net, u.pin, u.owners) == (0, 1, (1,))  # caged by part J

    d2 = _ring_cage_design(ring_own=False, wall_mid_on_net=True)
    mesh2 = build_mesh(d2, d2.pose0)
    ctx2 = _make_ctx(mesh2, d2, 2)
    w2 = build_wires(mesh2, d2, 2)
    (u2,) = w2.unreachable
    assert (u2.net, u2.pin, u2.owners) == (0, 1, (1,))  # J still caged
    assert any(b.net == 0 for b in w2.branches)  # pin 9 routed
    _assert_routing_rule(ctx2, d2, w2, 2)


def _pad_row_design(
    pitch: float,
    padw: float,
    n_row: int = 7,
    split: bool = False,
    side: int = 2,
    n_force: int = -1,
) -> TopoDesign:
    """A vertical row of ``n_row`` same-size pads at x=0 (foreign nets),
    net N's two pads at (±5, 0). ``split`` gives every row pad its own
    part with a courtyard hugging the pad — the round-4 finding-1 repro
    where a non-pad courtyard corner splits the channel into edges the
    old corner-to-corner test never saw."""
    sq = lambda hx, hy: np.asarray(
        [[-hx, -hy], [hx, -hy], [hx, hy], [-hx, hy], [-hx, -hy]], float
    )
    ys = [(-(n_row - 1) / 2.0 + i) * pitch for i in range(n_row)]
    if split:
        parts = ("A", "B") + tuple(f"H{i}" for i in range(n_row))
        pin_part = [0, 1] + [2 + i for i in range(n_row)]
        pin_xy = [[0.0, 0.0]] * (2 + n_row)
        pose = [[-5, 0, 0, 0], [5, 0, 0, 0]] + [[0, y, 0, 0] for y in ys]
        outl = (sq(0.4, 0.4),) * 2 + tuple(
            sq(padw / 2 + 0.02, padw / 2 + 0.02) for _ in range(n_row)
        )
    else:
        parts = ("A", "B", "H")
        pin_part = [0, 1] + [2] * n_row
        pin_xy = [[0.0, 0.0], [0.0, 0.0]] + [[0.0, y] for y in ys]
        pose = [[-5, 0, 0, 0], [5, 0, 0, 0], [0, 0, 0, 0]]
        outl = (sq(0.4, 0.4),) * 2 + (
            sq(padw / 2 + 0.25, abs(ys[0]) + padw / 2 + 0.25),
        )
    nets = [0, 0] + [1 + i for i in range(n_row)]
    return TopoDesign(
        parts=parts,
        pin_xy=np.asarray(pin_xy, float),
        pin_part=np.asarray(pin_part),
        pin_side=np.asarray([0, 0] + [side] * n_row, np.int8),
        pin_label=np.asarray(["1"] * len(nets), dtype=object),
        pin_net=np.asarray(nets, np.int32),
        net_pins=(np.asarray([0, 1]),)
        + tuple(np.asarray([2 + i]) for i in range(n_row)),
        flex=(),
        swap_groups=(),
        fixed=np.zeros(len(parts), bool),
        freedoms=np.full(len(parts), 3, np.uint8),
        outline=np.asarray([[-10, -10], [10, -10], [10, 10], [-10, 10]], float),
        pose0=np.asarray(pose, float),
        part_outline=outl,
        net_layer_force=np.asarray([n_force] + [-1] * n_row, np.int8),
        net_name=("N",) + tuple(f"M{i}" for i in range(n_row)),
        pin_wh=np.tile([[padw, padw]], (len(nets), 1)),
    )


def _crossing_ys(b) -> list[float]:
    """y values where the branch's taut polyline crosses x=0."""
    ys = []
    pts = b.points
    for (x0, y0), (x1, y1) in itertools.pairwise(pts):
        if (x0 <= 0 < x1) or (x1 <= 0 < x0):
            s = (0.0 - x0) / (x1 - x0) if x1 != x0 else 0.0
            ys.append(y0 + s * (y1 - y0))
    return ys


def test_build_wires_gap_priced_by_geometry() -> None:
    """Round-4 gap rule: every mesh edge is priced against each ringed
    pad grown by ``trace/2 + clearance`` — an edge stays open on a layer
    only where a free stretch survives, so a channel closes exactly when
    ``gap < trace + 2·clearance`` and only on layers where the pads have
    copper. Channels split by non-pad vertices (a courtyard corner
    between split parts) still close (finding 1); pads without a built
    ring contribute nothing (finding 2); ``Wires.gap_free_mm``/
    ``gap_cap`` record the free stretch and how many further traces fit
    for slice 4."""
    from precis.pcb.topoplace.mesh import build_mesh
    from precis.pcb.topoplace.wires import _make_ctx, _pad_eval, build_wires

    r_def = 0.15 / 2.0 + 0.15  # default rule growth: need = 0.45 mm

    # 7 x 1.6 mm TH pads at 2.54 pitch — channel 0.94 mm: the wire
    # threads it nearly straight (finding 4: ~10 mm, not a wrap around
    # the row ends) and the channel edges carry a capacity record
    d = _pad_row_design(2.54, 1.6)
    mesh = build_mesh(d, d.pose0)
    w = build_wires(mesh, d, 2)
    assert not w.unreachable
    (b,) = [b for b in w.branches if b.net == 0]
    assert b.length_mm == pytest.approx(10.0, rel=0.05)
    gaps = [e for e in b.edges if w.gap_free_mm[e].max() >= 0.0]
    assert gaps and all(w.gap_cap[e].max() >= 1 for e in gaps)

    # Split TH parts, channels 0.25 and 0.30 mm < 0.45 needed: the
    # courtyard corners in the channel used to defeat the corner-to-
    # corner test — now the edges are covered and the wire detours:
    # x=0 is covered out to the grown row top, so the path crosses the
    # row's axis only beyond it.
    for gap in (0.25, 0.30):
        pitch = 0.5 + gap
        d2 = _pad_row_design(pitch, 0.5, split=True)
        w2 = build_wires(build_mesh(d2, d2.pose0), d2, 2)
        assert not w2.unreachable
        (b2,) = [bb for bb in w2.branches if bb.net == 0]
        row_top = 3 * pitch + 0.25
        assert _crossing_ys(b2)
        assert all(abs(y) > row_top - 0.01 for y in _crossing_ys(b2))
    # the same parts at a 0.5 mm channel DO thread — and the channel
    # record counts the one trace that fits (round-5 frame fix: the
    # (f + c)/(t + c) form read a 0.05 mm free stretch as cap 0)
    d3 = _pad_row_design(1.0, 0.5, split=True)
    mesh3 = build_mesh(d3, d3.pose0)
    ctx3 = _make_ctx(mesh3, d3, 2)
    w3 = build_wires(mesh3, d3, 2)
    assert not w3.unreachable
    (b3,) = [bb for bb in w3.branches if bb.net == 0]
    assert b3.length_mm < 11.0
    gaps3 = [e for e in b3.edges if w3.gap_free_mm[e].max() >= 0.0]
    assert gaps3 and all(w3.gap_cap[e].max() >= 1 for e in gaps3)
    _assert_routing_rule(ctx3, d3, w3, 2)

    # 0.5 mm-pitch SMT row of 0.3 mm pads (0.2 mm channel): top layer
    # closed, bottom left open (finding 3). N forced onto top detours;
    # free N ducks under the same geometry.
    d4 = _pad_row_design(0.5, 0.3, n_row=7, side=0, n_force=0)
    w4 = build_wires(build_mesh(d4, d4.pose0), d4, 2)
    (b4,) = [bb for bb in w4.branches if bb.net == 0]
    assert _crossing_ys(b4)
    assert all(abs(y) > 3 * 0.5 + 0.15 - 0.01 for y in _crossing_ys(b4))
    d5 = _pad_row_design(0.5, 0.3, n_row=7, side=0, n_force=-1)
    ctx5 = _make_ctx(build_mesh(d5, d5.pose0), d5, 2)
    blocked5, free5, _ep, _pe = _pad_eval(ctx5, r_def)
    assert (blocked5 & 1).any()  # some channel edge closed on top
    assert not (blocked5 & 2).any()  # none closed on bottom
    assert (free5[:, 0] < free5[:, 1]).any()


def _channel_wall_design(n_nets: int = 3, drop: tuple[int, ...] = ()) -> TopoDesign:
    """A TH pad wall at x=0 spanning the board: every gap closed except
    one 0.6 mm channel (cap 1 per outer layer → 2 total). ``n_nets``
    two-pin nets must all cross x=0 — demand over capacity. ``drop``
    removes pads by index (after the channel-opening shift) to open a
    wider single channel — e.g. ``drop=(9, 10, 11)`` leaves a 3.4 mm
    gap between the pads at y=-1.6 and y=+2.4."""
    sq = lambda hx, hy: np.asarray(
        [[-hx, -hy], [hx, -hy], [hx, hy], [-hx, hy], [-hx, -hy]], float
    )
    padw = 0.6
    # pitch 1.0 → 0.4 gaps (closed, < 0.45); the pad at y=0.4 shifted to
    # 0.6 opens one 0.6 gap between -0.6 and +0.6. Row ends reach the
    # outline (0.1 residual, closed) so the channel is the ONLY way.
    ys = [-9.6 + i * 1.0 for i in range(20)]
    ys[10] = 0.6  # the y=0.4 pad shifts to 0.6, opening the 0.6 gap
    ys = [y for i, y in enumerate(ys) if i not in drop]
    n_wall = len(ys)
    parts = tuple(f"{c}{i}" for i in range(n_nets) for c in "AB") + tuple(
        f"H{i}" for i in range(n_wall)
    )
    n_pins = 2 * n_nets + n_wall
    pin_xy = [[0.0, 0.0]] * n_pins
    pin_part = list(range(n_pins))
    pose = [
        [
            (-5.0 if j % 2 == 0 else 5.0),
            -2.0 + 2.0 * (j // 2),
            0,
            0,
        ]
        for j in range(2 * n_nets)
    ]
    pose += [[0.0, y, 0, 0] for y in ys]
    nets = [j // 2 for j in range(2 * n_nets)] + [n_nets + i for i in range(n_wall)]
    return TopoDesign(
        parts=parts,
        pin_xy=np.asarray(pin_xy, float),
        pin_part=np.asarray(pin_part),
        pin_side=np.asarray([2] * n_pins, np.int8),
        pin_label=np.asarray(["1"] * n_pins, dtype=object),
        pin_net=np.asarray(nets, np.int32),
        net_pins=tuple(np.asarray([2 * i, 2 * i + 1]) for i in range(n_nets))
        + tuple(np.asarray([2 * n_nets + i]) for i in range(n_wall)),
        flex=(),
        swap_groups=(),
        fixed=np.zeros(len(parts), bool),
        freedoms=np.full(len(parts), 3, np.uint8),
        outline=np.asarray([[-10, -10], [10, -10], [10, 10], [-10, 10]], float),
        pose0=np.asarray(pose, float),
        part_outline=(sq(0.4, 0.4),) * (2 * n_nets)
        + tuple(sq(padw / 2 + 0.02, padw / 2 + 0.02) for _ in range(n_wall)),
        net_layer_force=np.full(n_nets + n_wall, -1, np.int8),
        net_name=tuple(f"N{i}" for i in range(n_nets))
        + tuple(f"W{i}" for i in range(n_wall)),
        pin_wh=np.tile([[padw, padw]], (n_pins, 1)),
    )


def test_corridor_overflow_recorded_and_refused() -> None:
    """gr520904 S6: three nets through a channel that takes two — the
    demand-vs-capacity cut records the edge with its bounding owners,
    and ``route_design`` refuses ``corridor overflow`` instead of
    cramming copper."""
    from precis.pcb.topoplace.mesh import build_mesh
    from precis.pcb.topoplace.wires import CorridorOverflow, build_wires

    d = _channel_wall_design(3)
    mesh = build_mesh(d, d.pose0)
    w = build_wires(mesh, d, 2)
    assert not w.unreachable
    assert w.overflow, "three wires through a 2-slot channel must cut"
    cut = max(w.overflow, key=lambda c: c.demand - c.cap)
    assert cut.demand == pytest.approx(3.0)
    assert cut.cap == pytest.approx(2.0)
    # the channel's walls are the two H parts bounding the 0.6 mm gap
    assert len(cut.owners) == 2 and all(o.startswith("H") for o in cut.owners)

    from types import SimpleNamespace

    from precis.pcb.topoplace.realise import route_design

    with pytest.raises(CorridorOverflow) as excinfo:
        route_design(
            d,
            SimpleNamespace(
                n_layers=2, net_name=d.net_name, net_current_a=[0.001] * len(d.net_name)
            ),
        )
    msg = str(excinfo.value)
    assert "corridor overflow" in msg
    assert all(o in msg for o in cut.owners)
    assert "next:" in msg


def test_corridor_cut_names_its_demanding_nets() -> None:
    """gr520904 S6 follow-up: a cut carries the NAMES of the nets whose
    routed demand overflows it, and the refusal's message lists them —
    capped at 3 edges x 6 nets with ``+N more`` — so the failure points
    at the nets to reroute, not just an edge index."""
    from precis.pcb.topoplace.mesh import build_mesh
    from precis.pcb.topoplace.wires import CorridorOverflow, build_wires

    d = _channel_wall_design(6)
    w = build_wires(build_mesh(d, d.pose0), d, 2)
    assert w.overflow
    cut = max(w.overflow, key=lambda c: c.demand - c.cap)
    assert cut.demand == pytest.approx(3.0)
    assert cut.nets == ("N0", "N1", "N2")

    msg = str(CorridorOverflow(w.overflow))
    assert f"at ({cut.x:.2f}, {cut.y:.2f})" in msg
    assert "nets N0 N1 N2" in msg


def test_corridor_overflow_message_caps_edges_and_nets() -> None:
    """The refusal shows the worst three edges, six net names each, and
    counts the rest — the format a real refusal on a dense board
    (nano: 'edge at (38.70, 37.89) between J_N1, demand 6 > cap 4, nets
    D10 D7 D8 D9 SW9') must land in."""
    from precis.pcb.topoplace.wires import CorridorCut, CorridorOverflow

    cuts = tuple(
        CorridorCut(
            edge=i,
            demand=10.0 - i,
            cap=2.0,
            owners=(f"J{i}",),
            nets=tuple(f"N{i}{c}" for c in "abcdefg"),
            x=float(i),
            y=0.5,
        )
        for i in range(5)
    )
    msg = str(CorridorOverflow(cuts))
    assert "edge at (0.00, 0.50) between J0, demand 10 > cap 2" in msg
    assert "edge at (1.00, 0.50) between J1, demand 9 > cap 2" in msg
    assert "edge at (2.00, 0.50) between J2, demand 8 > cap 2" in msg
    assert "nets N0a N0b N0c N0d N0e N0f +1 more" in msg
    # worst-3 cap: edges 3 and 4 are counted, not described
    assert "J3" not in msg and "J4" not in msg
    assert "+2 more edge(s)" in msg


def test_power_net_routes_at_routing_width_not_widen_target() -> None:
    """gr520904 follow-up (Reto's widen ruling): a rail's
    ``est_current_a`` resolves to an IPC-2221 width (~2.8 mm at 5 A) —
    that is the post-route ``widen_power`` TARGET, never a routing
    demand. ``route_net_rules`` drops the current-derived bump so the
    corridor cut and the A* occupancy price the width the track is BORN
    at: a channel that fits the routing width does not refuse the rail,
    and its copper is emitted thin."""
    from types import SimpleNamespace

    from precis.pcb.capabilities import capability_for
    from precis.pcb.session import clearance_net_rules, route_net_rules
    from precis.pcb.topoplace.realise import route_design

    caps = capability_for("4layer")
    # three rails across the wall's one 3.4 mm channel: a point survives
    # the routing rule's growth (0.45 mm need) but not the fat target's
    # (~3.1 mm), and under the fat map the wall-adjacent corridor band
    # closes entirely — the nets are stranded, not merely detoured
    d = _channel_wall_design(3, drop=(9, 10, 11))
    design = {
        "nets": [
            {"name": f"N{i}", "net_class": "power", "est_current_a": 5.0}
            for i in range(3)
        ],
        "net_classes": {},
    }
    full = clearance_net_rules(design, caps)
    assert full["N0"].track_width_mm > 2.0
    route_rules = route_net_rules(design, caps)
    route_w = route_rules["N0"].track_width_mm
    assert route_w + 2.0 * route_rules["N0"].clearance_mm <= 0.6
    assert route_w < full["N0"].track_width_mm
    # an authored class width is a rule width, not a widen target — it
    # stands in the route map
    assert route_net_rules(
        {
            "nets": [{"name": "N0", "net_class": "power", "est_current_a": 5.0}],
            "net_classes": {"power": {"track_width_mm": 0.4}},
        },
        caps,
    )["N0"].track_width_mm == pytest.approx(0.4)

    ir = SimpleNamespace(
        n_layers=2, net_name=list(d.net_name), net_current_a=[5.0] * len(d.net_name)
    )
    # control — the full-resolution map still prices the widen target as
    # a routing demand and the channel refuses: three rails at ~9.7
    # finest-rule slots each against the corridor's 20
    from precis.pcb.topoplace.wires import CorridorOverflow

    with pytest.raises(CorridorOverflow):
        route_design(d, ir, net_rules=full)

    plan, res = route_design(d, ir, net_rules=route_rules)
    assert {p.net for p in plan.pieces} >= {0, 1, 2}
    assert all(t.width_mm == pytest.approx(route_w) for t in res.tracks)


def _slot_wall_design() -> TopoDesign:
    """Two parallel TH pad rows at x=±0.6 forming a 0.6 mm slot, walls
    open only below their bottom end (y<-3.9) and closed to the top
    outline. The near wall's only gap is at y~-3.0, the far wall's at
    y~-1.0 — crossing x=0 through the slot takes two cap-1 channels;
    ducking the open bottom end is ~2.6 mm longer. Two nets cross: with
    congestion pricing the second must detour, not pile in."""
    sq = lambda hx, hy: np.asarray(
        [[-hx, -hy], [hx, -hy], [hx, hy], [-hx, hy], [-hx, hy]], float
    )
    padw = 0.6
    ys = [-3.6 + i * 1.0 for i in range(14)]  # -3.6 .. 9.6, top residual 0.1
    # shift one pad +0.2 per row: the gap below it opens 0.4 -> 0.6
    wall = [
        (x, y + 0.2 if i == ci else y)
        for x, ci in ((-0.6, 1), (0.6, 3))
        for i, y in enumerate(ys)
    ]
    n_wall = len(wall)
    parts = ("A0", "B0", "A1", "B1") + tuple(f"H{i}" for i in range(n_wall))
    n_pins = 4 + n_wall
    pose = [
        [-5.0, -2.9, 0, 0],
        [5.0, -2.9, 0, 0],
        [-5.0, -1.4, 0, 0],
        [5.0, -1.4, 0, 0],
    ]
    pose += [[x, y, 0, 0] for x, y in wall]
    nets = [0, 0, 1, 1] + [2 + i for i in range(n_wall)]
    return TopoDesign(
        parts=parts,
        pin_xy=np.asarray([[0.0, 0.0]] * n_pins),
        pin_part=np.asarray(list(range(n_pins))),
        pin_side=np.asarray([2] * n_pins, np.int8),
        pin_label=np.asarray(["1"] * n_pins, dtype=object),
        pin_net=np.asarray(nets, np.int32),
        net_pins=(np.asarray([0, 1]), np.asarray([2, 3]))
        + tuple(np.asarray([4 + i]) for i in range(n_wall)),
        flex=(),
        swap_groups=(),
        fixed=np.zeros(len(parts), bool),
        freedoms=np.full(len(parts), 3, np.uint8),
        outline=np.asarray([[-10, -10], [10, -10], [10, 10], [-10, 10]], float),
        pose0=np.asarray(pose, float),
        part_outline=(sq(0.4, 0.4),) * 4
        + tuple(sq(padw / 2 + 0.02, padw / 2 + 0.02) for _ in range(n_wall)),
        net_layer_force=np.full(2 + n_wall, -1, np.int8),
        net_name=("N0", "N1") + tuple(f"W{i}" for i in range(n_wall)),
        pin_wh=np.tile([[padw, padw]], (n_pins, 1)),
    )


def test_congestion_detours_before_refusing() -> None:
    """gr520904 S6: two nets must cross the slot — its two cap-1
    channels take one wire each, so the occupancy-priced A* sends the
    second net around the walls' open bottom end instead of cramming a
    second wire through or refusing. Without the congestion term both
    nets take the channels and every channel edge cuts at demand 2."""
    from precis.pcb.topoplace.mesh import build_mesh
    from precis.pcb.topoplace.wires import build_wires

    d = _slot_wall_design()
    mesh = build_mesh(d, d.pose0)
    w = build_wires(mesh, d, 1)
    assert not w.unreachable
    assert not w.overflow
    by_net = {int(b.net): [p for p in b.points] for b in w.branches}
    assert 0 in by_net and 1 in by_net
    # N0's straight line at y=-2.9 sits on the near wall's channel: it
    # tunnels the slot (never dips below the channel mouth at -3.3)
    assert min(p[1] for p in by_net[0]) > -3.0
    # N1's channel would be the same corridor — occupied, it detours
    # under the walls' bottom end (pad edge -3.9) instead
    assert min(p[1] for p in by_net[1]) < -3.7


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


def _coincident_run_braids(pa, pb) -> bool:
    """Two taut polylines sharing a coincident stretch swap sides
    through it — the only way an order flip across a shared run is a
    real braid. The legs at the run's two ends sort into lens-boundary
    order (descending angle at pa's start end, ascending at the
    other); an alternating tag sequence means the pair braided."""
    import math

    n_a, n_b = len(pa), len(pb)
    cands: set[tuple[int, int, int, int]] = set()
    for j0 in range(n_a - 1):
        for i0 in range(n_b):
            for step in (1, -1):
                i1 = i0 + step
                if not (0 <= i1 < n_b):
                    continue
                if (
                    math.dist(pa[j0], pb[i0]) > 1e-9
                    or math.dist(pa[j0 + 1], pb[i1]) > 1e-9
                ):
                    continue
                j1, k = j0 + 1, i1
                while (
                    j1 + 1 < n_a
                    and 0 <= k + step < n_b
                    and math.dist(pa[j1 + 1], pb[k + step]) <= 1e-9
                ):
                    j1 += 1
                    k += step
                cands.add((j0, j1, min(i0, k), max(i0, k)))
    for j_lo, j_hi, i_lo, i_hi in cands:
        if any(
            c != (j_lo, j_hi, i_lo, i_hi) and j_lo >= c[0] and j_hi <= c[1]
            for c in cands
        ):
            continue  # sub-run of a longer coincident stretch
        fwd = math.dist(pa[j_lo], pb[i_lo]) <= 1e-9
        ix, iy = (i_lo, i_hi) if fwd else (i_hi, i_lo)
        bx, by = (ix - 1, iy + 1) if fwd else (ix + 1, iy - 1)
        legs_x: list[tuple[float, str]] = []
        if j_lo > 0:
            v = np.asarray(pa[j_lo - 1]) - np.asarray(pa[j_lo])
            legs_x.append((math.atan2(float(v[1]), float(v[0])), "a"))
        if 0 <= bx < n_b:
            v = np.asarray(pb[bx]) - np.asarray(pb[ix])
            legs_x.append((math.atan2(float(v[1]), float(v[0])), "b"))
        legs_y: list[tuple[float, str]] = []
        if j_hi + 1 < n_a:
            v = np.asarray(pa[j_hi + 1]) - np.asarray(pa[j_hi])
            legs_y.append((math.atan2(float(v[1]), float(v[0])), "a"))
        if 0 <= by < n_b:
            v = np.asarray(pb[by]) - np.asarray(pb[iy])
            legs_y.append((math.atan2(float(v[1]), float(v[0])), "b"))
        if len(legs_x) != 2 or len(legs_y) != 2:
            continue  # a strand ends at the run end: inconclusive

        def same_ray(x: float, y: float) -> bool:
            dd = abs(x - y)
            return dd <= 1e-6 or abs(dd - 2.0 * math.pi) <= 1e-6

        if same_ray(legs_x[0][0], legs_x[1][0]) or same_ray(legs_y[0][0], legs_y[1][0]):
            continue  # external legs coincide: inconclusive
        seq = [t for _a, t in sorted(legs_x, key=lambda o: -o[0])] + [
            t for _a, t in sorted(legs_y, key=lambda o: o[0])
        ]
        if seq in (["a", "b", "a", "b"], ["b", "a", "b", "a"]):
            return True
    return False


def test_coincident_run_braid_check() -> None:
    """A flip across a shared stretch is a real crossing only when the
    strands swap sides through it — the guard 2c's parity input relies
    on (round-5: the coincident run used to excuse every flip)."""
    x, y = (0.0, 0.0), (1.0, 0.0)
    # braid: a enters below, exits above; b the mirror
    assert _coincident_run_braids(
        [(0.0, -1.0), x, y, (2.0, 1.0)], [(0.0, 1.0), x, y, (2.0, -1.0)]
    )
    # no braid: each strand keeps its side
    assert not _coincident_run_braids(
        [(0.0, -1.0), x, y, (2.0, -1.0)], [(0.0, 1.0), x, y, (2.0, 1.0)]
    )
    # braid on the SAME side: the outer strand swaps at the far end
    assert _coincident_run_braids(
        [(0.0, -1.0), x, y, (2.0, -2.0)], [(-1.0, -1.0), x, y, (3.0, -1.0)]
    )
    # no braid: both strands leave on the side they entered
    assert not _coincident_run_braids(
        [(0.0, -1.0), x, y, (2.0, 1.0)], [(1.0, -1.0), x, y, (1.0, 1.0)]
    )
    # antiparallel traversal is geometry the same way: kept side, no
    # braid
    assert not _coincident_run_braids(
        [(0.0, -1.0), x, y, (2.0, -1.0)], [(3.0, 1.0), y, x, (0.0, 1.0)]
    )


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
    d, _inst, _nets = synth_design(seed=0, n_parts=40, pad_mm=0.5)
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
        import math

        pa, pb = ba.points, bb.points
        if any(
            segments_cross(pa[i], pa[i + 1], pb[j], pb[j + 1])
            for i in range(len(pa) - 1)
            for j in range(len(pb) - 1)
        ):
            return True
        # a coincident shared stretch collapses both strands onto one
        # line: an order flip across it is a real braid ONLY when the
        # strands enter and leave on exchanged sides — the same ray
        # alternation the shared-bend case uses, applied to the run's
        # entry and exit legs (round-5: a blanket True excused every
        # flip on a shared stretch)
        if _coincident_run_braids(pa, pb):
            return True
        # a shared bend vertex is a real crossing when the four rays
        # alternate cyclically around it (an X touch segments_cross
        # cannot see); coincident rays merge first — wires arriving or
        # leaving together touch without piercing

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
    # R3-3: the repair pass's dropped cycles/round cap can leave a tied
    # pair unresolved — surfaced, not silent, so 2c sees how clean the
    # parity input is. Pinned where it bites: the standard ringless
    # n=100 seed-0 fixture, where the all-portal step metric read 42
    # and the round-5 hybrid (portal on pad-ring tris only) reads 1.
    assert w.order_residual == 0
    d100, _i100, _n100 = synth_design(seed=0, n_parts=100)
    w100 = build_wires(build_mesh(d100, d100.pose0), d100, 4)
    assert w100.order_residual <= 6


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
    floor at 100 parts — the old-form assert is back (round-5 ruling):
    wall time of the wire build only, ringless synth n=100 seed 0; the
    rep takes the min of two wall samples — under parallel load the
    least-loaded sample is the honest one.

    The padded/ringless CPU ratio stays on as a second guard for
    ring/gap-path regressions, measured as INTERLEAVED pairs: a gate
    container's load inflates both sides of a pair in the same moment,
    so the pairwise ratio cancels it and the min picks the least-loaded
    pair. The old min-per-side let one side's reps all land in a burst —
    4.7x against the 4.0x budget under parallel load (gr537677).
    """
    import time

    from precis.pcb.topoplace.wires import build_wires

    d, _inst, _nets = synth_design(seed=0, n_parts=100)
    mesh = build_mesh(d, d.pose0)

    def wire_wall() -> float:
        t0 = time.monotonic()
        build_wires(mesh, d, 4)
        return time.monotonic() - t0

    w = build_wires(mesh, d, 4)
    dt = min(wire_wall(), wire_wall())
    rate = 1.0 / dt
    print(
        f"\nwires: {len(w.branches)} branches in {dt * 1e3:.0f} ms -> {rate:.1f} evals/s"
    )
    assert rate >= 1.0

    def timed(pad_mm: float) -> float:
        dd, _i2, _n2 = synth_design(seed=0, n_parts=100, pad_mm=pad_mm)
        t0c = time.process_time()
        build_wires(build_mesh(dd, dd.pose0), dd, 4)
        return time.process_time() - t0c

    ringless: list[float] = []
    padded: list[float] = []
    for _ in range(3):
        ringless.append(timed(0.0))
        padded.append(timed(0.5))
    ratios = [p / r for r, p in zip(ringless, padded, strict=True)]
    print(
        f"mesh+wires: ringless {min(ringless) * 1e3:.0f} ms cpu, "
        f"padded {min(padded) * 1e3:.0f} ms cpu, "
        f"pairwise {', '.join(f'{r:.1f}x' for r in ratios)} "
        f"(min {min(ratios):.1f}x)"
    )
    assert min(ratios) <= 4.0


def test_build_wires_deterministic() -> None:
    from precis.pcb.topoplace.wires import build_wires

    d, _inst, _nets = synth_design(seed=3, pad_mm=0.5)
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
    for part in (j1, j2, j3, j4, j5, j6):
        for num in ("1", "2"):
            part[num].etype("bidi", fmax_hz=0.0, assume="connector pin")
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
    # gr529963: the zero-clearance pad-union bodies ride alongside —
    # same ring count, strictly smaller than the courtyard (no 0.4mm
    # offset).
    assert len(td.part_body) == len(td.parts)
    assert all(len(b) >= 4 for b in td.part_body)
    for ring, body in zip(td.part_outline, td.part_body):
        assert np.ptp(body[:, 0]) < np.ptp(ring[:, 0])
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
