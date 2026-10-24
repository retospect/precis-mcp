"""pcb-workspace-code slice 0b acceptance (docs/backlog/pcb-workspace-code.md,
Slices row 0b): a workspace script on a fixture board reads the board's
3-D shape — outline polygon, one prism per placed part with height +
``origin`` honesty, hole centres/diameters, keepouts — **matching
``precis.pcb.export.mechanical_profile``** (one derivation, in
``precis_geom.pcb_shape``), and ``shape.fits(...)`` answers a fit check
against an se part or an enclosure.

The trusted-side parity pin: ``precis_se.board.derive_board`` is the same
derivation plus the mm→m seam and the block-frame anchor — if the geom
shape moves, the bound-board envelope/ports move identically.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import precis_geom.pcb as pcb
import precis_se.board as se_board
from precis.cad import dsl as cad_dsl
from precis.pcb import export as pcb_export
from precis.pcb import package_heights
from precis_geom.pcb import Board, Footprint, pad, rect
from precis_geom.pcb_shape import derive_shape

ROOT = Path(__file__).resolve().parents[2]
BOARDS = ROOT / "examples" / "boards"


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sanity() -> Board:
    return _load(BOARDS / "sanity" / "main.py", "example_sanity").build()


def _nano() -> Board:
    return _load(BOARDS / "nano_usb_pd" / "main.py", "example_nano_usb_pd").build()


def _profile_of(b: Board) -> dict[str, Any]:
    """What the trusted side derives for the same board: the batch's
    components as ``pcb_load`` rows + the batch's features through
    ``export.mechanical_profile``."""
    batch = b.to_batch()
    return pcb_export.mechanical_profile(
        {"instances": [dict(c) for c in batch["components"]]},
        batch.get("features") or [],
    )


# --- data: the shape matches mechanical_profile ----------------------------


def test_shape_profile_matches_mechanical_profile_on_fixture_board() -> None:
    """The acceptance line: a workspace script's ``b.shape()`` on the
    sanity fixture reads the same profile the trusted side emits."""
    b = _sanity()
    shape = b.shape()
    want = _profile_of(b)
    assert shape.profile == want
    assert shape.ok and shape.why_not is None
    assert (
        shape.outline
        == want["outline"]
        == [
            [0.0, 0.0],
            [100.0, 0.0],
            [100.0, 100.0],
            [0.0, 100.0],
        ]
    )
    assert shape.thickness_mm == want["thickness_mm"] == 1.6
    assert (
        {p.refdes for p in shape.prisms}
        == {blk["refdes"] for blk in want["blocks"]}
        == {"J_T", "J_B", "J_L", "J_R"}
    )
    for p in shape.prisms:
        blk = next(b for b in want["blocks"] if b["refdes"] == p.refdes)
        assert (p.x_mm, p.y_mm, p.side) == (blk["x"], blk["y"], blk["layer"])
    # HDR-SMT-1x10 matches no package row → all four proposed at the
    # table fallback, honestly flagged.
    assert all(p.origin == "proposed" for p in shape.prisms)
    assert all("fallback" in (p.source or "") for p in shape.prisms)
    # the authored footprint gives every prism a real extent (pad bbox):
    # 10 pads at 2.54 mm pitch ±0.75 mm, ±1.5 mm in y, centred on the pose.
    jt = next(p for p in shape.prisms if p.refdes == "J_T")
    assert jt.extent_mm == pytest.approx((50 - 12.18, 75 - 1.5, 50 + 12.18, 75 + 1.5))
    # rot=90 headers get the rotated AABB (long axis along x→y).
    jl = next(p for p in shape.prisms if p.refdes == "J_L")
    assert jl.extent_mm == pytest.approx((25 - 1.5, 50 - 12.18, 25 + 1.5, 50 + 12.18))
    assert shape.to_dict() and json.dumps(shape.to_dict())  # it is data


def test_shape_height_origins_and_holes_and_keepouts() -> None:
    b = Board("mech", outline=rect(60, 40))
    b.hole(4.0, 4.0, drill=3.2, ring=6.5)
    b.hole(56.0, 36.0, drill=3.2, ring=6.5)
    b.keepout([[45, 30], [58, 30], [58, 38], [45, 38]], layers=["F.Cu"], reason="RF")
    fp = Footprint(
        "MNT-2",
        [
            pad("1", "rect", -2.0, 0, w=2.0, h=3.0),
            pad("2", "rect", 2.0, 0, w=2.0, h=3.0),
        ],
    )
    b.part("M1", footprint=fp, height_mm=4.0).at(10, 10)
    b.part("C1", footprint="C0603").at(30, 20)
    b.part("X1", footprint="TOTALLY-UNKNOWN-PKG").at(50, 10)
    shape = b.shape()

    prisms = {p.refdes: p for p in shape.prisms}
    assert prisms["M1"].origin == "user" and prisms["M1"].height_mm == 4.0
    assert prisms["M1"].z_mm == pytest.approx((1.6, 5.6))
    # pads bbox ±3 x, ±1.5 y around the part origin → translated.
    assert prisms["M1"].extent_mm == pytest.approx((7.0, 8.5, 13.0, 11.5))
    assert prisms["C1"].origin == "proposed"
    assert prisms["C1"].height_mm == pytest.approx(
        package_heights.height_mm("C0603")[0]
    )
    assert "0603" in (prisms["C1"].source or "")
    assert prisms["X1"].origin == "proposed"
    assert "fallback" in (prisms["X1"].source or "")
    assert prisms["X1"].extent_mm is None  # unregistered name → point column

    assert [h.name for h in shape.holes] == ["H1", "H2"]
    assert (shape.holes[0].x_mm, shape.holes[0].y_mm) == (4.0, 4.0)
    assert shape.holes[0].diameter_mm == 3.2
    assert shape.holes[0].through_mm == shape.thickness_mm

    assert len(shape.keepouts) == 1
    k = shape.keepouts[0]
    assert k.poly[0] == (45, 30) and k.layers == ("F.Cu",) and k.reason == "RF"

    assert shape.envelope_mm()["w"] == 60.0
    assert shape.envelope_mm()["h"] == pytest.approx(
        1.6 + package_heights.height_mm("TOTALLY-UNKNOWN-PKG")[0]
    )


def test_unplaced_parts_get_no_prism_and_are_named() -> None:
    b = Board("sparse", outline=rect(20, 10))
    b.part("U1", footprint="SOIC-8").at(10, 5)
    b.part("R1", footprint="R0402")
    shape = b.shape()
    assert [p.refdes for p in shape.prisms] == ["U1"]
    assert shape.unplaced == ["R1"]


def test_no_outline_reports_why_not() -> None:
    shape = derive_shape(
        {"units": "mm", "thickness_mm": 1.6, "outline": [], "holes": [], "blocks": []}
    )
    assert not shape.ok and shape.why_not
    res = shape.fits(pcb.enclosure(100, 100, 20))
    assert not res.ok and res.findings[0].code == "fit.no_shape"


# --- one derivation: parity with the se binding ----------------------------


def test_se_binding_is_the_same_derivation() -> None:
    """derive_board's metres must equal this shape's mm — centred x/y,
    z re-anchored to the lowest extent."""
    b = Board("parity", outline=rect(100, 80))
    b.hole(3.0, 3.0, drill=3.2, ring=6.0)
    b.hole(97.0, 77.0, drill=3.2, ring=6.0)
    b.part("U1", footprint="CUSTOM-IC", height_mm=3.0).at(10, 20)
    b.part("C1", footprint="C0603").at(30, 40, side="bottom")
    shape = b.shape()
    derived = se_board.derive_board(
        shape.profile,
        [dict(c) for c in b.to_batch()["components"]],
    )
    assert derived.ok
    cx, cy = shape.centre_mm
    shift = shape.bottom_extent_mm
    se_prisms = {p["refdes"]: p for p in derived.segments[0]["prisms"]}
    for p in shape.prisms:
        sp = se_prisms[p.refdes]
        assert sp["x_m"] == pytest.approx((p.x_mm - cx) / 1000.0)
        assert sp["y_m"] == pytest.approx((p.y_mm - cy) / 1000.0)
        assert sp["height_m"] == pytest.approx(p.height_mm / 1000.0)
        assert sp["origin"] == p.origin and sp["layer"] == p.side
        for got, want_mm in zip(sp["z_m"], p.z_mm):
            assert got == pytest.approx((want_mm + shift) / 1000.0)
    assert set(derived.ports or {}) == {h.name for h in shape.holes}
    env = shape.envelope_mm()
    assert derived.envelope is not None
    spec = cad_dsl.parse(derived.envelope)
    assert spec.params["w"] == pytest.approx(env["w"] / 1000.0)
    assert spec.params["h"] == pytest.approx(env["h"] / 1000.0)
    assert derived.segments[0]["keepouts"] == []  # additive key exists


# --- the callable: fit checks ----------------------------------------------


def test_fit_enclosure_containment() -> None:
    b = Board("fit", outline=rect(50, 40))
    b.part("U1", footprint="SOIC-8", height_mm=1.75).at(25, 20)
    shape = b.shape()  # only solid above the slab is U1: total 3.35 mm
    assert shape.fits(pcb.enclosure(50, 40, 4.0)).ok
    res = shape.fits(pcb.enclosure(50, 40, 3.0))
    assert not res.ok
    assert res.findings[0].code == "fit.outside_enclosure"
    assert res.findings[0].subject == "U1"
    # the declared units seam: the same cavity in metres.
    assert shape.fits(pcb.enclosure(0.05, 0.04, 0.004, units="m")).ok


def test_fit_unplaced_parts_fail_the_check() -> None:
    """gr513665: nano_usb_pd authors 86 parts and places none — the old
    code returned ``ok=True`` with zero findings off the bare slab. The
    verdict must say the parts were never checked."""
    shape = _nano().shape()
    assert len(shape.unplaced) == 86 and shape.prisms == []
    res = shape.fits(pcb.enclosure(110, 90, 20))
    assert res.ok is False
    assert res.checked_parts == 0 and res.unchecked_parts == 86
    finding = next(f for f in res.findings if f.code == "fit.parts_unplaced")
    assert finding.severity == "error" and finding.subject == "board"
    assert "86 of 86 parts have no pose and were NOT checked" in finding.detail
    # first 8 refdes named, the rest counted
    assert "+78 more" in finding.detail
    assert "part.at" in finding.detail and "re-run fits()" in finding.detail
    assert "0 of 86 parts checked" in res.summary()


def test_fit_fully_placed_board_gets_no_unplaced_finding() -> None:
    b = _sanity()
    shape = b.shape()
    res = shape.fits(pcb.enclosure(100, 100, 20))
    assert res.ok
    assert not any(f.code == "fit.parts_unplaced" for f in res.findings)
    assert res.checked_parts == len(shape.prisms) == 4
    assert res.unchecked_parts == 0
    assert res.summary().startswith("fit OK")


def test_fit_partly_placed_board_splits_the_counts() -> None:
    b = Board("sparse", outline=rect(20, 10))
    b.part("U1", footprint="SOIC-8").at(10, 5)
    b.part("R1", footprint="R0402")
    res = b.shape().fits(pcb.enclosure(20, 10, 10))
    assert res.ok is False
    assert res.checked_parts == 1 and res.unchecked_parts == 1
    finding = next(f for f in res.findings if f.code == "fit.parts_unplaced")
    assert "1 of 2 parts have no pose and were NOT checked: R1" in finding.detail
    assert "+0 more" not in finding.detail
    assert "1 of 2 parts checked" in res.summary()


def test_fit_enclosure_overflow_names_the_axis() -> None:
    """gr513753: an overflow finding must name the axis (and amount on
    each) — 'exceeds enclosure by 3.1 mm' made the builder guess."""
    b = Board("fit-axis", outline=rect(50, 40))
    b.part("U1", footprint="SOIC-8", height_mm=1.75).at(25, 20)
    shape = b.shape()  # top at 3.35 mm; only z overflows a 3.0 mm cavity
    res = shape.fits(pcb.enclosure(50, 40, 3.0))
    assert not res.ok
    f = res.findings[0]
    assert f.code == "fit.outside_enclosure" and f.subject == "U1"
    assert "z by 0.35 mm" in f.detail
    assert "top" in f.detail
    assert "x by" not in f.detail and "y by" not in f.detail
    # one solid overflowing two axes: each is named with its own amount.
    b2 = Board("fit-axes", outline=rect(50, 40))
    b2.part("J1", footprint="HDR-TH", height_mm=5.0).at(0, 20)
    res = b2.shape().fits(pcb.enclosure(40, 40, 3.0))
    detail = next(f.detail for f in res.findings if f.subject == "J1")
    assert "x by 5 mm" in detail and "z by 3.6 mm" in detail


def test_chip_passive_spellings_map_to_chip_heights() -> None:
    """gr513752(b): ``CHIP-`` and underscored ``R_``/``C_`` spellings
    join the ``[RCL]`` chip rows (the dogfood board's ``CHIP-0603`` used
    to miss every row and take the 11.5 mm fallback)."""
    assert package_heights.height_mm("CHIP-0402")[0] == pytest.approx(0.55)
    assert package_heights.height_mm("CHIP-0603")[0] == pytest.approx(0.9)
    assert package_heights.height_mm("CHIP-0805")[0] == pytest.approx(1.35)
    assert package_heights.height_mm("CHIP-1206")[0] == pytest.approx(1.8)
    assert package_heights.height_mm("R_0402")[0] == pytest.approx(0.55)
    assert package_heights.height_mm("C_0603")[0] == pytest.approx(0.9)
    assert package_heights.height_mm("R_0805")[0] == pytest.approx(1.35)
    # the KiCad-style suffixed spelling
    assert package_heights.height_mm("R_0603_1608Metric")[0] == pytest.approx(0.9)
    # gr513775: the CAP-/RES-/IND-<eia> spellings examples/boards use
    assert package_heights.height_mm("CAP-1206")[0] == pytest.approx(1.8)
    assert package_heights.height_mm("CAP-0402-100nF")[0] == pytest.approx(0.55)
    assert package_heights.height_mm("RES-0402")[0] == pytest.approx(0.55)
    assert package_heights.height_mm("IND-0603")[0] == pytest.approx(0.9)


@pytest.mark.parametrize(
    ("spelling", "expected_mm"),
    [
        # chip rows: [RCL]/CHIP-/CAP-/RES-/IND- prefixes share one EIA code
        ("R0402", 0.55),
        ("R402", 0.55),
        ("C_0402", 0.55),
        ("CHIP-0402", 0.55),
        ("CAP-0402-100nF", 0.55),
        ("CHIP-0603", 0.9),
        ("R_0603_1608Metric", 0.9),  # rides the rows' `_` tail
        ("IND-0603", 0.9),
        ("RES-0603", 0.9),
        ("CHIP_0805", 1.35),
        ("CAP-0805", 1.35),
        ("CAP-1206", 1.8),
        ("R1206", 1.8),
        # the package-class rows
        ("C_1210", 2.8),
        ("CAP-SMD", 6.3),
        ("LED-SMD", 1.1),
        ("HDR-SMD", 8.5),
        ("HDR-TH", 11.5),
        ("SOT-223", 1.8),  # row order: before the `^SOT-` catch-all
        ("SOT-89", 1.6),
        ("SOT-23", 1.3),
        ("TSSOP-8", 1.2),
        ("SOIC-8", 1.75),
        ("QFN-32", 1.0),
        ("LQFP-48", 1.6),
        ("SW-SMD", 4.3),
        # near-misses land the table-maximum (HDR-TH 11.5 mm) fallback
        ("0402", 11.5),  # no class prefix
        ("R-0402", 11.5),  # dash after a lone letter
        ("R01206", 11.5),  # the 1206 row takes no `0?` lead-in
        ("CHIP-1210", 11.5),  # 1210 row has no CHIP-/CAP- alternative
        ("C_1210_3225Metric", 11.5),  # that row's `\b` rejects a `_` tail
        ("SO8", 11.5),  # SO- needs the dash
        ("SON-8", 11.5),  # bare SON is not a leadless alternative
        ("IND-7x7", 11.5),  # nano_usb_pd's inductor — non-EIA spelling
        ("CAP-ELEC-5P", 11.5),  # its THT electrolytic likewise
        ("WIDGET-9", 11.5),  # the unknown
    ],
)
def test_height_table_spellings(spelling: str, expected_mm: float) -> None:
    """gr513774: the table contract as data — each spelling a
    HEIGHT_TABLE row's comment documents (matched or a near-miss) lands
    that row's height or the table-maximum fallback."""
    h, _source = package_heights.height_mm(spelling)
    assert h == pytest.approx(expected_mm)


def test_height_table_fallback_names_itself() -> None:
    """An unmapped footprint's source note says it is the fallback —
    the fit finding quotes it."""
    h, source = package_heights.height_mm("WIDGET-9")
    assert h == pytest.approx(11.5)
    assert "fallback" in source


def test_chip_passive_footprint_fits_5mm_cavity() -> None:
    """gr513752: a CHIP-0603 part is proposed at 0.9 mm, not the 11.5 mm
    fallback — it fits the 5 mm cavity the dogfood build used."""
    b = Board("chip", outline=rect(30, 25))
    b.part("R1", footprint="CHIP-0603").at(15, 12)
    shape = b.shape()
    p = shape.prisms[0]
    assert p.origin == "proposed" and p.height_mm == pytest.approx(0.9)
    assert shape.fits(pcb.enclosure(30, 25, 5)).ok


def test_fallback_height_finding_names_the_assumption() -> None:
    """gr513752(a): an unmapped footprint still takes the table
    fallback, but a finding naming the part now says the height was
    assumed and how to set a real one."""
    b = Board("unk", outline=rect(30, 25))
    b.part("X1", footprint="WIDGET-9").at(15, 12)
    shape = b.shape()
    assert shape.prisms[0].height_mm == pytest.approx(11.5)
    res = shape.fits(pcb.enclosure(30, 25, 5))
    assert not res.ok
    f = next(f for f in res.findings if f.subject == "X1")
    assert "height assumed 11.5 mm" in f.detail
    assert "'WIDGET-9'" in f.detail
    assert "height_mm=" in f.detail
    # the same honesty tail on the interference path
    res = shape.fits(pcb.box(5, 5, 3, at=(15, 12, 1.6), name="lid"))
    f = next(
        f for f in res.findings if f.code == "fit.interference" and f.subject == "X1"
    )
    assert "height assumed 11.5 mm" in f.detail


def test_fit_part_interference_and_clearance() -> None:
    b = Board("fit2", outline=rect(50, 40))
    b.part("U1", footprint="SOIC-8", height_mm=1.75).at(25, 20)
    shape = b.shape()
    # a 5 mm block dropped on U1's position, base on the top copper face
    res = shape.fits(pcb.box(5, 5, 3, at=(25, 20, 1.6), name="motor"))
    assert not res.ok
    assert any(f.code == "fit.interference" and f.subject == "U1" for f in res.findings)
    # parked in a clear corner: no interference, slab contact is allowed
    res = shape.fits(pcb.box(5, 5, 3, at=(46, 36, 1.6), name="motor"))
    assert res.ok
    # the same block parked 0.1 mm beside U1's column clears at 0 but
    # fails a 1 mm ask (U1's extent is unknown — unregistered footprint —
    # so only its column and z are read).
    near = pcb.box(5, 5, 3, at=(27.6, 20, 1.6), name="motor")
    assert shape.fits(near).ok
    res = shape.fits(near, clearance_mm=1.0)
    assert not res.ok
    assert res.findings[0].code == "fit.interference"


def test_fit_hole_and_keepout_are_warns_not_errors() -> None:
    b = Board("fit3", outline=rect(50, 40))
    b.hole(5.0, 5.0, drill=3.2, ring=6.5)
    b.keepout([[40, 30], [48, 30], [48, 38], [40, 38]], reason="RF")
    shape = b.shape()
    res = shape.fits(pcb.box(4, 4, 4, at=(5, 5, 1.6), name="lid-boss"))
    assert res.ok  # warn-tier only
    assert any(f.code == "fit.hole_covered" and f.subject == "H1" for f in res.findings)
    res = shape.fits(pcb.box(4, 4, 2, at=(44, 34, 1.6), name="antenna"))
    assert res.ok
    assert any(f.code == "fit.keepout" for f in res.findings)


def test_fit_multi_solid_call_and_summary() -> None:
    b = _sanity()
    shape = b.shape()
    # the multi-solid call: one enclosure + one part in a single check
    res = pcb.fit(
        shape,
        [
            pcb.enclosure(100, 100, 20, name="case"),
            pcb.box(4, 4, 2, at=(50, 75, 1.6), name="boss"),
        ],
    )
    # J_T's header prism (fallback 11.5 mm tall at (50, 75)) is hit by the boss
    assert not res.ok
    assert any(
        f.subject == "J_T" and f.code == "fit.interference" for f in res.findings
    )
    assert "fit FAIL" in res.summary()
    res = shape.fits([pcb.enclosure(100, 100, 20, name="case")])
    assert res.ok and res.summary().startswith("fit OK")


def test_bottom_side_extent_is_not_mirrored() -> None:
    """The repo convention (pinned): every courtyard placer keeps
    ``mirrored=False`` for bottom-side parts
    (``ir.world_courtyard_polygons``, ``silk.world_courtyard_rings``) —
    the shape follows it: an off-centre local extent is rotated and
    translated, never x-mirrored."""
    b = Board("bot", outline=rect(60, 40))
    fp = Footprint("ASY", [], courtyard={"bbox": [1.0, -1.0, 4.0, 1.0]})
    b.part("B1", footprint=fp, height_mm=2.0).at(30, 20, side="bottom")
    shape = b.shape()
    p = shape.prisms[0]
    assert p.side == "bottom" and p.z_mm == pytest.approx((-2.0, 0.0))
    # un-mirrored: local (1..4, -1..1) sits at (31..34, 19..21), not (26..29)
    assert p.extent_mm == pytest.approx((31.0, 19.0, 34.0, 21.0))


def test_fit_at_none_defaults_and_bottom_prism() -> None:
    b = Board("fit4", outline=rect(50, 40))
    b.part("B1", footprint="SOIC-8", height_mm=1.75).at(25, 20, side="bottom")
    shape = b.shape()
    # at=None defaults to outline-centre x/y, slab-top z: a solid sitting
    # on the top face clears the prism hanging below.
    res = shape.fits(pcb.box(5, 5, 3, name="top-block"))
    assert res.ok
    # the same solid under the bottom face hits it.
    res = shape.fits(pcb.box(5, 5, 3, at=(25, 20, -3.0), name="under"))
    assert not res.ok
    assert res.findings[0].code == "fit.interference"
    assert res.findings[0].subject == "B1"


def test_fit_spec_validation_raises_builder_error() -> None:
    b = Board("v", outline=rect(50, 40))
    shape = b.shape()
    with pytest.raises(pcb.PcbBuilderError, match="role"):
        shape.fits({"w": 5, "d": 5, "h": 3, "role": "enclose"})
    with pytest.raises(pcb.PcbBuilderError, match="missing keys"):
        shape.fits({"w": 5, "d": 5})
    with pytest.raises(pcb.PcbBuilderError, match="units"):
        shape.fits(pcb.box(5, 5, 3, units="furlong"))
    with pytest.raises(pcb.PcbBuilderError, match="finite and positive"):
        shape.fits(pcb.box(0, 5, 3))
    with pytest.raises(pcb.PcbBuilderError, match="finite and positive"):
        shape.fits(pcb.box(-1.0, 5, 3))
    with pytest.raises(pcb.PcbBuilderError, match="finite and positive"):
        shape.fits(pcb.box(float("nan"), 5, 3))
    with pytest.raises(pcb.PcbBuilderError, match="at must be three"):
        shape.fits({"w": 5, "d": 5, "h": 3, "at": (1, 2)})
