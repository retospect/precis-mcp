"""Non-fab mechanical 2D layers (gr451662): layers derive from the board
model with the expected outlines, check connectivity, and never touch the
fab bundle."""

from __future__ import annotations

import copy
import re
from typing import Any

import pytest
from shapely.geometry import Polygon, box

from precis.pcb import gerber, mech2d

_PADS = [
    {
        "layer": "F.Cu",
        "net": f"E{i}{j}",
        "shape": "rect",
        "x": 15.0 + i * 2.0,
        "y": 10.0 + j * 2.0,
        "w": 1.9,
        "h": 1.9,
        "role": "electrode",
    }
    for i in range(3)
    for j in range(3)
]
_MODEL: dict[str, Any] = {
    "layers": ["F.Cu", "B.Cu"],
    "outline": [[0.0, 0.0], [40.0, 0.0], [40.0, 30.0], [0.0, 30.0]],
    "copper": [
        {
            "ctype": "via",
            "net": "E00",
            "x": 5.0,
            "y": 20.0,
            "dia_mm": 0.6,
            "drill_mm": 0.3,
        },
    ],
    "pads": _PADS,
}


def _layers(**kw: Any) -> dict[str, mech2d.MechLayer]:
    return {
        layer.name: layer
        for layer in mech2d.mechanical_layers(_MODEL, mech2d.MechSpec(**kw))
    }


def test_layer_outlines() -> None:
    layers = _layers()
    ito, adh = layers["ito_cut"], layers["spacer_adhesive"]
    # ITO: board inset by 0.5 mm, minus the two alignment cutouts.
    assert ito.sheet.bounds == pytest.approx((0.5, 0.5, 39.5, 29.5))
    assert len(ito.sheet.interiors) == 2
    assert ito.sheet.area == pytest.approx(
        39 * 29 - sum(f.area for f in ito.alignment), rel=1e-9
    )
    # Adhesive: same plus the array window (electrode bbox + 1 mm margin).
    window = box(13.05, 8.05, 20.95, 15.95)
    assert adh.sheet.area == pytest.approx(ito.sheet.area - window.area, rel=1e-6)
    assert not adh.sheet.intersects(window.buffer(-0.01))


def test_alignment_is_hole_plus_slot_and_asymmetric() -> None:
    hole, slot = _layers()["ito_cut"].alignment
    assert slot.area > hole.area * 1.5  # slot is longer than the hole
    # 180-degree rotation about the board centre must not map features onto
    # themselves.
    cx, cy = 20.0, 15.0
    rot = Polygon([(2 * cx - x, 2 * cy - y) for x, y in slot.exterior.coords])
    assert rot.intersection(hole).area == 0
    assert rot.intersection(slot).area == 0


def test_sheets_are_connected_and_surround_alignment() -> None:
    for layer in _layers(via_covers=True).values():
        mech2d.check_connected(layer)


def test_check_connected_rejects_split_sheet() -> None:
    adh = _layers()["spacer_adhesive"]
    split = mech2d.MechLayer(
        "x", "spacer_adhesive", adh.sheet.difference(box(-1, 5, 41, 6)), adh.alignment
    )
    with pytest.raises(ValueError, match="components"):
        mech2d.check_connected(split)


def test_via_covers_cut_openings() -> None:
    plain = _layers()["spacer_adhesive"].sheet
    covered = _layers(via_covers=True)["spacer_adhesive"].sheet
    assert len(covered.interiors) == len(plain.interiors) + 1
    assert covered.area < plain.area


def test_kerf_offsets_at_export_only() -> None:
    layer = _layers()["spacer_adhesive"]
    before = layer.sheet.wkt
    nominal = mech2d.cut_paths(layer)
    kerfed = mech2d.cut_paths(layer, kerf_mm=0.1)
    assert layer.sheet.wkt == before  # source geometry untouched
    # outer path grows by kerf/2 per side
    assert max(x for x, _ in kerfed[0]) == pytest.approx(
        max(x for x, _ in nominal[0]) + 0.05
    )
    # the window cutout path moves into the kept sheet's side: it grows
    assert Polygon(kerfed[-1]).area != Polygon(nominal[-1]).area


def test_mirror_flag_flips_x_about_sheet_centre() -> None:
    layer = _layers()["ito_cut"]
    plain = mech2d.cut_paths(layer)[0]
    mirrored = mech2d.cut_paths(layer, mirror=True)[0]
    cx = 20.0
    assert sorted(round(2 * cx - x, 6) for x, _ in plain) == sorted(
        round(x, 6) for x, _ in mirrored
    )
    assert sorted(y for _, y in plain) == sorted(y for _, y in mirrored)


def test_svg_frame_and_fingerprint() -> None:
    files = mech2d.export_mechanical(_MODEL, name="b")
    fp = mech2d.board_fingerprint(_MODEL)
    assert sorted(files) == [
        f"b-mech-ito_cut-{fp[:12]}.svg",
        f"b-mech-spacer_adhesive-{fp[:12]}.svg",
    ]
    svg = files[f"b-mech-ito_cut-{fp[:12]}.svg"]
    assert f'data-board-fingerprint="{fp}"' in svg
    vb = re.search(r'viewBox="(\S+) (\S+) (\S+) (\S+)"', svg)
    assert vb is not None
    # Y flipped: top of the ITO (board y 29.5) plus 1 mm margin -> svg y -30.5.
    assert float(vb.group(2)) == pytest.approx(-30.5)
    assert float(vb.group(1)) == pytest.approx(-0.5)


def test_fingerprint_tracks_board_geometry() -> None:
    moved = copy.deepcopy(_MODEL)
    moved["pads"][0]["x"] += 0.1
    assert mech2d.board_fingerprint(moved) != mech2d.board_fingerprint(_MODEL)


def test_fab_export_unchanged_by_mechanical_layers() -> None:
    before = gerber.export_fab(copy.deepcopy(_MODEL), name="b")
    mech2d.export_mechanical(_MODEL, name="b")
    after = gerber.export_fab(copy.deepcopy(_MODEL), name="b")
    assert before == after
    assert not any("mech" in f.lower() or f.endswith(".svg") for f in after)


@pytest.mark.parametrize("bad", ["../../x", "/abs/p", ".hidden", "a b", ""])
def test_export_name_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="unsafe export name"):
        mech2d.export_mechanical(_MODEL, name=bad)


def test_bowtie_outline_is_repaired_or_refused() -> None:
    model = copy.deepcopy(_MODEL)
    model["outline"] = [[0, 0], [40, 30], [40, 0], [0, 30]]  # bow-tie
    try:
        layers = mech2d.mechanical_layers(model)
    except ValueError as exc:
        assert "board outline" in str(exc) or "too small" in str(exc)
    else:
        assert all(not layer.sheet.is_empty for layer in layers)
    model["outline"] = [[0, 0], [10, 10], [20, 20]]  # zero-area
    with pytest.raises(ValueError, match="board outline"):
        mech2d.mechanical_layers(model)


def test_fingerprint_covers_electrode_size_and_spec() -> None:
    base = mech2d.board_fingerprint(_MODEL)
    bigger = copy.deepcopy(_MODEL)
    bigger["pads"][0]["w"] = 2.5
    assert mech2d.board_fingerprint(bigger) != base
    for kw in (
        {"sheet_inset_mm": 0.6},
        {"window_margin_mm": 1.5},
        {"via_covers": True},
        {"align_hole_dia_mm": 2.2},
    ):
        assert mech2d.board_fingerprint(_MODEL, mech2d.MechSpec(**kw)) != base
    via = copy.deepcopy(_MODEL)
    via["copper"][0]["dia_mm"] = 0.9
    assert mech2d.board_fingerprint(via) != base


def test_window_swallowing_alignment_feature_is_rejected() -> None:
    for margin in (14.0, 9.3):  # swallows the hole / leaves < 1 mm web
        layers = mech2d.mechanical_layers(
            _MODEL, mech2d.MechSpec(window_margin_mm=margin)
        )
        with pytest.raises(ValueError, match="alignment feature|components"):
            mech2d.check_connected(layers[1])
    # web check proper: window edge 0.55 mm from the hole, sheet intact
    adh = mech2d.mechanical_layers(_MODEL)[1]
    hole = adh.alignment[0]
    thin = mech2d.MechLayer(
        "t",
        "spacer_adhesive",
        adh.sheet.difference(box(4.55, 2.0, 8.0, 5.0)),
        adh.alignment,
    )
    assert hole.distance(box(4.55, 2.0, 8.0, 5.0)) < 1.0
    with pytest.raises(ValueError, match="less than"):
        mech2d.check_connected(thin)


def test_empty_sheet_svg_raises() -> None:
    empty = mech2d.MechLayer("e", "ito_cut", Polygon())
    with pytest.raises(ValueError, match="empty"):
        mech2d.layer_svg(empty, fingerprint="x")
