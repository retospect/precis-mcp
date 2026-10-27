"""Non-copper fab layers (gr520329 slice 1): the Board builder's typed
fab-layer methods and the ``features[]`` rows they store, the
``fab_layers_from_features`` extraction every model consumer reads,
the ``check_bend_zones`` DRC rules, and the gerber files
``export_gerbers`` emits per layer — one fixture board exercising every
method, no DB.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

import precis_geom.pcb as pcb
from precis.pcb import drc, gerber
from precis.pcb.session import fab_layers_from_features


def _board() -> pcb.Board:
    """One flex board touching every fab-layer method: a coverlay opening
    per side, a bottom stiffener, a top adhesive, a parylene tape mask,
    doc notes on two drawings layers, and one bend zone — a vertical
    fold line at x=10, bent 90° around a 3 mm radius."""
    b = pcb.Board("flex", stackup=2, outline=pcb.rect(20.0, 30.0))
    b.coverlay_opening([[2, 2], [8, 2], [8, 6], [2, 6]], side="top")
    b.coverlay_opening([[12, 2], [18, 2], [18, 6], [12, 6]], side="bottom")
    b.stiffener(
        [[1, 20], [9, 20], [9, 28], [1, 28]],
        material="fr4",
        thickness_mm=0.5,
        side="bottom",
    )
    b.adhesive([[11, 20], [19, 20], [19, 28], [11, 28]], side="top")
    b.tape_mask([[4, 8], [6, 8], [6, 10], [4, 10]])
    b.doc_note("KEEP CLEAR", at=[3, 26])
    b.doc_note("REV A", at=[3, 24], layer="Cmts.User")
    b.bend_zone([[10, 0], [10, 30]], radius_mm=3.0, angle_deg=90.0)
    return b


def test_builder_stores_fab_feature_rows() -> None:
    feats = _board().to_batch()["features"]
    by_type: dict[str, list[dict[str, Any]]] = {}
    for f_ in feats:
        by_type.setdefault(f_["ftype"], []).append(f_)

    cov = by_type["coverlay_opening"]
    assert [f_["layer"] for f_ in cov] == ["top", "bottom"]
    assert cov[0]["geom"]["polygon"] == [
        [2.0, 2.0],
        [8.0, 2.0],
        [8.0, 6.0],
        [2.0, 6.0],
    ]

    (st,) = by_type["stiffener"]
    assert st["layer"] == "bottom"
    assert st["geom"]["material"] == "fr4"
    assert st["geom"]["thickness_mm"] == 0.5
    assert len(st["geom"]["polygon"]) == 4

    (ad,) = by_type["adhesive"]
    assert ad["layer"] == "top"

    (tm,) = by_type["tape_mask"]
    assert tm["geom"]["kind"] == "parylene"

    notes = by_type["doc_note"]
    assert notes[0]["note"] == "KEEP CLEAR"
    assert notes[0]["layer"] == "Dwgs.User"
    assert notes[0]["x"] == 3.0 and notes[0]["y"] == 26.0
    assert notes[1]["note"] == "REV A"
    assert notes[1]["layer"] == "Cmts.User"

    (bz,) = by_type["bend_zone"]
    assert bz["geom"]["line"] == [[10.0, 0.0], [10.0, 30.0]]
    assert bz["geom"]["radius_mm"] == 3.0
    assert bz["geom"]["angle_deg"] == 90.0


def test_builder_validation() -> None:
    b = pcb.Board("v", stackup=2)
    with pytest.raises(pcb.PcbBuilderError, match="at least 3"):
        b.coverlay_opening([[0, 0], [1, 1]], side="top")
    with pytest.raises(pcb.PcbBuilderError, match="side"):
        b.coverlay_opening([[0, 0], [1, 0], [1, 1]], side="top-left")
    with pytest.raises(pcb.PcbBuilderError, match="not finite"):
        b.adhesive([[0, 0], [1, 0], [math.inf, 1]], side="top")
    with pytest.raises(pcb.PcbBuilderError, match="material"):
        b.stiffener(
            [[0, 0], [1, 0], [1, 1]], material="  ", thickness_mm=0.5, side="top"
        )
    with pytest.raises(pcb.PcbBuilderError, match="thickness_mm"):
        b.stiffener(
            [[0, 0], [1, 0], [1, 1]], material="fr4", thickness_mm=0, side="top"
        )
    with pytest.raises(pcb.PcbBuilderError, match="coat kind"):
        b.tape_mask([[0, 0], [1, 0], [1, 1]], kind="")
    with pytest.raises(pcb.PcbBuilderError, match="note text"):
        b.doc_note("", at=[0, 0])
    with pytest.raises(pcb.PcbBuilderError, match="\\[x, y\\] pair"):
        b.doc_note("hi", at=[0])
    with pytest.raises(pcb.PcbBuilderError, match="zero length"):
        b.bend_zone([[5, 5], [5, 5]], radius_mm=3, angle_deg=90)
    with pytest.raises(pcb.PcbBuilderError, match="radius_mm"):
        b.bend_zone([[0, 0], [0, 5]], radius_mm=0, angle_deg=90)
    with pytest.raises(pcb.PcbBuilderError, match="non-zero bend"):
        b.bend_zone([[0, 0], [0, 5]], radius_mm=3, angle_deg=0)
    with pytest.raises(pcb.PcbBuilderError, match="non-zero bend"):
        b.bend_zone([[0, 0], [0, 5]], radius_mm=3, angle_deg=400)


def test_fab_layers_from_features() -> None:
    model = fab_layers_from_features(_board().to_batch()["features"])
    assert {o["side"] for o in model["coverlay_openings"]} == {"top", "bottom"}
    (st,) = model["stiffeners"]
    assert st["side"] == "bottom"
    assert st["material"] == "fr4"
    assert st["thickness_mm"] == 0.5
    assert st["polygon"] == [[1.0, 20.0], [9.0, 20.0], [9.0, 28.0], [1.0, 28.0]]
    assert [a["side"] for a in model["adhesives"]] == ["top"]
    assert [m["kind"] for m in model["tape_masks"]] == ["parylene"]
    assert {n["layer"] for n in model["doc_notes"]} == {"Dwgs.User", "Cmts.User"}
    (zone,) = model["bend_zones"]
    assert zone == {
        "line": [[10.0, 0.0], [10.0, 30.0]],
        "radius_mm": 3.0,
        "angle_deg": 90.0,
    }


def test_fab_layers_from_features_skips_malformed() -> None:
    messy = fab_layers_from_features(
        [
            # stiffener with no thickness -> t=0 -> dropped
            {
                "ftype": "stiffener",
                "layer": "top",
                "geom": {"polygon": [[0, 0], [1, 0], [1, 1]]},
            },
            # bend zone whose fold line has one point -> dropped
            {
                "ftype": "bend_zone",
                "geom": {"line": [[0, 0]], "radius_mm": 1, "angle_deg": 90},
            },
            # doc note with no anchor -> dropped
            {"ftype": "doc_note", "note": "x"},
            # coverlay opening with no valid side -> dropped
            {
                "ftype": "coverlay_opening",
                "geom": {"polygon": [[0, 0], [1, 0], [1, 1]]},
            },
        ]
    )
    assert messy == {}


# ── bend-zone DRC ────────────────────────────────────────────────────────────
#
# The fixture's zone: fold line (10,0)-(10,30), radius 3 mm, 90° bend —
# the checked strip is the line buffered by half the bend allowance,
# 3·π/4 ≈ 2.356 mm either side: x ∈ [7.64, 12.36], flat-capped at the
# fold's ends.

_ZONE = {"line": [[10.0, 0.0], [10.0, 30.0]], "radius_mm": 3.0, "angle_deg": 90.0}


def _drc_model(
    copper: list[dict[str, Any]], instances: list[dict[str, Any]]
) -> dict[str, Any]:
    return {"bend_zones": [_ZONE], "copper": copper, "instances": instances}


def test_check_bend_zones_flags() -> None:
    model = _drc_model(
        copper=[
            # barrel at the fold's midpoint -> error
            {"ctype": "via", "net": "GND", "x": 10.0, "y": 15.0},
            # clear of the strip -> silent
            {"ctype": "via", "net": "VCC", "x": 15.0, "y": 15.0},
            # straight across at a right angle -> silent
            {
                "ctype": "track",
                "layer": "F.Cu",
                "net": "SIG1",
                "segments": [
                    {"shape": "line", "start": [5.0, 10.0], "end": [15.0, 10.0]}
                ],
            },
            # 45° across -> warn
            {
                "ctype": "track",
                "layer": "F.Cu",
                "net": "SIG2",
                "segments": [
                    {"shape": "line", "start": [5.0, 5.0], "end": [15.0, 15.0]}
                ],
            },
            # ~10° off perpendicular, inside the 15° tolerance -> silent
            {
                "ctype": "track",
                "layer": "F.Cu",
                "net": "SIG3",
                "segments": [
                    {
                        "shape": "line",
                        "start": [5.0, 11.0],
                        "end": [15.0, 11.0 + 10.0 * math.tan(math.radians(10))],
                    }
                ],
            },
        ],
        instances=[
            {"refdes": "U1", "x": 9.0, "y": 20.0},  # inside the strip -> error
            {"refdes": "U2", "x": 20.0, "y": 20.0},  # clear -> silent
        ],
    )
    findings = drc.check_bend_zones(model)
    by_rule: dict[str, list[drc.DrcFinding]] = {}
    for f_ in findings:
        by_rule.setdefault(f_.rule, []).append(f_)
    assert set(by_rule) == {"bend_zone_via", "bend_zone_part", "bend_zone_trace"}
    (via,) = by_rule["bend_zone_via"]
    assert via.severity == "error"
    assert "GND" in via.where
    (part,) = by_rule["bend_zone_part"]
    assert part.severity == "error"
    assert "U1" in part.where
    (trace,) = by_rule["bend_zone_trace"]
    assert trace.severity == "warn"
    assert "SIG2" in trace.where


def test_check_bend_zones_courtyard() -> None:
    # U3's origin sits clear of the strip (x=14 > 12.36), but its
    # courtyard reaches back to x=11.5 — the courtyard arm of the part
    # rule catches what the origin check misses.
    model = _drc_model(copper=[], instances=[{"refdes": "U3", "x": 14.0, "y": 25.0}])
    assert drc.check_bend_zones(model) == []
    findings = drc.check_bend_zones(
        model,
        courtyards=[("U3", [(11.5, 23.0), (16.5, 23.0), (16.5, 27.0), (11.5, 27.0)])],
    )
    assert [f_.rule for f_ in findings] == ["bend_zone_part"]
    assert findings[0].severity == "error"
    assert "U3" in findings[0].where


def test_check_bend_zones_empty_and_malformed() -> None:
    assert drc.check_bend_zones({"copper": [], "instances": []}) == []
    model = _drc_model(
        copper=[{"ctype": "via", "net": "GND", "x": 10.0, "y": 15.0}],
        instances=[],
    )
    model["bend_zones"] = [{"line": [[10, 0]], "radius_mm": 3, "angle_deg": 90}]
    assert drc.check_bend_zones(model) == []


# ── gerber export ────────────────────────────────────────────────────────────


def _gerber_model() -> dict[str, Any]:
    return {
        "layers": ["F.Cu", "B.Cu"],
        "outline": [[0.0, 0.0], [20.0, 0.0], [20.0, 30.0], [0.0, 30.0]],
        "copper": [],
        "pads": [],
        "silkscreen": {"top": [], "bottom": []},
        **fab_layers_from_features(_board().to_batch()["features"]),
    }


def test_export_gerbers_fab_files() -> None:
    files = gerber.export_gerbers(_gerber_model(), name="brd")
    for fname in (
        "brd-F_Coverlay.gbr",
        "brd-B_Coverlay.gbr",
        "brd-B_Stiffener.gbr",
        "brd-F_Adhesive.gbr",
        "brd-Dwgs_User.gbr",
        "brd-Cmts_User.gbr",
    ):
        assert fname in files, fname
    # only authored sides produce a file
    assert "brd-F_Stiffener.gbr" not in files
    assert "brd-B_Adhesive.gbr" not in files

    cov = files["brd-F_Coverlay.gbr"]
    assert "%TF.FileFunction,Other,Coverlay_Top*%" in cov
    assert "%TF.FileFunction,Other,Coverlay_Bot*%" in files["brd-B_Coverlay.gbr"]
    assert "G36*" in cov and "G37*" in cov
    assert "X2000000Y2000000D02*" in cov  # first polygon vertex, mm→units

    stiff = files["brd-B_Stiffener.gbr"]
    assert "%TF.FileFunction,Other,Stiffener_Bot*%" in stiff
    assert "G04 stiffener fr4 0.5mm*" in stiff
    assert "G36*" in stiff

    assert "%TF.FileFunction,Glue,Top*%" in files["brd-F_Adhesive.gbr"]

    dwgs = files["brd-Dwgs_User.gbr"]
    assert "%TF.FileFunction,Other,Dwgs.User*%" in dwgs
    assert "G04 tape mask (parylene)*" in dwgs
    # bend zone: the fold line stroked (10,0) → (10,30) in gerber units
    assert "X10000000Y0D02*" in dwgs
    assert "X10000000Y30000000D01*" in dwgs
    # plus its stroked BEND R=… note — more drawn segments than the line
    assert dwgs.count("D01*") > 1

    cmts = files["brd-Cmts_User.gbr"]
    assert "%TF.FileFunction,Other,Cmts.User*%" in cmts
    assert "D01*" in cmts  # the REV A note strokes


def test_export_gerbers_rigid_board_gains_no_fab_files() -> None:
    model = {
        "layers": ["F.Cu", "B.Cu"],
        "outline": [[0.0, 0.0], [5.0, 0.0], [5.0, 5.0], [0.0, 5.0]],
        "copper": [],
        "pads": [],
        "silkscreen": {"top": [], "bottom": []},
    }
    files = gerber.export_gerbers(model, name="plain")
    assert set(files) == {
        "plain-F_Cu.gbr",
        "plain-B_Cu.gbr",
        "plain-F_Mask.gbr",
        "plain-B_Mask.gbr",
        "plain-F_Paste.gbr",
        "plain-B_Paste.gbr",
        "plain-F_Silkscreen.gbr",
        "plain-B_Silkscreen.gbr",
        "plain-Edge_Cuts.gbr",
    }
    # The fab path is add-only: with no features a bundle's copper and
    # mask bytes are what the writers emit for the same model directly.
    assert files["plain-F_Cu.gbr"] == gerber.copper_gerber(model, "F.Cu")
    assert files["plain-B_Cu.gbr"] == gerber.copper_gerber(model, "B.Cu")
    assert files["plain-F_Mask.gbr"] == gerber.soldermask_gerber(model, "top")
    assert files["plain-B_Mask.gbr"] == gerber.soldermask_gerber(model, "bottom")
