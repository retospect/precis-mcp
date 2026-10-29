"""Slice-5 route proxies: the via estimate, fold-back mileage and the
direction-mix penalty — exact math on tiny hand-built designs, the
power-class exemption, and the flow preset's shape."""

from __future__ import annotations

import numpy as np
import pytest

from precis.pcb.topoplace import (
    PRESETS,
    ObjectiveShares,
    TopoDesign,
    backtrack_total,
    direction_total,
    via_estimate_total,
)
from precis.pcb.topoplace.design import FREEDOM_BACK_SIDE, FREEDOM_ROTATE


def _design(
    parts: list[str],
    pins: list[tuple[int, float, float, int]],  # (part, x, y, net)
    pin_side: list[int],
    pose0: list[tuple[float, float, float, float]],
    *,
    net_exempt: tuple[bool, ...] = (),
    anchors: tuple[tuple[float, float], ...] = (),
    net_anchors: tuple[tuple[int, ...], ...] = (),
    part_rings: bool = False,
) -> TopoDesign:
    """A tiny placed design: one MST edge per 2-pin net, outline 40 mm
    square, everything movable. ``part_rings`` gives every part a 10×6
    mm closed courtyard square around its origin."""
    nets: dict[int, list[int]] = {}
    for pid, (_part, _x, _y, net) in enumerate(pins):
        if net >= 0:
            nets.setdefault(net, []).append(pid)
    n_nets = len(nets)
    square = np.array(
        [[-5.0, -3.0], [5.0, -3.0], [5.0, 3.0], [-5.0, 3.0], [-5.0, -3.0]],
        dtype=np.float64,
    )
    return TopoDesign(
        parts=tuple(parts),
        pin_xy=np.array([(x, y) for _p, x, y, _n in pins], dtype=np.float64).reshape(
            -1, 2
        ),
        pin_part=np.array([p for p, *_ in pins], dtype=np.int64),
        pin_side=np.array(pin_side, dtype=np.int8),
        pin_label=np.array([f"P{i}" for i in range(len(pins))], dtype=object),
        pin_net=np.array([n for *_r, n in pins], dtype=np.int32),
        net_pins=tuple(np.array(v, dtype=np.int64) for v in nets.values()),
        flex=(),
        swap_groups=(),
        fixed=np.zeros(len(parts), dtype=bool),
        freedoms=np.full(
            len(parts), FREEDOM_ROTATE | FREEDOM_BACK_SIDE, dtype=np.uint8
        ),
        outline=np.array(
            [[0.0, 0.0], [40.0, 0.0], [40.0, 40.0], [0.0, 40.0]], dtype=np.float64
        ),
        pose0=np.array(pose0, dtype=np.float64),
        net_direction_exempt=(
            np.array(net_exempt, dtype=bool)
            if net_exempt
            else np.zeros(n_nets, dtype=bool)
        ),
        anchor_xy=np.array(anchors, dtype=np.float64).reshape(-1, 2),
        net_anchors=tuple(np.array(a, dtype=np.int64) for a in net_anchors),
        part_outline=tuple(square.copy() for _ in parts) if part_rings else (),
    )


# -- the via estimate -------------------------------------------------------


def test_via_estimate_single_side_zero() -> None:
    """Two pins on the same face: no layer change, estimate 0."""
    d = _design(
        ["A", "B"],
        [(0, 0.0, 0.0, 0), (1, 10.0, 0.0, 0)],
        [0, 0],
        [(0.0, 0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 0.0)],
    )
    assert via_estimate_total(d, d.pose0) == 0


def test_via_estimate_two_sides_one() -> None:
    """One pin per face: exactly one layer change on a 2-layer board."""
    d = _design(
        ["A", "B"],
        [(0, 0.0, 0.0, 0), (1, 10.0, 0.0, 0)],
        [0, 1],
        [(0.0, 0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 0.0)],
    )
    assert via_estimate_total(d, d.pose0) == 1


def test_via_estimate_th_pin_wildcard() -> None:
    """A TH pin forces nothing — the plated barrel exists on every layer.
    SMD-top + TH → 0; adding a bottom SMD → 1."""
    d = _design(
        ["A", "B", "C"],
        [(0, 0.0, 0.0, 0), (1, 10.0, 0.0, 0), (2, 20.0, 0.0, 0)],
        [0, 2, 1],
        [(0.0, 0.0, 0.0, 0.0)] * 3,
    )
    assert via_estimate_total(d, d.pose0) == 1
    d2 = _design(
        ["A", "B"],
        [(0, 0.0, 0.0, 0), (1, 10.0, 0.0, 0)],
        [0, 2],
        [(0.0, 0.0, 0.0, 0.0)] * 2,
    )
    assert via_estimate_total(d2, d2.pose0) == 0


def test_via_estimate_flip_toggles_side() -> None:
    """A flip since the seed pose toggles the pin's physical side — the
    estimate follows the CURRENT pose, not the build side."""
    d = _design(
        ["A", "B"],
        [(0, 0.0, 0.0, 0), (1, 10.0, 0.0, 0)],
        [0, 0],
        [(0.0, 0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 0.0)],
    )
    assert via_estimate_total(d, d.pose0) == 0
    flipped = d.pose0.copy()
    flipped[1, 3] = 1.0  # B to the bottom face
    assert via_estimate_total(d, flipped) == 1


# -- backtrack mileage ------------------------------------------------------


def test_backtrack_zero_on_monotone_routes() -> None:
    """An L-shaped tree never doubles back: 0."""
    d = _design(
        ["A", "B"],
        [(0, 0.0, 0.0, 0), (0, 10.0, 0.0, 0), (1, 10.0, 5.0, 0)],
        [0, 0, 0],
        [(0.0, 0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 0.0)],
    )
    assert backtrack_total(d, d.pose0) == 0.0


def test_backtrack_zigzag_priced_exactly() -> None:
    """Pins (0,0),(5,3),(10,0): the MST runs 6 mm in y over a 3 mm
    extent — exactly the 3 mm doubled back."""
    d = _design(
        ["A", "B"],
        [(0, 0.0, 0.0, 0), (1, 5.0, 3.0, 0), (0, 10.0, 0.0, 0)],
        [0, 0, 0],
        [(0.0, 0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 0.0)],
    )
    assert backtrack_total(d, d.pose0) == 3.0


def test_backtrack_includes_fixed_via_anchors() -> None:
    """An authored fixed via anchors the tree: the anchor's extent counts
    — a via dropped between fold-back pins pays the same mileage."""
    d = _design(
        ["A", "B"],
        [(0, 0.0, 0.0, 0), (0, 10.0, 0.0, 0)],
        [0, 0],
        [(0.0, 0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 0.0)],
        anchors=((5.0, -3.0),),
        net_anchors=((0,),),
    )
    assert backtrack_total(d, d.pose0) == 3.0


# -- direction mix ----------------------------------------------------------


def _cross_pair_design(net_exempt: tuple[bool, ...] = ()) -> TopoDesign:
    """Net 0 runs east-west across the middle; net 1 runs north-south
    through the same midpoint — the maximally-perpendicular close pair."""
    return _design(
        ["A", "B"],
        [(0, 0.0, 0.0, 0), (0, 10.0, 0.0, 0), (1, 0.0, -2.0, 1), (1, 0.0, 2.0, 1)],
        [0, 0, 0, 0],
        [(0.0, 0.0, 0.0, 0.0), (5.0, 0.0, 0.0, 0.0)],
        net_exempt=net_exempt,
    )


def test_direction_zero_length_wires_never_score() -> None:
    """A zero-length wire (coincident same-net pins, or an authored
    anchor landing exactly on a pin) has no direction — its unit vector
    is (0,0), which the cosine scores as perpendicular to EVERYTHING, a
    phantom direction-mix debt (fable-review nit). Masked to 0; both
    the reference path and the staged deltas share ``_dir_matrix`` so
    the parity contract is unaffected."""
    from precis.pcb.topoplace.proxy import _dir_matrix

    zero = np.array([[0.0, 0.0, 0.0, 0.0]])  # zero-length wire
    flat = np.array([[1.0, 0.0, 2.0, 0.0]])  # horizontal wire
    up = np.array([[0.0, 0.0, 0.0, 1.0]])  # vertical wire
    nets2 = np.array([0, 1])
    ex2 = np.zeros(2, dtype=bool)
    m = _dir_matrix(zero, nets2, flat, nets2, ex2, scale=10.0)
    assert m[0, 0] == 0.0
    m2 = _dir_matrix(
        flat,
        np.array([0]),
        up,
        np.array([1]),
        np.zeros(2, dtype=bool),
        scale=10.0,
    )
    assert 0.98 < m2[0, 0] <= 1.0  # a real perpendicular pair still scores


def test_direction_perpendicular_pair_scores() -> None:
    d = _cross_pair_design()
    # midpoints coincide (d = 0), headings differ by 90°: (1−|cos|)·1 = 1.
    assert direction_total(d, d.pose0, scale=5.0) == 1.0


def test_direction_parallel_pairs_free() -> None:
    """Same compass heading: cos = 1 → the pair scores nothing even at
    the same proximity."""
    d = _design(
        ["A", "B"],
        [(0, 0.0, 0.0, 0), (0, 10.0, 0.0, 0), (1, 0.0, 3.0, 1), (1, 10.0, 3.0, 1)],
        [0, 0, 0, 0],
        [(0.0, 0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 0.0)],
    )
    assert direction_total(d, d.pose0, scale=5.0) == 0.0


def test_direction_same_net_and_exempt_never_score() -> None:
    d = _design(
        ["A", "B"],
        [(0, 0.0, 0.0, 0), (0, 10.0, 0.0, 0), (1, 0.0, -2.0, 0), (1, 0.0, 2.0, 0)],
        [0, 0, 0, 0],
        [(0.0, 0.0, 0.0, 0.0), (5.0, 0.0, 0.0, 0.0)],
    )
    assert direction_total(d, d.pose0, scale=5.0) == 0.0  # same net
    d_ex = _cross_pair_design(net_exempt=(False, True))
    assert direction_total(d_ex, d_ex.pose0, scale=5.0) == 0.0  # power rule


def test_direction_decays_with_distance() -> None:
    d = _design(
        ["A", "B"],
        [(0, 0.0, 0.0, 0), (0, 10.0, 0.0, 0), (1, 0.0, 28.0, 1), (1, 0.0, 32.0, 1)],
        [0, 0, 0, 0],
        [(0.0, 0.0, 0.0, 0.0), (5.0, 0.0, 0.0, 0.0)],
    )
    # midpoints 30 mm apart, scale 5: exp(−900/50) ≈ 1.5e-8.
    assert direction_total(d, d.pose0, scale=5.0) == pytest.approx(0.0, abs=1e-6)


def test_direction_proxy_state_tracks_total() -> None:
    """The ProxyState property agrees with the full recompute at init and
    after committed moves (the same parity contract as the spread term)."""
    from precis.pcb.topoplace import ProxyState

    d = _cross_pair_design()
    w = ObjectiveShares(
        crossings=0.5, ratsnest=0.2, vias=0.1, backtrack=0.1, direction=0.1
    )
    proxy = ProxyState(d, d.pose0, w)
    assert proxy.direction == direction_total(d, d.pose0, scale=proxy._d_scale)
    assert proxy.vias == via_estimate_total(d, d.pose0)
    assert proxy.backtrack == backtrack_total(d, d.pose0)
    row = proxy.pose[1].copy()
    row[0] = 8.0
    proxy.try_move(1, row)
    proxy.commit()
    assert proxy.direction == direction_total(d, proxy.pose, scale=proxy._d_scale)
    assert proxy.backtrack == backtrack_total(d, proxy.pose)
    assert proxy.vias == via_estimate_total(d, proxy.pose)


# -- pad containment (the overlap term's outline half) ---------------------


def _edge_pair_design() -> TopoDesign:
    """Both parts inside the 40 mm outline: nets run (0,0)-(10,0) and
    (20,0)-(30,0)."""
    return _design(
        ["A", "B"],
        [(0, 0.0, 0.0, 0), (0, 10.0, 0.0, 0), (1, 20.0, 0.0, 1), (1, 30.0, 0.0, 1)],
        [0, 0, 0, 0],
        [(0.0, 0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 0.0)],
    )


def test_containment_zero_inside_priced_outside() -> None:
    """Inside the outline: 0. Sliding part B +25 mm puts its pins 5 and
    15 mm past the x=40 edge — the DRC's ``outline_containment``
    distance, priced exactly."""
    from precis.pcb.topoplace.proxy import all_pin_world, pad_containment_total

    d = _edge_pair_design()
    assert pad_containment_total(d, all_pin_world(d, d.pose0)) == 0.0
    out = d.pose0.copy()
    out[1, 0] = 25.0
    assert pad_containment_total(d, all_pin_world(d, out)) == pytest.approx(20.0)


def test_containment_proxy_delta_through_commit() -> None:
    from precis.pcb.topoplace import ProxyState

    d = _edge_pair_design()
    w = ObjectiveShares(crossings=0.5, ratsnest=0.2, overlap=0.3)
    proxy = ProxyState(d, d.pose0, w)
    assert proxy.overlap == 0.0
    row = proxy.pose[1].copy()
    row[0] = 25.0
    proxy.try_move(1, row)
    proxy.commit()
    assert proxy.overlap == pytest.approx(20.0)
    proxy.revert()  # no-op after commit; the state stays committed
    assert proxy.overlap == pytest.approx(20.0)


def test_edge_clearance_skips_edge_mated_parts() -> None:
    """gr530632: an edge-mated part's face is SUPPOSED to sit on the
    outline — the edge half of the overlap term charges it nothing, in
    both the full recompute and the try_move staged delta. A sibling at
    the same standoff still pays, so the term stays live."""
    import dataclasses

    from precis.pcb.topoplace.proxy import ProxyState, edge_clearance_total

    d = _design(
        ["A", "B"],
        [(0, 0.0, 0.0, 0), (0, 2.0, 0.0, 0), (1, 0.0, 0.0, 1), (1, 2.0, 0.0, 1)],
        [0, 0, 0, 0],
        [(10.0, 20.0, 0.0, 0.0), (20.0, 20.0, 0.0, 0.0)],
        part_rings=True,
    )
    near = d.pose0.copy()
    near[1, 0] = 34.8  # B's ring 0.2mm off the x=40 edge — pays 0.4−0.2
    assert edge_clearance_total(d, near) == pytest.approx(0.2)
    d_mate_b = dataclasses.replace(d, part_edge_mate=np.array([False, True]))
    assert edge_clearance_total(d_mate_b, near) == 0.0

    # A moved to the same hot spot while mated: no edge debt appears,
    # through the full recompute AND the staged delta / commit path.
    d_mate_a = dataclasses.replace(d, part_edge_mate=np.array([True, False]))
    hot = near.copy()
    hot[0, 0] = 34.8
    assert edge_clearance_total(d_mate_a, hot) == pytest.approx(0.2)  # only B
    w = ObjectiveShares(crossings=0.5, ratsnest=0.2, overlap=0.3)
    proxy = ProxyState(d_mate_a, d.pose0, w)
    assert proxy.overlap == 0.0
    row = proxy.pose[0].copy()
    row[0] = 34.8  # A's ring 0.2mm off the edge — exempt, so nothing accrues
    proxy.try_move(0, row)
    assert proxy.overlap == 0.0
    proxy.commit()
    assert proxy.overlap == 0.0
    proxy_full = ProxyState(d, d.pose0, w)
    proxy_full.try_move(0, row)
    assert proxy_full.overlap == pytest.approx(0.2)  # the un-exempt twin pays


def test_edge_clearance_priced_against_courtyard() -> None:
    """The DRC's ``courtyard_edge_clearance``: a part ring whose edge
    sits 0.2 mm off the outline pays 0.4 − 0.2 = 0.2 mm; well clear it
    pays nothing."""
    from precis.pcb.topoplace.proxy import ProxyState, edge_clearance_total

    d = _design(
        ["A", "B"],
        [(0, 0.0, 0.0, 0), (0, 10.0, 0.0, 0), (1, 0.0, 0.0, 1), (1, 2.0, 0.0, 1)],
        [0, 0, 0, 0],
        [(10.0, 20.0, 0.0, 0.0), (20.0, 20.0, 0.0, 0.0)],
        part_rings=True,
    )
    # B's 10×6 ring at (34.8, 20) spans x 29.8..39.8: gap 0.2 mm to the
    # x=40 edge, 0.2 mm short of the 0.4 mm floor. At x=33 the gap is
    # 2 mm — clear. Pins stay inside the outline throughout.
    near = d.pose0.copy()
    near[1, 0] = 34.8
    assert edge_clearance_total(d, near) == pytest.approx(0.2)
    clear = d.pose0.copy()
    clear[1, 0] = 33.0
    assert edge_clearance_total(d, clear) == 0.0

    w = ObjectiveShares(crossings=0.5, ratsnest=0.2, overlap=0.3)
    proxy = ProxyState(d, d.pose0, w)
    row = proxy.pose[1].copy()
    row[0] = 34.8
    proxy.try_move(1, row)
    proxy.commit()
    # pins stay inside (contribution 0); the ring pays the 0.2 mm.
    assert proxy.overlap == pytest.approx(0.2)


def test_edge_clearance_priced_against_body() -> None:
    """gr529963 (ruling Reto 2026-10-28): the edge term prices the
    pad-union BODY against _EDGE_CLEAR_MM, not the courtyard — pricing
    the courtyard squeezed placement to an effective 0.8mm body-to-edge
    demand and disagreed with the DRC rule's own measure. A design with
    explicit ``part_body`` rings: the courtyard 0.2mm off the edge pays
    NOTHING while the body clears; a body 0.3mm off pays 0.1, through
    the full recompute AND the staged delta / commit path. A part with
    no body ring (synthetic designs) falls back to its courtyard ring —
    the case the test above pins."""
    import dataclasses

    from precis.pcb.topoplace.proxy import ProxyState, edge_clearance_total

    d = _design(
        ["A", "B"],
        [(0, 0.0, 0.0, 0), (0, 10.0, 0.0, 0), (1, 0.0, 0.0, 1), (1, 2.0, 0.0, 1)],
        [0, 0, 0, 0],
        [(10.0, 20.0, 0.0, 0.0), (20.0, 20.0, 0.0, 0.0)],
        part_rings=True,
    )
    # An 8x4 body inside each 10x6 courtyard (same closure convention).
    body = np.array(
        [[-4.0, -2.0], [4.0, -2.0], [4.0, 2.0], [-4.0, 2.0], [-4.0, -2.0]],
        dtype=np.float64,
    )
    d = dataclasses.replace(d, part_body=(body.copy(), body.copy()))
    # B's courtyard at (34.8, 20) spans x 29.8..39.8 — 0.2mm off the
    # x=40 edge — but its body spans 30.8..38.8: 1.2mm clear. Pays
    # nothing now; priced the 0.2mm shortfall before the ruling.
    near = d.pose0.copy()
    near[1, 0] = 34.8
    assert edge_clearance_total(d, near) == 0.0
    # The same move on the courtyard-fallback design (no part_body)
    # still pays — the fallback keeps synthetic designs on the ring.
    assert edge_clearance_total(
        dataclasses.replace(d, part_body=()), near
    ) == pytest.approx(0.2)
    # A body that crowds the edge: B at x=35.7 -> body spans 31.7..39.7,
    # gap 0.3, shortfall 0.1 — staged delta and commit agree exactly.
    w = ObjectiveShares(crossings=0.5, ratsnest=0.2, overlap=0.3)
    proxy = ProxyState(d, d.pose0, w)
    assert proxy.overlap == 0.0
    row = proxy.pose[1].copy()
    row[0] = 35.7
    proxy.try_move(1, row)
    assert proxy.overlap == pytest.approx(0.1)
    proxy.commit()
    assert proxy.overlap == pytest.approx(edge_clearance_total(d, proxy.pose))


# -- the maximin margin term (Reto 2026-10-29) ------------------------------


def test_margin_edge_part_softmin_by_hand_and_inward_move() -> None:
    """Part A's courtyard parked 0.2 mm off the x=40 board edge pays the
    edge axis ``want(A) − 0.2`` with the want ring-radius-aware
    (:func:`_edge_wants` — a small ring's outline can be wholly inside
    the edge margin and drop, a big ring's cannot); part B's big ring
    wants nothing and its other gaps clear everything. The term is
    exactly the softmin formula over the two worsts, and moving the edge
    part inward past the want takes it to 0 — the staged ProxyState
    tracks both, bit-consistent with the full recompute."""
    import dataclasses
    import math

    from precis.pcb.topoplace import ProxyState, margin_total
    from precis.pcb.topoplace.proxy import (
        _MARGIN_SOFTMIN_TAU_MM,
        _SILK_EDGE_ROOM_MM,
        HOLE_WANT_MM,
        _edge_wants,
    )

    assert HOLE_WANT_MM == 0.0
    d = _design(
        ["A", "B"],
        [(0, 0.0, 0.0, 0), (0, 10.0, 0.0, 0), (1, 20.0, 0.0, 1), (1, 30.0, 0.0, 1)],
        [0, 0, 0, 0],
        [(39.3, 20.0, 0.0, 0.0), (20.0, 20.0, 0.0, 0.0)],
        part_rings=True,
    )
    # A's ring is a 1x1 square (r=sqrt(0.5)) instead of the 10x6 default:
    # the default's ring is bigger than the edge-margin band and wants
    # nothing, which would make this test vacuous.
    small = np.array(
        [[-0.5, -0.5], [0.5, -0.5], [0.5, 0.5], [-0.5, 0.5], [-0.5, -0.5]],
        dtype=np.float64,
    )
    d = dataclasses.replace(d, part_outline=(small, d.part_outline[1]))
    want_a = _SILK_EDGE_ROOM_MM - math.sqrt(0.5)
    assert _edge_wants([None, None])[1] == 0.0  # ringless part wants nothing
    # A's ring spans x 38.8..39.8 -> 0.2 mm off the edge; B's spans
    # 15..25 x 17..23 -> every gap >= 15 mm. worst = (want_a - 0.2, 0).
    tau = _MARGIN_SOFTMIN_TAU_MM
    worst_a = want_a - 0.2
    expected = -tau * math.log((math.exp(-worst_a / tau) + math.exp(0.0)) / 2.0)
    assert margin_total(d, d.pose0) == pytest.approx(expected)

    w = ObjectiveShares(crossings=0.5, ratsnest=0.2, overlap=0.2, margin=0.1)
    proxy = ProxyState(d, d.pose0, w)
    assert proxy.margin == pytest.approx(expected)
    before = proxy.margin
    row = proxy.pose[0].copy()
    row[0] = 33.0  # A's ring 32.5..33.5: 6.5 mm off the edge — the want met
    proxy.try_move(0, row)
    assert proxy.margin < before
    proxy.commit()
    assert proxy.margin == pytest.approx(0.0, abs=1e-12)
    assert proxy.margin == pytest.approx(margin_total(d, proxy.pose))


def test_margin_hole_axis_uses_the_body_not_the_courtyard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gr525380's notch geometry on the margin term's hole axis: a
    mounting-hole keep-out parked inside a part's U-notch clears the
    pad-union BODY, so the axis is 0 even at a positive want — while the
    same hole inside the courtyard ring (the no-body fallback) pays the
    full want. With HOLE_WANT_MM = 0 the axis is inert by design; the
    monkeypatch raises the knob so the test sees the measure."""
    import dataclasses

    from precis.pcb.topoplace import ProxyState, TopoZone, margin_total

    monkeypatch.setattr("precis.pcb.topoplace.proxy.HOLE_WANT_MM", 0.5)
    d = _design(
        ["A"],
        [(0, 0.0, 0.0, 0), (0, 10.0, 0.0, 0)],
        [0, 0],
        [(20.0, 20.0, 0.0, 0.0)],
        part_rings=True,
    )
    # The U body (opening +y): bars at x ±2..±4, a bottom bar at
    # y −4..−2, the notch |x| < 2, y > −2 open. The hole keep-out (a
    # 0.5 mm square at local (0, 2)) sits 1.75 mm clear of both bar
    # walls — inside the notch, clear of the body.
    u_ring = np.array(
        [
            [-4.0, -4.0],
            [4.0, -4.0],
            [4.0, 4.0],
            [2.0, 4.0],
            [2.0, -2.0],
            [-2.0, -2.0],
            [-2.0, 4.0],
            [-4.0, 4.0],
            [-4.0, -4.0],
        ],
        dtype=np.float64,
    )
    hole = np.array(
        [[-0.25, 1.75], [0.25, 1.75], [0.25, 2.25], [-0.25, 2.25], [-0.25, 1.75]],
        dtype=np.float64,
    ) + np.array([20.0, 20.0])  # board-level (world) ring
    d = dataclasses.replace(
        d,
        part_body=(u_ring,),
        zones=(TopoZone(kind="mounting_hole", ring=hole, name="mh"),),
    )
    # Body clears the notch hole: every axis 0, so the softmin is 0.
    assert margin_total(d, d.pose0) == pytest.approx(0.0, abs=1e-12)
    w = ObjectiveShares(crossings=0.5, ratsnest=0.2, margin=0.3)
    proxy = ProxyState(d, d.pose0, w)
    assert proxy.margin == pytest.approx(0.0, abs=1e-12)
    # The no-body fallback (courtyard ring covers the notch) pays the
    # full want — a one-part softmin returns the worst verbatim.
    d_nobody = dataclasses.replace(d, part_body=())
    assert margin_total(d_nobody, d_nobody.pose0) == pytest.approx(0.5)
    # And a small nudge keeps the proxy exactly on the recompute.
    row = proxy.pose[0].copy()
    row[0] += 1.0
    proxy.try_move(0, row)
    proxy.commit()
    assert proxy.margin == pytest.approx(margin_total(d, proxy.pose))


# -- the flow preset --------------------------------------------------------


def test_placer_legality_gate_never_raises_violations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The acceptance gate refuses moves that raise the violation total:
    whatever the crossings temptation, an anneal from an overlapping
    seed ends no worse than it began.

    gr530656: the assertion is on the ANNEAL's best pose, not the
    settled one — shove/legalize mop up behind a broken gate and the
    test would pass anyway. ``_settle_pose`` is stubbed to identity so
    the gate is the only thing between the seed and the result."""
    import precis.pcb.topoplace.placer as placer_mod
    from precis.pcb.topoplace import topo_place
    from precis.pcb.topoplace.legalize import LegalizeResult
    from precis.pcb.topoplace.proxy import overlap_area
    from precis.pcb.topoplace.shove import ShoveResult

    def _no_settle(design, pose):
        return (
            pose,
            ShoveResult(
                pose=pose, resolved=True, rounds=0, moved_mm=0.0, lp_optimum_mm=0.0
            ),
            LegalizeResult(pose=pose, resolved=True, rounds=0, moved_mm=0.0),
        )

    monkeypatch.setattr(placer_mod, "_settle_pose", _no_settle)

    d = _design(
        ["A", "B"],
        [(0, 0.0, 0.0, 0), (0, 10.0, 0.0, 0), (1, 0.0, 0.0, 1), (1, 2.0, 0.0, 1)],
        [0, 0, 0, 0],
        [(10.0, 20.0, 0.0, 0.0), (13.0, 20.0, 0.0, 0.0)],
        part_rings=True,
    )
    seed_ov = overlap_area(d, d.pose0)
    assert seed_ov > 0.0  # stacked courtyards: 7×6 mm
    res = topo_place(
        d,
        objective=ObjectiveShares(crossings=0.8, ratsnest=0.0, overlap=0.2),
        effort=800,
        seed=3,
    )
    assert overlap_area(d, res.best_pose) <= seed_ov + 1e-9


# -- silk room (the outline-pair margin) ------------------------------------


def test_silkroom_shortfall_priced_exactly_and_tracked() -> None:
    """Two same-side courtyard squares (10×6 mm) 0.5 mm apart: the term
    pays ``_SILK_ROOM_REQ_MM − 0.5`` exactly at init, and the ProxyState
    property tracks the full recompute through a try_move/commit that
    spreads the pair past the requirement."""
    from precis.pcb.topoplace import ProxyState
    from precis.pcb.topoplace.proxy import _SILK_ROOM_REQ_MM, _ring_silkroom_total

    assert 0.0 < _SILK_ROOM_REQ_MM < 2.0  # 0.65 under module fallbacks
    d = _design(
        ["A", "B"],
        [(0, 0.0, 0.0, 0), (0, 10.0, 0.0, 0), (1, 20.0, 0.0, 1), (1, 30.0, 0.0, 1)],
        [0, 0, 0, 0],
        [(5.0, 20.0, 0.0, 0.0), (15.5, 20.0, 0.0, 0.0)],  # edges 0.5 mm apart
        part_rings=True,
    )
    w = ObjectiveShares(crossings=0.5, ratsnest=0.2, overlap=0.2, silkroom=0.1)
    proxy = ProxyState(d, d.pose0, w)
    expected = _ring_silkroom_total(proxy._rings, d.pose0, proxy._th, _SILK_ROOM_REQ_MM)
    assert proxy.silkroom == pytest.approx(expected)
    assert proxy.silkroom == pytest.approx(_SILK_ROOM_REQ_MM - 0.5)
    row = proxy.pose[1].copy()
    row[0] = 10.0 + _SILK_ROOM_REQ_MM + 5.0  # gap exactly at requirement
    proxy.try_move(1, row)
    proxy.commit()
    assert proxy.silkroom == pytest.approx(
        _ring_silkroom_total(proxy._rings, proxy.pose, proxy._th, _SILK_ROOM_REQ_MM)
    )
    assert proxy.silkroom == pytest.approx(0.0, abs=1e-9)


def test_silkroom_opposite_sides_exempt() -> None:
    """The same 0.5 mm gap across the board's two faces prices nothing —
    the ``_collides`` rule the overlap term uses."""
    from precis.pcb.topoplace import ProxyState

    d = _design(
        ["A", "B"],
        [(0, 0.0, 0.0, 0), (0, 10.0, 0.0, 0), (1, 20.0, 0.0, 1), (1, 30.0, 0.0, 1)],
        [0, 0, 0, 0],
        [(5.0, 20.0, 0.0, 0.0), (15.5, 20.0, 0.0, 1.0)],  # B on the bottom face
        part_rings=True,
    )
    w = ObjectiveShares(crossings=0.5, ratsnest=0.2, overlap=0.2, silkroom=0.1)
    proxy = ProxyState(d, d.pose0, w)
    assert proxy.silkroom == pytest.approx(0.0, abs=1e-9)


def test_flow_preset_is_crossings_primary() -> None:
    f = PRESETS["flow"]
    assert f.crossings > f.vias
    assert f.crossings > f.ratsnest
    assert f.crossings > f.backtrack
    assert f.crossings > f.direction
    assert f.crossings > f.silkroom
    assert f.crossings > f.margin
    assert (
        f.crossings
        + f.ratsnest
        + f.vias
        + f.backtrack
        + f.direction
        + f.overlap
        + f.spread
        + f.measures
        + f.silkroom
        + f.margin
        == pytest.approx(1.0)
    )
