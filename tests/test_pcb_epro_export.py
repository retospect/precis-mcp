"""EasyEDA Pro ``.epro2`` writer (docs/backlog/pcb-epro-export.md slice 2b).

Pure half over an in-memory model (no DB): structure, referential
integrity, determinism, the synthesized-pad refusal, and THE frame test —
write a model, read it back through :mod:`precis.pcb.epro`, and every pad
lands where the model put it, top and bottom, at several rotations. One
handler-level test covers ``view='epro'`` over a live store.
"""

from __future__ import annotations

import io
import json
import math
import os
import pathlib
import zipfile
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.pcb import PcbHandler
from precis.pcb import epro, epro_write, padplace
from precis.pcb.gerber import SynthesizedPadError

_LAYERS = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
#: 0.5 um, in mm — the "one quantum" the backlog item names.
_TOL_MM = 0.0005

#: Asymmetric on purpose: three different pads, none on an axis of symmetry,
#: so a wrong mirror axis or rotation sense moves at least one of them.
_ASYM_FP = {
    "pads": [
        {
            "number": "1",
            "shape": "RECT",
            "x": -1.2,
            "y": 0.5,
            "w": 0.6,
            "h": 0.9,
            "rot": 0.0,
            "layer": "F.Cu",
            "drill": None,
        },
        {
            "number": "2",
            "shape": "OVAL",
            "x": 0.8,
            "y": 0.3,
            "w": 0.5,
            "h": 1.1,
            "rot": 0.0,
            "layer": "F.Cu",
            "drill": None,
        },
        {
            "number": "3",
            "shape": "ELLIPSE",
            "x": 0.1,
            "y": -1.4,
            "w": 1.0,
            "h": 1.0,
            "rot": 0.0,
            "layer": "F.Cu",
            "drill": 0.4,
        },
    ],
}

_POLY_FP = {
    "pads": [
        {
            "number": "1",
            "shape": "polygon",
            "x": 0.5,
            "y": 0.25,
            "w": 1.0,
            "h": 0.5,
            "poly": [[0.0, 0.0], [1.0, 0.0], [1.0, 0.5], [0.0, 0.5]],
            "rot": 0.0,
            "layer": "F.Cu",
            "drill": None,
        },
    ],
}

_OUTLINE = [[0.0, 0.0], [30.0, 0.0], [30.0, 20.0], [0.0, 20.0]]


def _inst(refdes, x, y, rot, layer, fp="ASYM3") -> dict[str, Any]:
    return {
        "refdes": refdes,
        "x": x,
        "y": y,
        "rot": rot,
        "layer": layer,
        "footprint": fp,
    }


_INSTANCES = [
    _inst("R1", 6.0, 5.0, 0.0, "top"),
    _inst("R2", 15.0, 5.0, 90.0, "top"),
    _inst("R3", 24.0, 5.0, 270.0, "top"),
    _inst("R4", 6.0, 14.0, 0.0, "bottom"),
    _inst("R5", 15.0, 14.0, 90.0, "bottom"),
    _inst("R6", 24.0, 14.0, 270.0, "bottom"),
    _inst("R7", 10.0, 10.0, 37.0, "top"),
    _inst("R8", 20.0, 10.0, 180.0, "bottom"),
    _inst("P1", 27.0, 17.0, 90.0, "bottom", fp="POLY1"),
]


def _model(
    instances: list[dict[str, Any]] | None = None,
    *,
    outline: list[list[float]] | None = None,
    extra_pads: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """A fab model assembled the way ``_fab_model`` does it — pads through
    :func:`padplace.place_footprint_pads`, no DB."""
    insts = list(_INSTANCES if instances is None else instances)
    fps = {"ASYM3": _ASYM_FP, "POLY1": _POLY_FP}
    pads: list[dict[str, Any]] = []
    drills: list[dict[str, Any]] = []
    for inst in insts:
        # 1 -> NET_A, 2 -> GND, 3 -> unconnected on every part but R1/R4,
        # so the "no net, no PAD_NET" branch and a shared net both occur.
        net_of = {"1": f"N_{inst['refdes']}", "2": "GND"}
        if inst["refdes"] in ("R1", "R4"):
            net_of["3"] = "SHARED"
        p, d = padplace.place_footprint_pads(
            fps[inst["footprint"]]["pads"],
            inst,
            layers=_LAYERS,
            pin_to_net=net_of,
        )
        pads += p
        drills += d
    return {
        "layers": _LAYERS,
        "outline": _OUTLINE if outline is None else outline,
        "copper": [],
        "pads": pads + (extra_pads or []),
        "drills": drills,
        "silkscreen": {"top": [], "bottom": []},
        "mask_open_regions": [],
        "instances": insts,
        **extra,
    }


def _export(model=None, **kw) -> epro_write.EproExport:
    return epro_write.epro_files(
        _model() if model is None else model, slug="epro-test", **kw
    )


def _read(exported: epro_write.EproExport) -> epro.EproProject:
    return epro.read_archive(epro_write.zip_epro(exported.files))


# ── frame conversions ───────────────────────────────────────────────────
def test_mil_constant_matches_the_reader():
    assert epro_write.MM_PER_MIL == epro._MM_PER_MIL


def test_to_mils_is_the_reader_inverse():
    assert epro_write.to_mils(0.0254) == 1.0
    assert epro_write.to_mils(25.4) == 1000.0
    assert epro_write.to_mils(0.0) == 0.0
    assert math.copysign(1.0, epro_write.to_mils(-0.0)) == 1.0
    for mm in (0.0, 1.2345, 19.9999, 123.456789):
        assert epro.Frame.length(epro_write.to_mils(mm)) == pytest.approx(mm, abs=1e-5)


def test_to_epro_xy_flips_y_and_is_the_exact_inverse_of_the_reader_frame():
    fr = epro_write.EproFrame(x0_mm=-5.0, y1_mm=25.0)
    # board (0, 0) is the outline's bottom-left corner; in Y-down it is the
    # corner with the LARGEST y, and the translation keeps both positive.
    x0, y0 = epro_write.to_epro_xy(0.0, 0.0, fr)
    x1, y1 = epro_write.to_epro_xy(30.0, 20.0, fr)
    assert x0 > 0 and y1 > 0
    assert y0 > y1  # Y grew down: the top edge has the SMALLER y
    rd = epro.Frame(min_x_mil=x0, max_y_mil=y0)
    assert rd.xy(x0, y0) == (0.0, 0.0)
    for x, y in [(0.0, 0.0), (30.0, 20.0), (12.3456, 7.8901)]:
        mx, my = epro_write.to_epro_xy(x, y, fr)
        gx, gy = rd.xy(mx, my)
        assert gx == pytest.approx(x, abs=_TOL_MM)
        assert gy == pytest.approx(y, abs=_TOL_MM)


def test_to_footprint_local_top_and_bottom():
    # a pad 1 mm east and 0.5 mm north of the component, Y-up
    top = epro_write.to_footprint_local((11.0, 10.5), (10.0, 10.0), bottom=False)
    bot = epro_write.to_footprint_local((11.0, 10.5), (10.0, 10.0), bottom=True)
    assert top == (pytest.approx(39.3701), pytest.approx(-19.685))  # north = -Y
    assert bot == (pytest.approx(39.3701), pytest.approx(19.685))  # mirrored in Y


def test_every_written_coordinate_is_positive_even_off_board():
    model = _model(
        [_inst("R1", -8.0, -6.0, 0.0, "top"), _inst("R2", 40.0, 30.0, 0.0, "top")]
    )
    pcb = _read(_export(model)).pcb()
    for r in pcb.of_type("COMPONENT"):
        assert r.body is not None
        assert r.body["x"] > 0 and r.body["y"] > 0
    poly = pcb.bodies("POLY")[0]["path"]
    nums = [v for v in poly if not isinstance(v, str)]
    assert min(nums) > 0


# ── container + stream structure ────────────────────────────────────────
def test_zip_integrity_and_members():
    blob = epro_write.zip_epro(_export().files)
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        assert zf.testzip() is None
        names = zf.namelist()
        assert "project2.json" in names
        assert [n for n in names if n.endswith(".epru")] == ["epro-test.epru"]
        assert len(names) == 2
        manifest = json.loads(zf.read("project2.json"))
    assert set(manifest) == {
        "title",
        "cbb_project",
        "editorVersion",
        "introduction",
        "description",
        "tags",
    }
    assert manifest["title"] == "epro-test"


def test_every_line_parses_through_the_reader_and_the_shape_is_a_pcb_then_footprints():
    ex = _export()
    stream = ex.files["epro-test.epru"]
    lines = stream.rstrip("\n").split("\n")
    for ln in lines:
        head, sep, body = ln.partition("||")
        assert sep and body.endswith("|")
        json.loads(head)
        b = body[:-1]
        assert b == '""' or isinstance(json.loads(b), dict)
    docs = epro.split_documents(stream)
    assert [d.doc_type for d in docs] == ["PCB"] + ["FOOTPRINT"] * len(_INSTANCES)
    assert sum(1 for ln in lines if ln.startswith('{"type":"DOCHEAD"')) == len(docs)


def test_tickets_are_unique_and_monotonic():
    stream = _export().files["epro-test.epru"]
    tickets = [
        json.loads(ln.partition("||")[0])["ticket"] for ln in stream.split("\n") if ln
    ]
    assert tickets == sorted(tickets) and len(set(tickets)) == len(tickets)


def test_referential_integrity():
    project = _read(_export())
    pcb = project.pcb()
    layer_ids = {b["layerId"] for b in pcb.bodies("LAYER")}
    comp_ids = {r.id for r in pcb.of_type("COMPONENT")}
    assert len(comp_ids) == len(_INSTANCES)
    fp_docs = {d.uuid: d for d in project.by_type("FOOTPRINT")}
    assert len(fp_docs) == len(_INSTANCES)

    # NET declared before use: walk the records in stream order
    declared: set[str] = set()
    for r in pcb.records:
        if r.type == "NET":
            declared.add(json.loads(str(r.id))[1])
        elif r.type == "PAD_NET":
            assert r.body is not None
            assert r.body["padNet"] in declared, r.id

    # every layerId declared
    for t in ("POLY", "COMPONENT", "ATTR", "STRING"):
        for b in pcb.bodies(t):
            assert b["layerId"] in layer_ids, (t, b)
    for d in fp_docs.values():
        for b in d.bodies("PAD"):
            assert b["layerId"] in layer_ids

    # ATTR.parentId resolves; Footprint ATTR is a FOOTPRINT document uuid
    attrs = pcb.bodies("ATTR")
    assert {a["parentId"] for a in attrs} == comp_ids
    fp_of: dict[str, str] = {}
    for a in attrs:
        assert a["parentId"] in comp_ids
        if a["key"] == "Footprint":
            assert a["value"] in fp_docs
            fp_of[a["parentId"]] = a["value"]
    assert set(fp_of) == comp_ids
    assert len(set(fp_of.values())) == len(comp_ids)  # one document per instance

    # PAD_NET -> a real component, a real pad number and a real pad element
    n_pad_nets = 0
    for r in pcb.of_type("PAD_NET"):
        _, cid, num, pad_el = json.loads(str(r.id))
        assert cid in comp_ids
        pads = fp_docs[fp_of[cid]].of_type("PAD")
        assert pad_el in {p.id for p in pads}
        assert num in {(p.body or {}).get("num") for p in pads if p.id == pad_el}
        n_pad_nets += 1
    assert n_pad_nets > 0


def test_footprint_attr_uuid_and_component_ids_are_stable_per_refdes():
    a = _read(_export(_model(_INSTANCES[:3])))
    b = _read(_export(_model(list(reversed(_INSTANCES[:3])))))
    assert {d.uuid for d in a.by_type("FOOTPRINT")} == {
        d.uuid for d in b.by_type("FOOTPRINT")
    }
    assert a.pcb().uuid == b.pcb().uuid
    # and one refdes keeps its ids when a sibling is added
    c = _read(_export(_model(_INSTANCES[:1])))
    r1_a = [r.id for r in a.pcb().of_type("COMPONENT")]
    r1_c = [r.id for r in c.pcb().of_type("COMPONENT")]
    assert set(r1_c) <= set(r1_a)


# ── determinism ─────────────────────────────────────────────────────────
def test_same_model_gives_identical_bytes_twice():
    a = epro_write.zip_epro(_export().files)
    b = epro_write.zip_epro(_export().files)
    assert a == b
    # instance order in the model does not leak into the file
    shuffled = _model(list(reversed(_INSTANCES)))
    plain = _model()
    shuffled["pads"] = plain["pads"]
    assert epro_write.zip_epro(_export(shuffled).files) == a


# ── synthesized-pad refusal ─────────────────────────────────────────────
def _synth_model() -> dict[str, Any]:
    model = _model()
    for p in model["pads"]:
        if p["refdes"] == "R2":
            p["synthesized"] = True
    return model


def test_synthesized_pads_are_refused_by_default():
    with pytest.raises(SynthesizedPadError, match="R2"):
        _export(_synth_model())


def test_allow_synthesized_writes_a_string_naming_the_refdes():
    ex = _export(_synth_model(), allow_synthesized=True)
    pcb = _read(ex).pcb()
    strings = pcb.bodies("STRING")
    assert len(strings) == 1
    assert "R2" in strings[0]["text"] and "SYNTHESIZED" in strings[0]["text"]
    assert strings[0]["layerId"] in {b["layerId"] for b in pcb.bodies("LAYER")}
    assert any("allow_synthesized" in w and "R2" in w for w in ex.warnings)
    # and a clean model writes none
    assert _read(_export()).pcb().bodies("STRING") == []


def test_the_refusal_is_the_gerber_exception_not_a_second_one():
    from precis.pcb import gerber

    assert epro_write.SynthesizedPadError is gerber.SynthesizedPadError


# ── warned-about drops ──────────────────────────────────────────────────
def test_things_the_slice_does_not_carry_are_warned_about():
    fid = {
        "layer": "F.Cu",
        "net": "",
        "shape": "circle",
        "x": 1.0,
        "y": 1.0,
        "w": 1.0,
        "role": "fiducial",
    }
    lonely = {**fid, "net": "LONELY", "role": None}
    model = _model(
        extra_pads=[fid, lonely],
        copper=[
            {
                "ctype": "pour",
                "layer": "F.Cu",
                "net": "GND",
                "polygon": [[0, 0], [1, 0], [1, 1]],
            }
        ],
        mask_open_regions=[{"side": "top", "polygon": [[0, 0], [1, 0], [1, 1]]}],
        silkscreen={
            "top": [
                {
                    "width_mm": 0.15,
                    "role": "refdes",
                    "refdes": "R1",
                    "segments": [
                        {"shape": "line", "start": [1, 1], "end": [2, 1]},
                        {"shape": "line", "start": [2, 1], "end": [2, 2]},
                    ],
                },
                {
                    "shape": "region",
                    "role": "sn-box",
                    "polygon": [[0, 0], [1, 0], [1, 1]],
                },
            ],
            "bottom": [],
        },
    )
    model["drills"].append({"x": 2.0, "y": 2.0, "dia_mm": 3.0, "plated": False})
    model["instances"].append({"refdes": "X9", "x": None, "y": None})
    text = " | ".join(_export(model).warnings)
    for needle in (
        "2 pad(s) belong to no placed part",
        "1 net(s) have no exported pad and are not in the file: LONELY",
        # R7 sits at 37 degrees with rect pads (review E1)
        "1 part(s) sit at a non-right-angle rotation with rect/obround pads (R7)",
        "1 pour(s)/plane(s)",
        "1 soldermask-opening",
        "1 refdes silk stroke(s) suppressed",
        "1 silk region fill(s)",
        "1 drill(s) belong to no exported pad or via",
        "unplaced part(s) omitted: X9",
    ):
        assert needle in text, needle


# ── silk + editable designators (slice 2e) ──────────────────────────────
def _line(a, b):
    return {"shape": "line", "start": list(a), "end": list(b)}


def test_designator_attr_is_anchored_where_the_silk_pass_put_the_label():
    label = {
        "refdes": "R1",
        "side": "top",
        "x": 4.0,
        "y": 3.2,
        "angle": 0.0,
        "height_mm": 0.8,
        "stroke_width_mm": 0.15,
    }
    model = _model(silk_labels=[label])
    ex = _export(model)
    pcb = _read(ex).pcb()
    frame = epro_write.frame_for(model)

    def designator(refdes):
        (a,) = [
            b
            for b in pcb.bodies("ATTR")
            if b.get("key") == "Designator" and b.get("value") == refdes
        ]
        return a

    r1 = designator("R1")
    lx, ly = epro_write.to_epro_xy(4.0, 3.2, frame)
    assert r1["x"] == pytest.approx(lx) and r1["y"] == pytest.approx(ly)
    assert r1["origin"] == "LEFT_BOTTOM"
    assert r1["fontSize"] == pytest.approx(epro_write.to_mils(0.8))
    assert r1["strokeWidth"] == pytest.approx(epro_write.to_mils(0.15))
    assert r1["mirror"] is False
    assert r1["angle"] == 0
    assert r1["valueVisible"] is True

    r2 = designator("R2")
    comp = next(
        r.body
        for r in pcb.of_type("COMPONENT")
        if r.id == r2["parentId"] and r.body is not None
    )
    r2_inst = next(i for i in model["instances"] if i["refdes"] == "R2")
    cx, cy = epro_write.to_epro_xy(r2_inst["x"], r2_inst["y"], frame)
    assert (comp["x"], comp["y"]) == (cx, cy)
    assert r2["x"] == comp["x"] and r2["y"] == comp["y"]
    assert r2["origin"] == "LEFT_BOTTOM"
    assert ex.stats["labels_anchored"] == 1


def _silk_model():
    outline = [
        _line((5, 5), (7, 5)),
        _line((7, 5), (7, 6)),
        _line((7, 6), (5, 6)),
        _line((5, 6), (5, 5)),
    ]
    return _model(
        silkscreen={
            "top": [
                {"width_mm": 0.15, "role": "outline", "segments": outline},
                {
                    "width_mm": 0.2,
                    "role": "pin1",
                    "segments": [_line((4.5, 4.5), (4.9, 4.5))],
                },
                {
                    "width_mm": 0.15,
                    "role": "refdes",
                    "refdes": "R1",
                    "segments": [
                        _line((1, 1), (2, 1)),
                        _line((2, 1), (2, 2)),
                        _line((2, 2), (3, 2)),
                    ],
                },
            ],
            "bottom": [
                {
                    "width_mm": 0.3,
                    "role": "title",
                    "segments": [_line((9, 9), (10, 9)), _line((10, 9), (10, 10))],
                },
                {
                    "width_mm": 0.15,
                    "polarity": "clear",
                    "segments": [_line((12, 9), (13, 9))],
                },
            ],
        }
    )


def test_silk_strokes_become_polys_and_refdes_strokes_are_suppressed():
    model = _silk_model()
    ex = _export(model)
    pcb = _read(ex).pcb()
    top = [b for b in pcb.bodies("POLY") if b["layerId"] == 3]
    bot = [b for b in pcb.bodies("POLY") if b["layerId"] == 4]
    assert len(top) == 2 and len(bot) == 1
    for b in top + bot:
        assert b["netName"] == ""
        assert b["polyType"] == "NORMAL"
    widths = sorted(b["width"] for b in top)
    assert widths == pytest.approx(
        sorted([epro_write.to_mils(0.15), epro_write.to_mils(0.2)])
    )
    assert bot[0]["width"] == pytest.approx(epro_write.to_mils(0.3))

    _outline, frame = epro.board_outline(pcb)
    box = next(b for b in top if b["width"] == pytest.approx(epro_write.to_mils(0.15)))
    pts = [frame.xy(x, y) for x, y in epro._poly_points(box["path"])]
    want = [(5, 5), (7, 5), (7, 6), (5, 6), (5, 5)]
    assert len(pts) == 5
    for (gx, gy), (wx, wy) in zip(pts, want, strict=True):
        assert abs(gx - wx) < _TOL_MM and abs(gy - wy) < _TOL_MM

    assert ex.stats["silk_polys"] == 3
    assert ex.stats["silk_refdes_suppressed"] == 1
    assert ex.stats["silk_dropped"] == 1
    text = " | ".join(ex.warnings)
    assert "suppressed" in text and "knockout" in text


def test_silk_order_does_not_leak_into_the_bytes():
    a = _silk_model()
    b = _silk_model()
    b["silkscreen"]["top"].reverse()
    assert epro_write.zip_epro(_export(a).files) == epro_write.zip_epro(
        _export(b).files
    )


# ── THE frame test: write, read back, every pad where the model put it ──
def _round_trip(model: dict[str, Any]):
    project = _read(_export(model))
    design, _frame = epro.build_design(project)
    return project, design


def _board_pads_from_reader(design) -> dict[tuple[str, str], tuple[float, float]]:
    """Every footprint pad of every component, placed on the board through
    the reader's own component (rot/side) and :func:`padplace.place_pad_point`
    — the same path the import takes."""
    fps = {f["name"]: f for f in design.footprints}
    out: dict[tuple[str, str], tuple[float, float]] = {}
    for comp in design.components:
        for pad in fps[comp["footprint"]]["pads"]:
            out[(comp["refdes"], pad["pin"])] = padplace.place_pad_point(pad, comp)
    return out


def test_frame_round_trip_every_pad_lands_within_half_a_micron():
    model = _model()
    _project, design = _round_trip(model)
    got = _board_pads_from_reader(design)

    # a THT pad is repeated once per copper layer in the model: one pad
    want = {(p["refdes"], p["pin"]): (p["x"], p["y"]) for p in model["pads"]}
    assert set(got) == set(want)
    assert len(got) == 3 * 8 + 1  # eight asymmetric parts + the polygon pad

    # the model's outline starts at (0, 0), which is the reader's origin
    worst = 0.0
    for key, (wx, wy) in want.items():
        gx, gy = got[key]
        worst = max(worst, abs(gx - wx), abs(gy - wy))
    assert worst < _TOL_MM, f"worst pad error {worst * 1000:.3f} um"


def test_frame_round_trip_covers_both_sides_and_the_rotations_that_matter():
    sides = {(i["layer"], i["rot"]) for i in _INSTANCES}
    assert {("top", 0.0), ("top", 90.0), ("top", 270.0), ("top", 37.0)} <= sides
    assert {("bottom", 0.0), ("bottom", 90.0), ("bottom", 270.0)} <= sides
    _project, design = _round_trip(_model())
    by_refdes = {c["refdes"]: c for c in design.components}
    assert by_refdes["R4"]["layer"] == "bottom" and by_refdes["R1"]["layer"] == "top"
    # the written angle is baked to 0; the reader's bottom half-turn is 180
    assert {by_refdes[r]["rot"] for r in ("R1", "R2", "R3", "R7")} == {0.0}
    assert {by_refdes[r]["rot"] for r in ("R4", "R5", "R6", "R8")} == {180.0}


def test_frame_round_trip_catches_a_wrong_mirror_axis():
    """The test above must be able to FAIL: mirror the bottom side in X in
    the writer's local frame and the asymmetric part no longer round-trips."""
    model = _model()
    ex = _export(model)
    stream = ex.files["epro-test.epru"]
    # flip centerX sign on every footprint pad of R4 (bottom, rot 0)
    r4_uuid = epro_write._uuid_hex("epro-test", "fp:R4")
    out: list[str] = []
    in_r4 = False
    for ln in stream.split("\n"):
        if ln.startswith('{"type":"DOCHEAD"'):
            in_r4 = r4_uuid in ln
        if in_r4 and ln.startswith('{"type":"PAD"'):
            head, _, body = ln.partition("||")
            d = json.loads(body[:-1])
            d["centerX"] = -d["centerX"]
            ln = f"{head}||{json.dumps(d, separators=(',', ':'))}|"
        out.append(ln)
    files = {**ex.files, "epro-test.epru": "\n".join(out)}
    design, _ = epro.build_design(epro.read_archive(epro_write.zip_epro(files)))
    got = _board_pads_from_reader(design)
    want = {(p["refdes"], p["pin"]): (p["x"], p["y"]) for p in model["pads"]}
    worst = max(
        max(abs(got[k][0] - want[k][0]), abs(got[k][1] - want[k][1]))
        for k in want
        if k[0] == "R4"
    )
    assert worst > 0.5


def test_net_and_refdes_per_pad_round_trip():
    model = _model()
    _project, design = _round_trip(model)
    want = {(p["refdes"], p["pin"], p["net"]) for p in model["pads"] if p["net"]}
    got = {(c["refdes"], c["pin"], c["net"]) for c in design.connections}
    assert got == want
    assert {c["refdes"] for c in design.components} == {i["refdes"] for i in _INSTANCES}
    assert {n["name"] for n in design.nets} == {net for _, _, net in want}


def test_pad_geometry_round_trips_shape_size_hole_and_polygon():
    model = _model()
    _project, design = _round_trip(model)
    fps = {f["name"]: f for f in design.footprints}
    comps = {c["refdes"]: c for c in design.components}
    placed = {(p["refdes"], p["pin"]): p for p in model["pads"]}

    # R2 sits at 90: the model already swapped w/h to board axes, and the
    # file carries them verbatim (the component is baked to angle 0)
    pads = {p["pin"]: p for p in fps[comps["R2"]["footprint"]]["pads"]}
    assert pads["1"]["shape"] == "rect"
    assert (pads["1"]["w"], pads["1"]["h"]) == (
        pytest.approx(placed[("R2", "1")]["w"], abs=1e-4),
        pytest.approx(placed[("R2", "1")]["h"], abs=1e-4),
    )
    assert pads["2"]["shape"] == "obround" and pads["3"]["shape"] == "circle"
    assert pads["3"]["drill"] == pytest.approx(0.4, abs=1e-4)
    assert "drill" not in pads["1"]

    # polygon vertices, through the same transform as the pad centres
    p1 = placed[("P1", "1")]
    poly_pad = {p["pin"]: p for p in fps[comps["P1"]["footprint"]]["pads"]}["1"]
    assert poly_pad["shape"] == "polygon"
    got = [
        padplace.place_pad_point({"x": vx, "y": vy}, comps["P1"])
        for vx, vy in poly_pad["poly"]
    ]
    assert len(got) == len(p1["poly"])
    for (gx, gy), (wx, wy) in zip(got, p1["poly"], strict=True):
        assert abs(gx - wx) < _TOL_MM and abs(gy - wy) < _TOL_MM


def test_a_model_whose_outline_is_not_at_the_origin_round_trips_relative_to_it():
    """The reader's frame is the outline's corner. A model drawn at
    (100, 50) comes back offset by exactly that — consistently for the
    parts and the outline, which is what keeps them registered."""
    ox, oy = 100.0, 50.0
    insts = [
        {**i, "x": i["x"] + ox, "y": i["y"] + oy}
        for i in _INSTANCES
        if i["refdes"] in ("R1", "R4")
    ]
    outline = [[x + ox, y + oy] for x, y in _OUTLINE]
    model = _model(insts, outline=outline)
    _project, design = _round_trip(model)
    got = _board_pads_from_reader(design)
    for p in model["pads"]:
        gx, gy = got[(p["refdes"], p["pin"])]
        assert abs(gx - (p["x"] - ox)) < _TOL_MM
        assert abs(gy - (p["y"] - oy)) < _TOL_MM
    feat = next(f for f in design.features if f["ftype"] == "outline")
    assert feat["geom"]["path"][0] == [0.0, 0.0]


# ── pours (slice 2d): rectangle zones only, Pro re-pours ────────────────
def test_a_rectangular_pour_is_written_as_an_r_zone_pro_re_pours():
    pour = {
        "ctype": "pour",
        "layer": "In1.Cu",
        "net": "GND",
        "polygon": [[2.0, 2.0], [28.0, 2.0], [28.0, 18.0], [2.0, 18.0], [2.0, 2.0]],
        "holes": [[[10.0, 10.0], [11.0, 10.0], [11.0, 11.0]]],
    }
    model = _model(copper=[pour])
    ex = _export(model)
    pcb = _read(ex).pcb()
    (rec,) = pcb.bodies("POUR")
    frame = epro_write.frame_for(model)
    fx, fy = epro_write.to_epro_xy(2.0, 2.0, frame)
    assert rec["netName"] == "GND" and rec["layerId"] == 15
    assert rec["path"] == [
        ["R", fx, fy, epro_write.to_mils(26.0), epro_write.to_mils(16.0), 0, 0]
    ]
    assert rec["pourType"] == {"pourType": "SOLID", "fineness": 8}
    assert rec["width"] == 0.2 and rec["name"] == "POUR1"
    # the box spans y-h..y in the Y-down frame: its top edge is board y=18
    top_x, top_y = epro_write.to_epro_xy(28.0, 18.0, frame)
    assert fy - epro_write.to_mils(16.0) == pytest.approx(top_y, abs=1e-3)
    assert fx + epro_write.to_mils(26.0) == pytest.approx(top_x, abs=1e-3)
    assert ex.stats["pours"] == 1 and ex.stats["pours_dropped"] == 0
    assert any("re-pours" in w for w in ex.warnings)
    # the reader's net census counts the pour's net as used
    assert "GND" in epro.live_nets(pcb)[0]


def test_a_free_polygon_pour_is_dropped_with_a_warning_not_guessed():
    tri = {
        "ctype": "pour",
        "layer": "B.Cu",
        "net": "GND",
        "polygon": [[2.0, 2.0], [28.0, 2.0], [15.0, 18.0]],
    }
    off = {**tri, "layer": "X.Cu", "polygon": [[0, 0], [1, 0], [1, 1], [0, 1]]}
    ex = _export(_model(copper=[tri, off]))
    assert _read(ex).pcb().bodies("POUR") == []
    assert ex.stats["pours"] == 0 and ex.stats["pours_dropped"] == 2
    assert any("non-rectangular" in w and "2 pour(s)" in w for w in ex.warnings)
    assert not any("re-pours" in w for w in ex.warnings)


def test_axis_aligned_rect_detection():
    rect = epro_write._axis_aligned_rect
    assert rect([[0, 0], [2, 0], [2, 1], [0, 1]]) == ((0.0, 0.0), (2.0, 1.0))
    assert rect([[2, 1], [0, 1], [0, 0], [2, 0], [2, 1]]) == ((0.0, 0.0), (2.0, 1.0))
    assert rect([[0, 0], [2, 0], [2, 1]]) is None
    assert rect([[0, 0], [2, 0.5], [2, 1], [0, 1]]) is None
    assert rect([]) is None


# ── handler: view='epro' over a live store ──────────────────────────────
_DESIGN = {
    "components": [
        {
            "refdes": "U1",
            "label": "QFN",
            "part": "C2838500",
            "footprint": "QFN-32",
            "x": 10.0,
            "y": 10.0,
            "rot": 90.0,
            "pins": [{"name": "VDD"}, {"name": "GND"}],
        },
        {
            "refdes": "C1",
            "label": "100nF 0402",
            "part": "C1525",
            "footprint": "0402",
            "x": 14.0,
            "y": 10.0,
            "layer": "bottom",
            "pins": [{"name": "1"}, {"name": "2"}],
        },
    ],
    "nets": [{"name": "VCC3V3", "class": "power"}, {"name": "GND", "class": "gnd"}],
    "connections": [
        {"net": "VCC3V3", "refdes": "U1", "pin": "VDD"},
        {"net": "VCC3V3", "refdes": "C1", "pin": "1"},
        {"net": "GND", "refdes": "U1", "pin": "GND"},
        {"net": "GND", "refdes": "C1", "pin": "2"},
    ],
    "features": [
        {"ftype": "outline", "geom": {"path": [[0, 0], [20, 0], [20, 20], [0, 20]]}},
    ],
}

_FP_U1: dict[str, Any] = {
    "pads": [
        {
            "number": "1",
            "shape": "RECT",
            "x": -1.0,
            "y": 0.0,
            "w": 0.3,
            "h": 0.6,
            "rot": 0.0,
            "layer": "F.Cu",
            "drill": None,
        },
        {
            "number": "2",
            "shape": "RECT",
            "x": 0.0,
            "y": -1.0,
            "w": 0.3,
            "h": 0.6,
            "rot": 0.0,
            "layer": "F.Cu",
            "drill": None,
        },
    ],
    "pin_map": {"1": {"name": "VDD", "tags": []}, "2": {"name": "GND", "tags": []}},
}
_FP_C1 = {
    "pads": [
        {**_FP_U1["pads"][0], "x": -0.5, "w": 0.4, "h": 0.4},
        {**_FP_U1["pads"][0], "number": "2", "x": 0.5, "w": 0.4, "h": 0.4},
    ],
    "pin_map": {"1": {"name": "1", "tags": []}, "2": {"name": "2", "tags": []}},
}


@pytest.fixture
def pcb(store):
    return PcbHandler(hub=Hub(store=store))


def _seed(pcb, *, cache_c1: bool = True) -> str:
    pcb.put(id="eprotest", args=_DESIGN)
    pcb.store.part_footprint_put("C2838500", _FP_U1)
    if cache_c1:
        pcb.store.part_footprint_put("C1525", _FP_C1)
    return "eprotest"


def test_handler_epro_view_writes_a_readable_file(pcb, tmp_path):
    slug = _seed(pcb)
    resp = pcb.get(id=slug, view="epro", args={"dir": str(tmp_path)})
    path = tmp_path / "eprotest.epro2"
    assert path.exists() and str(path) in resp.body
    assert "No schematic is included" in resp.body
    assert "Update PCB from schematic" in resp.body
    assert "copper: none" in resp.body
    assert "UNVERIFIED: no file from this writer has been opened" in resp.body

    project = epro.read_archive(path.read_bytes())
    design, _ = epro.build_design(project)
    assert {c["refdes"] for c in design.components} == {"U1", "C1"}
    by = {c["refdes"]: c for c in design.components}
    assert by["C1"]["layer"] == "bottom" and by["U1"]["layer"] == "top"
    assert {(c["refdes"], c["pin"], c["net"]) for c in design.connections} >= {
        ("U1", "VDD", "VCC3V3"),
        ("C1", "2", "GND"),
    }
    # the same bytes twice
    pcb.get(id=slug, view="epro", args={"dir": str(tmp_path)})
    first = path.read_bytes()
    pcb.get(id=slug, view="epro", args={"dir": str(tmp_path)})
    assert path.read_bytes() == first


def test_handler_epro_view_refuses_synthesized_pads_unless_allowed(pcb, tmp_path):
    slug = _seed(pcb, cache_c1=False)
    with pytest.raises(BadInput, match="synthesized"):
        pcb.get(id=slug, view="epro", args={"dir": str(tmp_path)})
    assert not (tmp_path / "eprotest.epro2").exists()
    resp = pcb.get(
        id=slug,
        view="epro",
        args={"dir": str(tmp_path), "allow_synthesized": True},
    )
    assert "SYNTHESIZED" in resp.body
    pcb_doc = epro.read_archive((tmp_path / "eprotest.epro2").read_bytes()).pcb()
    assert "C1" in pcb_doc.bodies("STRING")[0]["text"]


def test_a_right_angle_part_carries_no_oblique_marker():
    model = _model()
    marked = {p["refdes"] for p in model["pads"] if "oblique_rot" in p}
    assert marked == {"R7"}


def test_a_net_name_the_record_separator_cannot_carry_is_refused():
    model = _model([_inst("R1", 6.0, 5.0, 0.0, "top")])
    for pad in model["pads"]:
        if pad.get("net") == "GND":
            pad["net"] = "A||B"
    with pytest.raises(ValueError, match="'\\|\\|'"):
        _export(model)


def test_handler_turns_an_unwritable_name_into_bad_input(pcb, tmp_path, monkeypatch):
    def refuse(*_a, **_k):
        raise ValueError("NET record id contains '||'")

    monkeypatch.setattr(epro_write, "epro_files", refuse)
    slug = _seed(pcb)
    with pytest.raises(BadInput, match="cannot write .epro2"):
        pcb.get(id=slug, view="epro", args={"dir": str(tmp_path)})


def test_handler_epro_view_drops_gerber_only_notes(pcb, tmp_path, monkeypatch):
    """Dogfood 2026-10-02 on heater-base-test: ~170 silk-placement notes
    and the gerber "route first" hint pushed the response past the frame,
    for artifacts the .epro2 does not carry. They are counted, not listed."""
    real = PcbHandler._fab_model

    def noisy(self, *a, **k):
        built = real(self, *a, **k)
        assert built is not None
        model, warnings = built
        extra = [f"silk: R{i}: refdes label moved" for i in range(50)]
        return model, [*warnings, *extra, "no realized copper yet — run route"]

    monkeypatch.setattr(PcbHandler, "_fab_model", noisy)
    slug = _seed(pcb)
    body = pcb.get(id=slug, view="epro", args={"dir": str(tmp_path)}).body
    assert "refdes label moved" not in body
    assert "no realized copper yet" not in body
    assert "silk-placement note(s) not shown" in body


# ── copper (slice 2c) ───────────────────────────────────────────────────
def _copper() -> list[dict[str, Any]]:
    """Every copper shape the writer carries, inside the 30x20 outline. The
    F.Cu track chains line -> cw arc -> line end to end."""
    return [
        {
            "ctype": "track",
            "layer": "F.Cu",
            "net": "GND",
            "width_mm": 0.2,
            "segments": [
                {"shape": "line", "start": [5.0, 5.0], "end": [10.0, 5.0]},
                {
                    "shape": "arc",
                    "start": [10.0, 5.0],
                    "end": [8.0, 7.0],
                    "center": [10.0, 7.0],
                    "cw": True,
                },
                {"shape": "line", "start": [8.0, 7.0], "end": [8.0, 12.0]},
            ],
        },
        {
            "ctype": "track",
            "layer": "In1.Cu",
            "net": "N_R1",
            "width_mm": 0.15,
            "segments": [{"shape": "line", "start": [6.0, 3.0], "end": [14.0, 3.0]}],
        },
        {
            "ctype": "track",
            "layer": "B.Cu",
            "net": "SHARED",
            "width_mm": 0.15,
            "segments": [
                {
                    "shape": "arc",
                    "start": [20.0, 15.0],
                    "end": [22.0, 17.0],
                    "center": [20.0, 17.0],
                    "cw": False,
                }
            ],
        },
        {
            "ctype": "via",
            "net": "GND",
            "x": 25.0,
            "y": 10.0,
            "dia_mm": 0.6,
            "drill_mm": 0.3,
        },
        {
            "ctype": "via",
            "net": "GND",
            "x": 26.0,
            "y": 12.0,
            "dia_mm": 0.6,
            "drill_mm": 0.3,
            "span": ["F.Cu", "In1.Cu"],
        },
    ]


def _canon_segments(tracks) -> dict[tuple, tuple[float, ...]]:
    """``{rounded key: raw coords}`` for every segment of every track row.
    The reader chains coincident segments and may walk one backwards, so
    endpoints are sorted and an arc's ``cw`` flips with the swap."""
    out: dict[tuple, tuple[float, ...]] = {}
    for t in tracks:
        if t.get("ctype", "track") != "track":
            continue
        for seg in t["segments"]:
            a = (float(seg["start"][0]), float(seg["start"][1]))
            b = (float(seg["end"][0]), float(seg["end"][1]))
            flipped = tuple(round(v, 4) for v in b) < tuple(round(v, 4) for v in a)
            if flipped:
                a, b = b, a
            coords = [*a, *b]
            kind: tuple = ("line",)
            if seg.get("shape") == "arc":
                c = (float(seg["center"][0]), float(seg["center"][1]))
                kind = ("arc", bool(seg["cw"]) != flipped)
                coords += [*c]
            key = (
                t["layer"],
                t["net"],
                round(float(t["width_mm"]), 4),
                *kind,
                *(round(v, 4) for v in coords),
            )
            out[key] = tuple(coords)
    return out


def _worst_um(want: dict, got: dict) -> float:
    """Worst coordinate error between two canonical maps, in um (the keys
    are the rounded coordinates, so the same key pairs the same segment)."""
    assert set(want) == set(got)
    return 1000.0 * max(
        (max(abs(x - y) for x, y in zip(want[k], got[k], strict=True)) for k in want),
        default=0.0,
    )


def _read_copper(model: dict[str, Any]):
    pcb = _read(_export(model)).pcb()
    _outline, frame = epro.board_outline(pcb)
    flat, _warn = epro.measured_copper(pcb, frame)
    return pcb, flat


def test_copper_round_trips_lines_arcs_and_vias_within_half_a_micron():
    cu = _copper()
    _pcb, flat = _read_copper(_model(copper=cu))

    want = _canon_segments(cu)
    got = _canon_segments(flat)
    assert len(want) == 5  # 2 lines + 1 arc on F.Cu, 1 line, 1 arc
    assert set(want) == set(got)
    assert _worst_um(want, got) < _TOL_MM * 1000.0

    vias = sorted((v for v in flat if v["ctype"] == "via"), key=lambda v: v["x"])
    assert len(vias) == 2
    for v, src in zip(vias, cu[3:], strict=True):
        assert abs(v["x"] - src["x"]) < _TOL_MM and abs(v["y"] - src["y"]) < _TOL_MM
        assert v["dia_mm"] == pytest.approx(0.6, abs=1e-4)
        assert v["drill_mm"] == pytest.approx(0.3, abs=1e-4)
        assert v["net"] == "GND"
        assert list(v["span"]) == ["F.Cu", "B.Cu"]  # the blind one is flattened


def test_arc_handedness_round_trips_both_ways():
    # same chord (10,10)-(12,12); the centres are mirror images across it
    ccw = {
        "shape": "arc",
        "start": [10.0, 10.0],
        "end": [12.0, 12.0],
        "center": [10.0, 12.0],
        "cw": False,
    }
    cw = {**ccw, "center": [12.0, 10.0], "cw": True}
    cu = [
        {
            "ctype": "track",
            "layer": "F.Cu",
            "net": "GND",
            "width_mm": 0.2,
            "segments": [ccw],
        },
        {
            "ctype": "track",
            "layer": "B.Cu",
            "net": "GND",
            "width_mm": 0.2,
            "segments": [cw],
        },
    ]
    _pcb, flat = _read_copper(_model(copper=cu))
    want, got = _canon_segments(cu), _canon_segments(flat)
    assert set(want) == set(got)  # the key carries cw and the centre
    assert _worst_um(want, got) < _TOL_MM * 1000.0
    by_layer = {k[0]: k for k in got}
    assert by_layer["F.Cu"][4] is False and by_layer["B.Cu"][4] is True


def test_arc_sweep_deg_unit():
    o, a, b = (0.0, 0.0), (1.0, 0.0), (0.0, 1.0)
    assert epro_write.arc_sweep_deg(a, b, o, cw=False) == pytest.approx(-90.0)
    assert epro_write.arc_sweep_deg(a, b, o, cw=True) == pytest.approx(270.0)
    assert epro_write.arc_sweep_deg(a, a, o, cw=False) == 0.0
    assert epro_write.arc_sweep_deg(a, a, o, cw=True) == 0.0


def test_copper_nets_are_declared_before_use_including_copper_only_nets():
    assert epro_write.copper_layer_ids(_LAYERS) == {
        "F.Cu": 1,
        "In1.Cu": 15,
        "In2.Cu": 16,
        "B.Cu": 2,
    }
    cu = [
        {
            "ctype": "track",
            "layer": "F.Cu",
            "net": "CU_ONLY",
            "width_mm": 0.2,
            "segments": [{"shape": "line", "start": [5.0, 5.0], "end": [9.0, 5.0]}],
        },
        {
            "ctype": "track",
            "layer": "B.Cu",
            "net": "CU_ONLY",
            "width_mm": 0.2,
            "segments": [
                {
                    "shape": "arc",
                    "start": [5.0, 8.0],
                    "end": [7.0, 10.0],
                    "center": [5.0, 10.0],
                    "cw": False,
                }
            ],
        },
        {
            "ctype": "via",
            "net": "CU_ONLY",
            "x": 12.0,
            "y": 12.0,
            "dia_mm": 0.6,
            "drill_mm": 0.3,
        },
    ]
    pcb = _read(_export(_model(copper=cu))).pcb()
    layer_ids = {b["layerId"] for b in pcb.bodies("LAYER")}
    declared: set[str] = set()
    seen = {"LINE": 0, "ARC": 0, "VIA": 0}
    for r in pcb.records:
        if r.type == "NET":
            declared.add(json.loads(str(r.id))[1])
        elif r.type in seen:
            assert r.body is not None
            seen[r.type] += 1
            assert "CU_ONLY" in declared, r.type  # NET came first
            assert r.body["netName"] in declared
            if "layerId" in r.body:
                assert r.body["layerId"] in layer_ids
    assert seen == {"LINE": 1, "ARC": 1, "VIA": 1}


def test_copper_drops_and_flattenings_are_warned():
    line = {"shape": "line", "start": [5.0, 5.0], "end": [9.0, 5.0]}
    pour: dict[str, Any] = {
        "ctype": "pour",
        "layer": "F.Cu",
        "net": "GND",
        "polygon": [[0, 0], [1, 0], [1, 1]],
    }

    def track(layer, net, width, segments):
        return {
            "ctype": "track",
            "layer": layer,
            "net": net,
            "width_mm": width,
            "segments": segments,
        }

    degenerate = {
        "shape": "arc",
        "start": [5.0, 7.0],
        "end": [5.0, 7.0],
        "center": [3.0, 7.0],
        "cw": True,
    }
    # start and end on one bearing from the centre: a 0/360 sweep
    full_circle = {**degenerate, "end": [6.0, 7.0]}
    cu = [
        {
            "ctype": "via",
            "net": "GND",
            "x": 20.0,
            "y": 5.0,
            "dia_mm": 0.6,
            "drill_mm": 0.3,
            "span": ["F.Cu", "In1.Cu"],
        },
        pour,
        track("F.Cu", "", 0.2, [line]),
        track("F.Cu", "GND", 0.2, [degenerate]),
        track("F.Cu", "GND", 0.2, [full_circle]),
        track("X.Cu", "GND", 0.2, [line]),
        track("F.Cu", "GND", 0, [line]),
    ]
    model = _model(copper=cu)
    # a drill sitting exactly under the via is the via's, not a loose one
    model["drills"].append({"x": 20.0, "y": 5.0, "dia_mm": 0.3, "plated": True})
    ex = _export(model)
    text = " | ".join(ex.warnings)
    for needle in (
        "blind/buried",
        "THROUGH-HOLE",
        "pour(s)",
        "carry no net",
        "no finite centre",
        "zero-length",
        "layer the stackup does not declare",
        "no width",
    ):
        assert needle in text, needle
    assert "belong to no exported pad or via" not in text

    pcb = _read(ex).pcb()
    assert [b["netName"] for b in pcb.bodies("LINE")] == [""]  # netless, written
    assert pcb.bodies("ARC") == []  # the degenerate arc was dropped
    assert len(pcb.bodies("VIA")) == 1
    # the triangle pour is counted as dropped (2d writes rectangles only)
    assert (ex.stats["lines"], ex.stats["vias"], ex.stats["pours"]) == (1, 1, 0)
    assert ex.stats["pours_dropped"] == 1


def test_copper_order_does_not_leak_into_the_bytes():
    cu = _copper()
    a = epro_write.zip_epro(_export(_model(copper=cu)).files)
    b = epro_write.zip_epro(_export(_model(copper=list(reversed(cu)))).files)
    assert a == b


# ── the real board, when someone points at one ──────────────────────────
@pytest.mark.skipif(
    not os.environ.get("PRECIS_EPRO_FIXTURE"),
    reason="set PRECIS_EPRO_FIXTURE=/path/to/real.epro2 to run",
)
def test_real_board_copper_and_bottom_pads_round_trip():
    data = pathlib.Path(os.environ["PRECIS_EPRO_FIXTURE"]).read_bytes()
    project = epro.read_archive(data)
    pcb = project.pcb()
    design, frame = epro.build_design(project, pcb)
    flat, _ = epro.measured_copper(pcb, frame)
    layers = list(epro.copper_layers(pcb).values())

    outline = next(f for f in design.features if f["ftype"] == "outline")
    fps = {f["name"]: f for f in design.footprints}
    net_of = {(c["refdes"], c["pin"]): c["net"] for c in design.connections}
    pads: list[dict[str, Any]] = []
    drills: list[dict[str, Any]] = []
    for comp in design.components:
        fp = fps.get(comp.get("footprint"))
        if fp is None:
            continue
        pad_net = {
            str(p["pad"]): net_of[(comp["refdes"], p["name"])]
            for p in comp.get("pins", [])
            if (comp["refdes"], p["name"]) in net_of
        }
        raw = [{**p, "number": p["pin"]} for p in fp["pads"]]
        pp, dd = padplace.place_footprint_pads(
            raw, comp, layers=layers, pin_to_net=pad_net
        )
        pads += pp
        drills += dd
    model = {
        "layers": layers,
        "outline": outline["geom"]["path"],
        "copper": flat,
        "pads": pads,
        "drills": drills,
        "silkscreen": {"top": [], "bottom": []},
        "mask_open_regions": [],
        "instances": design.components,
    }
    ex = epro_write.epro_files(model, slug="real")
    project2 = epro.read_archive(epro_write.zip_epro(ex.files))
    pcb2 = project2.pcb()
    design2, frame2 = epro.build_design(project2, pcb2)
    flat2, _ = epro.measured_copper(pcb2, frame2)

    want, got = _canon_segments(flat), _canon_segments(flat2)
    assert want, "the fixture carries no copper segments"
    assert set(want) == set(got)
    worst_cu = _worst_um(want, got)
    assert worst_cu < _TOL_MM * 1000.0, f"worst copper error {worst_cu:.3f} um"

    old, new = _board_pads_from_reader(design), _board_pads_from_reader(design2)
    bottom = {c["refdes"] for c in design.components if c.get("layer") == "bottom"}
    assert bottom, "the fixture has no bottom-side part"
    worst_pad = 0.0
    n = 0
    for key, (ox, oy) in old.items():
        if key[0] not in bottom:
            continue
        nx, ny = new[key]
        worst_pad = max(worst_pad, abs(nx - ox), abs(ny - oy))
        n += 1
    assert n > 0
    n_vias = sum(1 for v in flat if v["ctype"] == "via")
    print(
        f"REAL: {len(want)} segments, {n_vias} vias, worst copper "
        f"{worst_cu:.3f} um, {n} bottom pads worst {worst_pad * 1000:.3f} um"
    )
    assert worst_pad < _TOL_MM, f"worst bottom-pad error {worst_pad * 1000:.3f} um"
