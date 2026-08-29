"""precis.pcb.silk -- the silkscreen builder. No DB (a plain-dict graph via
precis.pcb.ir.from_graph, the same fixture style tests/test_pcb_realize.py
uses).

Covers: courtyard/pin-1/refdes geometry off a real IR, refdes suppression
+ relocation when the default placement would overlap a pad, the "silk
never overlaps a pad" invariant (checked with an INDEPENDENT geometry
routine, not the builder's own private overlap predicate), and the
gerber.py round-trip (real D01/D02, readable back via gerber_view).
"""

from __future__ import annotations

import math

import pytest

from precis.pcb import DEFAULT_STACKUP, gerber, gerber_view, realize
from precis.pcb.ir import from_graph
from precis.pcb.silk import build_silk, readable_text_rotation


def _graph(
    refdes: str, n_pins: int, *, x: float = 0.0, y: float = 0.0, rot: float = 0.0
):
    """A single placed instance with ``n_pins`` distinct pins (one
    single-member net per pin — enough for ``from_graph`` to synthesize a
    real land pattern via ``precis.pcb.landpattern.offsets_for``, no
    segments needed for silk itself)."""
    return {
        "instances": [{"refdes": refdes, "x": x, "y": y, "rot": rot}],
        "nets": [
            {
                "name": f"N{i}",
                "net_class": "signal",
                "domain": "electrical",
                "members": [{"refdes": refdes, "pin": str(i + 1)}],
            }
            for i in range(n_pins)
        ],
    }


def _multi(*graphs: dict) -> dict:
    instances: list = []
    nets: list = []
    for g in graphs:
        instances += g["instances"]
        nets += g["nets"]
    return {"instances": instances, "nets": nets}


# ── independent overlap checker (deliberately NOT silk.py's own helper) ──
def _segment_hits_pad(a, b, pad, *, n=25) -> bool:
    """Sample the raw centreline (no stroke-width inflation) and test each
    sample point against the pad's own boundary -- a strictly SIMPLER,
    independently-written check than the builder's SAT-based one. If this
    ever trips, the builder's inflated/SAT check has a real bug, not a
    disagreement over margins."""
    shape = pad.get("shape", "circle")
    for k in range(n + 1):
        t = k / n
        px = a[0] + (b[0] - a[0]) * t
        py = a[1] + (b[1] - a[1]) * t
        if shape == "circle":
            if math.hypot(px - pad["x"], py - pad["y"]) < pad["w"] / 2.0:
                return True
        else:
            w, h = pad["w"], pad.get("h", pad["w"])
            if (
                pad["x"] - w / 2 < px < pad["x"] + w / 2
                and pad["y"] - h / 2 < py < pad["y"] + h / 2
            ):
                return True
    return False


def _draws_hit_any_pad(draws: list[dict], pads: list[dict]) -> bool:
    for draw in draws:
        for seg in draw["segments"]:
            for pad in pads:
                if _segment_hits_pad(seg["start"], seg["end"], pad):
                    return True
    return False


# ── basic shape / no-overlap-with-nothing case ────────────────────────────
def test_build_silk_places_refdes_courtyard_and_pin1_for_a_normal_part():
    ir = from_graph(_graph("U1", 16, x=10.0, y=5.0), stackup=DEFAULT_STACKUP)
    result = build_silk(ir, pads=[])
    assert not result.dropped
    assert not result.relocated
    top = result.draws["top"]
    assert top  # something was drawn
    roles = {d["role"] for d in top}
    assert roles == {"outline", "pin1", "refdes"}
    for d in top:
        assert d["refdes"] == "U1"
        assert d["source"] == "synthesized"


def test_unplaced_instance_gets_no_silk():
    graph = {
        "instances": [{"refdes": "U1"}],  # no x/y
        "nets": [
            {
                "name": "N0",
                "net_class": "signal",
                "domain": "electrical",
                "members": [{"refdes": "U1", "pin": "1"}],
            }
        ],
    }
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    result = build_silk(ir, pads=[])
    assert result.draws == {"top": [], "bottom": []}
    assert not result.dropped
    assert not result.relocated


def test_pinless_instance_gets_no_courtyard_or_pin1_but_still_a_refdes():
    graph = {
        "instances": [{"refdes": "MH1", "x": 0.0, "y": 0.0}],
        "nets": [],
    }
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    result = build_silk(ir, pads=[])
    roles = {d["role"] for d in result.draws["top"]}
    assert roles == {"refdes"}  # no pins -> no courtyard, no pin-1 marker


def test_side_routes_to_the_bottom_bucket():
    ir = from_graph(_graph("C1", 2, x=0.0, y=0.0), stackup=DEFAULT_STACKUP)
    result = build_silk(ir, pads=[], instance_sides={"C1": "bottom"})
    assert result.draws["top"] == []
    assert result.draws["bottom"]


# ── refdes lands within its part's extent (the default, centered case) ───
def test_refdes_text_lands_within_the_part_extent_when_unobstructed():
    ir = from_graph(_graph("U2", 16, x=0.0, y=0.0), stackup=DEFAULT_STACKUP)
    result = build_silk(ir, pads=[])
    assert not result.relocated and not result.dropped
    refdes_draws = [d for d in result.draws["top"] if d["role"] == "refdes"]
    assert refdes_draws
    outline = next(d for d in result.draws["top"] if d["role"] == "outline")
    reach = max(
        abs(p) for seg in outline["segments"] for p in seg["start"] + seg["end"]
    )
    for d in refdes_draws:
        for seg in d["segments"]:
            for x, y in (seg["start"], seg["end"]):
                assert abs(x) <= reach + 1e-6
                assert abs(y) <= reach + 1e-6


# ── suppression: relocate, then drop ──────────────────────────────────────
def test_refdes_relocates_when_the_default_center_spot_overlaps_a_pad():
    ir = from_graph(_graph("U3", 16, x=0.0, y=0.0), stackup=DEFAULT_STACKUP)
    # a big pad sitting right on the part's centre -- the default candidate
    # (centered on the part) cannot possibly clear it.
    pads = [{"shape": "circle", "x": 0.0, "y": 0.0, "w": 3.0, "net": "FOREIGN"}]
    result = build_silk(ir, pads=pads)
    assert not result.dropped
    assert result.relocated
    assert any("U3" in msg for msg in result.relocated)
    refdes_draws = [
        d for d in result.draws["top"] if d["role"] == "refdes" and d["refdes"] == "U3"
    ]
    assert refdes_draws
    assert not _draws_hit_any_pad(refdes_draws, pads)


def test_refdes_dropped_when_every_candidate_overlaps_a_pad():
    ir = from_graph(_graph("U4", 16, x=0.0, y=0.0), stackup=DEFAULT_STACKUP)
    # a pad so large it blankets the part and every relocation candidate.
    pads = [{"shape": "circle", "x": 0.0, "y": 0.0, "w": 40.0, "net": "FOREIGN"}]
    result = build_silk(ir, pads=pads)
    assert any("U4" in msg and "dropped" in msg for msg in result.dropped)
    refdes_draws = [
        d for d in result.draws["top"] if d["role"] == "refdes" and d["refdes"] == "U4"
    ]
    assert refdes_draws == []
    # and nothing else for U4 (courtyard/pin1) survived either -- consistent
    # with "silk never overlaps a pad" below, not a special case for text.
    other_draws = [d for d in result.draws["top"] if d["refdes"] == "U4"]
    assert not _draws_hit_any_pad(other_draws, pads)


# ── silk never overlaps a pad (property, independent checker) ────────────
@pytest.mark.parametrize("pad_w", [0.5, 2.0, 6.0, 15.0])
def test_silk_never_overlaps_a_pad_at_various_sizes(pad_w):
    ir = from_graph(
        _multi(_graph("U5", 8, x=0.0, y=0.0), _graph("C5", 2, x=6.0, y=0.0)),
        stackup=DEFAULT_STACKUP,
    )
    pads = [
        {"shape": "circle", "x": 0.0, "y": 0.0, "w": pad_w, "net": "N"},
        {"shape": "rect", "x": 6.0, "y": 0.0, "w": pad_w, "h": pad_w / 2, "net": "M"},
    ]
    result = build_silk(ir, pads=pads)
    assert not _draws_hit_any_pad(result.draws["top"], pads)
    assert not _draws_hit_any_pad(result.draws["bottom"], pads)


def test_silk_never_overlaps_a_pad_with_rotation_and_mirror():
    ir = from_graph(_graph("U6", 16, x=2.0, y=-3.0, rot=53.0), stackup=DEFAULT_STACKUP)
    pads = [{"shape": "circle", "x": 2.0, "y": -3.0, "w": 1.5, "net": "N"}]
    result = build_silk(ir, pads=pads, instance_sides={"U6": "bottom"})
    assert not _draws_hit_any_pad(result.draws["bottom"], pads)


# ── real pads_for_ir input (the synthesized-bound fallback the handler
# falls back to when the footprint cache is empty) ────────────────────────
def test_silk_clears_pads_for_irs_own_synthesized_pads():
    graph = _graph("U7", 8, x=0.0, y=0.0)
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    pads = realize.pads_for_ir(ir, [layer["name"] for layer in DEFAULT_STACKUP])
    assert pads  # the fixture actually produced pad geometry
    result = build_silk(ir, pads=pads)
    assert not result.dropped
    assert not _draws_hit_any_pad(result.draws["top"], pads)


# ── gerber round-trip ──────────────────────────────────────────────────
def test_silk_round_trips_through_gerber_and_parses_back():
    ir = from_graph(_graph("U8", 16, x=1.0, y=1.0), stackup=DEFAULT_STACKUP)
    result = build_silk(ir, pads=[])
    model = {
        "layers": [layer["name"] for layer in DEFAULT_STACKUP],
        "silkscreen": result.draws,
    }
    top_gerber = gerber.silkscreen_gerber(model, "top")
    assert "D01*" in top_gerber
    assert "D02*" in top_gerber
    assert top_gerber.strip().endswith("M02*")

    art = gerber_view.parse_gerber(top_gerber)
    assert art.strokes  # real geometry came back, not an empty legend file
    total_points = sum(len(s.points) for s in art.strokes)
    assert total_points > 4  # more than one trivial 2-point stroke

    # every parsed stroke width matches the draw width we asked for
    for s in art.strokes:
        assert s.width == pytest.approx(gerber.DEFAULT_SILK_WIDTH_MM)


# ── refdes text must stay READABLE however the part is rotated ───────────
@pytest.mark.parametrize(
    ("rot", "expected"),
    [
        (0.0, 0.0),
        (90.0, 90.0),
        (180.0, 0.0),  # upside-down folded back
        (270.0, 90.0),  # bottom-to-top, not top-to-bottom
        (-90.0, 90.0),
        (45.0, 45.0),  # not a right angle: left alone, still readable
        (135.0, -45.0),
        (360.0, 0.0),
        (720.0 + 180.0, 0.0),  # many turns still folds
    ],
)
def test_refdes_rotation_folds_into_the_readable_half_turn(rot, expected):
    assert readable_text_rotation(rot) == pytest.approx(expected)


def test_a_part_rotated_180_gets_upright_text_not_mirrored_text():
    """A refdes exists to be read. Rotating the part must move WHERE the
    label sits without turning the glyphs upside-down -- so the strokes of
    a part at 180 must be the 0-degree strokes translated, never the
    0-degree strokes point-reflected."""
    up = build_silk(
        from_graph(_graph("U1", 16, x=10.0, y=5.0, rot=0.0), stackup=DEFAULT_STACKUP),
        pads=[],
    )
    flipped = build_silk(
        from_graph(_graph("U1", 16, x=10.0, y=5.0, rot=180.0), stackup=DEFAULT_STACKUP),
        pads=[],
    )

    def _text_span(result):
        segs = [
            seg
            for d in result.draws["top"]
            if d["role"] == "refdes"
            for seg in d["segments"]
        ]
        assert segs, "no refdes strokes at all"
        ys = [p[1] for seg in segs for p in (seg["start"], seg["end"])]
        xs = [p[0] for seg in segs for p in (seg["start"], seg["end"])]
        return max(xs) - min(xs), max(ys) - min(ys)

    # Same glyphs, same orientation => identical extents. A 180-degree
    # glyph rotation would preserve extents too, so extents alone are not
    # enough -- compare the strokes as a SET after removing the common
    # translation, which a point reflection would not survive.
    assert _text_span(up) == pytest.approx(_text_span(flipped))

    def _normalized(result):
        segs = [
            seg
            for d in result.draws["top"]
            if d["role"] == "refdes"
            for seg in d["segments"]
        ]
        pts = [p for seg in segs for p in (seg["start"], seg["end"])]
        ox, oy = min(p[0] for p in pts), min(p[1] for p in pts)
        return sorted(
            (
                round(seg["start"][0] - ox, 4),
                round(seg["start"][1] - oy, 4),
                round(seg["end"][0] - ox, 4),
                round(seg["end"][1] - oy, 4),
            )
            for seg in segs
        )

    assert _normalized(up) == _normalized(flipped)


def test_empty_silk_still_produces_a_valid_legend_file():
    model = {"layers": [layer["name"] for layer in DEFAULT_STACKUP], "silkscreen": {}}
    content = gerber.silkscreen_gerber(model, "top")
    assert "%TF.FileFunction,Legend,Top*%" in content
    art = gerber_view.parse_gerber(content)
    assert art.strokes == []
