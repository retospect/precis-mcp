"""precis.pcb.generators -- pure geometry, no DB (same style
tests/test_pcb_planes.py already uses for this subsystem).

Covers the load-bearing claims the store/handler-level
``test_pcb_ewod_generator.py`` can't cheaply assert: every electrode
polygon is a valid simple ring, the zigzag gap between same-row/same-col
neighbours is a CONSTANT ``gap`` (acceptance criterion 2's geometric
assertion), and no two DIFFERENT electrodes' copper (electrode body, neck
stub track, or via) ever overlaps -- a short-circuit in this kind, found
the hard way while building this slice (round-2 stress test: the naive
"3x3 via sub-grid" and "uniform-width diagonal stub" both clipped a
neighbour; :func:`precis.pcb.generators._plaza_capacity` and the (former)
tapered ``_stub_polygon`` were the fixes).

**pcb-pre-place-route-blocks Slice 2** moved the neck stub and the plaza
via off ``footprints[0]['pads']`` and onto :attr:`~precis.pcb.generators.
GeneratorExpansion.copper` as real ``track``/``via`` rows
(:func:`precis.pcb.generators._stub_track_row`/:func:`~precis.pcb.
generators._via_row`) — ``pads`` now carries exactly ONE row per pin (the
electrode body). Tests below that used to find the stub/via among a
pin's several pads now read them off ``exp.copper`` instead
(:func:`_copper_by_net`/:func:`_copper_shape`); the cross-net-overlap
sweep folds copper shapes in alongside pad shapes so the "no short"
property still covers the whole fabric, not just the bodies.

**Rulings 2026-09-19 item 11** adds a THIRD copper row per driven
electrode: a B.Cu breakout stub running from the plaza via outward
(:func:`precis.pcb.generators._breakout_track_row`). It sits on a
DIFFERENT physical layer (B.Cu) from the electrode body/neck (F.Cu), so
:func:`_copper_shape`/:func:`_all_shapes` now carry each shape's own
layer set alongside its geometry — the cross-net overlap sweep only
flags a geometric intersection when the two shapes' layer sets actually
share a layer; a B.Cu breakout passing near a foreign F.Cu electrode body
in plain (x, y) is not a short (different physical layer), which a
layer-blind sweep would have wrongly flagged the moment breakout
geometry was added.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from shapely.geometry import (  # type: ignore[import-untyped]
    LineString,
    Polygon,
)
from shapely.geometry import Point as SPoint
from shapely.validation import explain_validity  # type: ignore[import-untyped]

from precis.pcb import generators as G

_PIN_RE = re.compile(r"R(\d+)C(\d+)")


def _pads_by_pin(exp: G.GeneratorExpansion) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for p in exp.footprints[0]["pads"]:
        out.setdefault(p["pin"], []).append(p)
    return out


def _shape(p: dict) -> Polygon:
    if p["shape"] == "polygon":
        return Polygon(p["poly"]).buffer(0)
    return SPoint(p["x"], p["y"]).buffer(p["w"] / 2.0)


def _pin_for_net(name: str, net: str) -> str:
    prefix = f"{name}_"
    assert net.startswith(prefix), (net, prefix)
    return net[len(prefix) :]


def _copper_by_pin(
    exp: G.GeneratorExpansion, name: str = "ARR"
) -> dict[str, list[dict]]:
    """``exp.copper`` (the neck track + plaza via, pcb-pre-place-route-
    blocks Slice 2) keyed by pin -- the copper-row analogue of
    :func:`_pads_by_pin`, since those two rows no longer live in
    ``pads``."""
    out: dict[str, list[dict]] = {}
    for item in exp.copper:
        pin = _pin_for_net(name, str(item["net"]))
        out.setdefault(pin, []).append(item)
    return out


def _copper_shape(item: dict[str, Any]) -> tuple[Polygon, frozenset[str]]:
    """The item's own geometry, paired with the set of physical layers it
    occupies — a via's ``span`` (both ends it bridges), a track's own
    single drawn ``layer``. Rulings 2026-09-19 item 11's B.Cu breakout is
    the first copper row this module ever emits that does NOT share a
    layer with the electrode body/neck (both F.Cu), so the overlap sweep
    below needs this to avoid flagging harmless cross-layer proximity."""
    geom = item["geom"]
    if item["ctype"] == "via":
        shape = SPoint(geom["x"], geom["y"]).buffer(geom["dia_mm"] / 2.0)
        return shape, frozenset(geom["span"])
    seg = geom["segments"][0]
    line = LineString([tuple(seg["start"]), tuple(seg["end"])])
    r = float(geom["width_mm"]) / 2.0
    shape = line.buffer(r) if r > 0 else line
    return shape, frozenset({str(item["layer"])})


def _all_shapes(
    exp: G.GeneratorExpansion, name: str = "ARR"
) -> list[tuple[str, frozenset[str], Polygon]]:
    """Every net's copper as one shape list — electrode bodies (``pads``,
    always F.Cu) AND the neck track + plaza via + B.Cu breakout
    (``copper``) — so a cross-net overlap sweep still covers the whole
    fabric now that only the body lives in ``pads``. Each entry carries
    its own layer set (see :func:`_copper_shape`) so the sweep can tell a
    real short (same layer, different net) from harmless cross-layer
    proximity."""
    shapes: list[tuple[str, frozenset[str], Polygon]] = [
        (p["pin"], frozenset({"F.Cu"}), _shape(p)) for p in exp.footprints[0]["pads"]
    ]
    for item in exp.copper:
        shape, layers = _copper_shape(item)
        shapes.append((_pin_for_net(name, str(item["net"])), layers, shape))
    return shapes


@pytest.mark.parametrize("grid", [[3, 3], [8, 8], [9, 9], [3, 8]])
def test_every_electrode_polygon_is_a_valid_simple_ring(grid):
    exp = G.expand("ewod_pad_array", "ARR", {"grid": grid})
    for p in exp.footprints[0]["pads"]:
        if p["shape"] != "polygon":
            continue
        poly = Polygon(p["poly"])
        assert poly.is_valid, explain_validity(poly)


@pytest.mark.parametrize("grid", [[3, 3], [8, 8], [9, 9], [3, 8]])
def test_zigzag_gap_between_row_neighbours_is_constant(grid):
    exp = G.expand("ewod_pad_array", "ARR", {"grid": grid})
    gap = exp.canonical_params["gap"]
    by_pin = _pads_by_pin(exp)
    electrodes = {
        pin: Polygon(
            next(
                p["poly"]
                for p in pads
                if p["shape"] == "polygon" and len(p["poly"]) > 4
            )
        )
        for pin, pads in by_pin.items()
    }
    checked = 0
    for pin, poly in electrodes.items():
        m = _PIN_RE.match(pin)
        assert m is not None
        r, c = int(m.group(1)), int(m.group(2))
        east = f"R{r}C{c + 1}"
        if east in electrodes:
            d = poly.distance(electrodes[east])
            assert d == pytest.approx(gap, abs=1e-6)
            checked += 1
        south = f"R{r + 1}C{c}"
        if south in electrodes:
            d = poly.distance(electrodes[south])
            assert d == pytest.approx(gap, abs=1e-6)
            checked += 1
    assert checked > 0


@pytest.mark.parametrize(
    ("variant", "grid"),
    [
        ("full", [9, 9]),
        ("full", [8, 8]),
        ("full", [3, 8]),
        ("rim", [4, 4]),
        ("rim", [8, 8]),
    ],
)
def test_no_cross_net_copper_overlap(variant, grid):
    exp = G.expand("ewod_pad_array", "ARR", {"grid": grid, "variant": variant})
    shapes = _all_shapes(exp)  # bodies + neck tracks/vias/breakouts (copper)
    n = len(shapes)
    for i in range(n):
        pin_i, layers_i, gi = shapes[i]
        for j in range(i + 1, n):
            pin_j, layers_j, gj = shapes[j]
            if pin_i == pin_j:
                continue  # same net -- redundant overlap is fine by design
            if not (layers_i & layers_j):
                continue  # different physical layers -- can't short
            assert gi.intersection(gj).area < 1e-9, (
                f"{pin_i} and {pin_j} pads overlap -- would short two different nets"
            )


def test_pads_1024_generates_in_seconds_and_is_closed_form():
    # Closed-form means LINEAR in pad count, so assert the scaling ratio
    # between two sizes measured back-to-back (robust to shared-VM load,
    # which inflates both alike — a flat 5s wall-clock budget flaked at 8s
    # under sibling gate load while a quiet box measures ~1.1s/1024).
    import time

    start = time.monotonic()
    small = G.expand("ewod_pad_array", "ARR", {"pads": 256})
    t_small = time.monotonic() - start

    start = time.monotonic()
    exp = G.expand("ewod_pad_array", "ARR", {"pads": 1024})
    t_big = time.monotonic() - start

    assert small.ledger["summary"]["pads_total"] > 200
    assert exp.ledger["summary"]["pads_total"] > 900
    per_pad_small = t_small / small.ledger["summary"]["pads_total"]
    per_pad_big = t_big / exp.ledger["summary"]["pads_total"]
    assert per_pad_big < 4 * per_pad_small, (
        f"superlinear expansion: {per_pad_small * 1000:.2f}ms/pad @256 -> "
        f"{per_pad_big * 1000:.2f}ms/pad @1024"
    )
    assert t_big < 60.0, f"pathologically slow even for a loaded box: {t_big:.1f}s"


def test_9x9_full_matches_the_acceptance_criterion_counts():
    # docs/backlog/pcb-ewod-multitile.md acceptance criterion 1: "9x9 full
    # = 72 electrodes + 9 via plazas".
    exp = G.expand("ewod_pad_array", "ARR", {"grid": [9, 9]})
    assert exp.ledger["summary"]["pads_total"] == 72
    assert exp.ledger["summary"]["plazas"] == 9
    assert exp.ledger["summary"]["pads_unusable"] == 0


def test_pad_sizes_merges_a_1x2_span_into_one_pad():
    # docs/backlog/pcb-ewod-multitile.md Slice 2: "a pad may span m x n
    # grid cells -- merged outline, one net" (round 6). A plain 3x3 field
    # has 8 electrodes (R1C1 is the auto-placed plaza); merging R0C0+R0C1
    # drops the count by one (two cells -> one pad) and the merged pad's
    # own ledger entry carries a span.
    exp = G.expand(
        "ewod_pad_array",
        "ARR",
        {"grid": [3, 3], "pad_sizes": [{"cells": [[0, 0], [0, 1]]}]},
    )
    assert exp.ledger["summary"]["pads_total"] == 7
    merged = exp.ledger["pads"]["R0C0"]
    assert merged["span"] == [1, 2]
    assert merged["cells"] == [[0, 0], [0, 1]]
    by_pin = _pads_by_pin(exp)
    assert "R0C0" in by_pin
    assert "R0C1" not in by_pin
    assert len(by_pin["R0C0"]) == 1  # body only -- no stub/via pad rows
    # exactly one via for the merged pad, same "one via suffices" rule as
    # any ordinary single-cell electrode -- now a copper row, not a pad.
    # Two tracks (Rulings 2026-09-19 item 11): the F.Cu neck plus the
    # B.Cu breakout stub outward from the via.
    copper_by_pin = _copper_by_pin(exp)
    vias = [c for c in copper_by_pin["R0C0"] if c["ctype"] == "via"]
    tracks = [c for c in copper_by_pin["R0C0"] if c["ctype"] == "track"]
    assert len(vias) == 1
    assert len(tracks) == 2
    assert {t["layer"] for t in tracks} == {"F.Cu", "B.Cu"}


def test_pad_sizes_merged_electrode_body_is_a_valid_simple_ring_and_wider_than_one_cell():
    exp = G.expand(
        "ewod_pad_array",
        "ARR",
        {"grid": [3, 3], "pad_sizes": [{"cells": [[0, 0], [0, 1]]}]},
    )
    by_pin = _pads_by_pin(exp)
    body = next(
        p for p in by_pin["R0C0"] if p["shape"] == "polygon" and len(p["poly"]) > 4
    )
    poly = Polygon(body["poly"])
    assert poly.is_valid, explain_validity(poly)
    pitch = exp.canonical_params["pitch"]
    gap = exp.canonical_params["gap"]
    single_width = pitch - gap
    minx, _miny, maxx, _maxy = poly.bounds
    assert (maxx - minx) > single_width * 1.5


def test_pad_sizes_merged_pad_keeps_constant_gap_against_its_neighbours():
    # Same geometric assertion as test_zigzag_gap_between_row_neighbours_is_
    # constant, but the merged pad's own neighbours (R0C0+R0C1 merged,
    # neighbouring R0C2 to the east and R1C0/R1C1 to the south).
    exp = G.expand(
        "ewod_pad_array",
        "ARR",
        {"grid": [3, 3], "pad_sizes": [{"cells": [[0, 0], [0, 1]]}]},
    )
    gap = exp.canonical_params["gap"]
    by_pin = _pads_by_pin(exp)
    merged_body = Polygon(
        next(
            p["poly"]
            for p in by_pin["R0C0"]
            if p["shape"] == "polygon" and len(p["poly"]) > 4
        )
    )
    for neighbour_pin in ("R0C2", "R1C0", "R1C1"):
        if neighbour_pin not in by_pin:
            continue
        neighbour_body = Polygon(
            next(
                p["poly"]
                for p in by_pin[neighbour_pin]
                if p["shape"] == "polygon" and len(p["poly"]) > 4
            )
        )
        d = merged_body.distance(neighbour_body)
        assert d == pytest.approx(gap, abs=1e-6), f"{neighbour_pin}: gap={d}"


def test_pad_sizes_rejects_a_span_covering_a_plaza_cell():
    # docs/backlog/pcb-ewod-multitile.md decisions log: "merged pads never
    # cover a plaza (would short the 8 escape nets)" -- P1_1 is the
    # auto-placed plaza in a 3x3 grid.
    with pytest.raises(ValueError, match="plaza"):
        G.expand(
            "ewod_pad_array",
            "ARR",
            {"grid": [3, 3], "pad_sizes": [{"cells": [[1, 1], [1, 2]]}]},
        )


def test_pad_sizes_rejects_a_non_rectangular_cell_set():
    with pytest.raises(ValueError, match="solid rectangle"):
        G.expand(
            "ewod_pad_array",
            "ARR",
            {"grid": [4, 4], "pad_sizes": [{"cells": [[0, 0], [0, 1], [1, 1]]}]},
        )


def test_pad_sizes_rejects_double_claimed_cells():
    with pytest.raises(ValueError, match="more than one"):
        G.expand(
            "ewod_pad_array",
            "ARR",
            {
                "grid": [4, 4],
                "pad_sizes": [
                    {"cells": [[0, 0], [0, 1]]},
                    {"cells": [[0, 1], [1, 1]]},
                ],
            },
        )


@pytest.mark.parametrize(
    ("variant", "grid", "cells"),
    [
        ("full", [9, 9], [[0, 0], [0, 1]]),
        ("full", [9, 9], [[5, 5], [5, 6], [6, 5], [6, 6]]),
        ("rim", [4, 4], [[0, 0], [0, 1]]),
        ("rim", [4, 4], [[0, 3], [1, 3]]),
    ],
)
def test_no_cross_net_copper_overlap_with_a_merged_pad(variant, grid, cells):
    exp = G.expand(
        "ewod_pad_array",
        "ARR",
        {"grid": grid, "variant": variant, "pad_sizes": [{"cells": cells}]},
    )
    shapes = _all_shapes(exp)  # bodies + neck tracks/vias/breakouts (copper)
    n = len(shapes)
    for i in range(n):
        pin_i, layers_i, gi = shapes[i]
        for j in range(i + 1, n):
            pin_j, layers_j, gj = shapes[j]
            if pin_i == pin_j:
                continue
            if not (layers_i & layers_j):
                continue  # different physical layers -- can't short
            assert gi.intersection(gj).area < 1e-9, (
                f"{pin_i} and {pin_j} pads overlap -- would short two different nets"
            )


def test_pad_sizes_merge_idempotent_canonical_params():
    params = {"grid": [3, 3], "pad_sizes": [{"cells": [[0, 0], [0, 1]]}]}
    exp1 = G.expand("ewod_pad_array", "ARR", params)
    exp2 = G.expand("ewod_pad_array", "ARR", params)
    assert exp1.canonical_params == exp2.canonical_params


def test_one_via_per_usable_electrode():
    # pcb-pre-place-route-blocks Slice 2: every pin has exactly ONE pad
    # (the body) regardless of usability; the via lives in `copper` now,
    # one per USABLE (driven) electrode.
    exp = G.expand("ewod_pad_array", "ARR", {"grid": [3, 3]})
    by_pin = _pads_by_pin(exp)
    for pin, pads in by_pin.items():
        assert len(pads) == 1, f"{pin} has {len(pads)} pads, expected exactly 1 (body)"
    copper_by_pin = _copper_by_pin(exp)
    for pin, items in copper_by_pin.items():
        vias = [c for c in items if c["ctype"] == "via"]
        assert len(vias) == 1, f"{pin} has {len(vias)} vias, expected exactly 1"


def test_reserved_slot_gets_no_via_and_no_stub():
    exp = G.expand("ewod_pad_array", "ARR", {"grid": [3, 3], "reserve": ["P1_1:N"]})
    by_pin = _pads_by_pin(exp)
    r0c1_pads = by_pin["R0C1"]
    assert len(r0c1_pads) == 1  # electrode body only -- no stub, no via
    assert not any(p.get("drill") for p in r0c1_pads)
    # the suppressed net gets no copper at all (pcb-pre-place-route-
    # blocks Slice 2 -- see test_pcb_ewod_fabric.py for the ledger side).
    copper_by_pin = _copper_by_pin(exp)
    assert "R0C1" not in copper_by_pin
