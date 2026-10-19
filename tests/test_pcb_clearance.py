"""The clearance kernel (``docs/backlog/pcb-vector-router.md`` slice 2):
closed-form distances against dense sampling, outward polygons, and the
kernel against the independent O(n^2) oracle
(:func:`precis.pcb.drc.clearance_violations_naive`) over randomized boards
with arc tracks and rect/obround/polygon pads."""

from __future__ import annotations

import math
import random
from types import SimpleNamespace
from typing import Any

import pytest
from shapely.geometry import Point

from precis.pcb import DEFAULT_STACKUP, drc
from precis.pcb import clearance as k
from precis.pcb.capabilities import capability_for
from precis.pcb.realize import RealizeConfig, _net_layer_clearances
from precis.pcb.rules import resolve_net_rules

_LAYERS = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]


def _sampled_arc(arc: k.Arc, n: int = 20000) -> list[tuple[float, float]]:
    return [arc.point_at(arc.a0 + arc.sweep * i / n) for i in range(n + 1)]


def _random_arc(rng: random.Random) -> k.Arc:
    return k.Arc(
        rng.uniform(-2, 2),
        rng.uniform(-2, 2),
        rng.uniform(0.2, 3.0),
        rng.uniform(-math.pi, math.pi),
        rng.choice([-1, 1]) * rng.uniform(0.1, 2 * math.pi - 0.1),
    )


@pytest.mark.parametrize("seed", range(40))
def test_point_segment_and_arc_distances_match_dense_sampling(seed: int) -> None:
    rng = random.Random(seed)
    arc, other = _random_arc(rng), _random_arc(rng)
    p = (rng.uniform(-4, 4), rng.uniform(-4, 4))
    a = (rng.uniform(-4, 4), rng.uniform(-4, 4))
    b = (rng.uniform(-4, 4), rng.uniform(-4, 4))
    pts = _sampled_arc(arc)
    tol = arc.radius * abs(arc.sweep) / 20000 + 1e-9
    assert k.point_arc(p, arc) == pytest.approx(
        min(math.dist(p, q) for q in pts), abs=tol
    )
    assert k.segment_arc(a, b, arc) == pytest.approx(
        min(k.point_segment(q, a, b) for q in pts), abs=tol
    )
    assert k.arc_arc(arc, other) == pytest.approx(
        min(k.point_arc(q, other) for q in pts), abs=tol
    )


def test_arc_contains_its_endpoints_and_not_the_far_side() -> None:
    quarter = k.Arc(0.0, 0.0, 1.0, 0.0, math.pi / 2)
    assert quarter.contains_angle(0.0) and quarter.contains_angle(math.pi / 2)
    assert not quarter.contains_angle(math.pi)
    clockwise = k.Arc(0.0, 0.0, 1.0, 0.0, -math.pi / 2)
    assert clockwise.contains_angle(-math.pi / 4)
    assert not clockwise.contains_angle(math.pi / 4)


def test_arc_track_outer_side_is_measured_exactly_not_on_chords() -> None:
    """The defect the spec names: 15° chords read the outer side of an
    R = 5 mm arc up to R(1 - cos 7.5°) ≈ 43 µm too far... from a point
    outside, so a real violation could pass."""
    arc_track = {
        "ctype": "track",
        "layer": "F.Cu",
        "net": "A",
        "width_mm": 0.2,
        "segments": [
            {
                "shape": "arc",
                "start": [5.0, 0.0],
                "end": [0.0, 5.0],
                "center": [0.0, 0.0],
                "cw": False,
            }
        ],
    }
    # A via just outside the arc at 7.5° (mid-chord of the first 15° chord).
    theta = math.radians(7.5)
    gap_mm = 0.1
    r = 5.0 + 0.1 + gap_mm + 0.15
    via = {
        "ctype": "via",
        "net": "B",
        "x": r * math.cos(theta),
        "y": r * math.sin(theta),
        "dia_mm": 0.3,
        "drill_mm": 0.15,
    }
    track_body, via_body = k.body_of(arc_track), k.body_of(via)
    assert track_body is not None and via_body is not None
    assert k.gap(track_body, via_body) == pytest.approx(gap_mm, abs=1e-9)
    chord = drc._copper_item_polygon(arc_track)
    assert chord is not None
    # The old flattening read ~43 µm more room than there is.
    assert chord.distance(Point(via["x"], via["y"]).buffer(0.15)) - gap_mm > 0.04


@pytest.mark.parametrize("radius", [0.05, 0.3, 2.0, 10.0])
def test_outward_circle_contains_the_circle_within_the_sagitta(radius: float) -> None:
    poly = k.outward_circle(1.0, 2.0, radius)
    # Shrunk by 1e-9 relative: the reference polygon's vertices otherwise
    # land exactly on the tangent points, a float-rounding tie.
    circle = Point(1.0, 2.0).buffer(radius * (1 - 1e-9), quad_segs=256)
    assert poly.covers(circle)
    far = max(math.dist((1.0, 2.0), c) for c in poly.exterior.coords)
    assert far - radius <= k.DELTA_MM + 1e-12


def test_outward_segments_per_quadrant_at_the_5_um_sagitta() -> None:
    """Decision 7's note, recomputed: 5 per quadrant at R = 0.3 mm, 12 at
    R = 2 mm (deviation R(1/cos(pi/4q) - 1) <= 5 um)."""
    assert k.outward_segments_per_quadrant(0.3) == 5
    assert k.outward_segments_per_quadrant(2.0) == 12


# ── kernel vs the independent oracle ─────────────────────────────────────


def _rand_track(rng: random.Random, net: str, layer: str) -> dict[str, Any]:
    x, y = rng.uniform(0, 10), rng.uniform(0, 10)
    segs: list[dict[str, Any]] = []
    for _ in range(rng.randint(1, 3)):
        if rng.random() < 0.4:
            radius = rng.uniform(0.3, 3.0)
            a0 = rng.uniform(-math.pi, math.pi)
            sweep = rng.uniform(0.2, 2.5)
            cw = rng.random() < 0.5
            cx, cy = x - radius * math.cos(a0), y - radius * math.sin(a0)
            a1 = a0 - sweep if cw else a0 + sweep
            ex, ey = cx + radius * math.cos(a1), cy + radius * math.sin(a1)
            segs.append(
                {
                    "shape": "arc",
                    "start": [x, y],
                    "end": [ex, ey],
                    "center": [cx, cy],
                    "cw": cw,
                }
            )
        else:
            ex, ey = x + rng.uniform(-3, 3), y + rng.uniform(-3, 3)
            segs.append({"shape": "line", "start": [x, y], "end": [ex, ey]})
        x, y = ex, ey
    return {
        "ctype": "track",
        "layer": layer,
        "net": net,
        "width_mm": rng.uniform(0.1, 0.4),
        "segments": segs,
    }


def _rand_pad(rng: random.Random, net: str, layer: str) -> dict[str, Any]:
    shape = rng.choice(["rect", "rect", "circle", "obround", "polygon"])
    x, y = rng.uniform(0, 10), rng.uniform(0, 10)
    pad: dict[str, Any] = {
        "net": net,
        "layer": layer,
        "x": x,
        "y": y,
        "shape": shape,
        "w": rng.uniform(0.2, 2.0),
        "h": rng.uniform(0.2, 2.0),
    }
    if shape == "polygon":
        n = rng.randint(3, 6)
        rad = rng.uniform(0.2, 1.0)
        pad["poly"] = [
            [
                x + rad * math.cos(2 * math.pi * i / n),
                y + rad * math.sin(2 * math.pi * i / n),
            ]
            for i in range(n)
        ]
    if rng.random() < 0.15:
        pad["drill"] = 0.3
    return pad


def _rand_model(rng: random.Random) -> dict[str, Any]:
    nets = ["A", "B", "C"]
    copper: list[dict[str, Any]] = []
    for _ in range(rng.randint(1, 5)):
        copper.append(_rand_track(rng, rng.choice(nets), rng.choice(_LAYERS[::3])))
    for _ in range(rng.randint(0, 3)):
        copper.append(
            {
                "ctype": "via",
                "net": rng.choice(nets),
                "x": rng.uniform(0, 10),
                "y": rng.uniform(0, 10),
                "dia_mm": rng.uniform(0.3, 0.7),
                "drill_mm": 0.2,
            }
        )
    pads = [
        _rand_pad(rng, rng.choice(nets), rng.choice(_LAYERS[::3]))
        for _ in range(rng.randint(0, 4))
    ]
    return {"layers": _LAYERS, "copper": copper, "pads": pads}


@pytest.mark.parametrize("chunk", range(10))
def test_kernel_agrees_with_the_oracle_on_random_arc_and_pad_layouts(
    chunk: int,
) -> None:
    """≥ 500 layouts (10 chunks × 50): same violating pairs, same gaps."""
    for trial in range(50):
        rng = random.Random(chunk * 1000 + trial)
        model = _rand_model(rng)
        required = rng.uniform(0.1, 1.5)
        naive = {
            (i, j): g
            for i, j, g in drc.clearance_violations_naive(model, required_mm=required)
        }
        kernel = {
            (i, j): g
            for i, j, g, _layer in drc.clearance_pairs_indexed(
                model, required_mm=required
            )
        }
        assert set(naive) == set(kernel), (chunk, trial, set(naive) ^ set(kernel))
        for key, g in naive.items():
            assert g == pytest.approx(kernel[key], abs=1e-6), (chunk, trial, key)


@pytest.mark.parametrize(
    ("coated", "outer_mm"), [(True, 0.4), (False, 1.25)], ids=["coated", "uncoated"]
)
def test_router_grid_reads_the_same_ipc_column_as_drc(
    coated: bool, outer_mm: float
) -> None:
    """The router's per-layer reservation for a 250 V class is the DRC's
    floor on every layer: B1 inside, B4/B2 outside by the electrode-pad
    coat (Reto 2026-10-18, "Yes, IPC column per layer")."""
    cap = capability_for("4layer")
    hv = resolve_net_rules(
        "hv", layer_is_outer=True, fab_caps=cap, overrides={"working_voltage_v": 250}
    )
    plain = resolve_net_rules("", layer_is_outer=True, fab_caps=cap)
    pads = [SimpleNamespace(raw={"role": "electrode"})] if coated else []
    ir: Any = SimpleNamespace(stackup=list(DEFAULT_STACKUP), footprint_pads=pads)
    got = _net_layer_clearances(ir, {1: hv, 2: plain}, RealizeConfig())
    assert got == pytest.approx(
        {(1, 0): outer_mm, (1, 1): 0.2, (1, 2): 0.2, (1, 3): outer_mm}
    )
