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
        copper=[{"ctype": "track", "layer": "F.Cu", "net": "GND"}],
        mask_open_regions=[{"side": "top", "polygon": [[0, 0], [1, 0], [1, 1]]}],
        silkscreen={"top": [{"width_mm": 0.15, "segments": []}], "bottom": []},
    )
    model["drills"].append({"x": 2.0, "y": 2.0, "dia_mm": 3.0, "plated": False})
    model["instances"].append({"refdes": "X9", "x": None, "y": None})
    text = " | ".join(_export(model).warnings)
    for needle in (
        "2 pad(s) belong to no placed part",
        "1 net(s) have no exported pad and are not in the file: LONELY",
        # R7 sits at 37 degrees with rect pads (review E1)
        "1 part(s) sit at a non-right-angle rotation with rect/obround pads (R7)",
        "1 copper item(s)",
        "1 soldermask-opening",
        "silkscreen is NOT exported",
        "1 drill(s) belong to no exported pad",
        "unplaced part(s) omitted: X9",
    ):
        assert needle in text, needle


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
    assert "Copper (tracks, vias, pours) is NOT exported yet" in resp.body
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
