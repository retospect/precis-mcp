"""The shared sheet job (:mod:`precis.sheet`): model validation, the laser
SVG writer and the DXF R12 writer — build-1 acceptance of
docs/backlog/flatpack-furniture-generator.md.

The DXF reader here is test-local (group-code pairs → entities); the tree
has no DXF dependency and gains none.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET

import pytest

from precis.sheet import (
    Circle,
    Layer,
    Part,
    Polyline,
    Sheet,
    SheetExportError,
    SheetJob,
    SheetJobError,
    order_layers,
)
from precis.sheet.dxf import export_dxf
from precis.sheet.svg_laser import PALETTE, export_laser_svg

SVG_NS = "{http://www.w3.org/2000/svg}"


def _job() -> SheetJob:
    """One layer per vector op; two parts; a drill circle; one part-less
    open score line."""
    job = SheetJob(
        Sheet(100, 80, "corrugated-3mm", 3.0),
        layers=[
            Layer("engrave", "engrave_vector"),
            Layer("score", "score", {"laser": {"power_pct": 25, "speed_mm_s": 80}}),
            Layer("holes", "drill", {"laser": {"passes": 2}}),
            Layer("cut", "cut", {"cricut": {"pressure": 3, "blade": "fine"}}),
        ],
        parts=[Part("a", "panel A"), Part("b", "panel B")],
    )
    job.shapes += [
        Polyline("cut", ((10, 10), (40, 10), (40, 40), (10, 40)), part="a"),
        Polyline("cut", ((20, 20), (30, 20), (30, 25), (20, 25)), part="a", inner=True),
        Polyline("cut", ((50, 10), (90, 10), (90, 70), (50, 70)), part="b"),
        Circle("holes", 70, 40, 1.5, part="b", inner=True),
        Polyline("score", ((12, 25), (38, 25)), closed=False, part="a"),
        Polyline("engrave", ((60, 60), (80, 60)), closed=False),
    ]
    return job


# ── model ──────────────────────────────────────────────────────────────


def test_validate_accepts_the_fixture() -> None:
    assert _job().validate().problems() == []


def test_order_layers_is_the_machine_run_order() -> None:
    layers = [Layer("c", "cut"), Layer("r", "engrave_raster"), Layer("d", "drill")]
    assert [layer.name for layer in order_layers(layers)] == ["r", "d", "c"]


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (lambda j: j.layers.append(Layer("x", "mill")), "unknown op 'mill'"),
        (lambda j: j.layers.append(Layer("cut", "cut")), "declared twice"),
        (
            lambda j: j.shapes.append(Circle("nope", 1, 1, 1)),
            "layer 'nope' is not declared",
        ),
        (
            lambda j: j.shapes.append(Circle("cut", 1, 1, 1, part="zz")),
            "part 'zz' is not declared",
        ),
        (
            lambda j: j.layers.append(Layer("y", "cut", {"plotter": {}})),
            "unknown machine 'plotter'",
        ),
        (lambda j: j.shapes.append(Polyline("cut", ((0, 0),))), "at least two points"),
    ],
)
def test_validate_names_each_problem(mutate, needle) -> None:
    job = _job()
    mutate(job)
    with pytest.raises(SheetJobError, match=needle):
        job.validate()


def test_shapes_on_orders_parts_then_inner_before_outer() -> None:
    job = _job()
    cut = job.shapes_on("cut")
    assert [(s.part, s.inner) for s in cut] == [("a", True), ("a", False), ("b", False)]


# ── laser SVG ──────────────────────────────────────────────────────────


def test_laser_svg_one_group_per_layer_with_palette_and_settings() -> None:
    out = export_laser_svg(_job())
    root = ET.fromstring(out.text)
    assert root.get("width") == "100mm" and root.get("height") == "80mm"
    assert root.get("viewBox") == "0 0 100 80"
    groups = root.findall(f"{SVG_NS}g")
    assert [g.get("id") for g in groups] == ["engrave", "score", "holes", "cut"]
    assert [g.get("stroke") for g in groups] == list(PALETTE[:4])
    assert [g.get("data-op") for g in groups] == [
        "engrave_vector",
        "score",
        "drill",
        "cut",
    ]
    score = groups[1]
    assert (
        score.get("data-power-pct"),
        score.get("data-speed-mm-s"),
        score.get("data-passes"),
    ) == ("25", "80", "1")
    holes = groups[2]
    assert holes.get("data-passes") == "2" and holes.get("data-power-pct") == "100"
    assert groups[3].get("fill") == "none"


def test_laser_svg_document_order_is_cut_order_and_y_is_flipped() -> None:
    root = ET.fromstring(export_laser_svg(_job()).text)
    cut = root.findall(f"{SVG_NS}g")[3]
    paths = cut.findall(f"{SVG_NS}path")
    assert [(p.get("data-part"), p.get("data-role")) for p in paths] == [
        ("a", "inner"),
        ("a", "outer"),
        ("b", "outer"),
    ]
    # sheet (10, 10) is SVG (10, 70); the outline closes with Z
    assert paths[1].get("d") == "M 10 70 L 40 70 L 40 40 L 10 40 Z"
    score = root.findall(f"{SVG_NS}g")[1].find(f"{SVG_NS}path")
    assert score is not None and score.get("d") == "M 12 55 L 38 55"
    circle = root.findall(f"{SVG_NS}g")[2].find(f"{SVG_NS}circle")
    assert circle is not None
    assert (circle.get("cx"), circle.get("cy"), circle.get("r")) == ("70", "40", "1.5")


def test_laser_svg_notes_say_which_layers_used_defaults() -> None:
    out = export_laser_svg(_job())
    by_layer = {n.split()[1]: n for n in out.notes if n.startswith("  #")}
    assert "laser defaults" in by_layer["engrave"]
    assert "laser defaults" in by_layer["cut"]  # cricut settings only
    assert "laser defaults" not in by_layer["score"]
    assert (
        f"{PALETTE[1]} score op=score power 25 % speed 80 mm/s passes 1"
        in by_layer["score"]
    )
    # the same table rides in the file's leading comment
    assert out.text.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<!--\n')
    assert by_layer["score"] in out.text


def test_laser_svg_refuses_raster_by_layer_name() -> None:
    job = _job()
    job.layers.insert(0, Layer("photo", "engrave_raster"))
    with pytest.raises(SheetExportError, match="layer 'photo': op 'engrave_raster'"):
        export_laser_svg(job)


# ── DXF ────────────────────────────────────────────────────────────────


def _dxf_pairs(text: str) -> list[tuple[int, str]]:
    lines = text.splitlines()
    assert len(lines) % 2 == 0
    return [(int(lines[i]), lines[i + 1]) for i in range(0, len(lines), 2)]


def _dxf_entities(text: str) -> list[dict]:
    """Minimal R12 reader: entities of the ENTITIES section, POLYLINE
    vertices folded into their owner."""
    pairs = _dxf_pairs(text)
    i = pairs.index((2, "ENTITIES"))
    ents: list[dict] = []
    cur: dict | None = None
    for code, value in pairs[i + 1 :]:
        if (code, value) == (0, "ENDSEC"):
            break
        if code == 0:
            if value == "VERTEX":
                cur = {"type": "VERTEX"}
                ents[-1]["vertices"].append(cur)
            elif value == "SEQEND":
                cur = {"type": "SEQEND"}  # its own group codes are dropped
            else:
                cur = {"type": value, "vertices": []}
                ents.append(cur)
            continue
        assert cur is not None
        cur[code] = value
    return ents


def test_dxf_layers_match_and_entities_round_trip() -> None:
    job = _job()
    out = export_dxf(job)
    pairs = _dxf_pairs(out.text)
    assert pairs[0][0] == 999 and "mm" in pairs[0][1]
    assert (1, "AC1009") in pairs
    i = pairs.index((2, "LAYER"))
    layer_names = [v for c, v in pairs[i:] if c == 2 and v != "LAYER"][:4]
    assert layer_names == ["engrave", "score", "holes", "cut"]

    ents = _dxf_entities(out.text)
    assert [e["type"] for e in ents] == ["POLYLINE"] * 2 + ["CIRCLE"] + ["POLYLINE"] * 3
    assert [e[8] for e in ents] == ["engrave", "score", "holes", "cut", "cut", "cut"]
    circle = ents[2]
    assert (float(circle[10]), float(circle[20]), float(circle[40])) == pytest.approx(
        (70, 40, 1.5), abs=1e-6
    )
    inner = ents[3]
    assert inner[70] == "1"
    got = [(float(v[10]), float(v[20])) for v in inner["vertices"]]
    assert got == pytest.approx([(20, 20), (30, 20), (30, 25), (20, 25)], abs=1e-6)
    assert ents[0][70] == "0"  # open score/engrave lines stay open
    assert pairs[-1] == (0, "EOF")


def test_dxf_round_trips_fractional_mm() -> None:
    job = SheetJob(Sheet(10, 10), [Layer("cut", "cut")])
    pts = ((0.123456, 0.654321), (math.pi, math.e), (1 / 3, 2 / 3))
    job.shapes.append(Polyline("cut", pts))
    ents = _dxf_entities(export_dxf(job).text)
    got = [(float(v[10]), float(v[20])) for v in ents[0]["vertices"]]
    for (gx, gy), (ex, ey) in zip(got, pts, strict=True):
        assert abs(gx - ex) < 1e-6 and abs(gy - ey) < 1e-6


def test_dxf_refuses_raster_by_layer_name() -> None:
    job = _job()
    job.layers.append(Layer("photo", "engrave_raster"))
    with pytest.raises(SheetExportError, match="layer 'photo'"):
        export_dxf(job)
