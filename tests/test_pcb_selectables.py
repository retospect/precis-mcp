"""The pure selectable-object catalogue — hand-built model, no DB."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from typing import Any

import pytest

from precis.pcb import gerber, gerber_view, selectables

_LAYERS = ["F.Cu", "In1.Cu", "B.Cu"]


def _rows() -> dict[str, Any]:
    return {
        "instances": [
            {"instance_id": 1, "refdes": "R1", "component_id": 10},
            {"instance_id": 2, "refdes": "R2", "component_id": 20},
            {"instance_id": 3, "refdes": "D1", "component_id": 30},
        ],
        "components": [{"component_id": c} for c in (10, 20, 30)],
        "pins": [
            {"pin_id": 100, "component_id": 10, "name": "1", "pad": "A"},
            # two live pins with one name on D1: ambiguous, must stay unmatched
            {"pin_id": 300, "component_id": 30, "name": "1", "pad": "1"},
            {"pin_id": 301, "component_id": 30, "name": "1", "pad": "2"},
        ],
        "nets": [{"net_id": 7, "name": "SIG"}, {"net_id": 8, "name": "GND"}],
    }


def _draw(role: str, refdes: str, x: float) -> dict[str, Any]:
    return {
        "width_mm": 0.15,
        "role": role,
        "refdes": refdes,
        "segments": [{"shape": "line", "start": [x, 0.0], "end": [x + 1.0, 0.0]}],
    }


def _model() -> dict[str, Any]:
    return {
        "layers": _LAYERS,
        "outline": [[0, 0], [30, 0], [30, 20], [0, 20]],
        "copper": [
            {
                "ctype": "track",
                "layer": "F.Cu",
                "net": "SIG",
                "width_mm": 0.25,
                "segments": [
                    {"shape": "line", "start": [2.0, 2.0], "end": [10.0, 2.0]}
                ],
            },
            {
                "ctype": "via",
                "net": "SIG",
                "x": 10.0,
                "y": 9.0,
                "dia_mm": 0.6,
                "drill_mm": 0.3,
                "span": ["F.Cu", "B.Cu"],
                "fixed": True,
            },
            # a fiducial's plane blocker rides in copper but is not a via
            {
                "ctype": "via",
                "net": "",
                "x": 1.0,
                "y": 1.0,
                "dia_mm": 2.0,
                "drill_mm": 0.0,
                "layers": list(_LAYERS),
                "role": "fiducial",
            },
            {
                "ctype": "pour",
                "layer": "In1.Cu",
                "net": "GND",
                "polygon": [[5, 5], [15, 5], [15, 15], [5, 15]],
            },
        ],
        "pads": [
            {
                "layer": "F.Cu",
                "net": "SIG",
                "shape": "rect",
                "x": 2.0,
                "y": 2.0,
                "w": 0.6,
                "h": 0.8,
                "refdes": "R1",
                "pin": "1",
            },
            # a second same-labelled pad of the same pin
            {
                "layer": "F.Cu",
                "net": "SIG",
                "shape": "rect",
                "x": 3.0,
                "y": 2.0,
                "w": 0.6,
                "h": 0.8,
                "refdes": "R1",
                "pin": "1",
            },
            {
                "layer": "F.Cu",
                "net": "",
                "shape": "polygon",
                "x": 20.0,
                "y": 10.0,
                "w": 2.0,
                "h": 2.0,
                "poly": [[19, 9], [21, 9], [21, 11], [19, 11]],
                "refdes": "R2",
                "pin": "1",
            },
            {
                "layer": "F.Cu",
                "net": "GND",
                "shape": "circle",
                "x": 25.0,
                "y": 15.0,
                "w": 1.0,
                "refdes": "D1",
                "pin": "1",
            },
            # a plated mounting-hole ring: no live instance
            {
                "layer": "F.Cu",
                "net": "",
                "shape": "circle",
                "x": 28.0,
                "y": 18.0,
                "w": 3.0,
                "drill": 1.5,
                "refdes": "MH1",
                "pin": "1",
            },
        ]
        + [
            {
                "layer": name,
                "net": "",
                "shape": "circle",
                "x": 29.0,
                "y": 1.0,
                "w": 1.0,
                "role": "fiducial",
            }
            for name in _LAYERS
        ],
        "silkscreen": {
            "top": [
                _draw("refdes", "R1", 1.0),
                _draw("refdes", "R1", 5.0),
                _draw("title", "", 8.0),
                _draw("outline", "R1", 0.0),
            ],
            "bottom": [_draw("sn-text", "", 2.0)],
        },
    }


def _build(model: dict[str, Any] | None = None) -> dict[str, selectables.SelectObject]:
    objects = selectables.build(model or _model(), **_rows())
    keys = [o.identity["key"] for o in objects]
    assert len(keys) == len(set(keys)), keys
    return {o.identity["key"]: o for o in objects}


def test_pads_carry_pin_identity_net_and_geometry() -> None:
    got = _build()
    pad = got["pad:1:1"].identity
    assert (pad["refdes"], pad["pin"], pad["layer"]) == ("R1", "1", "F.Cu")
    assert (pad["pin_id"], pad["pin_name"], pad["pad_number"]) == (100, "1", "A")
    assert (pad["net"], pad["net_id"]) == ("SIG", 7)
    assert pad["geometry"] == {"shape": "rect", "x": 2.0, "y": 2.0, "w": 0.6, "h": 0.8}
    assert got["pad:1:1"].hits == [
        {"shape": "rect", "layer": "F_Cu", "x": 1.7, "y": 1.6, "w": 0.6, "h": 0.8}
    ]
    assert "pad:1:1#2" in got  # the further same-labelled pad, in model order


def test_polygon_pad_is_a_pad_with_a_poly_hit_and_vertex_count_only() -> None:
    pad = _build()["pad:2:1"]
    assert pad.identity["geometry"]["vertices"] == 4
    assert '"poly"' not in json.dumps(pad.identity)
    assert pad.hits[0]["shape"] == "poly" and len(pad.hits[0]["points"]) == 4
    assert pad.identity["net"] is None and pad.identity["net_id"] is None
    # no pcb_pins row: never guessed
    assert pad.identity["pin_id"] is None


def test_ambiguous_pin_match_is_not_guessed() -> None:
    pad = _build()["pad:3:1"].identity
    assert (pad["pin_id"], pad["pin_name"], pad["pad_number"]) == (None, None, None)


def test_mounting_ring_has_no_instance_and_a_coordinate_key() -> None:
    pad = _build()["pad:MH1:1:28.0000:18.0000"].identity
    assert pad["instance_id"] is None
    assert pad["geometry"]["drill"] == 1.5


def test_via_excludes_the_fiducial_plane_blocker() -> None:
    got = _build()
    vias = [o for o in got.values() if o.identity["kind"] == "via"]
    assert [v.identity["key"] for v in vias] == ["via:10.0000:9.0000"]
    via = vias[0]
    assert via.identity["layers"] == ["F.Cu", "In1.Cu", "B.Cu"]
    assert (via.identity["dia_mm"], via.identity["drill_mm"]) == (0.6, 0.3)
    assert via.identity["fixed"] is True and via.identity["net_id"] == 7
    assert {h["layer"] for h in via.hits} == {"F_Cu", "In1_Cu", "B_Cu"}


def test_silk_groups_by_side_role_and_refdes_with_anchor() -> None:
    got = _build()
    label = got["silk:F_Silkscreen:refdes:R1"]
    assert len(label.hits) == 2  # both refdes draws are one group
    assert label.identity["content"] == "R1"
    assert label.identity["anchor"] == {
        "kind": "instance",
        "instance_id": 1,
        "refdes": "R1",
    }
    title = got["silk:F_Silkscreen:title:"].identity
    assert title["anchor"] == {"kind": "board"} and title["content"] is None
    assert "silk:B_Silkscreen:sn-text:" in got
    assert "silk:F_Silkscreen:outline:R1" in got


def test_outline_segments_and_fiducials() -> None:
    got = _build()
    assert [k for k in got if k.startswith("outline:")] == [
        f"outline:{i}" for i in range(4)
    ]
    last = got["outline:3"].identity
    assert (last["start"], last["end"], last["layer"]) == (
        [0.0, 20.0],
        [0.0, 0.0],
        "Edge_Cuts",
    )
    fid = got["fiducial:1"]
    assert fid.identity["id"] == "FID1" and fid.identity["layers"] == _LAYERS
    assert {h["layer"] for h in fid.hits} == {"F_Cu", "In1_Cu", "B_Cu"}
    assert "fiducial:2" not in got
    # fiducial pads are not pads
    assert not any(k.startswith("pad:") and "29.0000" in k for k in got)


def test_net_hits_are_its_tracks_and_pours() -> None:
    got = _build()
    assert [h["shape"] for h in got["net:7"].hits] == ["stroke"]
    assert got["net:7"].hits[0]["width"] == 0.25
    assert [(h["shape"], h["layer"]) for h in got["net:8"].hits] == [("poly", "In1_Cu")]
    assert got["net:8"].identity == {
        "kind": "net",
        "key": "net:8",
        "net_id": 8,
        "name": "GND",
    }


def test_instance_hit_is_the_pad_bbox_and_identity_is_the_given_one() -> None:
    given = [
        {
            "kind": "instance",
            "key": "instance:1",
            "instance_id": 1,
            "refdes": "R1",
            "footprint": {},
        }
    ]
    objects = selectables.build(_model(), **_rows(), identities=given)
    inst = next(o for o in objects if o.identity["key"] == "instance:1")
    assert inst.identity == given[0]
    (hit,) = inst.hits
    assert hit["layer"] == "" and hit["shape"] == "rect"
    assert (hit["x"], hit["y"], hit["w"], hit["h"]) == pytest.approx(
        (1.7, 1.6, 1.6, 0.8)
    )


def test_json_safe_and_overlay_renders_objects_in_priority_order() -> None:
    objects = selectables.build(_model(), **_rows())
    json.dumps([o.identity for o in objects], allow_nan=False)
    files = gerber.export_fab(_model(), name="t", allow_synthesized=True)
    manifest = {
        "board_ref_id": 1,
        "snapshot": "tok",
        "instances": [],
        "objects": objects,
    }
    svg = gerber_view.render_fab_svg(files, title="t", selection_manifest=manifest)
    assert '<g class="pcb-select">' in svg and 'id="pcb-objects"' in svg
    assert 'data-pcb-object="pad:1:1"' in svg and 'data-pcb-kind="via"' in svg
    kinds = re.findall(r'data-pcb-kind="(\w+)"', svg)
    order = [selectables.KIND_ORDER.index(k) for k in kinds]
    assert order == sorted(order)
    root = ET.fromstring(svg)
    meta = next(e for e in root.iter() if e.get("id") == "pcb-objects")
    assert [o["key"] for o in json.loads(meta.text or "")] == [
        o.identity["key"] for o in objects
    ]
    plain = gerber_view.render_fab_svg(files, title="t")
    assert "pcb-select" not in plain
