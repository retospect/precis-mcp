"""Slice 1a: the incremental proxy (parity with a full recompute and
evals/s), the placer (vs ``autoplace`` at equal evals), the archive's
diversity invariant, and seed determinism — spec §1a's own tests."""

from __future__ import annotations

import time
from typing import Any

import numpy as np
import pytest

from precis.pcb.place import autoplace
from precis.pcb.topoplace import (
    ObjectiveShares,
    ProxyState,
    backtrack_total,
    direction_total,
    evaluate,
    placer,
    topo_place,
    via_estimate_total,
)
from precis.pcb.topoplace.archive import mean_displacement, rotations_differ
from precis.pcb.topoplace.legalize import LegalizeResult
from precis.pcb.topoplace.shove import ShoveResult
from tests.fixtures.pcb.topo_example_a import example_a
from tests.fixtures.pcb.topo_synth import synth_design

W = ObjectiveShares()


def test_try_move_commit_matches_full_recompute_1000_moves() -> None:
    """Incremental == full recompute, exactly, over 1000 seeded random
    moves (translate / rotate / flip, commit-or-revert) on the
    100-part synthetic board."""
    d, _inst, _nets = synth_design(seed=0)
    proxy = ProxyState(d, d.pose0, W)
    rng = np.random.default_rng(2)
    movable = np.flatnonzero(~d.fixed)
    for i in range(1000):
        p = int(movable[rng.integers(len(movable))])
        row = proxy.pose[p].copy()
        kind = rng.random()
        if kind < 0.8:
            row[0] += rng.normal(0.0, 3.0)
            row[1] += rng.normal(0.0, 3.0)
        elif kind < 0.9:
            row[2] = (row[2] + float(rng.integers(1, 4))) % 4
        else:
            row[3] = 1.0 - row[3]
        proxy.try_move(p, row)
        (proxy.commit if rng.random() < 0.5 else proxy.revert)()
        if i % 25 == 0:
            c, length = evaluate(d, proxy.pose)
            assert c == proxy.crossings, i
            assert length == proxy.length, i
    c, length = evaluate(d, proxy.pose)
    assert c == proxy.crossings
    assert length == proxy.length


def test_proxy_throughput_floor_and_printed_rate() -> None:
    """Proxy evals/s on the 100-part synthetic, printed every run —
    hard floor 500/s (spec's CI floor; target ≥2000/s on the dev Mac).
    Runs BOTH the default shares and the flow preset: flow turns the
    route proxies on, and commit() re-derives the direction term (O(W²)
    per accept — fable review S6) — so a flow-lane regression must trip
    this floor, not just the compact lane. A 25% forced acceptance keeps
    commits flowing regardless of pose quality. Floor rationale: the
    default lane keeps the spec's 500/s; the flow lane gets 100/s —
    measured 2026-10-29 with the margin term's label axis (canonical-
    spot walk + edge-ink erosion) at ~360/s quiet on the dev Mac, ~430/s
    for the prior margin term (a 1.19× label-axis cost, accepted: the
    axis is what keeps refdes labels on their parts), ~220/s on
    instrumented Linux CI shards (coverage + typeguard), and a loaded
    shared CI runner measured 144/s in r141 — so 100 trips only on a
    genuine ~2× regression, not on runner noise."""
    from precis.pcb.topoplace.design import PRESETS

    d, _inst, _nets = synth_design(seed=0)
    movable = np.flatnonzero(~d.fixed)
    n_evals = 4000
    for shares, label, floor in (
        (W, "default", 500.0),
        (PRESETS["flow"], "flow", 100.0),
    ):
        proxy = ProxyState(d, d.pose0, shares)
        rng = np.random.default_rng(1)  # identical move sequence per lane
        t0 = time.perf_counter()
        for _ in range(n_evals):
            p = int(movable[rng.integers(len(movable))])
            row = proxy.pose[p].copy()
            row[0] += rng.normal(0.0, 3.0)
            row[1] += rng.normal(0.0, 3.0)
            if proxy.try_move(p, row) < 0 or rng.random() < 0.25:
                proxy.commit()
            else:
                proxy.revert()
        rate = n_evals / (time.perf_counter() - t0)
        print(f"\nproxy throughput ({label}): {rate:.0f} evals/s (floor {floor:.0f})")
        assert rate >= floor


def test_spread_proxy_term_parity_and_direction() -> None:
    """The loop's board-fill term (gr515107): staged/commit tracks the
    full recompute, clumping raises it, dispersal lowers it, and parts
    sharing a net never repel each other."""
    from precis.pcb.topoplace.proxy import _part_shared, spread_total

    d, _inst, _nets = synth_design(seed=0)
    w = ObjectiveShares(crossings=0.5, ratsnest=0.1, overlap=0.2, spread=0.2)
    proxy = ProxyState(d, d.pose0, w)
    assert proxy.spread == pytest.approx(spread_total(d, d.pose0))
    rng = np.random.default_rng(3)
    movable = np.flatnonzero(~d.fixed)
    for i in range(300):
        p = int(movable[rng.integers(len(movable))])
        row = proxy.pose[p].copy()
        row[0] += rng.normal(0.0, 3.0)
        row[1] += rng.normal(0.0, 3.0)
        if rng.random() < 0.1:
            row[3] = 1.0 - row[3]
        proxy.try_move(p, row)
        (proxy.commit if rng.random() < 0.5 else proxy.revert)()
        if i % 25 == 0:
            assert proxy.spread == pytest.approx(spread_total(d, proxy.pose)), i
    assert proxy.spread == pytest.approx(spread_total(d, proxy.pose))

    # direction: every movable part clumped on one point is the
    # repulsion maximum — strictly worse than any real pose.
    clump = proxy.pose.copy()
    clump[movable, :2] = clump[movable[0], :2]
    assert spread_total(d, clump) > spread_total(d, proxy.pose)

    # exemption: two parts on the same net contribute nothing even at
    # distance 0 — ``_part_shared`` must mark them.
    for pins in d.net_pins:
        members = np.unique(d.pin_part[pins])
        assert _part_shared(d)[np.ix_(members, members)].all()


def test_route_proxy_terms_parity_300_moves() -> None:
    """The slice-5 route proxies (vias, backtrack, direction), the
    silkroom term and the maximin margin term join the exactness
    contract: with every share on, staged/commit tracks the full
    recompute over 300 seeded translate/rotate/flip moves. The direction
    term pins its seed scale like the spread term, so the reference
    recompute passes ``scale=proxy._d_scale`` (the outline makes that
    scale pose-independent). Silkroom and margin delta-accumulate at
    commit rather than re-deriving, so their staged values are pinned
    against the module totals every 25 moves — a wrong ``_d_sk`` or
    ``_d_mg`` would drift silently across commits otherwise."""
    d, _inst, _nets = synth_design(seed=0)
    from precis.pcb.topoplace.proxy import (
        _SILK_ROOM_REQ_MM,
        _ring_silkroom_total,
        margin_total,
    )

    w = ObjectiveShares(
        crossings=0.4,
        ratsnest=0.1,
        overlap=0.1,
        spread=0.1,
        vias=0.1,
        backtrack=0.1,
        direction=0.1,
        silkroom=0.1,
        margin=0.1,
    )
    proxy = ProxyState(d, d.pose0, w)
    assert proxy.vias == via_estimate_total(d, d.pose0)
    assert proxy.backtrack == backtrack_total(d, d.pose0)
    assert proxy.direction == direction_total(d, d.pose0, scale=proxy._d_scale)
    assert proxy.silkroom == pytest.approx(
        _ring_silkroom_total(proxy._rings, d.pose0, proxy._th, _SILK_ROOM_REQ_MM)
    )
    assert proxy.margin == pytest.approx(margin_total(d, d.pose0))
    rng = np.random.default_rng(7)
    movable = np.flatnonzero(~d.fixed)
    for i in range(300):
        p = int(movable[rng.integers(len(movable))])
        row = proxy.pose[p].copy()
        kind = rng.random()
        if kind < 0.8:
            row[0] += rng.normal(0.0, 3.0)
            row[1] += rng.normal(0.0, 3.0)
        elif kind < 0.9:
            row[2] = (row[2] + float(rng.integers(1, 4))) % 4
        else:
            row[3] = 1.0 - row[3]
        proxy.try_move(p, row)
        if rng.random() < 0.5:
            # The staged values are what the anneal decides on — and
            # commit() RE-DERIVES vias/backtrack/direction from committed
            # state, so without this snapshot a wrong try_move Δ would
            # be invisible to every assertion below (fable review S1:
            # the parity test was tautological for the three new terms).
            # Commit must never move a staged number.
            staged = (
                proxy.cost,
                proxy.vias,
                proxy.backtrack,
                proxy.direction,
                proxy.silkroom,
                proxy.margin,
            )
            proxy.commit()
            # Commit re-derives the float terms from committed state in a
            # different summation order than the staged telescope, so
            # staged vs committed agrees to ~1e-13 (observed: last-ulp at
            # move 2), not bit-exactly — that ulp IS the honest contract
            # (this staged snapshot is fable-review S1; without it a wrong
            # try_move Δ was invisible). The vias estimate is an int and
            # stays ==. The every-25 asserts below still pin the
            # COMMITTED state bit-exactly against the module totals.
            assert proxy.cost == pytest.approx(staged[0], abs=1e-9), i
            assert proxy.vias == staged[1], i
            assert proxy.backtrack == pytest.approx(staged[2], abs=1e-9), i
            assert proxy.direction == pytest.approx(staged[3], abs=1e-9), i
            # silkroom and margin delta-accumulate at commit (like
            # overlap/spread), so staged == committed by construction —
            # but a wrong try_move Δ would drift silently across commits,
            # which the every-25 recompute asserts below pin against the
            # module totals.
            assert proxy.silkroom == pytest.approx(staged[4], abs=1e-9), i
            assert proxy.margin == pytest.approx(staged[5], abs=1e-9), i
        else:
            proxy.revert()
        if i % 25 == 0:
            assert proxy.vias == via_estimate_total(d, proxy.pose), i
            assert proxy.backtrack == backtrack_total(d, proxy.pose), i
            assert proxy.direction == direction_total(
                d, proxy.pose, scale=proxy._d_scale
            ), i
            assert proxy.silkroom == pytest.approx(
                _ring_silkroom_total(
                    proxy._rings, proxy.pose, proxy._th, _SILK_ROOM_REQ_MM
                )
            ), i
            assert proxy.margin == pytest.approx(margin_total(d, proxy.pose)), i
    assert proxy.vias == via_estimate_total(d, proxy.pose)
    assert proxy.backtrack == backtrack_total(d, proxy.pose)
    assert proxy.direction == direction_total(d, proxy.pose, scale=proxy._d_scale)
    assert proxy.silkroom == pytest.approx(
        _ring_silkroom_total(proxy._rings, proxy.pose, proxy._th, _SILK_ROOM_REQ_MM)
    )
    assert proxy.margin == pytest.approx(margin_total(d, proxy.pose))


def _margin_label_fixture(
    spec: list[tuple[str, float, float, float]],
    *,
    edge_mate: tuple[bool, ...] = (),
):
    """Tiny hand-built design for the margin label/edge axis tests:
    ``spec`` rows are ``(refdes, x, y, ring_half_mm)`` on a 30mm square
    board, one pin per part, no nets."""
    from precis.pcb.topoplace.design import TopoDesign

    n = len(spec)

    def sq(h: float) -> np.ndarray:
        return np.asarray([[-h, -h], [h, -h], [h, h], [-h, h], [-h, -h]])

    return TopoDesign(
        parts=tuple(r for r, _x, _y, _h in spec),
        pin_xy=np.zeros((n, 2)),
        pin_part=np.arange(n),
        pin_side=np.zeros(n, dtype=np.int8),
        pin_label=np.asarray([str(i) for i in range(n)], dtype=object),
        pin_net=np.full(n, -1, dtype=np.int32),
        net_pins=(),
        flex=(),
        swap_groups=(),
        fixed=np.zeros(n, dtype=bool),
        freedoms=np.full(n, 3, dtype=np.uint8),
        outline=np.asarray([[0, 0], [30, 0], [30, 30], [0, 30]]),
        pose0=np.asarray([[x, y, 0.0, 0.0] for _r, x, y, _h in spec]),
        part_outline=tuple(sq(h) for _r, _x, _y, h in spec),
        part_edge_mate=np.asarray(edge_mate, dtype=bool)
        if edge_mate
        else np.zeros(0, bool),
    )


def test_margin_label_axis_blocked_when_surrounded() -> None:
    """The label axis pays _LBL_NO_SPOT_MM when every canonical spot's
    box is covered (taz-leaf run 34's R23: edge to the south, rings
    east/west/north) and 0 again the moment one blocker moves off."""
    from precis.pcb.topoplace.proxy import _LBL_NO_SPOT_MM, margin_total

    off = 2.2  # ring-to-ring 0.4mm — inside the label box gap
    spec = [("R1", 15.0, 15.0, 0.9)]
    for k, (dx, dy) in enumerate(
        ((0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1), (-1, 0), (-1, 1))
    ):
        spec.append((f"B{k}", 15.0 + dx * off, 15.0 + dy * off, 0.9))
    d = _margin_label_fixture(spec)
    w = ObjectiveShares(crossings=0.5, overlap=0.3, margin=0.2)
    proxy = ProxyState(d, d.pose0, w)
    # Every canonical box is covered and the ladder climbs past the
    # blockers — the axis is the GRADED walk (0 < excess ≤ NO_SPOT),
    # not the flat no-spot constant: the gradient is what lets the
    # anneal unstick the part (a flat 4.0 saturates the softmin).
    assert 0.0 < proxy._mg_l[0] <= _LBL_NO_SPOT_MM
    blocked_before = float(proxy.margin)
    # The south trio (B3 SE, B4 S, B5 SW — indices 4/5/6) steps away —
    # the wide bottom-corner boxes are clipped by the diagonals too, so
    # freeing only the cardinal south blocker is not enough.
    for idx in (4, 5, 6):
        row = proxy.pose[idx].copy()
        row[1] -= 10.0
        proxy.try_move(idx, row)
        proxy.commit()
    assert proxy._mg_l[0] == 0.0
    assert proxy.margin < blocked_before
    assert proxy.margin == pytest.approx(margin_total(d, proxy.pose))


def test_margin_edge_axis_flat_even_for_big_rings() -> None:
    """The edge axis is the flat _SILK_EDGE_REQ_MM, not a ring-radius
    want: a big ring whose courtyard sits 0.8mm off the edge pays the
    requirement's shortfall — under the retired want its circumradius
    (> room) priced 0, which is exactly how run 34's R23 hugged the
    outline for free. Edge-mated parts stay exempt."""
    from precis.pcb.topoplace.proxy import _SILK_EDGE_REQ_MM

    d = _margin_label_fixture([("U1", 5.8, 15.0, 5.0)])
    w = ObjectiveShares(crossings=0.5, overlap=0.3, margin=0.2)
    proxy = ProxyState(d, d.pose0, w)
    # ring edge at x=0.8 → distance to the outline is 0.8 exactly.
    assert proxy._mg_e[0] == pytest.approx(_SILK_EDGE_REQ_MM - 0.8)

    dm = _margin_label_fixture([("U1", 6.0, 15.0, 5.0)], edge_mate=(True,))
    pm = ProxyState(dm, dm.pose0, w)
    assert pm._mg_e[0] == 0.0


def test_label_spots_for_pins_canonical_greedy() -> None:
    """The pins are LABEL_SPOT_ORDER members, one per un-crowded part,
    chosen boxes never overlap, a fully surrounded part gets none, and
    the pick is deterministic across calls."""
    from precis.pcb.silk import LABEL_SPOT_ORDER
    from precis.pcb.topoplace import label_spots_for

    spread = _margin_label_fixture(
        [("R1", 6.0, 6.0, 0.9), ("R2", 15.0, 15.0, 0.9), ("R3", 24.0, 24.0, 0.9)]
    )
    pins = label_spots_for(spread, spread.pose0)
    assert sorted(pins) == ["R1", "R2", "R3"]
    assert all(p in LABEL_SPOT_ORDER for p in pins.values())
    assert pins == label_spots_for(spread, spread.pose0)

    off = 2.2
    spec = [("R1", 15.0, 15.0, 0.9)]
    for k, (dx, dy) in enumerate(
        ((0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1), (-1, 0), (-1, 1))
    ):
        spec.append((f"B{k}", 15.0 + dx * off, 15.0 + dy * off, 0.9))
    crowded = _margin_label_fixture(spec)
    pins_c = label_spots_for(crowded, crowded.pose0)
    assert "R1" not in pins_c  # every canonical box covered → no pin
    # chosen boxes pairwise non-overlapping (the greedy's own invariant)
    from precis.pcb.topoplace.proxy import _label_spot_boxes, _ring_geom

    boxes = []
    for i, refdes in enumerate(crowded.parts):
        name = pins_c.get(refdes)
        if name is None:
            continue
        ring = _ring_geom(crowded, crowded.pose0[i], i)
        box = dict(
            zip(
                LABEL_SPOT_ORDER,
                _label_spot_boxes(refdes, ring, crowded.silk.label_anchor_mm),
            )
        )[name]
        boxes.append(box)
    for a in range(len(boxes)):
        for b in range(a + 1, len(boxes)):
            ax0, ay0, ax1, ay1 = boxes[a]
            bx0, by0, bx1, by1 = boxes[b]
            assert ax1 <= bx0 or bx1 <= ax0 or ay1 <= by0 or by1 <= ay0


def test_label_boxes_for_route_keepouts() -> None:
    """The route stage threads the pins in as keep-out zones:
    :func:`label_boxes_for` returns one CLOSED world ring per pinned
    part at its pinned spot, nothing for unpinned parts, and every ring
    is a non-degenerate polygon."""
    from shapely.geometry import Polygon

    from precis.pcb.topoplace import label_boxes_for, label_spots_for

    spread = _margin_label_fixture(
        [("R1", 6.0, 6.0, 0.9), ("R2", 15.0, 15.0, 0.9), ("R3", 24.0, 24.0, 0.9)]
    )
    spots = label_spots_for(spread, spread.pose0)
    boxes = dict(label_boxes_for(spread, spread.pose0, spots))
    assert sorted(boxes) == sorted(spots)
    for refdes, ring in boxes.items():
        assert ring.shape[1] == 2 and ring[0].tolist() == ring[-1].tolist()
        g = Polygon(ring[:-1])
        assert g.is_valid and g.area > 0.0
    # unpinned part -> no row
    boxes_none = dict(label_boxes_for(spread, spread.pose0, {}))
    assert boxes_none == {}


def _pair_gap_fixture(
    poses: list[list[float]], *, capability: str | None = "4layer"
) -> Any:
    """Two 0402-class parts on a 30 mm board: 1.0×0.5 mm pad bodies
    (``part_body``), courtyards the bodies + 0.4 mm. ``poses`` rows are
    (x, y, rot_q, side)."""
    from precis.pcb.capabilities import capability_for
    from precis.pcb.topoplace.design import SilkRoom, TopoDesign

    n = len(poses)
    body = np.asarray(
        [[-0.5, -0.25], [0.5, -0.25], [0.5, 0.25], [-0.5, 0.25], [-0.5, -0.25]]
    )
    court = np.asarray(
        [[-0.9, -0.65], [0.9, -0.65], [0.9, 0.65], [-0.9, 0.65], [-0.9, -0.65]]
    )
    return TopoDesign(
        parts=tuple(f"R{i + 1}" for i in range(n)),
        pin_xy=np.zeros((n, 2)),
        pin_part=np.arange(n),
        pin_side=np.zeros(n, dtype=np.int8),
        pin_label=np.asarray(["1"] * n, dtype=object),
        pin_net=np.full(n, -1, dtype=np.int32),
        net_pins=(),
        flex=(),
        swap_groups=(),
        fixed=np.zeros(n, dtype=bool),
        freedoms=np.full(n, 3, dtype=np.uint8),
        outline=np.asarray([[0, 0], [30, 0], [30, 30], [0, 30]]),
        pose0=np.asarray(poses, dtype=float),
        part_outline=tuple(court.copy() for _ in range(n)),
        part_body=tuple(body.copy() for _ in range(n)),
        silk=(
            SilkRoom.from_capability(capability_for(capability))
            if capability
            else SilkRoom.fallback()
        ),
    )


def test_hard_pair_gap_legalizes_to_silk_separation() -> None:
    """topo-silk-spacing ruling: two 0402s seeded 0.9 mm apart pad-to-pad
    (courtyard gap 0.1 mm — inside the 0.20 mm courtyard pair
    requirement) end at least 1.00 mm pad gap after legalise, the
    separation two full outlines at the JLC-min 0.275 mm target need to
    draw without sharing a wall (2·0.275 + 3·0.15 hull-to-hull)."""
    from precis.pcb.topoplace.legalize import legalize_pose
    from precis.pcb.topoplace.proxy import _body_geom

    # centres 1.9mm apart: pad gap 1.9 - 1.0 = 0.9mm, courtyard gap 0.1mm
    d = _pair_gap_fixture([[10.0, 15.0, 0.0, 0.0], [11.9, 15.0, 0.0, 0.0]])
    res = legalize_pose(d, d.pose0)
    assert res.resolved, res.remaining
    ga = _body_geom(d, res.pose[0], 0)
    gb = _body_geom(d, res.pose[1], 1)
    assert float(ga.distance(gb)) >= 1.00 - 1e-3


def test_hard_pair_gap_exempts_one_body_pairs() -> None:
    """The named exemption has two classes (round-3 ruling): rigid-group
    members share one authored body — a tight (even overlapping) pose
    stands untouched. The ends of a hard upper-distance measure — the
    crystal-load-cap carry — are exempt from the GAP only: courtyards
    may touch but never overlap, so a tight disjoint pose stands while
    an overlapping one separates just to a touch, not to
    ``pair_req_mm``."""
    import dataclasses

    from precis.pcb.topoplace.design import TopoMeasure
    from precis.pcb.topoplace.legalize import legalize_pose
    from precis.pcb.topoplace.proxy import _ring_geom

    d = _pair_gap_fixture([[10.0, 15.0, 0.0, 0.0], [10.8, 15.0, 0.0, 0.0]])
    grouped = dataclasses.replace(
        d,
        group_of=np.asarray([0, 0], dtype=np.int32),
        group_members=((0, 1),),
    )
    res = legalize_pose(grouped, grouped.pose0)
    np.testing.assert_allclose(res.pose, grouped.pose0)
    assert res.resolved

    spec = TopoMeasure(ia=0, ib=1, bound="upper", goal_mm=2.0, weight=1.0, hard=True)
    # a hard near() measure exempts the GAP: 2.0mm centres sit the
    # courtyards 0.2mm apart — under pair_req, allowed
    d2 = _pair_gap_fixture([[10.0, 15.0, 0.0, 0.0], [12.0, 15.0, 0.0, 0.0]])
    measured = dataclasses.replace(d2, measures=(spec,))
    res2 = legalize_pose(measured, measured.pose0)
    np.testing.assert_allclose(res2.pose, measured.pose0)
    assert res2.resolved

    # but the carry is no licence to nest: an overlapping authored pose
    # separates to a TOUCH (its own requirement is 0 — the overlap is
    # the only thing pushed out)
    measured_overlap = dataclasses.replace(d, measures=(spec,))
    res3 = legalize_pose(measured_overlap, measured_overlap.pose0)
    assert res3.resolved, res3.remaining
    ra, rb = _ring_geom(d, res3.pose[0], 0), _ring_geom(d, res3.pose[1], 1)
    assert ra is not None and rb is not None
    assert float(ra.intersection(rb).area) == pytest.approx(0.0, abs=1e-9)
    assert float(ra.distance(rb)) < float(d.silk.pair_req_mm) - 0.1


def test_hard_pair_gap_fixed_fixed_reports_not_fails() -> None:
    """Round-2 ruling: a pair with BOTH ends authored ``fixed`` is
    adjacency the placer may not move — exempt from the hard gap like a
    one-body pair but REPORTED (``noted`` rows carry reason
    ``fixed-fixed``; ``pair_gap_flagged`` surfaces it for the info
    lines) and never a ``pcb.placement-gap`` failure. A pair with a
    movable side still resolves or fails."""
    import dataclasses

    from precis.pcb.topoplace.legalize import legalize_pose
    from precis.pcb.topoplace.proxy import pair_gap_flagged, pair_gap_violations

    d = _pair_gap_fixture([[10.0, 15.0, 0.0, 0.0], [10.8, 15.0, 0.0, 0.0]])
    locked = dataclasses.replace(d, fixed=np.asarray([True, True]))
    res = legalize_pose(locked, locked.pose0)
    np.testing.assert_allclose(res.pose, locked.pose0)  # nothing may move
    assert res.resolved, res.remaining
    assert res.noted and res.noted[0][:2] == (0, 1)
    assert res.noted[0][3] == "fixed-fixed"
    assert not pair_gap_violations(locked, res.pose)
    flagged = pair_gap_flagged(locked, res.pose)
    assert flagged == ((0, 1, res.noted[0][2]),)

    # one movable side → the hard gap still applies and resolves
    half = dataclasses.replace(d, fixed=np.asarray([True, False]))
    res2 = legalize_pose(half, half.pose0)
    np.testing.assert_allclose(res2.pose[0], half.pose0[0])  # fixed stays
    assert res2.resolved, res2.remaining
    assert not pair_gap_violations(half, res2.pose)


def test_legalize_moves_a_part_off_anothers_body() -> None:
    """Reto 2026-11-02: "No parts can be inside another part's
    courtyard." A catalog footprint's authored courtyard is the part's
    BODY — a small part seeded in the gap between a wide part's
    far-apart lands (the taz-leaf L1-inside-PV1 case, synthetic) is a
    collision the legalizer must move it out of, not an open spot."""
    from precis.pcb import DEFAULT_STACKUP
    from precis.pcb.ir import from_graph
    from precis.pcb.session import apply_real_pin_offsets
    from precis.pcb.topoplace.design import design_from_ir
    from precis.pcb.topoplace.legalize import legalize_pose
    from precis.pcb.topoplace.proxy import _ring_geom

    graph = {
        "instances": [
            {
                "refdes": "PV1",
                "part_lcsc": "C5358484",
                "x": 50.0,
                "y": 50.0,
                "fixed": "both",
            },
            {"refdes": "L1", "x": 50.0, "y": 52.0},
        ],
        "nets": [
            {
                "name": "IN",
                "members": [
                    {"refdes": "PV1", "pin": "1"},
                    {"refdes": "L1", "pin": "1"},
                ],
            },
            {
                "name": "OUT",
                "members": [
                    {"refdes": "PV1", "pin": "2"},
                    {"refdes": "L1", "pin": "2"},
                ],
            },
        ],
    }
    fp = {
        "pads": [
            {"number": "1", "shape": "RECT", "x": -10.7, "y": 0.0, "w": 3.0, "h": 8.0},
            {"number": "2", "shape": "RECT", "x": 10.7, "y": 0.0, "w": 3.0, "h": 8.0},
        ],
        "pin_map": {},
        "courtyard": {"bbox": [-12.2, -4.064, 12.2, 4.064]},
    }
    ir = from_graph(
        graph,
        stackup=DEFAULT_STACKUP,
        outline=[[0.0, 0.0], [90.0, 0.0], [90.0, 90.0], [0.0, 90.0]],
    )
    apply_real_pin_offsets(ir, {"PV1": fp}, footprint_sources={"PV1": "catalog"})
    d = design_from_ir(ir, {"PV1": fp})
    res = legalize_pose(d, d.pose0)
    assert res.resolved, res.remaining
    np.testing.assert_allclose(res.pose[0], d.pose0[0])  # the fixed part stays
    pv, l1 = _ring_geom(d, res.pose[0], 0), _ring_geom(d, res.pose[1], 1)
    assert pv is not None and l1 is not None
    assert float(pv.intersection(l1).area) == pytest.approx(0.0, abs=1e-9)


def test_legalize_escapes_a_seesaw_by_relocating_the_wedged_part() -> None:
    """taz-leaf x1: a movable part wedged between two FIXED rings whose
    exclusion zones overlap seesaws — the sweep re-picks the same worst
    pair every round, pushes it ~0.4mm alternately, and burns the whole
    round budget while other violating pairs wait (the C5/PV1/L2
    sandwich). The pose-revisit detector must break the loop
    structurally: the wedged part teleports to the nearest legal spot
    and the call resolves, instead of returning 2000 rounds of
    oscillation."""
    import dataclasses

    from precis.pcb.topoplace.legalize import legalize_pose
    from precis.pcb.topoplace.proxy import _ring_geom

    # R2 fixed at (15,17), R3 fixed at (15,13): a movable R1 at (15,15)
    # needs |dy| >= 2.3 from each (0.65 ring + 1.0 req + 0.65 ring) —
    # no feasible point between them, so every push resolves one pair
    # and violates the other.
    d = _pair_gap_fixture(
        [[15.0, 15.0, 0.0, 0.0], [15.0, 17.0, 0.0, 0.0], [15.0, 13.0, 0.0, 0.0]]
    )
    wedged = dataclasses.replace(d, fixed=np.asarray([False, True, True]))
    res = legalize_pose(wedged, wedged.pose0)
    assert res.resolved, res.remaining
    np.testing.assert_allclose(res.pose[1], wedged.pose0[1])  # fixed stays
    np.testing.assert_allclose(res.pose[2], wedged.pose0[2])
    g1 = _ring_geom(wedged, res.pose[0], 0)
    g2 = _ring_geom(wedged, res.pose[1], 1)
    g3 = _ring_geom(wedged, res.pose[2], 2)
    assert g1 is not None and g2 is not None and g3 is not None
    req = float(wedged.silk.pair_req_mm)
    assert float(g1.distance(g2)) >= req - 1e-3
    assert float(g1.distance(g3)) >= req - 1e-3


def test_legalize_names_the_pair_when_no_legal_spot_exists() -> None:
    """The other half of the seesaw ruling: when the board has NO free
    spot the wedged part can take, the pair is set aside (so every
    OTHER violating pair still gets fixed) and lands on ``remaining``
    by name — an honest failure the ``pcb.placement-gap`` gate reports,
    never a silently half-swept pose."""
    import dataclasses

    from precis.pcb.topoplace.legalize import legalize_pose

    # Same sandwich on a 6mm board, the fixed pair given board-spanning
    # rings (the 0.20mm courtyard requirement no longer lets two plain
    # 0402 rings wall off a whole board): the only unclaimed y-band is
    # 0.8mm — narrower than the mover's 1.3mm courtyard — so every grid
    # candidate sits inside a fixed ring's exclusion zone.
    wall = np.asarray(
        [[-3.0, -1.2], [3.0, -1.2], [3.0, 1.2], [-3.0, 1.2], [-3.0, -1.2]]
    )
    d = _pair_gap_fixture(
        [[3.0, 3.0, 0.0, 0.0], [3.0, 4.8, 0.0, 0.0], [3.0, 1.2, 0.0, 0.0]]
    )
    tiny = dataclasses.replace(
        d,
        part_outline=(d.part_outline[0], wall, wall.copy()),
        fixed=np.asarray([False, True, True]),
        outline=np.asarray([[0, 0], [6, 0], [6, 6], [0, 6]]),
    )
    res = legalize_pose(tiny, tiny.pose0)
    assert not res.resolved
    assert any(a == 0 and b in (1, 2) for a, b, _s in res.remaining)


def test_legalize_escape_stays_in_the_neighbourhood_or_names_it() -> None:
    """Round-3 ruling: the escape teleport is bounded to
    ``_ESCAPE_SPOT_DIAMETERS`` courtyard diameters — a legal spot past
    that is a different region of the board, and the named failure on
    ``remaining`` is worth more than the cross-board jump. R1 wedged
    between two board-wide fixed walls whose exclusion zones meet
    (under the 0.20mm courtyard requirement a 2.2mm grid of plain rings
    no longer walls anything off): every candidate within reach is
    inside a wall's keep-out, so the seesaw ends in NAMED pairs, not a
    teleport — and promptly, not after the full round budget."""
    import dataclasses

    from precis.pcb.topoplace.legalize import legalize_pose

    wall = np.asarray(
        [[-4.0, -2.0], [4.0, -2.0], [4.0, 2.0], [-4.0, 2.0], [-4.0, -2.0]]
    )
    d = _pair_gap_fixture(
        [[4.0, 4.0, 0.0, 0.0], [4.0, 6.0, 0.0, 0.0], [4.0, 2.0, 0.0, 0.0]]
    )
    boxed = dataclasses.replace(
        d,
        part_outline=(d.part_outline[0], wall, wall.copy()),
        fixed=np.asarray([False, True, True]),
        outline=np.asarray([[0, 0], [8, 0], [8, 8], [0, 8]]),
    )
    res = legalize_pose(boxed, boxed.pose0)
    assert not res.resolved
    assert any(a == 0 and b in (1, 2) for a, b, _s in res.remaining)
    assert res.rounds < 500, "each wedged pair is set aside, not fought forever"


def test_legalize_escape_radius_scales_with_the_obstacle() -> None:
    """Round-4 ruling: the escape radius is 3 × the LARGER ring side of
    the pair, not only the mover's — a part wedged against a wide
    authored body (the x1 BT1/PV1 case) may look past that body. R1
    seesaws between two fixed 20x8 rings; every legal spot is >10mm
    away — inside 3 x the obstacle's side, outside 3 x the mover's
    1.8mm ring. The escape teleports it out and resolves; a mover-only
    radius would have set the pair aside."""
    import dataclasses

    from precis.pcb.topoplace.legalize import legalize_pose

    big = np.asarray(
        [[-10.0, -4.0], [10.0, -4.0], [10.0, 4.0], [-10.0, 4.0], [-10.0, -4.0]]
    )
    d = _pair_gap_fixture(
        [[15.0, 12.7, 0.0, 0.0], [15.0, 8.0, 0.0, 0.0], [15.0, 17.0, 0.0, 0.0]]
    )
    wedged = dataclasses.replace(
        d,
        part_outline=(d.part_outline[0], big, big.copy()),
        fixed=np.asarray([False, True, True]),
    )
    res = legalize_pose(wedged, wedged.pose0)
    assert res.resolved, res.remaining
    np.testing.assert_allclose(res.pose[1], wedged.pose0[1])  # fixed stays
    np.testing.assert_allclose(res.pose[2], wedged.pose0[2])
    moved = float(np.hypot(*(res.pose[0, :2] - wedged.pose0[0, :2])))
    assert moved > 3.0 * 1.8, "reached past the mover's own 3 diameters"


def test_legalize_escape_keeps_a_hard_near_measure() -> None:
    """Round-3 ruling: a part carried by a hard dist-upper measure (the
    decap/crystal near its IC) may only escape to a spot that KEEPS the
    measure — a relocation that breaks it is no escape. Same fixed-ring
    sandwich as the seesaw test, plus a fixed anchor R4: at goal 15 the
    legal spots below the sandwich stay inside the bound, so the wedged
    part teleports and resolves; at goal 2.5 no reachable spot keeps
    the measure, so the fight is set aside and named."""
    import dataclasses

    from precis.pcb.topoplace.design import TopoMeasure
    from precis.pcb.topoplace.legalize import legalize_pose

    def measure(goal: float) -> TopoMeasure:
        return TopoMeasure(
            ia=0, ib=3, bound="upper", goal_mm=goal, weight=1.0, hard=True
        )

    # The sandwich is 1.4mm each way now (was 2.0): under the 0.20mm
    # courtyard requirement the old 0.7mm gaps were already legal and
    # the mover simply stood — 0.1mm gaps keep the seesaw.
    d = _pair_gap_fixture(
        [
            [15.0, 15.0, 0.0, 0.0],
            [15.0, 16.4, 0.0, 0.0],
            [15.0, 13.6, 0.0, 0.0],
            [15.0, 24.0, 0.0, 0.0],
        ]
    )
    kept = dataclasses.replace(
        d,
        fixed=np.asarray([False, True, True, True]),
        measures=(measure(15.0),),
    )
    res = legalize_pose(kept, kept.pose0)
    assert res.resolved, res.remaining
    np.testing.assert_allclose(res.pose[1], kept.pose0[1])  # fixed stays
    np.testing.assert_allclose(res.pose[2], kept.pose0[2])
    np.testing.assert_allclose(res.pose[3], kept.pose0[3])
    moved = float(np.hypot(*(res.pose[0, :2] - kept.pose0[0, :2])))
    assert moved > 1.0, "the escape teleported, not nudged"
    assert float(np.hypot(*(res.pose[0, :2] - res.pose[3, :2]))) <= 15.0 + 1e-6

    broken = dataclasses.replace(
        d,
        fixed=np.asarray([False, True, True, True]),
        measures=(measure(2.5),),
    )
    res2 = legalize_pose(broken, broken.pose0)
    assert not res2.resolved
    assert any(a == 0 and b in (1, 2) for a, b, _s in res2.remaining)


def test_silkroom_resolves_from_capability_not_fallback() -> None:
    """The capability threading's whole point: the draw-side figures the
    placer prices come off the capability row through the SAME functions
    the silk pass draws with — under the 2026-11-03 vendor-silk ruling
    the ring target is the JLC-MIN legal clearance (0.20 ink + half the
    0.15 pen), identical at every tier, so pair ask 0.20 mm, label gap
    0.45/0.525 mm and edge ask 0.95 mm coincide with the fallbacks by
    design; the capability still answers where the tiers differ
    (``via_to_silk_mm`` 0.25 vs the 0.15 fallback). The fallback path
    stays available for capability-less designs."""
    from precis.pcb.capabilities import capability_for
    from precis.pcb.topoplace.design import SilkRoom

    silk = SilkRoom.from_capability(capability_for("4layer"))
    assert silk.pair_req_mm == pytest.approx(0.2, abs=1e-9)
    assert silk.label_anchor_mm == pytest.approx(0.45, abs=1e-9)
    assert silk.label_clear_mm == pytest.approx(0.525, abs=1e-9)
    assert silk.edge_req_mm == pytest.approx(0.95, abs=1e-9)
    assert silk.via_to_silk_mm == pytest.approx(0.25, abs=1e-9)
    fallback = SilkRoom.fallback()
    assert fallback.pair_req_mm == pytest.approx(0.2, abs=1e-9)
    assert fallback.label_anchor_mm == pytest.approx(0.45, abs=1e-9)
    assert fallback.label_clear_mm == pytest.approx(0.525, abs=1e-9)
    assert fallback.edge_req_mm == pytest.approx(0.95, abs=1e-9)
    assert fallback.via_to_silk_mm == pytest.approx(0.15, abs=1e-9)
    assert SilkRoom.from_capability(None) == fallback
    # the design field defaults to the fallback (hand-built synthetics)
    d = _margin_label_fixture([("U1", 15.0, 15.0, 0.9)])
    assert d.silk == fallback


def test_settle_label_room_nudges_walkers_and_edge_parts() -> None:
    """The post-anneal repair is the guarantee: on the crowded fixture
    (centre part boxed in by eight blockers) it nudges until the centre
    part's label room comes back — walk 0 — and a part parked in the
    edge band comes off the outline. Survivors it cannot fix are left
    in place (honest, not hidden)."""
    from precis.pcb.topoplace import label_room_walks, settle_label_room

    off = 2.2
    spec = [("R1", 15.0, 15.0, 0.9)]
    for k, (dx, dy) in enumerate(
        ((0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1), (-1, 0), (-1, 1))
    ):
        spec.append((f"B{k}", 15.0 + dx * off, 15.0 + dy * off, 0.9))
    d = _margin_label_fixture(spec)
    walk0, _edge0 = label_room_walks(d, d.pose0)
    assert walk0[0] > 0.0

    repaired = settle_label_room(d, d.pose0)
    walk1, _edge1 = label_room_walks(d, repaired)
    assert walk1[0] == 0.0
    # somebody moved — the boxed-in centre part or a blocker it pushed
    assert not np.array_equal(repaired, d.pose0)

    # edge half: a ring straddling the edge band gets pulled inboard
    de = _margin_label_fixture([("U9", 1.2, 15.0, 0.9)])
    we0, ee0 = label_room_walks(de, de.pose0)
    assert ee0[0] > 0.0
    rep_e = settle_label_room(de, de.pose0)
    _we1, ee1 = label_room_walks(de, rep_e)
    assert ee1[0] == 0.0
    assert rep_e[0, 0] > de.pose0[0, 0]


def _decap_fixture(*, c1_fixed: bool = False):
    """U1 (5.2 mm ring) at the centre and its decap C1 (1.2 mm ring) 9 mm
    east: one hard proximity, C1's pin to U1's pad 2.5 mm east of U1's
    origin, within 2 mm (topo-route-review A7)."""
    import dataclasses

    from precis.pcb.topoplace.design import TopoMeasure

    d = _margin_label_fixture([("U1", 15.0, 15.0, 2.6), ("C1", 24.0, 15.0, 0.6)])
    spec = TopoMeasure(
        ia=1,
        ib=0,
        bound="upper",
        goal_mm=2.0,
        weight=1.0,
        hard=True,
        pts_a=((0.0, 0.0),),
        pts_b=((2.5, 0.0),),
    )
    return dataclasses.replace(
        d, measures=(spec,), fixed=np.asarray([False, c1_fixed])
    ), spec


def test_settle_measures_lands_the_decap_by_its_pin() -> None:
    """The anneal prices a hard measure; the settle meets it: C1 moves
    next to U1's pad, legally (no ring overlap), and U1 stays put."""
    from precis.pcb.topoplace import settle_measures
    from precis.pcb.topoplace.proxy import _measure_violation, _ring_geom

    d, spec = _decap_fixture()
    assert _measure_violation(spec, d.pose0[1], d.pose0[0]) > 0.0
    pose = settle_measures(d, d.pose0)
    assert _measure_violation(spec, pose[1], pose[0]) == 0.0
    np.testing.assert_array_equal(pose[0], d.pose0[0])
    u1, c1 = _ring_geom(d, pose[0], 0), _ring_geom(d, pose[1], 1)
    assert u1 is not None and c1 is not None
    assert u1.intersection(c1).area == 0.0


def test_settle_measures_never_moves_a_fixed_mover_or_the_ic() -> None:
    from precis.pcb.topoplace import settle_measures

    d, _spec = _decap_fixture(c1_fixed=True)
    np.testing.assert_array_equal(settle_measures(d, d.pose0), d.pose0)


def test_settle_measures_moves_a_blocker_off_the_only_spots() -> None:
    """A blocker sits on every legal spot within reach of U1's east pad:
    the only candidate rings overlap it or sit inside its silk room, so
    the settle nudges the blocker clear (a full ``pair_req_mm`` retreat
    — a partial one would leave the pair a named failure) and lands the
    decap — every non-exempt pair at the hard courtyard gap after, the
    measure-bound C1/U1 pair merely disjoint."""
    import dataclasses
    import itertools

    from precis.pcb.topoplace import settle_measures
    from precis.pcb.topoplace.design import TopoMeasure
    from precis.pcb.topoplace.proxy import _measure_violation, _ring_geom

    # Every candidate within 2mm of the pad either overlaps U1 (the
    # measure pair may touch, never overlap — round-3 ruling) or sits
    # on B1's ring / inside its silk room; B1 itself is legal (0.4mm
    # courtyard gap to U1 ≥ the 0.20mm pair_req). B1's ring is bigger
    # than a decap's (1.5mm half): under the shrunk pair_req a plain
    # 0.6mm ring left corner routes around it that the 2mm window could
    # still reach, and the blocker never had to move.
    d = _margin_label_fixture(
        [
            ("U1", 15.0, 15.0, 2.6),
            ("C1", 26.0, 15.0, 0.6),
            ("B1", 19.5, 15.0, 1.5),
        ]
    )
    spec = TopoMeasure(
        ia=1,
        ib=0,
        bound="upper",
        goal_mm=2.0,
        weight=1.0,
        hard=True,
        pts_a=((0.0, 0.0),),
        pts_b=((2.5, 0.0),),
    )
    d = dataclasses.replace(d, measures=(spec,))
    pose = settle_measures(d, d.pose0)
    assert _measure_violation(spec, pose[1], pose[0]) == 0.0
    assert not np.array_equal(pose[1], d.pose0[1])  # the decap landed
    assert not np.array_equal(pose[2], d.pose0[2])  # the blocker moved
    from precis.pcb.topoplace.proxy import one_body_pairs

    exempt = one_body_pairs(d)
    rings = [_ring_geom(d, pose[k], k) for k in range(len(d.parts))]
    for a, b in itertools.combinations(range(len(d.parts)), 2):
        ra, rb = rings[a], rings[b]
        assert ra is not None and rb is not None
        assert float(ra.intersection(rb).area) == pytest.approx(0.0, abs=1e-9)
        if (a, b) in exempt:
            continue  # gap-free, never overlap
        assert float(ra.distance(rb)) >= d.silk.pair_req_mm - 1e-9


def test_settle_measures_carries_a_crystals_load_caps() -> None:
    """B1 follow-up (taz-leaf X2/C1/C2): the crystal's hard bound to the
    IC pad is 8.5 mm off a 4 mm limit while its two load caps sit within
    their own 2 mm bounds of its pin. Moving the crystal alone breaks
    both cap bounds, so it used to stay put; now it carries its caps —
    one rigid translation — and every bound is met with disjoint rings."""
    import dataclasses
    import itertools

    from precis.pcb.topoplace import settle_measures
    from precis.pcb.topoplace.design import TopoMeasure
    from precis.pcb.topoplace.proxy import _measure_violation, _ring_geom

    d = _margin_label_fixture(
        [
            ("U1", 15.0, 15.0, 2.6),
            ("X2", 26.0, 15.0, 0.6),
            ("C1", 26.0, 16.8, 0.4),
            ("C2", 26.0, 13.2, 0.4),
        ]
    )

    def near(a: int, b: int, goal: float, pt_b: tuple[float, float]) -> TopoMeasure:
        return TopoMeasure(
            ia=a,
            ib=b,
            bound="upper",
            goal_mm=goal,
            weight=1.0,
            hard=True,
            pts_a=((0.0, 0.0),),
            pts_b=(pt_b,),
        )

    specs = (
        near(1, 0, 4.0, (2.5, 0.0)),
        near(2, 1, 2.0, (0.0, 0.0)),
        near(3, 1, 2.0, (0.0, 0.0)),
    )
    d = dataclasses.replace(d, measures=specs)
    assert _measure_violation(specs[0], d.pose0[1], d.pose0[0]) > 4.0
    pose = settle_measures(d, d.pose0)
    for s in specs:
        assert _measure_violation(s, pose[s.ia], pose[s.ib]) == 0.0, s
    np.testing.assert_array_equal(pose[0], d.pose0[0])
    shift = pose[1, :2] - d.pose0[1, :2]
    assert np.hypot(*shift) > 4.0
    for k in (2, 3):
        np.testing.assert_allclose(pose[k, :2] - d.pose0[k, :2], shift)
    rings = [_ring_geom(d, pose[k], k) for k in range(len(d.parts))]
    for a, b in itertools.combinations(rings, 2):
        assert a is not None and b is not None
        assert a.intersection(b).area == 0.0


def test_settle_pose_label_room_keeps_a_met_hard_measure() -> None:
    """The label settle runs after the measure settle and must not buy a
    label spot with the decap's DRC error."""
    from precis.pcb.topoplace import settle_pose
    from precis.pcb.topoplace.proxy import _measure_violation

    d, spec = _decap_fixture()
    pose = settle_pose(d, d.pose0)
    assert _measure_violation(spec, pose[1], pose[0]) == 0.0


def test_settle_label_room_never_pushes_past_edge_clearance() -> None:
    """gr538618: a settle move must never take a part's courtyard
    outside the board outline minus the edge line the anneal enforces
    (``_EDGE_CLEAR_MM`` off the exterior). esp32c3-s2's C7 was nudged
    outward to 0.29 mm off the top edge — the acceptance tuple let the
    walk term dominate the edge axis — minting board_edge_clearance
    x4, courtyard_edge_clearance x1 and outline_containment x1 where
    the anneal's pose had none. Here the ONLY label-freeing nudge goes
    north over the line: it must be refused, the part keeps its
    compliant pose, and the label conflict stays honestly reported."""
    import dataclasses

    from precis.pcb.topoplace import label_room_walks, settle_label_room
    from precis.pcb.topoplace.proxy import (
        _outline_geom,
        _ring_edge_violation,
        _ring_geom,
    )

    spec = [
        ("A1", 15.0, 27.75, 0.9),  # ring top 28.65 — compliant, edge axis 0
        ("B1", 15.0, 25.6, 0.9),  # covers every bottom canonical spot
        ("L1", 12.0, 28.4, 0.9),  # covers left; blocks west moves
        ("R1", 18.0, 28.4, 0.9),  # covers right; blocks east moves
        ("W1", 11.5, 26.0, 0.9),  # cages the southwest escape
        ("E1", 18.5, 26.0, 0.9),  # cages the southeast escape
    ]
    d = _margin_label_fixture(spec)
    # The blockers are pinned so the repair cannot free a spot by moving
    # THEM — A1's own nudges are the only lever. The cage leaves no
    # legal out: southward moves overlap B1 (0.35mm gap), lateral and
    # diagonal ones overlap L1/R1/W1/E1, and the first nudge that frees
    # a bottom label box is N+2.0 — which hangs the ring 0.65mm past
    # the y=30 edge while the part's pin stays inside, the exact
    # overhang shape the pads-only check missed on s2.
    d = dataclasses.replace(d, fixed=np.asarray([False, True, True, True, True, True]))
    walk0, _edge0 = label_room_walks(d, d.pose0)
    assert walk0[0] > 0.0

    repaired = settle_label_room(d, d.pose0)
    walk1, _edge1 = label_room_walks(d, repaired)
    outline = _outline_geom(d)
    ring = _ring_geom(d, repaired[0], 0)
    assert outline is not None and ring is not None
    assert _ring_edge_violation(outline, ring) == 0.0
    assert outline.covers(ring)
    # no legal move exists — the part keeps its pose and the conflict
    # is left reported, not repaired into a DRC finding
    assert repaired[0, 0] == pytest.approx(15.0)
    assert repaired[0, 1] == pytest.approx(27.75)
    assert walk1[0] > 0.0


def test_overlap_proxy_parity_over_staged_moves() -> None:
    """gr513870: ``_pair_overlap``'s disjoint scan runs on cached
    bounds/side arrays patched by the stage — the staged and committed
    ``overlap`` totals must stay equal to the ``overlap_area`` recompute
    through side flips, stacked staged moves (a swap's first half) and
    revert/commit interleaving."""
    from precis.pcb.topoplace.proxy import overlap_area

    def _area(d: Any, proxy: ProxyState) -> float:
        pose = proxy.pose.copy()
        for p, row in proxy._stage.items():
            pose[p] = row
        return overlap_area(d, pose)

    d, _inst, _nets = synth_design(seed=0)
    w = ObjectiveShares(crossings=0.5, ratsnest=0.1, overlap=0.4)
    proxy = ProxyState(d, d.pose0, w)
    rng = np.random.default_rng(4)
    movable = np.flatnonzero(~d.fixed)
    for i in range(400):
        p = int(movable[rng.integers(len(movable))])
        row = proxy.pose[p].copy()
        kind = rng.random()
        if kind < 0.7:
            row[0] += rng.normal(0.0, 3.0)
            row[1] += rng.normal(0.0, 3.0)
        elif kind < 0.9:
            row[2] = (row[2] + float(rng.integers(1, 4))) % 4
        else:
            row[3] = 1.0 - row[3]
        proxy.try_move(p, row)
        assert proxy.overlap == pytest.approx(_area(d, proxy)), i
        (proxy.commit if rng.random() < 0.5 else proxy.revert)()
        if i % 25 == 0:
            assert proxy.overlap == pytest.approx(overlap_area(d, proxy.pose)), i
    assert proxy.overlap == pytest.approx(overlap_area(d, proxy.pose))


def test_placer_beats_autoplace_at_equal_evals_median() -> None:
    """On the synthetic board, best proxy crossings ≤ autoplace's at
    equal eval count — printed per seed, hard on the median."""
    effort = 6000
    ours: list[int] = []
    theirs: list[int] = []
    for seed in range(5):
        d, inst, nets = synth_design(seed=seed)
        res = topo_place(d, objective=W, effort=effort, seed=seed)
        ap = autoplace(inst, nets, iters=effort, seed=seed)
        # autoplace's best pose read under the SAME pin-level metric —
        # centroid airwires hide the fan-in crossings two real pins
        # exhibit, so the comparison must happen inside this metric.
        ap_pose = d.pose0.copy()
        for i, row in enumerate(inst):
            ap_pose[i, 0], ap_pose[i, 1] = ap.positions[row["refdes"]]
        ac, _al = evaluate(d, ap_pose)
        ours.append(res.crossings)
        theirs.append(ac)
        print(
            f"\nseed {seed}: topo={res.crossings} "
            f"autoplace={ac} (centroid {ap.crossings_after})"
        )
    assert float(np.median(ours)) <= float(np.median(theirs))


def test_archive_diversity_invariant_and_size() -> None:
    """After a run: no two elites violate the diversity rule
    (≥ d_min displacement or ≥ 10 % rotations), count ≤ archive_size."""
    d, _inst, _nets = synth_design(seed=0)
    res = topo_place(d, objective=W, effort=4000, seed=0)
    elites = res.elites
    assert len(elites) <= 8
    for i, a in enumerate(elites):
        for b in elites[i + 1 :]:
            assert (
                mean_displacement(a.pose, b.pose) >= 2.0
                or rotations_differ(a.pose, b.pose) >= 0.1
            )


def test_determinism_same_seed_identical_elites_json() -> None:
    d, _inst, _nets = synth_design(seed=0)
    a = topo_place(d, objective=W, effort=3000, seed=7)
    b = topo_place(d, objective=W, effort=3000, seed=7)
    assert a.elites_json() == b.elites_json()
    assert (a.best_pose == b.best_pose).all()


def test_replace_never_ends_worse_than_the_stored_placement() -> None:
    """gr520372: a re-place starts from the stored placement, and that
    pose is the one to beat. The incumbent race used to rank only
    post-move poses, and the shove/legalize settle could cost the
    winner its edge, so a short run could return (and the job write) a
    placement worse than the board already had — 3 of these 8 seeds did
    (952, 951, 954 crossings over a stored 950). A strong stored pose (a
    settled 3000-eval result) re-placed at a small effort must come back
    no worse on (crossings, cost); when nothing beat it the result says
    it kept the start."""
    import dataclasses

    d, _inst, _nets = synth_design(seed=2)
    stored = topo_place(d, objective=W, effort=3000, seed=0).best_pose
    d2 = dataclasses.replace(d, pose0=stored)
    start = ProxyState(d2, stored, W)
    kept = 0
    for seed in range(8):
        res = topo_place(d2, objective=W, effort=200, seed=seed)
        assert res.crossings <= start.crossings, seed
        if res.crossings == start.crossings:
            assert res.best_cost <= start.cost + 1e-9, seed
        kept += res.kept_start
    assert kept, "no small run kept the strong stored pose"


def test_topo_place_event_reports_restarts() -> None:
    """gr513870's per-round progress: an ``event`` sink gets one line
    per anneal restart plus the closing line — a silent default keeps
    the library quiet."""
    d, _inst, _nets = synth_design(seed=0)
    lines: list[str] = []
    topo_place(d, objective=W, effort=2600, seed=0, event=lines.append)
    assert any(l.startswith("place: restart 1/") for l in lines)
    assert any(l.startswith("place: done —") for l in lines)


# ── the shove↔legalize post-pass loop (_settle_pose) ────────────────


def test_settle_pose_zero_move_pass_exits_immediately() -> None:
    """A pair-clean pose: one violation-free shove (0 rounds) plus one
    no-movement legalize sweep and the loop is done — its normal exit
    is the fixed point, nowhere near the cap. example_a's authored
    A2/C2 sit 0.2 mm courtyard-to-courtyard — under the hard silk pair
    gap (topo-silk-spacing) the first settle legitimately separates
    them, so the clean seed is the settle's own output."""
    d = example_a()
    settled, _s0, l0 = placer._settle_pose(d, d.pose0)
    assert l0.resolved
    pose, shoved, legal = placer._settle_pose(d, settled)
    np.testing.assert_array_equal(pose, settled)
    assert shoved.rounds == 0 and shoved.moved_mm == 0.0
    assert legal.rounds == 1 and legal.resolved


def test_settle_pose_capped_at_post_passes_on_a_fight(monkeypatch) -> None:
    """Movement that keeps shrinking the residual count but never
    finishes runs the full ``_POST_PASSES`` — the cap is the only thing
    that stops an endless shove↔legalize fight (a cap, not a runaway
    guard: it is 50, not 4)."""
    calls = {"s": 0}

    def fake_shove(d, pose):
        calls["s"] += 1
        p = pose.copy()
        p[0, 0] += 0.01
        return ShoveResult(
            pose=p,
            resolved=False,
            rounds=1,
            moved_mm=0.01,
            lp_optimum_mm=0.01,
            min_tri_area_mm2=1.0,
            remaining=((0, 1, 0.5),) * max(1, 60 - calls["s"]),
        )

    def fake_legalize(d, pose):
        p = pose.copy()
        p[0, 1] += 0.01
        return LegalizeResult(
            pose=p, resolved=False, rounds=1, moved_mm=0.01, remaining=()
        )

    monkeypatch.setattr("precis.pcb.topoplace.shove.shove_pose", fake_shove)
    monkeypatch.setattr("precis.pcb.topoplace.legalize.legalize_pose", fake_legalize)
    d = example_a()
    placer._settle_pose(d, d.pose0)
    assert calls["s"] == placer._POST_PASSES == 50


def test_settle_pose_stops_when_conflicts_stop_improving(monkeypatch) -> None:
    """A pass that moves parts but leaves the residual count unchanged
    is a fight, not progress — the loop breaks after it, far under the
    cap, and the merged result still accumulates every pass's fields."""
    calls = {"s": 0}

    def fake_shove(d, pose):
        calls["s"] += 1
        p = pose.copy()
        p[0, 0] += 0.01
        return ShoveResult(
            pose=p,
            resolved=False,
            rounds=1,
            moved_mm=0.01,
            lp_optimum_mm=0.01,
            min_tri_area_mm2=1.0,
            reason="infeasible" if calls["s"] == 1 else "",
            remaining=((0, 1, 0.5),),
        )

    def fake_legalize(d, pose):
        p = pose.copy()
        p[0, 1] += 0.01
        return LegalizeResult(
            pose=p, resolved=False, rounds=1, moved_mm=0.01, remaining=()
        )

    monkeypatch.setattr("precis.pcb.topoplace.shove.shove_pose", fake_shove)
    monkeypatch.setattr("precis.pcb.topoplace.legalize.legalize_pose", fake_legalize)
    d = example_a()
    _pose, shoved, legal = placer._settle_pose(d, d.pose0)
    assert calls["s"] == 2
    # accumulation: two passes, not the last one alone
    assert shoved.rounds == 2 and shoved.moved_mm == pytest.approx(0.02)
    assert shoved.reason == "infeasible"
    assert legal.rounds == 2 and legal.moved_mm == pytest.approx(0.02)


def test_settle_pose_returns_the_best_pose_seen(monkeypatch) -> None:
    """A regressing pass does not keep the board: when pass 2 leaves
    MORE residuals than pass 1, the loop returns pass 1's pose — and
    the merged result reads its ``remaining`` off that pass while
    ``lp_optimum_mm`` stays the FIRST pass's objective and
    ``min_tri_area_mm2`` the worst any pass saw."""
    calls = {"s": 0}
    poses_out: list[np.ndarray] = []

    def fake_shove(d, pose):
        calls["s"] += 1
        p = pose.copy()
        p[0, 0] += 0.5
        return ShoveResult(
            pose=p,
            resolved=False,
            rounds=1,
            moved_mm=0.5,
            lp_optimum_mm=0.5 if calls["s"] == 1 else 9.9,
            min_tri_area_mm2=1.0 if calls["s"] == 1 else 0.25,
            remaining=((0, 1, 0.5),) * (2 if calls["s"] == 1 else 4),
        )

    def fake_legalize(d, pose):
        p = pose.copy()
        p[0, 1] += 0.5
        poses_out.append(p)
        return LegalizeResult(
            pose=p,
            resolved=False,
            rounds=1,
            moved_mm=0.5,
            remaining=((0, 1, 0.25),) if calls["s"] == 1 else ((0, 1, 0.25),) * 3,
        )

    monkeypatch.setattr("precis.pcb.topoplace.shove.shove_pose", fake_shove)
    monkeypatch.setattr("precis.pcb.topoplace.legalize.legalize_pose", fake_legalize)
    d = example_a()
    pose, shoved, legal = placer._settle_pose(d, d.pose0)
    # pass 1: 2+1 conflicts, pose P1; pass 2: 4+3 (no improvement) ->
    # break, and the returned pose is P1's, not P2's
    assert calls["s"] == 2
    np.testing.assert_array_equal(pose, poses_out[0])
    assert len(legal.remaining) == 1  # pass 1's residual, not pass 2's 3
    assert shoved.lp_optimum_mm == pytest.approx(0.5)  # first pass only
    assert shoved.min_tri_area_mm2 == pytest.approx(0.25)  # worst across
    assert shoved.rounds == 2 and shoved.moved_mm == pytest.approx(1.0)


def test_settle_pose_pose_hash_breaks_an_oscillation(monkeypatch) -> None:
    """shove pushes +x, legalize undoes it: the pose revisits a seen
    hash and the loop breaks BEFORE the next shove — a 2-cycle the
    conflict count alone would miss (the residuals here keep
    shrinking)."""
    calls = {"s": 0}

    def fake_shove(d, pose):
        calls["s"] += 1
        p = pose.copy()
        p[0, 0] += 0.5
        return ShoveResult(
            pose=p,
            resolved=False,
            rounds=1,
            moved_mm=0.5,
            lp_optimum_mm=0.5,
            min_tri_area_mm2=1.0,
            remaining=((0, 1, 0.5),) * (5 - calls["s"]),
        )

    def fake_legalize(d, pose):
        p = pose.copy()
        p[0, 0] -= 0.5
        return LegalizeResult(
            pose=p, resolved=False, rounds=1, moved_mm=0.5, remaining=()
        )

    monkeypatch.setattr("precis.pcb.topoplace.shove.shove_pose", fake_shove)
    monkeypatch.setattr("precis.pcb.topoplace.legalize.legalize_pose", fake_legalize)
    d = example_a()
    placer._settle_pose(d, d.pose0)
    assert calls["s"] == 1


def test_settle_pose_is_deterministic() -> None:
    """Same input pose → identical output: the stacked-pair example-A
    board settles to a byte-identical pose with identical accumulated
    fields on a repeat run."""
    d = example_a()
    pose = d.pose0.copy()
    pose[1] = pose[0].copy()
    a = placer._settle_pose(d, pose)
    b = placer._settle_pose(d, pose)
    np.testing.assert_array_equal(a[0], b[0])
    assert a[1].moved_mm == b[1].moved_mm
    assert a[2].moved_mm == b[2].moved_mm


def test_placement_sides_top_forbids_bottom_flips() -> None:
    """gr520370: sides='top' never lands a part on the bottom — the flip
    move is only offered to a part already down (to come up), and the
    per-proposal check keeps a top part from ever going down."""
    import dataclasses

    d, _inst, _nets = synth_design(seed=0)
    d = dataclasses.replace(d, sides="top")
    res = topo_place(d, objective=W, effort=3000, seed=1)
    assert (res.best_pose[:, 3] < 0.5).all()
    for e in res.elites:
        assert (e.pose[:, 3] < 0.5).all()


def test_fixed_side_strips_the_back_side_freedom() -> None:
    """gr520370's per-part lock: ``fixed_side`` on an instance denies the
    BACK_SIDE freedom — the placer can't flip it, whatever the board's
    placement_sides says."""
    from precis.pcb.ir import from_graph
    from precis.pcb.topoplace.design import FREEDOM_BACK_SIDE, design_from_ir

    ir = from_graph(
        {
            "instances": [
                {"refdes": "A", "pins": [{"name": "1"}], "fixed_side": True},
                {"refdes": "B", "pins": [{"name": "1"}]},
            ],
            "nets": [
                {
                    "name": "N1",
                    "members": [
                        {"refdes": "A", "pin": "1"},
                        {"refdes": "B", "pin": "1"},
                    ],
                }
            ],
        }
    )
    td = design_from_ir(ir, {})
    assert not (td.freedoms[0] & FREEDOM_BACK_SIDE)
    assert td.freedoms[1] & FREEDOM_BACK_SIDE
    td_top = design_from_ir(ir, {}, placement_sides="top")
    assert td_top.sides == "top"


def test_authored_proximity_measure_pulls_parts_together() -> None:
    """gr520546: an authored ``near()``/proximity measure is priced in
    the proxy objective. U1 and U2 start ~20 mm apart with NO net
    between them — nothing but the measure can pull them inside its
    2 mm bound."""
    import dataclasses

    from precis.pcb.topoplace.design import TopoMeasure

    d, _inst, _nets = synth_design(seed=0, n_parts=4, n_nets=0, n_multinet=0, n_fixed=0)
    d = dataclasses.replace(
        d,
        measures=(
            TopoMeasure(ia=0, ib=1, bound="upper", goal_mm=2.0, weight=1.0, hard=True),
        ),
    )
    dist0 = float(np.hypot(*(d.pose0[0, :2] - d.pose0[1, :2])))
    assert dist0 > 10.0
    res = topo_place(
        d,
        objective=ObjectiveShares(
            crossings=0.0, ratsnest=0.0, overlap=0.1, measures=1.0
        ),
        effort=2000,
        seed=1,
    )
    dist1 = float(np.hypot(*(res.best_pose[0, :2] - res.best_pose[1, :2])))
    assert dist1 <= 2.0 + 0.5  # inside the bound, up to the MTV epsilon


def test_rigid_group_keeps_authored_offsets_through_the_anneal() -> None:
    """gr520546: an authored ``group``/``group_offset`` pair is ONE rigid
    body. A net pulls the group across the board while free parts sit
    in the way — translate, rotate, shove and legalize must all keep
    the authored member offset and relative rotation intact."""
    import dataclasses

    from precis.pcb.topoplace.design import qrot

    d, _inst, _nets = synth_design(seed=0, n_parts=6, n_nets=4, n_multinet=0, n_fixed=0)
    # U1/U2 are one assembly: U2 sits +12 mm x off the group's anchor,
    # same rotation.
    group_of = np.full(len(d.parts), -1, dtype=np.int32)
    group_of[0] = group_of[1] = 0
    group_off = np.zeros((len(d.parts), 3), dtype=np.float64)
    group_off[1] = (12.0, 0.0, 0.0)
    pose0 = d.pose0.copy()
    pose0[0] = (10.0, 10.0, 0.0, 0.0)
    pose0[1] = (22.0, 10.0, 0.0, 0.0)  # the authored shape at arot 0
    d = dataclasses.replace(
        d,
        group_of=group_of,
        group_members=((0, 1),),
        group_off=group_off,
        pose0=pose0,
    )
    res = topo_place(d, objective=ObjectiveShares(), effort=3000, seed=3)
    dx = res.best_pose[1, :2] - res.best_pose[0, :2]
    arot_q = round(res.best_pose[0, 2]) % 4
    np.testing.assert_allclose(qrot(dx, -arot_q), (12.0, 0.0), atol=1e-6)
    assert round(res.best_pose[0, 2]) % 4 == round(res.best_pose[1, 2]) % 4


def test_fixed_group_member_pins_the_whole_group() -> None:
    """gr520546: a fixed member pins the group — every member's pose0
    is rebuilt rigidly off the pinned member's anchor (J2's authored
    xy is overridden by its ``group_offset``), and all members land in
    ``d.fixed`` so no mover touches them."""
    from precis.pcb.ir import from_graph
    from precis.pcb.topoplace.design import design_from_ir

    ir = from_graph(
        {
            "instances": [
                {
                    "refdes": "J1",
                    "pins": [{"name": "1"}],
                    "x": 5.0,
                    "y": 5.0,
                    "rot": 0.0,
                    "fixed": "xy",
                    "group": "hdr",
                    "group_offset": {"x": 0.0, "y": 0.0, "rot": 0.0},
                },
                {
                    "refdes": "J2",
                    "pins": [{"name": "1"}],
                    "x": 40.0,
                    "y": 30.0,  # authored xy IGNORED: the group shape wins
                    "group": "hdr",
                    "group_offset": {"x": 15.24, "y": 0.0, "rot": 0.0},
                },
                {
                    "refdes": "R1",
                    "pins": [{"name": "1"}],
                    "x": 20.0,
                    "y": 8.0,
                },
            ],
            "nets": [],
        }
    )
    td = design_from_ir(ir, {})
    assert td.fixed[0] and td.fixed[1] and not td.fixed[2]
    assert td.group_members == ((0, 1),)
    np.testing.assert_allclose(td.pose0[0, :2], (5.0, 5.0))
    np.testing.assert_allclose(td.pose0[1, :2], (20.24, 5.0))
    assert td.pose0[1, 2] == 0.0


def test_group_without_offsets_or_poses_stays_independent() -> None:
    """GRIPE_FIX (gr520546 review): a group is rigid only when its shape
    is recoverable — every member carrying an authored ``group_offset``
    or a finite pose0. A fresh board's group (no offsets, nothing
    placed) otherwise collapsed every member onto the anchor's xy —
    a permanent stack the legalizer, shove LP and ``_part_hit`` all skip
    (same-group pairs), invisible to every gate. The members place
    independently and the run warns once, naming the group."""
    from precis.pcb.ir import from_graph
    from precis.pcb.topoplace.design import design_from_ir

    ir = from_graph(
        {
            "instances": [
                {"refdes": r, "pins": [{"name": "1"}], "group": "hdr"}
                for r in ("J1", "J2", "J3")
            ],
            "nets": [],
        }
    )
    td = design_from_ir(ir, {})
    assert td.group_members == ()
    assert (td.group_of == -1).all()
    assert len(td.warnings) == 1
    w = td.warnings[0]
    assert "'hdr'" in w and "group_offset" in w and "place once" in w


def test_group_of_posed_members_stays_rigid() -> None:
    """The gate's keep side: no ``group_offset`` authored anywhere, but
    every member placed — the pose0-implied shape still makes one rigid
    body, exactly as before (only a member with NEITHER source of shape
    demotes the group)."""
    from precis.pcb.ir import from_graph
    from precis.pcb.topoplace.design import design_from_ir

    ir = from_graph(
        {
            "instances": [
                {
                    "refdes": "J1",
                    "pins": [{"name": "1"}],
                    "x": 5.0,
                    "y": 5.0,
                    "group": "hdr",
                },
                {
                    "refdes": "J2",
                    "pins": [{"name": "1"}],
                    "x": 15.0,
                    "y": 5.0,
                    "group": "hdr",
                },
                {
                    "refdes": "J3",
                    "pins": [{"name": "1"}],
                    "x": 25.0,
                    "y": 5.0,
                    "group": "hdr",
                },
            ],
            "nets": [],
        }
    )
    td = design_from_ir(ir, {})
    assert td.group_members == ((0, 1, 2),)
    assert td.warnings == ()
    np.testing.assert_allclose(td.group_off[1, :2], (10.0, 0.0), atol=1e-9)
    np.testing.assert_allclose(td.group_off[2, :2], (20.0, 0.0), atol=1e-9)


def test_partly_poseless_group_stays_independent() -> None:
    """One placed member does NOT rescue a group whose other members
    carry neither offset nor pose: before the gate, the poseless
    members' nonfinite rel read as offset (0,0) and the normalisation
    stacked them onto the anchor's xy."""
    from precis.pcb.ir import from_graph
    from precis.pcb.topoplace.design import design_from_ir

    ir = from_graph(
        {
            "instances": [
                {
                    "refdes": "J1",
                    "pins": [{"name": "1"}],
                    "x": 5.0,
                    "y": 5.0,
                    "group": "hdr",
                },
                {"refdes": "J2", "pins": [{"name": "1"}], "group": "hdr"},
                {"refdes": "J3", "pins": [{"name": "1"}], "group": "hdr"},
            ],
            "nets": [],
        }
    )
    td = design_from_ir(ir, {})
    assert td.group_members == ()
    assert (td.group_of == -1).all()
    assert len(td.warnings) == 1
    assert "'hdr'" in td.warnings[0]
    # the placed member's pose is untouched — no anchor normalisation ran
    np.testing.assert_allclose(td.pose0[0, :2], (5.0, 5.0))


def test_ungrouped_run_matches_main_golden_pose_bytewise() -> None:
    """gr520546 review (SHOULD-FIX 3): with no authored groups a seeded
    run reproduces origin/main's ``best_pose`` byte for byte — the
    probe/translate movers stage ``clip(pose+delta)`` again and
    ``_scatter`` seeds a fixed part's NaN pose once more (pre-revert it
    stayed NaN and ``_settle_pose`` crashed inside shapely on this very
    board). Expected captured from origin/main's ``topo_place`` on this
    design: synth seed 0, 8 parts / 4 nets / 1 fixed (part 0's pose0 xy
    blanked), effort 2000, placer seed 7. Re-captured 2026-11-02 under
    the hard silk pair gap (topo-silk-spacing): the pre-rule pose left
    parts 5/6 inside ``pair_req_mm``, which the legaliser now separates
    — the pre-settle rows are unchanged bytewise. Re-captured again
    2026-11-03 under the vendor-silk ruling (JLC-min ring target 0.275,
    courtyard pair_req 1.00 → 0.20): the same-side pair 5/6 settles
    closer — rows 5/6 shifted, the rest is unchanged bytewise, and the
    remaining ring overlaps are all cross-side pairs the gap rule
    exempts. Re-captured 2026-11-04 with the fabrication reserve: only
    row 6 changes, by at most 0.000225 mm, to keep export rounding room;
    the other seven rows remain bytewise identical."""
    d, _inst, _nets = synth_design(seed=0, n_parts=8, n_nets=4, n_multinet=0, n_fixed=1)
    d.pose0[0, :2] = np.nan
    res = topo_place(d, objective=W, effort=2000, seed=7)
    expected = np.asarray(
        [
            [62.259355727257365, 88.9269524950184, 0.0, 0.0],
            [65.67894204934903, 97.3579915676985, 0.0, 1.0],
            [61.750907396549245, 89.30112262516882, 1.0, 1.0],
            [1.0528827910690437, 74.77661530325685, 0.0, 0.0],
            [59.92735564922514, 10.684827235968266, 1.0, 0.0],
            [1.083642436802397, 74.82382768731681, 2.0, 1.0],
            [2.0025166849133806, 72.82260385452521, 1.0, 1.0],
            [60.33448286642105, 11.092021830309974, 0.0, 1.0],
        ]
    )
    np.testing.assert_array_equal(res.best_pose, expected)


def test_all_fixed_design_short_circuits_the_anneal() -> None:
    """gr524239: a design with nothing movable must not burn the restart
    schedule — the pre-fix loop rebuilt the proxy state per restart and
    reported 0 evals anyway (~26 s on taz-leaf's all-fixed board).
    topo_place settles the authored pose once and returns the honest
    0-eval, 0-restart result."""
    d, _inst, _nets = synth_design(seed=0, n_parts=10, n_nets=20, n_fixed=10)
    result = topo_place(d, objective=W, effort=4000, seed=1)
    assert result.evals == 0
    assert result.restarts == 0
    assert result.best_pose is not None
    assert len(result.best_pose) == 10


def test_margin_edge_axis_prices_vendor_silk_extent_not_the_ring() -> None:
    """Reto 2026-11-03 ruling 4: the margin edge axis measures a part's
    OWN silk extent (the vendor footprint's ink hull) against the silk
    edge margin — so a part whose vendor silk reaches past its courtyard
    still keeps everything inside the board. A ring-outlined part keeps
    the courtyard-ring + edge_req pricing."""
    from precis.pcb import DEFAULT_STACKUP
    from precis.pcb.ir import from_graph
    from precis.pcb.topoplace.design import design_from_ir

    graph = {
        "instances": [
            {"refdes": "L1", "x": 10.0, "y": 8.0},
        ],
        "nets": [
            {
                "name": "N0",
                "members": [{"refdes": "L1", "pin": "1"}],
            },
            {
                "name": "N1",
                "members": [{"refdes": "L1", "pin": "2"}],
            },
        ],
    }
    # Pads tight around the origin; a vendor silk rectangle reaching far
    # -x past the courtyard — the ink edge is what the edge margin cuts.
    fp = {
        "pads": [
            {"number": "1", "shape": "rect", "x": -0.5, "y": 0.0, "w": 0.6, "h": 1.0},
            {"number": "2", "shape": "rect", "x": 0.5, "y": 0.0, "w": 0.6, "h": 1.0},
        ],
        "pin_map": {},
        "graphics": [
            {
                "kind": "polyline",
                "layer": "F.SilkS",
                "width_mm": 0.12,
                "points": [
                    [-4.0, -1.5],
                    [4.0, -1.5],
                    [4.0, 1.5],
                    [-4.0, 1.5],
                    [-4.0, -1.5],
                ],
            }
        ],
    }
    ir = from_graph(
        graph,
        stackup=DEFAULT_STACKUP,
        outline=[[0.0, 0.0], [30.0, 0.0], [30.0, 30.0], [0.0, 30.0]],
    )
    d = design_from_ir(ir, {"L1": fp})
    assert len(d.part_silk) == 1 and len(d.part_silk[0]) >= 4
    # The ink extent spans x in [-4.06, 4.06] part-local; at pose0
    # (10,8) the -x ink edge sits 10 - 4.06 = 5.94 off the cut edge —
    # inside the 0.6 silk edge margin, so the edge axis reads 0 against
    # the EXTENT. Against the courtyard ring + edge_req it would also
    # pass here; the discrimination test is a part at the edge.
    from precis.pcb.topoplace.proxy import margin_total

    pose = d.pose0.copy()
    pose[0, 0] = 5.0  # ink -x edge now 5.0 - 4.06 = 0.94 — still legal
    assert margin_total(d, pose) == pytest.approx(0.0, abs=1e-9)
    pose[0, 0] = 4.0  # ink -x edge 4.0 - 4.06 < 0.6 margin -> shortfall
    assert margin_total(d, pose) > 0.0


def test_design_from_ir_part_silk_empty_without_graphics() -> None:
    """A footprint with no silk graphics contributes no extent — the
    edge axis then prices the courtyard ring as before."""
    from precis.pcb import DEFAULT_STACKUP
    from precis.pcb.ir import from_graph
    from precis.pcb.topoplace.design import design_from_ir

    graph = {
        "instances": [{"refdes": "R1", "x": 10.0, "y": 10.0}],
        "nets": [
            {"name": "N0", "members": [{"refdes": "R1", "pin": "1"}]},
            {"name": "N1", "members": [{"refdes": "R1", "pin": "2"}]},
        ],
    }
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    d = design_from_ir(ir, {"R1": {"pads": [], "pin_map": {}, "graphics": []}})
    assert len(d.part_silk) == 1 and len(d.part_silk[0]) == 0
