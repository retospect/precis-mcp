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

_SLOT_FP = {
    "pads": [
        {
            "number": "1",
            "shape": "OVAL",
            "x": 0.0,
            "y": 0.0,
            "w": 4.0,
            "h": 2.0,
            "rot": 0.0,
            "layer": "F.Cu",
            "drill": 1.0,
            "slot": {"w": 3.0, "h": 1.0},
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
    fps = {"ASYM3": _ASYM_FP, "POLY1": _POLY_FP, "SLOT1": _SLOT_FP}
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


def test_to_footprint_xy_is_the_inverse_of_place_pad_point_everywhere():
    # authored-local mm is what place_pad_point maps to world: the writer
    # function must invert it exactly for every side x rotation.
    for layer, rot in (
        ("top", 0.0),
        ("top", 90.0),
        ("top", 37.0),
        ("bottom", 0.0),
        ("bottom", 90.0),
        ("bottom", 270.0),
    ):
        inst = {"x": 10.0, "y": 10.0, "rot": rot, "layer": layer}
        local = epro_write.to_footprint_xy((11.0, 10.5), inst)
        px, py = padplace.place_pad_point({"x": local[0], "y": local[1]}, inst)
        assert (px, py) == (pytest.approx(11.0), pytest.approx(10.5))


def test_to_footprint_xy_top_and_bottom():
    # a pad 1 mm east and 0.5 mm north of the component, Y-up, at rot 0:
    # the authored pad sits east/north on top; on the bottom the authored
    # X is mirrored (precis' bottom convention mirrors X, Pro's file
    # mirrors Y — the difference lands on the pad, not the placement).
    inst_t = {"x": 10.0, "y": 10.0, "rot": 0.0, "layer": "top"}
    inst_b = {"x": 10.0, "y": 10.0, "rot": 0.0, "layer": "bottom"}
    assert epro_write.to_footprint_xy((11.0, 10.5), inst_t) == (
        pytest.approx(1.0),
        pytest.approx(0.5),
    )
    assert epro_write.to_footprint_xy((11.0, 10.5), inst_b) == (
        pytest.approx(-1.0),
        pytest.approx(0.5),
    )


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
    # One FOOTPRINT document per DISTINCT footprint (slice 2f), not per
    # instance — _INSTANCES places ASYM3 eight ways and POLY1 once.
    assert [d.doc_type for d in docs] == ["PCB"] + ["FOOTPRINT"] * 2
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
    # Pro keeps the layer number in the LAYER record's id, not its body
    layer_ids = {json.loads(str(r.id))[1] for r in pcb.of_type("LAYER")}
    comp_ids = {r.id for r in pcb.of_type("COMPONENT")}
    assert len(comp_ids) == len(_INSTANCES)
    fp_docs = {d.uuid: d for d in project.by_type("FOOTPRINT")}
    assert len(fp_docs) == 2  # ASYM3 + POLY1, not one per instance

    # There is no NET record in Pro's own files: a net is declared by the
    # PAD_NET rows and the copper that name it. A declared net with no
    # members survives as a PAD_NET row whose pad slot is empty — the
    # stale-declaration mechanism the source file itself uses (S$151).
    declared = {b["padNet"] for b in pcb.bodies("PAD_NET")} | {
        str(b["netName"])
        for t in ("LINE", "ARC", "VIA", "POUR")
        for b in pcb.bodies(t)
        if b.get("netName")
    }
    model_nets = {f"N_{i['refdes']}" for i in _INSTANCES} | {"GND", "SHARED"}
    assert model_nets <= declared

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
    # every component points at one of the shared footprint documents
    assert set(fp_of.values()) == set(fp_docs)

    # PAD_NET -> a real component, a real pad number and a real pad
    # element — except the stale declaration rows, whose pad slot is "".
    n_pad_nets = 0
    for r in pcb.of_type("PAD_NET"):
        _, cid, num, pad_el = json.loads(str(r.id))
        assert cid in comp_ids
        if num == "":
            continue  # a net declaration row: no pad to resolve
        pads = fp_docs[fp_of[cid]].of_type("PAD")
        assert pad_el in {p.id for p in pads}
        assert num in {(p.body or {}).get("num") for p in pads if p.id == pad_el}
        n_pad_nets += 1
    assert n_pad_nets > 0

    # and the same mechanism declares a net no member references
    ghost = _read(_export(_model(nets=["GHOST_NET"]))).pcb()
    ghost_rows = [
        r
        for r in ghost.of_type("PAD_NET")
        if r.body and r.body.get("padNet") == "GHOST_NET"
    ]
    assert len(ghost_rows) == 1
    ghost_key = json.loads(str(ghost_rows[0].id))
    assert ghost_key[1] in {r.id for r in ghost.of_type("COMPONENT")}
    assert ghost_key[2] == ""  # pad slot empty — the stale declaration


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
    assert strings[0]["layerId"] in {
        json.loads(str(r.id))[1] for r in pcb.of_type("LAYER")
    }
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
    ex = _export(model)
    text = " | ".join(ex.warnings)
    for needle in (
        "2 pad(s) belong to no placed part",
        "1 net(s) have no exported pad and are not in the file: LONELY",
        "1 pour(s)/plane(s)",
        "1 soldermask-opening",
        "1 refdes silk stroke(s) suppressed",
        "1 silk region fill(s)",
        "unplaced part(s) omitted: X9",
    ):
        assert needle in text, needle
    # slice 2f: the loose NPTH drill is now EXPORTED as a free PAD, not
    # warned away; and R7's 37-degree pads are written with a real
    # component angle, so no oblique-shape warning fires either.
    pcb = _read(ex).pcb()
    npth = [
        b
        for b in pcb.bodies("PAD")
        if not b.get("plated") and (b.get("hole") or {}).get("width")
    ]
    assert len(npth) == 1


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
    # slice 2f: COMPONENT.angle carries the real rotation — the reader
    # recovers the instance's own rot on both sides.
    want_rot = {i["refdes"]: i["rot"] for i in _INSTANCES}
    assert {r: by_refdes[r]["rot"] for r in want_rot} == want_rot


def test_frame_round_trip_catches_a_wrong_mirror_axis():
    """The test above must be able to FAIL: mirror the bottom side in X in
    the writer's local frame and the asymmetric part no longer round-trips."""
    model = _model()
    ex = _export(model)
    stream = ex.files["epro-test.epru"]
    # flip centerX sign on every footprint pad of the shared ASYM3 doc —
    # its bottom-side members (R4/R5/R6/R8) must then land wrong, which is
    # exactly what a mistaken bottom-mirror axis does to a real Pro write.
    asym_uuid = epro_write._uuid_hex("epro-test", "fp:ASYM3")
    out: list[str] = []
    in_r4 = False
    for ln in stream.split("\n"):
        if ln.startswith('{"type":"DOCHEAD"'):
            in_r4 = asym_uuid in ln
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

    # slice 2f: the footprint doc carries AUTHORED pad dims (the real
    # component angle un-swaps the board-side rotation), not the board
    # extents — so the doc's pad w/h equals _ASYM_FP's authored w/h.
    pads = {p["pin"]: p for p in fps[comps["R2"]["footprint"]]["pads"]}
    assert pads["1"]["shape"] == "rect"
    assert (pads["1"]["w"], pads["1"]["h"]) == (
        pytest.approx(0.6, abs=1e-4),
        pytest.approx(0.9, abs=1e-4),
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
    # the "R" op anchors at its min-x/MAX-y corner in the file frame —
    # the board rect's (x0, y0)
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
    assert any("pour(s)/plane(s) written as POUR zones" in w for w in ex.warnings)
    # the reader's net census counts the pour's net as used
    assert "GND" in epro.live_nets(pcb)[0]


def test_a_free_polygon_pour_is_written_as_a_vertex_list_zone():
    """Slice 2f: Pro 3.2.206 writes the vertex-list ``"L"`` path for its
    own non-rectangular pours (observed on TAZ V1.15), so that is what a
    free polygon becomes — not a drop."""
    tri = {
        "ctype": "pour",
        "layer": "B.Cu",
        "net": "GND",
        "polygon": [[2.0, 2.0], [28.0, 2.0], [15.0, 18.0]],
    }
    ex = _export(_model(copper=[tri]))
    model_pcb = _read(ex).pcb()
    (rec,) = model_pcb.bodies("POUR")
    assert rec["netName"] == "GND" and rec["layerId"] == 2
    frame = epro_write.frame_for({"outline": _OUTLINE, "copper": [tri]})
    pts = [
        epro_write.to_epro_xy(x, y, frame)
        for x, y in [[2.0, 2.0], [28.0, 2.0], [15.0, 18.0], [2.0, 2.0]]
    ]
    assert rec["path"] == [epro_write._poly_path(pts)]
    assert ex.stats["pours"] == 1 and ex.stats["pours_dropped"] == 0
    assert any("pour(s)/plane(s) written as POUR zones" in w for w in ex.warnings)
    # and the reader's net census counts the pour's net as used
    assert "GND" in epro.live_nets(model_pcb)[0]


def test_a_pour_with_cached_fill_emits_a_poured_record():
    """Pro stores its computed pour fill in a ``POURED`` record id'd
    ``["POURED", <pour element id>]``; without one a zone opens unfilled
    and needs a manual rebuild copper. The source's fill rides through
    ``meta.epro_pours`` as ``fill`` and is re-emitted verbatim — in
    10-MIL units (the reader's documented trap), i.e. file-mils / 10."""
    pour = {
        "ctype": "pour",
        "layer": "B.Cu",
        "net": "GND",
        "polygon": [[2.0, 2.0], [28.0, 2.0], [28.0, 18.0], [2.0, 18.0], [2.0, 2.0]],
        "fill": [
            {
                "id": "copper0",
                "fill": True,
                "stroke_width": 0.0,
                "paths": [[[5.0, 5.0], [20.0, 5.0], [20.0, 15.0], [5.0, 15.0]]],
            }
        ],
    }
    ex = _export(_model(copper=[pour]))
    model_pcb = _read(ex).pcb()
    (prec,) = model_pcb.of_type("POUR")
    (drec,) = model_pcb.of_type("POURED")
    assert drec.id is not None and drec.body is not None
    assert json.loads(drec.id) == ["POURED", prec.id]
    (region,) = drec.body["pourFill"]
    assert region["fill"] is True and region["strokeWidth"] == 0.0
    assert region["id"] == "copper0"
    frame = epro_write.frame_for({"outline": _OUTLINE, "copper": [pour]})
    want = [
        (fx / 10.0, fy / 10.0)
        for fx, fy in (
            epro_write.to_epro_xy(x, y, frame)
            for x, y in [[5.0, 5.0], [20.0, 5.0], [20.0, 15.0], [5.0, 15.0]]
        )
    ]
    (ring,) = region["path"]
    got = [v for v in ring if not isinstance(v, str)]
    assert len(got) == 8
    for i, (wx, wy) in enumerate(want):
        assert got[2 * i] == pytest.approx(wx, abs=1e-3)
        assert got[2 * i + 1] == pytest.approx(wy, abs=1e-3)
    # and the fill survives a re-import as the pour's "fill" again
    design, _ = epro.build_design(_read(ex))
    (rt_pour,) = design.epro_pours
    assert len(rt_pour["fill"]) == 1
    rt_ring = rt_pour["fill"][0]["paths"][0]
    assert [xy for pt in rt_ring for xy in pt] == pytest.approx(
        [5.0, 5.0, 20.0, 5.0, 20.0, 15.0, 5.0, 15.0]
    )


def test_a_pour_without_fill_emits_no_poured():
    """A precis-authored pour has no computed fill to cache — the zone
    still opens unfilled and Pro rebuilds it on demand."""
    pour = {
        "ctype": "pour",
        "layer": "B.Cu",
        "net": "GND",
        "polygon": [[2.0, 2.0], [28.0, 2.0], [28.0, 18.0], [2.0, 18.0], [2.0, 2.0]],
    }
    ex = _export(_model(copper=[pour]))
    model_pcb = _read(ex).pcb()
    assert model_pcb.bodies("POUR") and model_pcb.of_type("POURED") == []


def test_a_pour_on_an_undeclared_layer_is_still_dropped():
    off = {
        "ctype": "pour",
        "layer": "X.Cu",
        "net": "GND",
        "polygon": [[0, 0], [1, 0], [1, 1], [0, 1]],
    }
    ex = _export(_model(copper=[off]))
    assert _read(ex).pcb().bodies("POUR") == []
    assert ex.stats["pours"] == 0 and ex.stats["pours_dropped"] == 1
    assert any("1 pour(s)" in w for w in ex.warnings)


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
    assert "show NO schematic pages" in resp.body
    assert "Update PCB from schematic" in resp.body
    # the seeded board has no routed copper, but its stackup declares an
    # In1.Cu GND plane — which now writes an outline-covering POUR zone
    assert "pour zone(s)" in resp.body
    assert "Verified: Reto opened" in resp.body
    assert "UNVERIFIED" not in resp.body

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
    layer_ids = {json.loads(str(r.id))[1] for r in pcb.of_type("LAYER")}
    # A net on copper is declared by the copper that names it (and by any
    # PAD_NET row) — there is no NET record in Pro's own files.
    declared = {b["padNet"] for b in pcb.bodies("PAD_NET")}
    seen = {"LINE": 0, "ARC": 0, "VIA": 0}
    for r in pcb.records:
        if r.type in seen:
            assert r.body is not None
            seen[r.type] += 1
            assert r.body["netName"] == "CU_ONLY"
            declared.add(str(r.body["netName"]))
            if "layerId" in r.body:
                assert r.body["layerId"] in layer_ids
    assert seen == {"LINE": 1, "ARC": 1, "VIA": 1}
    assert "CU_ONLY" in declared


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
    # the triangle pour is written as a vertex-list zone (2f); the
    # re-pour caveat is what warns, not a drop
    assert (ex.stats["lines"], ex.stats["vias"], ex.stats["pours"]) == (1, 1, 1)
    assert ex.stats["pours_dropped"] == 0


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


def test_slot_hole_is_written_back_as_a_slot_that_the_reader_reads_again():
    pad = {
        "x": 5.0,
        "y": 5.0,
        "w": 2.4,
        "h": 1.4,
        "shape": "obround",
        "pin": "1",
        "drill": 0.5,
        "slot": [[4.5, 5.0], [5.5, 5.0]],
    }
    inst = {"x": 5.0, "y": 5.0, "rot": 0.0, "layer": "top"}
    body = epro_write._pad_body(epro_write._local_pad(pad, inst), 0)
    hole = body["hole"]
    assert hole["holeType"] == "SLOT"
    back, warns = epro._pad_to_precis(body, "fp")
    assert back is not None and warns == []
    assert back["drill"] == pytest.approx(0.5, abs=0.01)
    assert back["slot"]["w"] == pytest.approx(1.5, abs=0.01)
    assert back["slot"]["h"] == pytest.approx(0.5, abs=0.01)


def test_a_rotated_instance_writes_its_slot_against_the_pad_frame():
    # 45 deg on the instance, not in the pad: the slot is axis-aligned in
    # the pad's own frame, so the file carries a real SLOT hole, not the
    # round-hole fallback (slice 2f — real component angles).
    ex = _export(_model([_inst("S1", 10.0, 10.0, 45.0, "top", fp="SLOT1")]))
    assert not any("slot hole" in w for w in ex.warnings)
    fp = _read(ex).by_type("FOOTPRINT")[0]
    (hole,) = [b["hole"] for b in fp.bodies("PAD") if "hole" in b]
    assert hole["holeType"] == "SLOT"


def test_an_oblique_slot_warns_that_it_is_written_round():
    # A slot diagonal in the PAD's own frame cannot be a SLOT hole —
    # hole w/h are pad-local, so it degrades to ROUND with a warning.
    model = _model()
    model["pads"].append(
        {
            "refdes": "R1",
            "pin": "9",
            "net": "",
            "shape": "obround",
            "x": 6.0,
            "y": 5.0,
            "w": 2.0,
            "h": 1.0,
            "drill": 0.5,
            "slot": [[5.5, 4.5], [6.5, 5.5]],
        }
    )
    ex = _export(model)
    assert any("slot hole off the pad" in w and "R1" in w for w in ex.warnings)


def test_a_right_angle_slot_writes_slot_and_does_not_warn():
    ex = _export(_model([_inst("S1", 10.0, 10.0, 90.0, "top", fp="SLOT1")]))
    assert not any("slot hole" in w for w in ex.warnings)


# ── the full structural round trip (slice 3) ────────────────────────────
def _design_model(design, pcb, frame) -> dict[str, Any]:
    """Assemble the writer's model from a read-in design — the same
    assembly ``_fab_model`` runs: footprint pads placed per instance,
    footprint holes and copper carried, the epro passthroughs attached.
    Used by the structural round-trip tests so BOTH the committed fixture
    and the real board go through one code path."""
    layers = list(epro.copper_layers(pcb).values())
    flat, _ = epro.measured_copper(pcb, frame)
    outline = next(f for f in design.features if f["ftype"] == "outline")
    fps = {f["name"]: f for f in design.footprints}
    net_of = {(c["refdes"], c["pin"]): c["net"] for c in design.connections}
    pads: list[dict[str, Any]] = []
    drills: list[dict[str, Any]] = []
    footprint_holes: dict[str, list[dict[str, Any]]] = {}
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
        if fp.get("holes"):
            footprint_holes[str(comp["refdes"])] = list(fp["holes"])
    # board-level mounting holes -> drills rows, as _fab_model's does
    for f in design.features:
        if f["ftype"] == "mounting_hole":
            g = f["geom"]
            drills.append(
                {
                    "x": f["x"],
                    "y": f["y"],
                    "dia_mm": g["diameter"],
                    "plated": g.get("plated", False),
                }
            )
    for pour in design.epro_pours:
        flat.append({"ctype": "pour", **pour})
    model: dict[str, Any] = {
        "layers": layers,
        "outline": outline["geom"]["path"],
        "copper": flat,
        "pads": pads,
        "drills": drills,
        "silkscreen": {"top": [], "bottom": []},
        "mask_open_regions": [],
        # the writer's model shape takes the graph's key names: the
        # reader's comp["part_meta"] is what pcb_apply stores under
        # component meta["part"], and the graph hoists back as
        # instance["part"]; pin etype/nc ride "pin_sem" the way the
        # handler's model assembles it from pcb_pins tags/meta.
        "instances": [
            {
                **c,
                "part": c.get("part_meta") or {},
                "pin_sem": {
                    str(p["name"]): {
                        k: v
                        for k, v in (
                            ("etype", p.get("etype")),
                            ("nc", True if p.get("nc") else None),
                        )
                        if v
                    }
                    for p in c.get("pins", [])
                },
            }
            for c in design.components
        ],
        "footprint_holes": footprint_holes,
        "nets": [n["name"] for n in design.nets],
        "epro_rules": design.epro_rules,
        "epro_board": design.epro_board,
        # derived plane assignments (derive_stackup's semantic: an inner
        # layer whose pours all name one net) — the writer emits a POUR
        # for each, so the plane survives the trip.
        "planes": _derived_planes(pcb),
    }
    return model


def _derived_planes(pcb) -> dict[str, str]:
    """derive_stackup's plane rule, pure: an INNER copper layer whose
    pours all name one net."""
    layers = epro.copper_layers(pcb)
    names = list(layers.values())
    pour_nets: dict[str, set[str]] = {}
    for b in pcb.bodies("POUR"):
        name = layers.get(int(b.get("layerId") or 0))
        if name:
            pour_nets.setdefault(name, set()).add(str(b.get("netName") or ""))
    outer = {names[0], names[-1]} if names else set()
    return {
        name: next(iter(nets))
        for name, nets in pour_nets.items()
        if name not in outer and len(nets - {""}) == 1
    }


def _pad_entries(design, layers) -> list[dict[str, Any]]:
    """Every placed pad of every instance, in board frame, tagged with
    its refdes and pad number/name — through the same
    ``place_footprint_pads`` the import takes. Compared by POSITION, not
    pin name: a pad the source calls ``BAT`` through its device pin_map
    is the same copper as pad number ``4`` on the re-imported file (no
    DEVICE records there)."""
    fps = {f["name"]: f for f in design.footprints}
    net_of = {(c["refdes"], c["pin"]): c["net"] for c in design.connections}
    out: list[dict[str, Any]] = []
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
        pp, _dd = padplace.place_footprint_pads(
            raw, comp, layers=layers, pin_to_net=pad_net
        )
        for p in pp:
            out.append({**p, "refdes": comp["refdes"], "pad_num": str(p["pin"])})
    return out


def _match_pads(
    want: list[dict[str, Any]], got: list[dict[str, Any]]
) -> tuple[
    list[tuple[dict[str, Any], dict[str, Any]]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """Pair want/got pads by (refdes, nearest position) within 0.005 mm —
    the exported file's mil quantization and a fresh float transform can
    shift a pad by ~0.5 um, which straddles any fixed rounding boundary."""
    unused = list(range(len(got)))
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    missing: list[dict[str, Any]] = []
    for w in want:
        best, best_d = None, 0.005
        for gi in unused:
            g = got[gi]
            if g["refdes"] != w["refdes"]:
                continue
            d = max(abs(g["x"] - w["x"]), abs(g["y"] - w["y"]))
            if d < best_d:
                best, best_d = gi, d
        if best is None:
            missing.append(w)
        else:
            unused.remove(best)
            pairs.append((w, got[best]))
    return pairs, missing, [got[gi] for gi in unused]


def _ring(points: list[list[float]]) -> list[tuple[float, float]]:
    """A polygon ring canonicalised for comparison: closing vertex
    dropped, rotated to start at the lowest (x, y) vertex."""
    pts = [(float(p[0]), float(p[1])) for p in points]
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    if not pts:
        return []
    k = min(range(len(pts)), key=lambda i: pts[i])
    return pts[k:] + pts[:k]


def _structural_diff(src_design, src_pcb, src_frame, out_bytes):
    """Compare a source design against the design the EXPORTED file
    re-imports to. Returns ``(summary_lines, failures)`` — failures are
    the lines a human should read first; the summary is the diff report
    the backlog asks to print."""
    project2 = epro.read_archive(out_bytes)
    pcb2 = project2.pcb()
    d2, f2 = epro.build_design(project2, pcb2)
    lines: list[str] = []
    fails: list[str] = []

    def check(ok: bool, msg: str) -> None:
        (fails if not ok else lines).append(("FAIL " if not ok else "  ok ") + msg)

    # parts
    want_c = {c["refdes"]: c for c in src_design.components}
    got_c = {c["refdes"]: c for c in d2.components}
    check(set(got_c) == set(want_c), f"parts {len(want_c)} -> {len(got_c)}")
    worst = 0.0
    for r, c in want_c.items():
        if r in got_c:
            worst = max(
                worst,
                abs(got_c[r]["x"] - c["x"]),
                abs(got_c[r]["y"] - c["y"]),
            )
            check(
                got_c[r]["rot"] == c["rot"] % 360.0 and got_c[r]["layer"] == c["layer"],
                f"  {r}: rot/layer {c['rot']}/{c['layer']} -> {got_c[r]['rot']}/{got_c[r]['layer']}",
            )
    check(worst < _TOL_MM, f"part positions worst {worst * 1000:.3f} um")

    # part identity: the LCSC C-number and the catalog attrs (value, MPN,
    # manufacturer, supplier) ride ATTR records out and back
    lc_w = {r: c.get("part_lcsc") for r, c in want_c.items()}
    lc_g = {r: c.get("part_lcsc") for r, c in got_c.items()}
    check(
        lc_w == lc_g,
        f"part_lcsc {sum(v is not None for v in lc_w.values())} -> "
        f"{sum(v is not None for v in lc_g.values())} "
        f"(missing {sorted(r for r in want_c if lc_g.get(r) != lc_w[r])[:6]})",
    )
    pm_w = {
        r: {k: v for k, v in (c.get("part_meta") or {}).items() if k != "lcsc_source"}
        for r, c in want_c.items()
    }
    pm_g = {
        r: {k: v for k, v in (c.get("part_meta") or {}).items() if k != "lcsc_source"}
        for r, c in got_c.items()
    }
    check(
        {r: v for r, v in pm_w.items() if v} == {r: v for r, v in pm_g.items() if v},
        "part attrs (value/mpn/mfr/supplier) verbatim per part",
    )
    # pin semantics: `Pin Type`/`NO_CONNECT` on the way in ride the
    # "Pin Types" ATTR on the way out — keyed by the pin NAME the
    # footprint's num carries, which is what the re-imported pin's own
    # pad/name reads back.
    sem_w = {
        r: {
            str(p["name"]): (p.get("etype"), bool(p.get("nc")))
            for p in c.get("pins", [])
            if p.get("etype") or p.get("nc")
        }
        for r, c in want_c.items()
    }
    sem_g = {
        r: {
            str(p["name"]): (p.get("etype"), bool(p.get("nc")))
            for p in c.get("pins", [])
            if p.get("etype") or p.get("nc")
        }
        for r, c in got_c.items()
    }
    check(
        {r: v for r, v in sem_w.items() if v} == {r: v for r, v in sem_g.items() if v},
        "pin etype/nc per part verbatim",
    )

    # pads incl. drills and slots — paired by (refdes, nearest position)
    layers = list(epro.copper_layers(src_pcb).values())
    layers2 = list(epro.copper_layers(pcb2).values())
    want_e = _pad_entries(src_design, layers)
    got_e = _pad_entries(d2, layers2)
    pairs, missing_p, extra_p = _match_pads(want_e, got_e)
    check(
        not missing_p and not extra_p,
        f"pads {len(want_e)} -> {len(got_e)} "
        f"(unmatched {[(p['refdes'], p['pad_num']) for p in missing_p][:5]}, "
        f"extra {[(p['refdes'], p['pad_num']) for p in extra_p][:5]})",
    )
    worst = 0.0
    for p, g in pairs:
        k = (p["refdes"], p["pad_num"])
        worst = max(worst, abs(g["x"] - p["x"]), abs(g["y"] - p["y"]))
        for fld in ("shape", "net"):
            check(
                g.get(fld) == p.get(fld),
                f"  pad {k}: {fld} {p.get(fld)!r} -> {g.get(fld)!r}",
            )
        for fld in ("w", "h"):
            check(
                abs(float(g.get(fld) or 0) - float(p.get(fld) or 0)) < _TOL_MM,
                f"  pad {k}: {fld} {p.get(fld)} -> {g.get(fld)}",
            )
        check(
            (g.get("drill") or None) == (p.get("drill") or None),
            f"  pad {k}: drill {p.get('drill')} -> {g.get('drill')}",
        )
        # slot: the two centre-line endpoints, either order, within tol
        s1, s2 = p.get("slot"), g.get("slot")
        if s1 or s2:
            ok = bool(s1) and bool(s2)
            if s1 and s2:
                a = {tuple(pt) for pt in s1}
                b = {tuple(pt) for pt in s2}
                ok = all(
                    min(max(abs(px - qx), abs(py - qy)) for qx, qy in b) < _TOL_MM
                    for px, py in a
                )
            check(ok, f"  pad {k}: slot {s1} -> {s2}")
    check(worst < _TOL_MM, f"pad positions worst {worst * 1000:.3f} um")

    # pad-net membership: every connection resolves onto a paired pad
    # (the per-pad net check above compares them directly) — count parity
    # proves no connection row was lost.
    check(
        len(d2.connections) == len(src_design.connections),
        f"connection rows {len(src_design.connections)} -> {len(d2.connections)}",
    )
    names_w = {n["name"] for n in src_design.nets}
    # the FILE's net declarations are the netlist's survival, not the
    # re-imported design's live census — a net kept alive in the source
    # only by stale PAD_NET rows (taz15's S$151) has its declaration but
    # no member, and precis' reader correctly does not count it live.
    # Pro declares nets through PAD_NET rows and copper netNames (there
    # is no NET record in its own files); both count here.
    declared_g = {str(b["padNet"]) for b in pcb2.bodies("PAD_NET")} | {
        str(b["netName"])
        for t in ("LINE", "ARC", "VIA", "POUR")
        for b in pcb2.bodies(t)
        if b.get("netName")
    }
    declared_g |= {
        json.loads(str(r.id))[1]
        for r in pcb2.of_type("NET")
        if r.id and str(r.id).startswith("[")
    }
    check(
        names_w <= declared_g,
        f"net declarations {len(names_w)} -> {len(declared_g)} in file"
        f" (missing {sorted(names_w - declared_g)[:6]})",
    )
    names_g = {n["name"] for n in d2.nets}
    lines.append(
        f"  -- {len(names_g)} nets live in the re-imported design"
        f" ({sorted(names_w - names_g)} declared but memberless)"
    )
    cls_w = {
        n["name"]: n.get("net_class") for n in src_design.nets if n.get("net_class")
    }
    cls_g = {n["name"]: n.get("net_class") for n in d2.nets}
    check(
        {k: cls_w[k] for k in cls_w if k in cls_g}
        == {k: v for k, v in cls_g.items() if k in cls_w},
        f"net classes {len(cls_w)} stamped -> {sorted(str(v) for v in set(cls_g.values()))}",
    )
    check(d2.net_classes == src_design.net_classes, "mapped rule classes identical")

    # copper: tracks + vias as canonical segments
    flat1, _ = epro.measured_copper(src_pcb, src_frame)
    flat2, _ = epro.measured_copper(pcb2, f2)
    w_segs, g_segs = _canon_segments(flat1), _canon_segments(flat2)
    check(set(g_segs) == set(w_segs), f"copper segments {len(w_segs)} -> {len(g_segs)}")
    worst_cu = _worst_um(w_segs, g_segs)
    check(worst_cu < _TOL_MM * 1000.0, f"copper worst {worst_cu:.3f} um")

    # pours: authored zone outlines, verbatim
    key = lambda p: (p["layer"], p["net"])
    want_pours = {key(p): _ring(p["polygon"]) for p in src_design.epro_pours}
    got_pours = {key(p): _ring(p["polygon"]) for p in d2.epro_pours}
    check(
        set(got_pours) == set(want_pours),
        f"pours {len(want_pours)} -> {len(got_pours)}",
    )
    worst_pour = 0.0
    for k, ring in want_pours.items():
        gp = got_pours.get(k)
        if gp is None or len(gp) != len(ring):
            fails.append(
                f"FAIL   pour {k}: vertex count {len(ring)} -> {len(gp or [])}"
            )
            continue
        for (ax, ay), (bx, by) in zip(ring, gp):
            worst_pour = max(worst_pour, abs(ax - bx), abs(ay - by))
    check(worst_pour < _TOL_MM, f"pour vertices worst {worst_pour * 1000:.3f} um")

    # pour fill: the source's computed POURed regions ride through
    # verbatim (the cache that makes Pro open the board already filled)
    want_fill = {
        key(p): sorted(len(r.get("paths") or []) for r in p.get("fill") or [])
        for p in src_design.epro_pours
    }
    got_fill = {
        key(p): sorted(len(r.get("paths") or []) for r in p.get("fill") or [])
        for p in d2.epro_pours
    }
    check(want_fill == got_fill, f"pour fill region counts {want_fill} -> {got_fill}")
    worst_fill = 0.0
    fill_pts = 0
    for k, sp in ((key(p), p) for p in src_design.epro_pours):
        gpour = next((g for g in d2.epro_pours if key(g) == k), None)
        if gpour is None:
            continue
        for rs, rg in zip(
            sorted(sp.get("fill") or [], key=lambda r: str(r.get("id"))),
            sorted(gpour.get("fill") or [], key=lambda r: str(r.get("id"))),
        ):
            for ring_s, ring_g in zip(rs["paths"], rg["paths"]):
                fill_pts += len(ring_s)
                for (ax, ay), (bx, by) in zip(ring_s, ring_g):
                    worst_fill = max(worst_fill, abs(ax - bx), abs(ay - by))
    check(
        worst_fill < _TOL_MM,
        f"pour fill vertices worst {worst_fill * 1000:.3f} um over {fill_pts} pts",
    )

    # rules: every record verbatim
    want_r = sorted((r["type"], str(r["id"])) for r in src_design.epro_rules)
    got_r = sorted((r["type"], str(r["id"])) for r in d2.epro_rules)
    check(got_r == want_r, f"rule records {len(want_r)} -> {len(got_r)}")
    bodies = {str(r["id"]): r["body"] for r in src_design.epro_rules}
    bodies2 = {str(r["id"]): r["body"] for r in d2.epro_rules}
    check(bodies == bodies2, "rule bodies byte-identical")

    # outline
    o1 = next(f for f in src_design.features if f["ftype"] == "outline")
    o2 = next((f for f in d2.features if f["ftype"] == "outline"), None)
    check(o2 is not None, "outline feature present")
    worst_o = 0.0
    if o2 is not None:
        r1, r2 = _ring(o1["geom"]["path"]), _ring(o2["geom"]["path"])
        check(len(r1) == len(r2), f"outline vertices {len(r1)} -> {len(r2)}")
        for (ax, ay), (bx, by) in zip(r1, r2):
            worst_o = max(worst_o, abs(ax - bx), abs(ay - by))
    check(worst_o < _TOL_MM, f"outline worst {worst_o * 1000:.3f} um")

    # mounting holes (board-level + footprint FILL holes)
    def holes_of(design):
        out = set()
        for f in design.features:
            if f["ftype"] == "mounting_hole":
                out.add(
                    (
                        round(f["x"], 3),
                        round(f["y"], 3),
                        round(f["geom"]["diameter"], 3),
                    )
                )
        fps = {f["name"]: f for f in design.footprints}
        for comp in design.components:
            fp = fps.get(comp.get("footprint"))
            for h in (fp or {}).get("holes", []):
                wx, wy = padplace.place_pad_point({"x": h["x"], "y": h["y"]}, comp)
                out.add((round(wx, 3), round(wy, 3), round(h["dia_mm"], 3)))
        return out

    h_w, h_g = holes_of(src_design), holes_of(d2)
    check(
        h_g == h_w, f"holes {len(h_w)} -> {len(h_g)} (missing {sorted(h_w - h_g)[:4]})"
    )

    # silk: source silk art is not imported (it is regenerated), so the
    # check is coverage — one editable Designator ATTR per component.
    designators = {
        a.get("value") for a in pcb2.bodies("ATTR") if a.get("key") == "Designator"
    }
    check(
        designators >= set(want_c),
        f"designators {len(want_c)} wanted -> {len(designators)} written",
    )
    silk_src = len(src_pcb.bodies("STRING")) + sum(
        1 for b in src_pcb.bodies("POLY") if str(b.get("layerId")) in {"3", "4"}
    )
    lines.append(
        f"  -- silk: {silk_src} source records are NOT imported "
        f"(regenerated on export); {len(pcb2.bodies('POLY'))} POLYs + "
        f"{len(designators)} designators written"
    )
    return lines, fails, d2, pcb2


#: Reto's TAZ Control Board V1.15 — the gitignored local copy. The test
#: skips when it is absent, so CI stays green; locally it is the proof
#: Reto asked for ("I want round trip to work").
_TAZ = pathlib.Path(__file__).parent.parent / "epro.local" / "taz15.epro2"
_TAZ_OUT = _TAZ.with_name("taz15-roundtrip.epro2")


@pytest.mark.skipif(
    not _TAZ.exists(), reason="taz15.epro2 lives in the local epro.local/ only"
)
def test_taz15_import_export_reimport_structural(store, pcb, tmp_path) -> None:
    """The real board, through the product path: ``import_epro`` ->
    ``view='epro'`` -> re-import -> structural diff. Every geometry
    category within 0.5 um; the written file is left next to the source
    in ``epro.local/`` for Reto to open in EasyEDA Pro (the UNVERIFIED
    banner stands until a human does)."""
    from precis.ingest import pcb_epro

    data = _TAZ.read_bytes()
    project = epro.read_archive(data)
    src_pcb = project.pcb("858a5f81603d1987")
    src_design, src_frame = epro.build_design(project, src_pcb)

    first = pcb_epro.import_epro(
        store,
        data,
        slug="taz15-rt",
        title="TAZ RT",
        board_uuid="858a5f81603d1987",
    )
    assert first.created
    # gr486958: the source's full outer pours are pour-carried nets —
    # B.Cu GND and F.Cu 3V3 land as stackup `pour_nets` plus the authored
    # `pcb_planes` rows the taz15-blank baseline used to set by hand.
    assert first.pour_nets == {"B.Cu": ["GND"], "F.Cu": ["3V3"]}
    assert {(p["layer"], p["net"]) for p in store.pcb_planes_list(first.ref_id)} >= {
        ("B.Cu", "GND"),
        ("F.Cu", "3V3"),
    }
    # The LCSC census (slice 3): every Supplier Part C-number the source
    # carries survives on the component. The board's exceptions are the
    # exact 11 the source simply lacks — JP1/JP2 are bare headers, the
    # C13-…C24 cluster are unpopulated/manual parts.
    src_lcsc = {str(c["refdes"]): c.get("part_lcsc") for c in src_design.components}
    assert sum(v is not None for v in src_lcsc.values()) == 85
    assert {r for r, v in src_lcsc.items() if v is None} == {
        "JP1",
        "JP2",
        "C13",
        "C14",
        "C15",
        "C16",
        "C17",
        "C19",
        "C20",
        "C21",
        "C24",
    }
    got_lcsc = {
        str(i["refdes"]): i.get("part_lcsc")
        for i in store.pcb_graph(first.ref_id)["instances"]
    }
    assert got_lcsc == src_lcsc
    resp = pcb.get(id="taz15-rt", view="epro", args={"dir": str(tmp_path)})
    out = tmp_path / "taz15-rt.epro2"
    assert out.exists(), resp.body

    out_bytes = out.read_bytes()
    # The file the diff clears is also the file Reto opens — copy it next
    # to the source so it survives the tmp dir.
    _TAZ_OUT.write_bytes(out_bytes)

    lines, fails, d2, pcb2 = _structural_diff(src_design, src_pcb, src_frame, out_bytes)
    print("\n".join(lines))
    for line in fails:
        print(line)
    # Record-level shape parity with the source: every (docType, record
    # type) the file emits carries every field Pro always writes for it,
    # with a compatible value kind — the parse-crash class Pro hit on
    # 2026-10-14 (LAYER colors, ATTR positions).
    shape_fails = _shape_failures_vs_source(data, out_bytes)
    for line in shape_fails[:20]:
        print(line)
    # and the exported file re-imports through the store as well — the
    # product path, not only the reader.
    second = pcb_epro.import_epro(
        store, out_bytes, slug="taz15-rt-back", title="TAZ RT back"
    )
    assert second.created and second.stats["components"] == len(src_design.components)
    # The pour zones come back verbatim, so the re-import derives the same
    # pour-carried nets.
    assert second.pour_nets == first.pour_nets
    assert not fails, f"{len(fails)} structural differences:\n" + "\n".join(fails[:30])
    assert not shape_fails, (
        f"{len(shape_fails)} record-shape differences vs the source:\n"
        + "\n".join(shape_fails[:30])
    )


def test_the_committed_fixture_round_trips_structurally(store, pcb, tmp_path) -> None:
    """The same structural diff in CI, on the committed tiny fixture —
    the file a reviewer can regenerate without Reto's board."""
    from precis.ingest import pcb_epro

    fixture = pathlib.Path(__file__).parent / "fixtures" / "pcb_epro_tiny"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in ("project2.json", "board.epru"):
            zf.writestr(name, (fixture / name).read_bytes())
    data = buf.getvalue()
    project = epro.read_archive(data)
    src_pcb = project.pcb()
    src_design, src_frame = epro.build_design(project, src_pcb)

    first = pcb_epro.import_epro(store, data, slug="tiny-rt", title="Tiny RT")
    resp = pcb.get(id="tiny-rt", view="epro", args={"dir": str(tmp_path)})
    out = tmp_path / "tiny-rt.epro2"
    assert out.exists(), resp.body

    lines, fails, d2, pcb2 = _structural_diff(
        src_design, src_pcb, src_frame, out.read_bytes()
    )
    print("\n".join(lines))
    assert not fails, f"{len(fails)} structural differences:\n" + "\n".join(fails[:30])


def test_the_committed_fixture_round_trips_through_the_pure_model() -> None:
    """Same diff without a store: design -> the model assembly -> write
    -> re-read. Pins that the file alone (not ``_fab_model``'s extras)
    carries the structure."""
    fixture = pathlib.Path(__file__).parent / "fixtures" / "pcb_epro_tiny"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in ("project2.json", "board.epru"):
            zf.writestr(name, (fixture / name).read_bytes())
    project = epro.read_archive(buf.getvalue())
    src_pcb = project.pcb()
    src_design, src_frame = epro.build_design(project, src_pcb)

    model = _design_model(src_design, src_pcb, src_frame)
    ex = epro_write.epro_files(model, slug="tiny-pure")
    out = epro_write.zip_epro(ex.files)
    lines, fails, _d2, _pcb2 = _structural_diff(src_design, src_pcb, src_frame, out)
    print("\n".join(lines))
    assert not fails, f"{len(fails)} structural differences:\n" + "\n".join(fails[:30])


# ── record field shapes vs Pro's own writes ─────────────────────────
#: Field shapes Pro itself writes per record type, transcribed from the
#: real TAZ V1.15 project (Pro 3.2.206) — every document type it carries.
#: ``req`` fields must be present on every emitted record of the type
#: with one of the listed value kinds (``num`` = int|float, ``none`` =
#: JSON null, ``str``, ``bool``, ``list``, ``dict``); ``opt`` fields may
#: appear. An emitted field the row does not list, an emitted record
#: type the table does not list, a ``req`` slot left null, or a null in
#: a ``num`` slot is a shape mismatch — the 2026-10-14 Pro open attempt
#: died on exactly those (x13 ``startsWith`` on a missing LAYER color,
#: then ``NaN is not permitted`` on a null ATTR x).
_PRO_SHAPE: dict[str, tuple[dict[str, set[str]], set[str]]] = {
    "DOCHEAD": (
        {
            "docType": {"str"},
            "client": {"str"},
            "uuid": {"str"},
            "updateTime": {"num"},
            "version": {"str"},
            "editVersion": {"str"},
            "user": {"dict"},
        },
        set(),
    ),
    "LAYER": (
        {
            "layerType": {"str"},
            "layerName": {"str"},
            "use": {"bool"},
            "show": {"bool"},
            "locked": {"bool"},
            "activeColor": {"str"},
            "inactiveColor": {"str"},
            "activateTransparency": {"num"},
            "inactiveTransparency": {"num"},
        },
        set(),
    ),
    "LAYER_PHYS": (
        {
            "material": {"str"},
            "thickness": {"num"},
            "permittivity": {"num"},
            "lossTangent": {"num"},
            "isKeepIsland": {"bool"},
            "zIndex": {"num"},
        },
        set(),
    ),
    "CANVAS": (
        {
            "originX": {"num"},
            "originY": {"num"},
            "unit": {"str"},
            "gridXSize": {"num"},
            "gridYSize": {"num"},
            "snapXSize": {"num"},
            "snapYSize": {"num"},
            "altSnapXSize": {"num"},
            "altSnapYSize": {"num"},
            "gridType": {"str"},
            "multiGridType": {"str"},
            "multiGridRatio": {"num"},
            "highlightValue": {"num"},
            "layerBrightness": {"str"},
        },
        set(),
    ),
    "META": (
        {"title": {"str"}, "source": {"str"}},
        {"tags", "parent", "board", "zIndex", "attributes", "images"},
    ),
    "PREFERENCE": (
        {
            "startTrackWidthFollowLast": {"bool"},
            "lastTrackWidth": {"num"},
            "startViaSizeFollowLast": {"bool"},
            "lastViaInnerDiameter": {"num"},
            "lastViaDiameter": {"num"},
            "snap": {"bool"},
            "routingMode": {"str"},
            "routingCorner": {"str"},
            "removeLoop": {"bool"},
            "rotatingObject": {"bool"},
            "trackFollow": {"none"},
            "stretchTrackMinCorner": {"num"},
            "preferenceConfig": {"str"},
            "realTimeUpdateUnusedLayers": {"bool"},
            "unusedPadRange": {"str"},
            "pushVia": {"str"},
            "pathOptimization4BePushed": {"str"},
            "currentPathOptimization4BePushed": {"str"},
            "removeCircuitsContainingVias": {"bool"},
            "removeAntenna": {"bool"},
        },
        set(),
    ),
    "ACTIVE_LAYER": ({"layerId": {"num"}}, set()),
    "SILK_OPTS": ({"defaultColor": {"str"}, "baseColor": {"str"}}, set()),
    "PRIMITIVE": ({"display": {"bool"}, "pick": {"bool"}}, set()),
    "RULE": ({"ruleState": {"str"}, "ruleContext": {"dict"}}, set()),
    "COMPONENT": (
        {
            "layerId": {"num"},
            "groupId": {"str"},
            "locked": {"bool"},
            "zIndex": {"num"},
            "partitionId": {"none"},
            "x": {"num"},
            "y": {"num"},
            "angle": {"num"},
            "attrs": {"dict"},
            "specialColor": {"none"},
            "footprintPrimitives": {"bool"},
            "pinSwap": {"bool"},
        },
        set(),
    ),
    "ATTR": (
        {
            "layerId": {"num"},
            "groupId": {"str"},
            "locked": {"bool"},
            "zIndex": {"num"},
            "partitionId": {"none"},
            "parentId": {"str"},
            "key": {"str"},
            "value": {"str", "none"},
            "keyVisible": {"bool"},
            "valueVisible": {"bool"},
            "x": {"num"},
            "y": {"num"},
            "angle": {"num"},
            "origin": {"str"},
            "fontFamily": {"str"},
            "fontSize": {"num"},
            "strokeWidth": {"num"},
            "expansion": {"num"},
            "reverse": {"bool"},
            "mirror": {"bool"},
            "specialColor": {"none", "str"},
        },
        {"bold", "italic"},
    ),
    "PAD": (
        {
            "layerId": {"num"},
            "groupId": {"str"},
            "locked": {"bool"},
            "zIndex": {"num"},
            "partitionId": {"none"},
            "netName": {"str"},
            "num": {"str"},
            "centerX": {"num"},
            "centerY": {"num"},
            "padAngle": {"num"},
            "hole": {"dict"},
            "defaultPad": {"dict"},
            "specialPad": {"list"},
            "padOffsetX": {"num"},
            "padOffsetY": {"num"},
            "relativeAngle": {"num"},
            "plated": {"bool"},
            "padType": {"str"},
            "topSolderExpansion": {"num"},
            "bottomSolderExpansion": {"num"},
            "topPasteExpansion": {"num"},
            "bottomPasteExpansion": {"num"},
            "connectMode": {"none"},
            "spokeSpace": {"none"},
            "spokeWidth": {"none"},
            "spokeAngle": {"none"},
            "unusedInnerLayers": {"list"},
            "refs": {"list"},
        },
        set(),
    ),
    "PAD_NET": (
        {"padNet": {"str"}, "padLen": {"none"}, "propagationDelay": {"none"}},
        set(),
    ),
    "LINE": (
        {
            "layerId": {"num"},
            "groupId": {"str"},
            "locked": {"bool"},
            "zIndex": {"num"},
            "partitionId": {"none"},
            "netName": {"str"},
            "startX": {"num"},
            "startY": {"num"},
            "endX": {"num"},
            "endY": {"num"},
            "width": {"num"},
        },
        {
            "angle",
            "arcType",
            "fillColor",
            "fillStyle",
            "lineGroup",
            "strokeColor",
            "strokeStyle",
            "strokeWidth",
        },
    ),
    "VIA": (
        {
            "layerId": {"num"},
            "groupId": {"str"},
            "locked": {"bool"},
            "zIndex": {"num"},
            "partitionId": {"none"},
            "netName": {"str"},
            "centerX": {"num"},
            "centerY": {"num"},
            "holeDiameter": {"num"},
            "viaDiameter": {"num"},
            "viaType": {"str"},
            "topSolderExpansion": {"none", "num"},
            "bottomSolderExpansion": {"none", "num"},
            "unusedInnerLayers": {"list"},
        },
        set(),
    ),
    "POLY": (
        {
            "layerId": {"num"},
            "groupId": {"str"},
            "locked": {"bool"},
            "zIndex": {"num"},
            "partitionId": {"none"},
            "netName": {"str"},
            "width": {"num"},
            "path": {"list"},
            "polyType": {"str"},
            "specialColor": {"none"},
        },
        {
            "closed",
            "points",
            "fillColor",
            "fillStyle",
            "strokeColor",
            "strokeStyle",
            "strokeWidth",
            "partId",
        },
    ),
    "POUR": (
        {
            "layerId": {"num"},
            "groupId": {"str"},
            "locked": {"bool"},
            "zIndex": {"num"},
            "partitionId": {"none"},
            "netName": {"str"},
            "width": {"num"},
            "name": {"str"},
            "order": {"num"},
            "path": {"list"},
            "pourType": {"dict"},
            "keepIsland": {"bool"},
        },
        set(),
    ),
    # The computed-fill cache that pairs with each POUR
    # (["POURED", <pour id>]): Pro shows the zone filled on open when it
    # is present. Its path coordinates are 10-mil Standard units, not
    # mils — checked against the source by _shape_failures_vs_source.
    "POURED": (
        {
            "pourFill": {"list"},
        },
        set(),
    ),
    "FILL": (
        {
            "layerId": {"num"},
            "groupId": {"str"},
            "locked": {"bool"},
            "zIndex": {"num"},
            "partitionId": {"none"},
            "netName": {"str"},
            "width": {"num"},
            "fillStyle": {"str"},
            "path": {"list"},
            "isBridgingCopper": {"bool"},
            "specialColor": {"none"},
        },
        {"refs", "networkList"},
    ),
    "STRING": (
        {
            "layerId": {"num"},
            "groupId": {"str"},
            "locked": {"bool"},
            "zIndex": {"num"},
            "partitionId": {"none"},
            "x": {"num"},
            "y": {"num"},
            "text": {"str"},
            "origin": {"str"},
            "fontFamily": {"str"},
            "fontSize": {"num"},
            "strokeWidth": {"num"},
            "expansion": {"num"},
            "angle": {"num"},
            "reverse": {"bool"},
            "mirror": {"bool"},
            "specialColor": {"none"},
        },
        set(),
    ),
}

#: Records a document MUST carry (the LAYER palette, the board-state
#: block) — Pro writes them on every document of a real export.
_DOC_REQUIRED: dict[str, set[str]] = {
    "PCB": {
        "LAYER",
        "LAYER_PHYS",
        "CANVAS",
        "META",
        "PREFERENCE",
        "ACTIVE_LAYER",
        "SILK_OPTS",
        "PRIMITIVE",
    },
    "FOOTPRINT": {
        "LAYER",
        "LAYER_PHYS",
        "CANVAS",
        "META",
        "PREFERENCE",
        "ACTIVE_LAYER",
    },
}

#: Numeric slots on text-family records that must never be null/NaN —
#: Pro's addText reads them even on invisible attrs.
_TEXT_NUM_SLOTS = ("x", "y", "angle", "fontSize", "strokeWidth")

#: String slots on the layer records parseLayer reads with startsWith.
_LAYER_STR_SLOTS = ("layerType", "layerName", "activeColor", "inactiveColor")


def _kind_of(v: Any) -> str:
    if v is None:
        return "none"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "num"
    if isinstance(v, str):
        return "str"
    if isinstance(v, list):
        return "list"
    return "dict"


def _epro_docs(raw: str) -> list[tuple[str, list[tuple[dict, dict]]]]:
    """``[(docType, [(meta, body), ...])]`` for an ``.epru`` stream."""
    docs: list[tuple[str, list[tuple[dict, dict]]]] = []
    cur: list[tuple[dict, dict]] | None = None
    cur_type = ""
    for line in raw.splitlines():
        if "||" not in line:
            continue
        head, rest = line.split("||", 1)
        meta = json.loads(head)
        body = json.loads(rest.rstrip("|"))
        if meta.get("type") == "DOCHEAD":
            cur_type = str(body.get("docType"))
            cur = []
            docs.append((cur_type, cur))
        assert cur is not None
        cur.append((meta, body))
    return docs


def _shape_failures(raw: str) -> list[str]:
    """Every record-shape difference between ``raw`` and :data:`_PRO_SHAPE`."""
    fails: list[str] = []
    for doc_type, records in _epro_docs(raw):
        seen = {str(m.get("type")) for m, _b in records}
        for needed in sorted(_DOC_REQUIRED.get(doc_type, set()) - seen):
            fails.append(f"{doc_type}: required record type {needed} absent")
        for meta, body in records:
            t = str(meta.get("type"))
            if t not in _PRO_SHAPE:
                fails.append(f"{doc_type}/{t}: Pro never writes this record type")
                continue
            req, opt = _PRO_SHAPE[t]
            for k in sorted(set(body) - set(req) - opt):
                fails.append(f"{doc_type}/{t}: field {k!r} is not a Pro field")
            for k, kinds in sorted(req.items()):
                if k not in body:
                    fails.append(f"{doc_type}/{t}: required field {k!r} absent")
                    continue
                got = _kind_of(body[k])
                if got not in kinds:
                    fails.append(
                        f"{doc_type}/{t}: field {k!r} is {got}, "
                        f"Pro writes {'/'.join(sorted(kinds))}"
                    )
                if isinstance(body[k], float) and not math.isfinite(body[k]):
                    fails.append(f"{doc_type}/{t}: field {k!r} is not finite")
            if t in ("ATTR", "STRING"):
                for k in _TEXT_NUM_SLOTS:
                    v = body.get(k)
                    if not isinstance(v, (int, float)) or not math.isfinite(v):
                        fails.append(
                            f"{doc_type}/{t}: {k} is {v!r} — addText() reads "
                            "every attr, hidden or not (NaN is not permitted)"
                        )
            if t == "LAYER":
                for k in _LAYER_STR_SLOTS:
                    if not isinstance(body.get(k), str) or not body[k]:
                        fails.append(
                            f"{doc_type}/LAYER: string field {k!r} missing — "
                            "parseLayer reads it with startsWith"
                        )
    return fails


def test_emitted_records_match_pro_field_shapes() -> None:
    """The writer's own model export: every record carries exactly the
    field set Pro writes for its type, every required record type is on
    every doc, and no text/layer record has a hole in a slot Pro reads
    unconditionally (the 2026-10-14 parse crash class)."""
    raw = _export().files["epro-test.epru"]
    fails = _shape_failures(raw)
    assert not fails, f"{len(fails)} shape mismatches:\n" + "\n".join(fails[:40])


def _epru_texts(blob: bytes) -> list[str]:
    zf = zipfile.ZipFile(io.BytesIO(blob))
    return [zf.read(n).decode("utf-8") for n in zf.namelist() if n.endswith(".epru")]


def _shape_failures_vs_source(src_blob: bytes, out_blob: bytes) -> list[str]:
    """Field-shape parity of an emitted file against a real Pro export,
    per ``(docType, recordType)``: every field the source always writes
    for that pair must be on our record with a compatible kind (a number
    where the source left null is fine — strictly more data); our extra
    fields and our ``(docType, recordType)`` pairs the source never uses
    are reported too."""
    src_shapes: dict[tuple[str, str], tuple[set[str], dict[str, set[str]]]] = {}
    src_counts: dict[tuple[str, str], int] = {}
    seen: dict[tuple[str, str], dict[str, int]] = {}
    for raw in _epru_texts(src_blob):
        for doc_type, records in _epro_docs(raw):
            for meta, body in records:
                key = (doc_type, str(meta.get("type")))
                src_counts[key] = src_counts.get(key, 0) + 1
                fmap = src_shapes.setdefault(key, (set(), {}))
                pres = seen.setdefault(key, {})
                for k, v in body.items():
                    fmap[1].setdefault(k, set()).add(_kind_of(v))
                    pres[k] = pres.get(k, 0) + 1
    for key, (always, _t) in src_shapes.items():
        for k, n in seen[key].items():
            if n == src_counts[key]:
                always.add(k)

    fails: list[str] = []
    for raw in _epru_texts(out_blob):
        for doc_type, records in _epro_docs(raw):
            for meta, body in records:
                t = str(meta.get("type"))
                key = (doc_type, t)
                if key not in src_shapes:
                    # fall back to the type's global shape across the
                    # source's other docs (e.g. STRING on the PCB doc):
                    # a field "always present" globally is one present on
                    # EVERY variant's own always-set.
                    variants = [
                        (alw, tmap)
                        for k2, (alw, tmap) in src_shapes.items()
                        if k2[1] == t
                    ]
                    if not variants:
                        fails.append(
                            f"{doc_type}/{t}: Pro never writes this record type"
                        )
                        continue
                    always = set.intersection(*(alw for alw, _ in variants))
                    tmap_all = {
                        f: set().union(*(tmap.get(f, set()) for _, tmap in variants))
                        for f in set().union(*(set(tmap) for _, tmap in variants))
                    }
                else:
                    always, tmap_all = src_shapes[key]
                missing = sorted(always - set(body))
                if missing:
                    fails.append(
                        f"{doc_type}/{t}: field(s) Pro always writes absent: "
                        f"{', '.join(missing[:6])}"
                    )
                for k, v in body.items():
                    kinds = tmap_all.get(k)
                    if kinds is None:
                        fails.append(f"{doc_type}/{t}: field {k!r} is not a Pro field")
                        continue
                    got = _kind_of(v)
                    ok = got in kinds or (
                        got == "num" and "none" in kinds
                    )  # a value where Pro left null is strictly more data
                    if not ok:
                        fails.append(
                            f"{doc_type}/{t}: field {k!r} is {got}, "
                            f"Pro writes {'/'.join(sorted(kinds))}"
                        )
                if t in ("ATTR", "STRING"):
                    for k in _TEXT_NUM_SLOTS:
                        v = body.get(k)
                        if not isinstance(v, (int, float)) or not math.isfinite(v):
                            fails.append(
                                f"{doc_type}/{t}: {k} is {v!r} — Pro's "
                                "addText reads every attr (NaN crash)"
                            )
                if t == "LAYER":
                    for k in _LAYER_STR_SLOTS:
                        if not isinstance(body.get(k), str) or not body[k]:
                            fails.append(
                                f"{doc_type}/LAYER: string field {k!r} "
                                "missing — parseLayer's startsWith crash"
                            )
    # de-duplicate the per-record noise: same failure line may repeat
    # across records; collapse to unique messages with counts
    uniq: dict[str, int] = {}
    for f in fails:
        uniq[f] = uniq.get(f, 0) + 1
    return sorted(f if n == 1 else f"{f}  (x{n})" for f, n in uniq.items())
