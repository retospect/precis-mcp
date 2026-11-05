"""Local copper previews preserve the geometry accepted by routing."""

import json
from types import SimpleNamespace

from shapely.geometry import Point, box

from precis.pcb import local
from precis.pcb.realize import RealizedTrack, RealizedVia, RealizeResult


def test_copper_json_preserves_validated_via_coordinates(tmp_path) -> None:
    site = 5.800521
    pad = box(4.5, 4.5, 5.5, 5.5)
    assert Point(site, site).distance(pad) - 0.225 >= 0.2
    assert Point(round(site, 4), round(site, 4)).distance(pad) - 0.225 < 0.2
    track = RealizedTrack(
        seg_id=0,
        net_id=0,
        layer=0,
        segments=(
            {"shape": "line", "start": [site, site], "end": [7.1234567, 8.7654321]},
        ),
        length_mm=3.327186,
        blocked_by=None,
        width_mm=0.150051,
    )
    via = RealizedVia(
        seg_id=0,
        net_id=0,
        x=site,
        y=site,
        dia_mm=0.45,
        drill_mm=0.2,
        layer_lo=0,
        layer_hi=1,
        endpoint="a",
    )
    route = local.LocalRoute(
        slug="precision",
        nets={"N0": {"status": "realized", "note": None}},
        routed=["N0"],
        failed=[],
        dangling=[],
        unrouted=[],
        tracks=1,
        vias=1,
        length_mm=track.length_mm,
        drc_errors=0,
        digest={},
        result=RealizeResult((track,), (via,), ()),
        design=SimpleNamespace(layer_names=["top", "bottom"]),
        ir=SimpleNamespace(net_name=["N0"]),
    )
    path = tmp_path / "copper.json"
    path.write_text(json.dumps(local.copper_json(route)), encoding="utf-8")
    saved = json.loads(path.read_text(encoding="utf-8"))
    saved_track, saved_via = saved["copper"]
    assert (saved_via["x"], saved_via["y"]) == (site, site)
    assert Point(saved_via["x"], saved_via["y"]).distance(pad) - 0.225 >= 0.2
    assert saved_via == {
        "ctype": "via",
        "net": "N0",
        "x": site,
        "y": site,
        "dia_mm": via.dia_mm,
        "drill_mm": via.drill_mm,
        "span": ["top", "bottom"],
    }
    assert saved_track["segments"] == list(track.segments)
    assert saved_track["width_mm"] == track.width_mm
    assert saved_track["length_mm"] == round(track.length_mm, 4)
    assert saved["routes"] == {"N0": {"status": "realized", "note": None}}
