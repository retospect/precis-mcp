"""Plaza packing (slice 2 of docs/backlog/pcb-global-plan-router.md):
the angular escape assignment for dense pad rows — ring detection, the
packed via lattice at the envelope, wedge-corridor planarity, and the
opt-in route path. Hand-built geometry first, then the measured
benchmarks (the QFP plaza member is fast; the EWOD replay is slow)."""

from __future__ import annotations

import itertools
import math

import numpy as np
import pytest

from precis.pcb import globalplan
from precis.pcb import session as pcb_session
from precis.pcb.maze import GridSpec


def _cross(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
    d: tuple[float, float],
) -> bool:
    """Segments a->b and c->d properly intersect (shared endpoints don't
    count — neighbouring stubs touch at the hub by design)."""

    def orient(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    o1, o2 = orient(a, b, c), orient(a, b, d)
    o3, o4 = orient(c, d, a), orient(c, d, b)
    return (o1 > 0) != (o2 > 0) and (o3 > 0) != (o4 > 0)


def test_pad_row_wraps_the_centre_or_is_rejected():
    circle = [
        (10 * math.cos(t), 10 * math.sin(t))
        for t in np.linspace(0, 2 * math.pi, 12, endpoint=False)
    ]
    assert globalplan._pad_row(circle) is not None
    # A square row is a pad row too — pads' radii wander ~40% between
    # side and corner and it must still qualify (the first cut's
    # circle-fit rejected exactly the QFP case).
    square = [
        (x, y)
        for side in range(4)
        for i in range(4)
        for x, y in (
            (5.0 * i - 7.5, 10.0)
            if side == 0
            else (10.0, 5.0 * i - 7.5)
            if side == 1
            else (5.0 * i - 7.5, -10.0)
            if side == 2
            else (-10.0, 5.0 * i - 7.5),
        )
    ]
    assert globalplan._pad_row(square) is not None
    # A straight connector row never wraps the centre.
    line = [(float(i), 0.0) for i in range(12)]
    assert globalplan._pad_row(line) is None
    # Under the minimum escape count there is no row to pack around.
    assert globalplan._pad_row(circle[:6]) is None


def test_via_lattice_packs_at_the_envelope():
    env = 0.65
    n, r0 = 20, 5.0
    # One ring's capacity: floor(2*pi*5/0.65) = 48 — 20 fits unstaggered.
    slots = globalplan.via_lattice([2 * math.pi * i / n for i in range(n)], r0, env)
    assert slots == [r0] * n
    # 60 > 48: the odd entries stagger out one hex row.
    n2 = 60
    slots2 = globalplan.via_lattice([2 * math.pi * i / n2 for i in range(n2)], r0, env)
    assert slots2[0] == r0 and slots2[1] == r0 + globalplan._VIA_STAGGER * env
    # Consecutive same-row slots keep the envelope: chord spacing on the
    # circle stays at or above the via envelope either way.
    for row in (0, 1):
        pts = [
            (
                (r0 + (globalplan._VIA_STAGGER * env if row else 0.0))
                * math.cos(2 * math.pi * i / n2),
                (r0 + (globalplan._VIA_STAGGER * env if row else 0.0))
                * math.sin(2 * math.pi * i / n2),
            )
            for i in range(row, n2, 2)
        ]
        for a, b in itertools.pairwise(pts):
            assert math.hypot(a[0] - b[0], a[1] - b[1]) >= env * 0.98


def test_angular_assignment_is_planar():
    """A hand-built ring: pads at radius 10, sources at radius 3 on the
    same angular order — the pad->via->source polylines must not cross."""
    n, r_pad, r_src, env = 16, 10.0, 3.0, 0.65
    centre = (0.0, 0.0)
    angles = [2 * math.pi * i / n for i in range(n)]
    pads = [(r_pad * math.cos(a), r_pad * math.sin(a)) for a in angles]
    # Sources arrive in the same cyclic order (the angular pin
    # assignment's job), landed anywhere inside the ring.
    sources = [(r_src * math.cos(a + 0.15), r_src * math.sin(a + 0.15)) for a in angles]
    ring_r = r_pad + 1.0 + env / 2.0 + 0.05
    slots = globalplan.via_lattice(angles, ring_r, env)
    vias = [(r * math.cos(a), r * math.sin(a)) for r, a in zip(slots, angles)]
    paths = list(zip(pads, vias, sources))
    for i in range(n):
        for j in range(i + 1, n):
            pa, va, sa = paths[i]
            pb, vb, sb = paths[j]
            for p, q in ((pa, va), (va, sa)):
                for r, s in ((pb, vb), (vb, sb)):
                    assert not _cross(p, q, r, s), (i, j)
    # Via slots stay in the margin, in pad-angle order: the i-th slot
    # sits on the i-th pad's own ray.
    for pad, via, _src in paths:
        assert math.atan2(via[1], via[0]) == pytest.approx(
            math.atan2(pad[1], pad[0]), abs=1e-9
        )


def test_plaza_mask_marks_stub_via_and_inner_run():
    spec = GridSpec(0.0, 0.0, 0.05, 60, 60, 2)
    mask = globalplan.plaza_mask(
        spec,
        pad_layer=0,
        inner_layer=1,
        pad_point=(0.5, 0.5),
        via_point=(1.5, 0.5),
        source_point=(1.5, 2.0),
        half_w=0.1,
        via_r=0.35,
    )
    # The stub lives only on the pad layer; the inner run only on the
    # inner layer; the via disk lands on every layer.
    assert mask[0, 10, 10] and mask[0, 10, 20]
    assert not mask[1, 10, 15]
    assert mask[1, 30, 30] and mask[1, 40, 30]
    assert mask[0, 10, 30] and mask[1, 10, 30]  # via disk, all layers


def test_content_hash_ignores_escape_when_unset():
    """``escape`` is a job param, not design content: absent it must not
    change the idempotency digest (the param-off invariant); set it and
    the job is a different run by design."""
    graph = {
        "instances": [{"refdes": "U1", "x": 1.0, "y": 2.0, "rot": 90.0}],
        "nets": [
            {
                "name": "N1",
                "net_class": "signal",
                "members": [{"refdes": "U1", "pin": "1"}],
            }
        ],
        "board": {"stackup": [{"name": "F.Cu"}, {"name": "B.Cu"}]},
    }
    base = pcb_session.content_hash(graph, {})
    assert pcb_session.content_hash(graph, {}) == base
    assert pcb_session.content_hash(graph, {"escape": "plaza"}) != base


# ── the plaza member and the replay (slow) ───────────────────────────────


@pytest.mark.slow
def test_plaza_escape_on_the_qfp_member():
    """The plaza member's plain result is 53/55 (warm "distance"). The
    opt-in must never score worse — the plain fallback always runs."""
    import time
    from dataclasses import replace

    from precis.pcb import realize as pcb_realize
    from tests.fixtures.pcb import qfp_fanout as qf

    spec = qf.FanoutSpec(pins_per_side=20, n_nets=55, sources="plaza")
    built = qf.build(spec)
    qf.warm_start(built, "distance")
    poses_before = tuple(
        (float(x), float(y)) for x, y in zip(built.ir.inst_x, built.ir.inst_y)
    )
    built.config = replace(built.config, escape="plaza")
    t0 = time.monotonic()
    rres = pcb_realize.realize(
        built.ir, config=built.config, footprints=built.footprints
    )
    seconds = time.monotonic() - t0
    routed = built.ir.n_segments - len(rres.unrouted)
    import json
    from pathlib import Path

    Path(".scratch/plaza-qfp-report.json").write_text(
        json.dumps(
            {
                "routed": routed,
                "total": built.ir.n_segments,
                "vias": len(rres.vias),
                "seconds": round(seconds, 1),
                "unrouted": sorted(
                    str(built.ir.net_name[int(built.ir.seg_net[s])])
                    for s in rres.unrouted
                ),
            }
        ),
        encoding="utf-8",
    )
    # Frozen parts untouched: the escape assigns corridors, never poses.
    poses_after = tuple(
        (float(x), float(y)) for x, y in zip(built.ir.inst_x, built.ir.inst_y)
    )
    assert poses_after == poses_before
    assert routed >= 53


@pytest.mark.slow
def test_plaza_escape_on_the_ewod_replay(store):
    """Same floor on the dogfood-6 replay: plain routes 51/55; the
    escape opt-in may cost time but never nets."""
    import time
    from dataclasses import replace

    from precis.pcb import realize
    from precis.workers.job_types.pcb_route import (
        _apply_pin_swap_warm_start,
        _resolve_pin_swap_groups,
    )
    from tests.test_pcb_escape_replay import _hydrate
    from tests.test_pcb_globalplan_ewod import _open_in2

    ir, graph, _features, footprints, fixed, config = _hydrate(store)
    _open_in2(ir, config)
    groups = _resolve_pin_swap_groups(ir, graph, [])
    _apply_pin_swap_warm_start(ir, groups, footprints, fixed, "distance")
    t0 = time.monotonic()
    rres = realize.realize(
        ir,
        config=replace(config, escape="plaza"),
        footprints=footprints,
        fixed_copper=fixed,
    )
    seconds = time.monotonic() - t0
    unrouted = sorted(str(ir.net_name[int(ir.seg_net[s])]) for s in rres.unrouted)
    routed = ir.n_segments - len(rres.unrouted)
    import json
    from pathlib import Path

    Path(".scratch/plaza-ewod-report.json").write_text(
        json.dumps(
            {
                "routed": routed,
                "total": ir.n_segments,
                "vias": len(rres.vias),
                "seconds": round(seconds, 1),
                "unrouted": unrouted,
            }
        ),
        encoding="utf-8",
    )
    assert routed >= 51
