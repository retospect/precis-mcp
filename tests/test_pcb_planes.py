"""precis.pcb.planes -- pure shapely geometry over synthetic copper, no DB
(same style tests/test_pcb_tiling.py already uses for this subsystem).

Covers :func:`cut_antipads`: the render-time "punch a keep-out ring into an
already-built pour" cut that :meth:`precis.handlers.pcb.PcbHandler.
_board_furniture` runs immediately after ``build_fiducials`` places a
fiducial on top of an already-poured plane (``docs/backlog/
pcb-fiducial-vs-copper.md``'s closed item -- a fiducial MAY sit inside a
flood, it just needs a no-pour ring, and this is the function that cuts
it). Every assertion here checks the HOLE actually exists -- an
independent, hand-rolled shapely read of the emitted ``polygon``/``holes``
dict, not the module's own ``point_in_pour`` ray-caster -- because a test
that only counts DRC findings (``tests/test_pcb_fab_render_all_layers.py``)
passes equally well if fiducial synthesis were deleted outright.
"""

from __future__ import annotations

from typing import Any

import pytest
from shapely.geometry import Point, Polygon

from precis.pcb.planes import cut_antipads, plane_pours, pour_notes, pour_quality

#: A plain 40x40mm square board, inset by 0.5mm edge clearance elsewhere --
#: big enough that a ~2mm fiducial ring is a small feature inside it, not
#: something that eats the whole pour.
_BOARD_OUTLINE = [[0.0, 0.0], [40.0, 0.0], [40.0, 40.0], [0.0, 40.0]]

_CLEARANCE_MM = 0.2


def _gnd_pour(*, layer: str = "F.Cu") -> dict[str, Any]:
    """One unobstructed GND pour covering ``_BOARD_OUTLINE`` -- built via
    :func:`plane_pours` itself (real emit shape, not hand-typed) rather
    than a literal dict, so a ``cut_antipads`` test starts from the exact
    shape :meth:`_board_furniture` actually reads off ``pcb_copper``."""
    pours = plane_pours(
        outline=_BOARD_OUTLINE,
        layers=[layer],
        plane_nets={0: "GND"},
        copper=[],
        clearance_mm=_CLEARANCE_MM,
        edge_clearance_mm=0.5,
    )
    assert len(pours) == 1  # sanity: one solid fragment, no holes yet
    return pours[0]


def _fiducial_blocker(
    x: float, y: float, *, dia_mm: float = 1.0, layers: list[str] | None = None
) -> dict[str, Any]:
    """``FiducialResult.plane_blockers``'s own shape
    (``src/precis/pcb/silk.py::build_fiducials``) -- a ``ctype='via'``
    disc, net ``""``, sized to the fiducial's MASK opening diameter, one
    real stackup layer NAME (not an index) in ``layers``."""
    return {
        "ctype": "via",
        "net": "",
        "x": x,
        "y": y,
        "dia_mm": dia_mm,
        "layers": layers if layers is not None else ["F.Cu"],
        "role": "fiducial",
    }


def _shapely_pour(pour: dict[str, Any]) -> Polygon:
    """An INDEPENDENT shapely read of a pour dict's ``polygon``/``holes``
    -- built straight off the raw coordinate lists here, not through
    :mod:`precis.pcb.drc`'s own ``_copper_item_polygon`` (the function
    under test is built on that same helper internally; asserting through
    it too would let a shared bug there hide from every test in this
    file)."""
    holes = [
        [(float(p[0]), float(p[1])) for p in hole] for hole in pour.get("holes") or []
    ]
    return Polygon([(float(p[0]), float(p[1])) for p in pour["polygon"]], holes)


def test_cut_antipads_opens_a_ring_that_the_fiducial_disc_does_not_touch():
    """The acceptance criterion, verbatim: given a plane-promoted pour on
    the fiducial's own layer, the emitted pour carries a hole AND the
    fiducial's mask-opening disc does not intersect the pour's filled
    area -- not merely that its CENTRE reads as unpoured, which a
    ray-caster bug could satisfy by accident."""
    pour = _gnd_pour()
    fx, fy = 20.0, 20.0
    mask_dia_mm = 2.0
    blocker = _fiducial_blocker(fx, fy, dia_mm=mask_dia_mm)

    cut = cut_antipads([pour], [blocker], clearance_mm=_CLEARANCE_MM)

    assert len(cut) == 1
    result = cut[0]
    assert result.get("holes")

    filled = _shapely_pour(result)
    # The fiducial's own mask-opening disc, not the antipad ring itself --
    # the ring is bigger (mask radius + clearance), so a disc that clears
    # the SMALLER shape is the sharper assertion.
    disc = Point(fx, fy).buffer(mask_dia_mm / 2.0, quad_segs=32)
    assert not filled.intersects(disc)

    # A point well away from the fiducial, still inside the board, is
    # untouched copper -- the cut is a local ring, not a wholesale erasure.
    assert filled.contains(Point(5.0, 5.0))


def test_cut_antipads_ignores_a_blocker_on_a_different_layer():
    """A blocker whose ``layers`` never names the pour's own layer changes
    nothing at all -- returned as the IDENTICAL dict object, not a
    shapely round-trip that merely happens to look the same."""
    pour = _gnd_pour(layer="F.Cu")
    blocker = _fiducial_blocker(20.0, 20.0, layers=["B.Cu"])

    cut = cut_antipads([pour], [blocker], clearance_mm=_CLEARANCE_MM)

    assert len(cut) == 1
    assert cut[0] is pour
    assert "holes" not in cut[0]


def test_cut_antipads_ignores_a_blocker_outside_the_pour():
    """A blocker on the RIGHT layer but geographically outside every pour
    (here: past the board edge entirely) leaves that pour unchanged --
    the module docstring's own "a blocker fully outside a pour changes
    nothing" case."""
    pour = _gnd_pour()
    blocker = _fiducial_blocker(500.0, 500.0)

    cut = cut_antipads([pour], [blocker], clearance_mm=_CLEARANCE_MM)

    assert len(cut) == 1
    assert cut[0] is pour


def test_cut_antipads_preserves_a_pours_existing_holes():
    """A pour that already carries a hole (a foreign via's antipad,
    cut by :func:`plane_pours` itself at realize time) keeps that hole
    after a SECOND, unrelated antipad is cut into it -- ``cut_antipads``
    reads the polygon's existing interior rings off the pour dict rather
    than starting from a hole-less exterior."""
    existing_hole_pour = plane_pours(
        outline=_BOARD_OUTLINE,
        layers=["F.Cu"],
        plane_nets={0: "GND"},
        copper=[
            {
                "ctype": "via",
                "net": "VCC",
                "x": 10.0,
                "y": 10.0,
                "dia_mm": 1.0,
                "layers": ["F.Cu"],
            }
        ],
        clearance_mm=_CLEARANCE_MM,
        edge_clearance_mm=0.5,
    )
    assert len(existing_hole_pour) == 1
    assert existing_hole_pour[0].get("holes")

    fx, fy = 30.0, 30.0  # far from the existing (10, 10) via hole
    blocker = _fiducial_blocker(fx, fy, dia_mm=2.0)

    cut = cut_antipads(existing_hole_pour, [blocker], clearance_mm=_CLEARANCE_MM)

    assert len(cut) == 1
    result = cut[0]
    holes = result.get("holes") or []
    assert len(holes) == 2  # the original via antipad AND the new fiducial ring

    filled = _shapely_pour(result)
    assert not filled.contains(Point(10.0, 10.0))  # original hole still open
    assert not filled.contains(Point(fx, fy))  # new fiducial ring open too
    assert filled.contains(Point(30.0, 5.0))  # elsewhere, still solid copper


def test_cut_antipads_returns_input_unchanged_with_no_pours_or_no_blockers():
    pour = _gnd_pour()
    blocker = _fiducial_blocker(20.0, 20.0)

    assert cut_antipads([], [blocker], clearance_mm=_CLEARANCE_MM) == []
    no_blockers = cut_antipads([pour], [], clearance_mm=_CLEARANCE_MM)
    assert no_blockers == [pour]
    assert no_blockers[0] is pour


# ── pour_quality (pcb-stackup-regions slice 1) ───────────────────────────


def _rect_pour(
    x0: float, y0: float, x1: float, y1: float, *, net: str = "GND", layer: str = "F.Cu"
) -> dict[str, Any]:
    return {
        "ctype": "pour",
        "net": net,
        "layer": layer,
        "polygon": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
        "holes": [],
    }


def test_pour_quality_one_rectangle_is_whole_with_no_neck():
    (q,) = pour_quality([_rect_pour(0, 0, 10, 5)])
    assert (q.net, q.layer) == ("GND", "F.Cu")
    assert q.fragments == 1
    assert q.largest_share == 1.0
    assert q.area_mm2 == pytest.approx(50.0)
    assert q.min_neck_mm is None


def test_pour_quality_two_disjoint_rectangles_report_the_larger_share():
    big, small = _rect_pour(0, 0, 10, 10), _rect_pour(20, 0, 25, 4)
    (q,) = pour_quality([big, small])
    assert q.fragments == 2
    assert q.area_mm2 == pytest.approx(120.0)
    assert q.largest_share == pytest.approx(100.0 / 120.0)


def test_pour_quality_dumbbell_reports_the_bridge_width_as_the_neck():
    dumbbell = {
        "ctype": "pour",
        "net": "GND",
        "layer": "F.Cu",
        "polygon": [
            [0, 0],
            [4, 0],
            [4, 1.75],
            [8, 1.75],
            [8, 0],
            [12, 0],
            [12, 4],
            [8, 4],
            [8, 2.25],
            [4, 2.25],
            [4, 4],
            [0, 4],
        ],
        "holes": [],
    }
    (q,) = pour_quality([dumbbell])
    assert q.fragments == 1
    assert q.min_neck_mm == pytest.approx(0.5, abs=0.05)


def test_pour_quality_keys_are_separate_and_sorted_by_net_then_layer():
    pours = [
        _rect_pour(0, 0, 5, 5, net="GND", layer="In1.Cu"),
        _rect_pour(0, 0, 5, 5, net="VCC", layer="F.Cu"),
        _rect_pour(0, 0, 5, 5, net="GND", layer="F.Cu"),
    ]
    got = [(q.net, q.layer) for q in pour_quality(pours)]
    assert got == [("GND", "F.Cu"), ("GND", "In1.Cu"), ("VCC", "F.Cu")]


def test_pour_notes_join_every_layer_of_a_net_into_one_clause():
    pours = [_rect_pour(0, 0, 10, 5), _rect_pour(0, 0, 10, 5, layer="In1.Cu")]
    notes = pour_notes(pour_quality(pours))
    assert list(notes) == ["GND"]
    assert notes["GND"].startswith("pour: F.Cu 1 piece(s)")
    assert "In1.Cu 1 piece(s)" in notes["GND"] and "neck >2mm" in notes["GND"]


def _dumbbell_pour(neck_half: float, *, net: str = "VCC", layer: str = "In2.Cu"):
    """Two lobes, x -3..4 and 6..14, joined at x 4..6 by a neck
    ``2 * neck_half`` wide: a via antipad at (5, 0) cuts a thin one in two."""
    from shapely.geometry import box

    shape = (
        box(-3, -3, 4, 3)
        .union(box(6, -3, 14, 3))
        .union(box(4, -neck_half, 6, neck_half))
    )
    return {
        "ctype": "pour",
        "net": net,
        "layer": layer,
        "polygon": [list(p) for p in shape.exterior.coords[:-1]],
        "holes": [],
    }


def _rescue_board():
    """GND (plane on In1.Cu) between U1 at (10, 0) and U2 at (0, 0), its
    route context, a GND through-via at U1, and U2's pin id."""
    from precis.pcb import DEFAULT_STACKUP
    from precis.pcb import realize as pcb_realize
    from precis.pcb.capabilities import capability_for
    from precis.pcb.ir import from_graph

    ir = from_graph(
        {
            "instances": [
                {"refdes": "U1", "x": 10.0, "y": 0.0},
                {"refdes": "U2", "x": 0.0, "y": 0.0},
            ],
            "nets": [
                {
                    "name": "GND",
                    "members": [
                        {"refdes": "U1", "pin": "1"},
                        {"refdes": "U2", "pin": "1"},
                    ],
                }
            ],
        },
        stackup=DEFAULT_STACKUP,
        outline=[[-5.0, -5.0], [16.0, -5.0], [16.0, 5.0], [-5.0, 5.0]],
    )
    ir.promote_plane(0, 1)  # GND on In1.Cu
    config = pcb_realize.RealizeConfig(
        router="vector", fab_caps=capability_for("4layer")
    )
    ctx = pcb_realize._route_context(ir, list(range(ir.n_segments)), config, None, None)
    assert ctx is not None
    rules = ctx.rules_by_net[0]
    assert rules.via_dia_mm is not None and rules.via_drill_mm is not None
    drop = pcb_realize.RealizedVia(
        seg_id=-1,
        net_id=0,
        x=10.0,
        y=0.0,
        dia_mm=rules.via_dia_mm,
        drill_mm=rules.via_drill_mm,
        layer_lo=0,
        layer_hi=len(ir.stackup) - 1,
        endpoint="a",
    )
    u2_pin = next(
        p
        for p in range(ir.n_pins)
        if str(ir.instance_refdes[int(ir.pin_instance[p])]) == "U2"
    )
    return ir, config, ctx, drop, u2_pin


def test_pour_pieces_counts_electrical_pieces_not_polygons():
    """Two GND pieces on In2.Cu are ONE electrical piece when a GND via in
    each reaches a shared GND pour on In1.Cu, two when nothing joins them:
    the count :func:`_keep_splitless_rescues` compares."""
    from dataclasses import replace

    from precis.pcb import realize as pcb_realize

    ir, _config, _ctx, drop, _u2 = _rescue_board()
    split = [
        _rect_pour(-3, -3, 4, 3, net="GND", layer="In2.Cu"),
        _rect_pour(6, -3, 14, 3, net="GND", layer="In2.Cu"),
    ]
    assert pcb_realize._pour_pieces(ir, [], [], split, []) == {("GND", 2): 2}
    bridge = _rect_pour(-3, -3, 14, 3, net="GND", layer="In1.Cu")
    left = replace(drop, x=0.0)
    got = pcb_realize._pour_pieces(ir, [], [left, drop], [*split, bridge], [])
    assert got == {("GND", 2): 1, ("GND", 1): 1}


def test_keep_splitless_rescues_drops_a_splitting_rescue_and_its_dependents():
    """All kept on one re-pour when together they split nothing; otherwise
    each is tried in order on top of those kept, and one that raises a
    plane's piece count is dropped, with a later rescue that ends on its
    via."""
    from precis.pcb import realize as pcb_realize

    bad = pcb_realize._PlaneRescue(1, [], [], None)
    good = pcb_realize._PlaneRescue(2, [], [], None)
    rides_bad = pcb_realize._PlaneRescue(3, [], [], 1)
    calls: list[list[int]] = []

    def repour(chosen):
        pids = [r.pid for r in chosen]
        calls.append(pids)
        return pids, {("VCC", 2): 2 if 1 in pids else 1}

    kept, result = pcb_realize._keep_splitless_rescues(
        [bad, good, rides_bad], repour, {("VCC", 2): 1}
    )
    assert kept == [good] and result == [2]
    assert calls == [[1, 2, 3], [], [1], [2]]

    calls.clear()
    kept, result = pcb_realize._keep_splitless_rescues([good], repour, {("VCC", 2): 1})
    assert kept == [good] and result == [2] and calls == [[2]]


def _wall_and_via(*, gnd_via: bool) -> list[dict[str, Any]]:
    """A foreign track cutting the 40x40 board into two halves, plus (when
    asked) a GND via in the left half only."""
    copper: list[dict[str, Any]] = [
        {
            "ctype": "track",
            "net": "VIN",
            "layer": "F.Cu",
            "width_mm": 2.0,
            "segments": [{"shape": "line", "start": [20.0, -1.0], "end": [20.0, 41.0]}],
        }
    ]
    if gnd_via:
        copper.append(
            {
                "ctype": "via",
                "net": "GND",
                "x": 10.0,
                "y": 20.0,
                "dia_mm": 0.75,
                "layers": ["F.Cu"],
            }
        )
    return copper


def _pour_with(copper: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return plane_pours(
        outline=_BOARD_OUTLINE,
        layers=["F.Cu"],
        plane_nets={0: "GND"},
        copper=copper,
        clearance_mm=_CLEARANCE_MM,
        edge_clearance_mm=0.5,
    )


def test_pour_drops_an_island_that_holds_no_same_net_pad_or_via():
    pours = _pour_with(_wall_and_via(gnd_via=True))
    assert len(pours) == 1
    assert _shapely_pour(pours[0]).centroid.x < 20.0


def test_pour_drops_a_fragment_that_is_only_a_same_net_via_land():
    # A GND via inside the foreign wall's antipad leaves a disc of GND the
    # size of its own land: not a plane piece.
    copper = _wall_and_via(gnd_via=True)
    copper.append({**copper[1], "x": 20.0, "y": 20.0})
    pours = _pour_with(copper)
    assert len(pours) == 1
    assert _shapely_pour(pours[0]).centroid.x < 20.0


def test_pour_with_no_same_net_copper_keeps_every_fragment():
    assert len(_pour_with(_wall_and_via(gnd_via=False))) == 2
