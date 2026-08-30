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

from shapely.geometry import Point, Polygon  # type: ignore[import-untyped]

from precis.pcb.planes import cut_antipads, plane_pours

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
