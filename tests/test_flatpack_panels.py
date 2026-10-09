"""Panel geometry of :mod:`precis_se.flatpack` — build-2 acceptance of
docs/backlog/flatpack-furniture-generator.md.

The load-bearing check is **ownership tiling**: sample the box on a grid
that avoids every panel boundary, extrude each panel's outline through its
frame, and require that every point of the shell (and of each shelf slab)
is owned by exactly one panel while nothing outside is owned at all. That
is "every finger mates its slot" and "every corner cube belongs to exactly
one panel" in one assertion, for both joint types and with shelves.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
import shapely
from shapely.geometry import Point, Polygon, box

from precis_se.flatpack import FlatpackError, Panel, box_panels, flatpack_box
from precis_se.flatpack.panels import (
    dog_bones,
    finger_count,
    inside_corners,
    kerf_offset,
    rings,
)

T = 3.0


def _by_id(panels: list[Panel] | tuple[Panel, ...]) -> dict[str, Panel]:
    return {p.id: p for p in panels}


# ── ownership tiling ───────────────────────────────────────────────────


def _expected_shell(
    pts: np.ndarray, W: float, H: float, D: float, t: float, shelves: list[float]
) -> np.ndarray:
    x, y, z = pts[:, 0], pts[:, 1], pts[:, 2]
    shell = (x < t) | (x > W - t) | (z < t) | (z > H - t) | (y > D - t)
    for h in shelves:
        shell |= (x > t) & (x < W - t) & (y < D - t) & (z > t + h) & (z < 2 * t + h)
    return shell


def _ownership(
    panels: list[Panel], W: float, H: float, D: float, step: float = 1.0
) -> tuple[np.ndarray, np.ndarray]:
    axes = [np.arange(step / 2, L, step) for L in (W, D, H)]
    gx, gy, gz = np.meshgrid(*axes, indexing="ij")
    pts = np.column_stack([gx.ravel(), gy.ravel(), gz.ravel()])
    owners = np.zeros(len(pts), dtype=int)
    for p in panels:
        d = pts - np.array(p.frame.origin)
        u, v, n = (
            d @ np.array(p.frame.eu),
            d @ np.array(p.frame.ev),
            d @ np.array(p.frame.en),
        )
        slab = (n > 0) & (n < p.thickness)
        inside = shapely.contains_xy(p.polygon, u[slab], v[slab])
        owners[np.flatnonzero(slab)[inside]] += 1
    return pts, owners


@pytest.mark.parametrize("joint", ["finger", "straight"])
@pytest.mark.parametrize(
    ("W", "H", "D", "shelves"),
    [(50, 50, 50, []), (50, 50, 50, [22]), (90, 60, 45, [20, 36])],
)
def test_panels_tile_the_shell_exactly_once(joint, W, H, D, shelves) -> None:
    panels = box_panels(W, H, D, t=T, shelves=shelves, joint=joint)
    pts, owners = _ownership(panels, W, H, D)
    shell = _expected_shell(pts, W, H, D, T, shelves)
    assert owners.max() == 1, "two panels claim the same material"
    assert np.array_equal(owners == 1, shell), (
        "a void in the shell, or material outside it"
    )


def test_t6_retiles_with_fewer_wider_fingers() -> None:
    panels = box_panels(50, 50, 50, t=6)
    pts, owners = _ownership(panels, 50, 50, 50)
    assert owners.max() == 1
    assert np.array_equal(owners == 1, _expected_shell(pts, 50, 50, 50, 6, []))
    side = _by_id(panels)["side-l"].polygon
    assert _spans(side, box(0, 44, 50, 50)) == pytest.approx(
        [(0, 50 / 3), (100 / 3, 50)]
    )


# ── the panel table ────────────────────────────────────────────────────


def test_finger_cube_panel_figures() -> None:
    panels = _by_id(box_panels(50, 50, 50, t=T, shelves=[22]))
    assert len(panels) == 6
    for pid in ("side-l", "side-r", "top", "bottom", "back"):
        assert panels[pid].size == (50, 50)
    shelf = panels["shelf-1"]
    assert shelf.size == (50, 47)
    body = box(3, 0, 47, 47)
    tabs = shelf.polygon.difference(body)
    assert len(tabs.geoms) == 4
    for tab in tabs.geoms:
        minx, miny, maxx, maxy = tab.bounds
        assert (maxx - minx, maxy - miny) == pytest.approx((3, 9))
    assert len(panels["side-l"].polygon.interiors) == 2


def test_straight_cube_panel_figures() -> None:
    panels = _by_id(box_panels(50, 50, 50, t=T, joint="straight", shelves=[22]))
    assert panels["side-l"].size == (50, 50)
    assert panels["top"].size == (44, 50) and panels["bottom"].size == (44, 50)
    assert panels["back"].size == (44, 44)
    assert panels["shelf-1"].size == (50, 47)
    assert len(panels["side-r"].polygon.interiors) == 2


def _spans(poly: Polygon, strip: Polygon) -> list[tuple[float, float]]:
    """Extents along the strip's long axis of the material inside ``strip``."""
    pieces = poly.intersection(strip)
    geoms = list(getattr(pieces, "geoms", [pieces]))
    sw, sh = strip.bounds[2] - strip.bounds[0], strip.bounds[3] - strip.bounds[1]
    axis = (0, 2) if sw >= sh else (1, 3)
    return sorted((g.bounds[axis[0]], g.bounds[axis[1]]) for g in geoms if g.area > 0)


def test_every_cube_edge_has_five_fingers_of_ten_mm() -> None:
    assert finger_count(50, T, "x") == (5, 10.0)
    panels = _by_id(box_panels(50, 50, 50, t=T))
    side, top, back = (panels[k].polygon for k in ("side-l", "top", "back"))
    odd = [(0, 10), (20, 30), (40, 50)]
    even = [(10, 20), (30, 40)]
    # side owns the odd positions on every edge
    assert _spans(side, box(0, 47, 50, 50)) == odd
    assert _spans(side, box(0, 0, 50, 3)) == odd
    assert _spans(side, box(47, 0, 50, 50)) == odd
    # top: even against the sides, odd against the back with t-short ends
    assert _spans(top, box(0, 0, 3, 50)) == even
    assert _spans(top, box(0, 47, 50, 50)) == [(3, 10), (20, 30), (40, 47)]
    # back: even everywhere
    assert _spans(back, box(0, 0, 3, 50)) == even
    assert _spans(back, box(0, 47, 50, 50)) == even


def test_finger_count_rule() -> None:
    assert finger_count(50, 6, "x") == (3, pytest.approx(50 / 3))
    assert finger_count(36, 3, "x") == (5, pytest.approx(7.2))  # 4 → ties round up
    assert finger_count(18, 3, "x") == (3, 6.0)
    with pytest.raises(FlatpackError, match=r"edge x is 17\.9 mm.*6t = 18 mm"):
        finger_count(17.9, 3, "x")


def test_short_edge_is_refused_naming_the_minimum() -> None:
    with pytest.raises(
        FlatpackError, match=r"top-back/bottom-back \(W\) is 12 mm.*18 mm"
    ):
        box_panels(12, 50, 50, t=T)
    # straight joints have no such floor
    assert len(box_panels(12, 50, 50, t=T, joint="straight")) == 5


def test_shelf_refusals_name_the_rule() -> None:
    with pytest.raises(FlatpackError, match="shelf height 1 mm must lie within"):
        box_panels(50, 50, 50, t=T, shelves=[1])
    with pytest.raises(FlatpackError, match="closer than 2t"):
        box_panels(50, 50, 50, t=T, shelves=[20, 24])
    with pytest.raises(FlatpackError, match="D ≥ 11t"):
        box_panels(50, 50, 50, t=6, shelves=[20])


def test_fit_widens_every_gap_and_slot_by_fit() -> None:
    plain = _by_id(box_panels(50, 50, 50, t=T, shelves=[22]))
    fitted = _by_id(box_panels(50, 50, 50, t=T, shelves=[22], fit=0.4))
    # a side's top-edge gaps widen from 10 to 10.4; its fingers lose 0.2 a side
    assert _spans(fitted["side-l"].polygon, box(0, 47, 50, 50)) == pytest.approx(
        [(0, 9.8), (20.2, 29.8), (40.2, 50)]
    )
    for pid in ("side-l", "side-r"):
        for plain_hole, fitted_hole in zip(
            plain[pid].polygon.interiors, fitted[pid].polygon.interiors, strict=True
        ):
            pw, ph = _size(Polygon(plain_hole))
            fw, fh = _size(Polygon(fitted_hole))
            assert (pw, ph) == pytest.approx((9, 3))
            assert (fw, fh) == pytest.approx((9.4, 3.4))
    # the shelf's tabs do not change: the clearance lives in the slot
    assert fitted["shelf-1"].polygon.equals(plain["shelf-1"].polygon)


def _size(poly: Polygon) -> tuple[float, float]:
    minx, miny, maxx, maxy = poly.bounds
    return (maxx - minx, maxy - miny)


# ── kerf ───────────────────────────────────────────────────────────────


def test_kerf_grows_outlines_and_shrinks_slots_by_the_mitre_formula() -> None:
    d = 0.1
    for p in box_panels(50, 50, 50, t=T, shelves=[22]):
        out = kerf_offset(p.polygon, 2 * d)
        before, after = Polygon(p.polygon.exterior), Polygon(out.exterior)
        assert after.area - before.area == pytest.approx(
            before.length * d + 4 * d * d, abs=1e-6
        )
        assert len(out.interiors) == len(p.polygon.interiors)
        for hole in p.polygon.interiors:
            h0 = Polygon(hole)
            h1 = min(
                (Polygon(r) for r in out.interiors),
                key=lambda r: r.centroid.distance(h0.centroid),
            )
            assert h0.area - h1.area == pytest.approx(
                h0.length * d - 4 * d * d, abs=1e-6
            )


def test_kerf_zero_is_identity() -> None:
    p = box_panels(50, 50, 50, t=T)[0]
    assert kerf_offset(p.polygon, 0) is p.polygon


# ── dog-bones ──────────────────────────────────────────────────────────


def test_no_dog_bones_at_cutter_zero() -> None:
    for p in box_panels(50, 50, 50, t=T, shelves=[22]):
        for ring in rings(p.polygon):
            for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1], strict=True):
                assert x0 == x1 or y0 == y1, "a non-rectilinear edge"


def test_dog_bones_sit_on_the_bisector_through_the_corner() -> None:
    cutter_d = 1.0
    r = cutter_d / 2
    plain = _by_id(box_panels(50, 50, 50, t=T, shelves=[22]))
    boned = _by_id(box_panels(50, 50, 50, t=T, shelves=[22], cutter_d=cutter_d))
    for pid, p in plain.items():
        corners = inside_corners(p.polygon)
        expected = {
            "side-l": 20,
            "side-r": 20,
            "top": 12,
            "bottom": 12,
            "back": 16,
            "shelf-1": 8,
        }
        assert len(corners) == expected[pid]
        bite = (
            r * r * (math.pi / 2 - 1)
        )  # the circle's two segments inside the material
        assert p.polygon.area - boned[pid].polygon.area == pytest.approx(
            len(corners) * bite, rel=0.02
        )
        for (cx, cy), (bx, by) in corners:
            centre = Point(cx + r * bx, cy + r * by)
            assert centre.distance(Point(cx, cy)) == pytest.approx(r)
            assert boned[pid].polygon.boundary.distance(Point(cx, cy)) < 1e-6
            for sign in (1, -1):
                probe = Point(
                    cx + 0.4 * r * sign * -by + 0.2 * r * bx,
                    cy + 0.4 * r * sign * bx + 0.2 * r * by,
                )
                assert p.polygon.contains(probe) and not boned[pid].polygon.contains(
                    probe
                )
        assert dog_bones(p.polygon, 0) is p.polygon


# ── the generator's figures and echo ───────────────────────────────────


def test_figures_fall_back_to_zero_and_say_so() -> None:
    fp = flatpack_box(50, 50, 50)
    assert (fp.figures.fit, fp.figures.kerf) == (0.0, 0.0)
    assert (
        fp.figures.fit_source == "default: no recorded fit for corrugated-3mm on laser"
    )
    assert (
        "kerf 0 mm (default: no recorded kerf for corrugated-3mm on laser)"
        in fp.job.notes
    )
    fp2 = flatpack_box(
        50, 50, 50, fit=0.1, kerf=0.2, cutter_d=3, material="plywood-6mm"
    )
    assert fp2.t == 6.0 and fp2.figures.machine == "cnc"
    assert (fp2.figures.fit_source, fp2.figures.kerf_source) == (
        "explicit fit",
        "explicit kerf",
    )
    with pytest.raises(FlatpackError, match="unknown material 'mdf'"):
        flatpack_box(50, 50, 50, material="mdf")


def test_params_echo_is_json_shaped() -> None:
    fp = flatpack_box(50, 50, 50, shelves=[22], sheet=(300, 200), shelf_load_n=5)
    assert fp.params == {
        "W": 50,
        "H": 50,
        "D": 50,
        "material": "corrugated-3mm",
        "t": 3.0,
        "shelves": [22.0],
        "joint": "finger",
        "fit": None,
        "kerf": None,
        "cutter_d": 0.0,
        "sheet": [300, 200],
        "shelf_load_n": 5,
    }
