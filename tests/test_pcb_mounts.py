"""gr496392 — corner mounting nuts with a concentric corner radius."""

from __future__ import annotations

import math
from typing import Any

import pytest

from precis.errors import BadInput
from precis.handlers.pcb import PcbHandler
from precis.pcb import mounts
from precis.pcb.session import mounting_holes_from_features, outline_from_features

from .test_pcb_declared_pins import _mini_design
from .test_pcb_profiles import pcb  # noqa: F401  (fixture)

_RECT = [[0, 0], [40, 0], [40, 30], [0, 30]]


def test_nuts_sit_at_radius_from_both_edges_concentric_with_the_arc() -> None:
    feats = mounts.corner_nut_features(_RECT)
    outline, nuts = feats[0], feats[1:]
    r = outline["geom"]["corner_radius_mm"]
    assert r == 5.0
    assert {(n["x"], n["y"]) for n in nuts} == {(5, 5), (35, 5), (35, 25), (5, 25)}
    # The rounded outline's corner arc is centred on the nut: every outline
    # point near the corner is r from the nut centre or farther along the
    # straight edges, and the arc points are exactly r away.
    pts = outline_from_features([outline])
    assert pts is not None
    near = [p for p in pts if p[0] < r - 1e-6 and p[1] < r - 1e-6]
    assert near
    assert all(
        math.hypot(p[0] - 5, p[1] - 5) == pytest.approx(r, abs=1e-6) for p in near
    )


def test_nut_is_a_plated_solder_nut_hole_with_envelope() -> None:
    holes = mounting_holes_from_features(mounts.corner_nut_features(_RECT)[1:])
    assert len(holes) == 4
    assert all(
        h.plated and h.ring_dia_mm == 6.2 and h.head_dia_mm == 5.56 for h in holes
    )


def test_default_radius_grows_with_a_big_body() -> None:
    feats = mounts.corner_nut_features(_RECT, body_dia_mm=12.0, ring_dia_mm=8.0)
    assert feats[0]["geom"]["corner_radius_mm"] == 6.5


@pytest.mark.parametrize(
    "path, kwargs, match",
    [
        ([[0, 0], [40, 0], [40, 30]], {}, "rectangle"),
        ([[0, 0], [40, 0], [40, 30], [0, 31]], {}, "rectangle"),
        (_RECT, {"radius_mm": 2.0}, "over the board edge"),
        ([[0, 0], [8, 0], [8, 30], [0, 30]], {}, "does not fit"),
        (_RECT, {"ring_dia_mm": 3.0}, "must exceed"),
    ],
)
def test_bad_geometry_is_refused(
    path: list[list[float]], kwargs: dict[str, Any], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        mounts.corner_nut_features(path, **kwargs)


@pytest.mark.db
def test_op_corner_nuts_stores_outline_and_four_nuts_and_is_idempotent(
    pcb: PcbHandler,  # noqa: F811
    store: Any,
) -> None:
    pcb.put(id="mnt-a", args=_mini_design())
    out = pcb.put(id="mnt-a", args={"op": "corner_nuts"})
    assert "4 corner nuts" in out.body
    pcb.put(id="mnt-a", args={"op": "corner_nuts", "radius_mm": 6.0})
    ref_id = store.get_ref(kind="pcb", id="mnt-a").id
    feats = store.pcb_features_list(ref_id)
    holes = [f for f in feats if f["ftype"] == "mounting_hole"]
    assert len(holes) == 4
    assert {(h["x"], h["y"]) for h in holes} == {(-9, -9), (9, -9), (9, 9), (-9, 9)}
    assert [
        f["geom"]["corner_radius_mm"] for f in feats if f["ftype"] == "outline"
    ] == [6.0]


@pytest.mark.db
def test_op_corner_nuts_without_outline_names_the_next_step(
    pcb: PcbHandler,  # noqa: F811
) -> None:
    design = _mini_design()
    design["features"] = []
    pcb.put(id="mnt-b", args=design)
    with pytest.raises(BadInput) as exc:
        pcb.put(id="mnt-b", args={"op": "corner_nuts"})
    assert "outline" in (exc.value.next or "")
