"""gr551980 — an SE validation/DRC finding must name the specific rule it
violated AND the measured value that tripped it, not just an error code:
``rule`` is the rule id, ``threshold_name`` the capability/rule field,
``measured``/``expected``(+``expected_hi``) the SI figures in ``unit``,
``requirement`` the constraint direction, and the derived ``comparison``
spells the verdict out in display units — ``measured 0.5 mm < 0.86 mm
(min_wall)``. The structured fields live on ``ValidationIssue`` (all
producers, not only Engine 3); the rendered columns land in
``view='validate'``, ``view='drc'`` and ``view='print'``.

Every design slug in this file is unique **per test** — the shared,
unisolated test DB / ``-n auto`` concurrency rule
(``[[test_db_shared_singleton]]``).
"""

from __future__ import annotations

import dataclasses
import json
import math
import re
from types import SimpleNamespace
from typing import Any, cast

import pytest

from precis.cad.scene import parse_source
from precis.dispatch import Hub
from precis.store import Store
from precis_se import catalog, fasten
from precis_se import drc as se_drc
from precis_se import printing as se_printing
from precis_se.flatpack import checks as flatpack_checks
from precis_se.handler import SeHandler
from precis_se.measures import MeasureSpec
from precis_se.ops import ConnectSpec, SeBlock, SeTree, apply_ops
from precis_se.properties import COMPUTERS
from precis_se.validate import ValidationIssue


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    return SeHandler(hub=hub)


def _printed(source: str) -> Any:
    return cast(Any, SimpleNamespace(spec=parse_source(source)))


def test_comparison_spells_out_the_verdict() -> None:
    """Structured fields → display units: lengths render mm, angles °,
    the requirement operator flips to the violated comparison, a band
    renders ``outside``, a string expectation renders ``vs``, no
    ``measured`` means nothing to compare."""
    f = ValidationIssue(rule="min_feature", subject="plate", detail="thin")
    assert f.comparison is None
    f = ValidationIssue(rule="r", subject="s", detail="d", measured=6.2e-4, unit="m")
    assert f.comparison == "measured 0.62 mm"
    f = ValidationIssue(
        rule="r",
        subject="s",
        detail="d",
        measured=6.2e-4,
        expected=8e-4,
        unit="m",
        requirement="min",
        threshold_name="min_wall",
    )
    assert f.comparison == "measured 0.62 mm < 0.8 mm (min_wall)"
    f = ValidationIssue(
        rule="r",
        subject="s",
        detail="d",
        measured=0.05,
        expected=0.0,
        unit="m",
        requirement="max",
        threshold_name="no interpenetration",
    )
    assert f.comparison == "measured 50 mm > 0 mm (no interpenetration)"
    f = ValidationIssue(
        rule="r",
        subject="s",
        detail="d",
        measured=6.2e-4,
        expected=0.004,
        expected_hi=0.008,
        unit="m",
        requirement="band",
        threshold_name="declared band",
    )
    assert f.comparison == "measured 0.62 mm outside [4 mm, 8 mm] (declared band)"
    f = ValidationIssue(
        rule="r",
        subject="s",
        detail="d",
        measured=math.radians(60),
        expected=math.radians(45),
        unit="rad",
        requirement="max",
    )
    assert f.comparison == "measured 60 ° > 45 °"


def test_asdict_carries_the_comparison() -> None:
    """``comparison`` is a real field set in ``__post_init__`` — so it
    survives ``dataclasses.asdict`` (the serialisation path a JSON row
    takes), not only attribute access."""
    f = ValidationIssue(
        rule="r",
        subject="s",
        detail="d",
        measured=6.2e-4,
        expected=8e-4,
        unit="m",
        requirement="min",
        threshold_name="min_wall",
    )
    row = dataclasses.asdict(f)
    assert row["comparison"] == "measured 0.62 mm < 0.8 mm (min_wall)"
    assert row["measured"] == pytest.approx(6.2e-4)
    assert row["unit"] == "m" and row["requirement"] == "min"


def test_min_feature_finding_carries_the_structured_fields() -> None:
    """Structured data: rule id (``min_feature``), SI measured figure in
    ``unit``, SI threshold + ``threshold_name``, direction in
    ``requirement``, rendered ``comparison`` in mm."""
    printed = _printed("component part\nwall add box:w0.5mmd10mmh10mm\n")
    f = se_printing._min_feature_finding("plate", printed, {"min_wall": 0.0008})
    assert f is not None
    assert f.rule == "min_feature"
    assert f.measured == pytest.approx(0.0005)
    assert f.expected == pytest.approx(0.0008)
    assert f.unit == "m" and f.requirement == "min"
    assert f.threshold_name == "min_wall"
    assert f.comparison == "measured 0.5 mm < 0.8 mm (min_wall)"


def _thin_plate(handler: SeHandler, slug: str) -> None:
    """A 10 mm × 10 mm × 0.5 mm fdm/pla plate — under fdm/pla's house
    ``min_wall`` of 0.86 mm (se_capabilities.json), so ``report_for``
    must flag ``min_feature``."""
    handler.put(
        id=slug,
        text=json.dumps(
            {
                "ops": [
                    {
                        "op": "add_block",
                        "name": "plate",
                        "envelope": "box:w0.01d0.01h0.0005",
                    }
                ]
            }
        ),
    )
    handler.edit(
        id=slug,
        ops=[{"op": "realize", "block": "plate", "mode": "fdm/pla"}],
    )


def test_validate_header_cites_rule_and_comparison(handler: SeHandler) -> None:
    _thin_plate(handler, "gr551980-thinv")
    body = handler.get(id="gr551980-thinv", view="validate").body
    assert "fdm findings:" in body
    assert "min_feature" in body
    assert "measured 0.5 mm < 0.86 mm (min_wall)" in body


def test_drc_pointer_cites_rule_and_comparison(handler: SeHandler) -> None:
    _thin_plate(handler, "gr551980-thind")
    body = handler.get(id="gr551980-thind", view="drc").body
    assert "fdm print-check" in body
    assert "min_feature: measured 0.5 mm < 0.86 mm (min_wall)" in body


def test_print_table_renders_the_comparison(handler: SeHandler) -> None:
    _thin_plate(handler, "gr551980-thinp")
    body = handler.get(id="gr551980-thinp", view="print", args={"block": "plate"}).body
    assert "measured 0.5 mm < 0.86 mm (min_wall)" in body


def test_validate_table_row_carries_measured_expected_comparison(
    handler: SeHandler,
) -> None:
    """A design-tier finding (undeclared interpenetration — two
    overlapping envelopes no connect sanctions) cites the same columns:
    the overlap depth vs the "no interpenetration" floor, rendered in
    mm though the structured figure is SI metres."""
    handler.put(
        id="gr551980-clash",
        text=json.dumps(
            {
                "ops": [
                    {"op": "add_block", "name": "a", "envelope": "cyl:r0.01h0.02"},
                    {"op": "add_block", "name": "b", "envelope": "cyl:r0.009h0.02"},
                ]
            }
        ),
    )
    body = handler.get(id="gr551980-clash", view="validate").body
    assert "undeclared_interpenetration" in body
    assert "no interpenetration" in body
    assert re.search(r"measured [\d.eE+-]+ mm > 0 mm \(no interpenetration\)", body)


# ── one pinned finding per producer family (review round 2) ────────────


def test_measure_unmet_carries_the_band(monkeypatch: pytest.MonkeyPatch) -> None:
    """``measure_unmet``: a computed measurand outside its declared band —
    measured + both band edges in the measure's unit."""
    tree = SeTree()
    apply_ops(
        tree, [{"op": "add_block", "name": "c", "envelope": "box:w0.01d0.01h0.01"}]
    )
    monkeypatch.setitem(COMPUTERS, "gr551980-pseudo", lambda _tree, _m: 0.005)
    tree.measures.append(
        MeasureSpec(
            block="c",
            name="gap",
            measurand="gr551980-pseudo",
            min_value=0.01,
            max_value=0.02,
            unit="m",
        )
    )
    (f,) = [f for f in se_drc.drc(tree).findings if f.rule == "measure_unmet"]
    assert f.measured == pytest.approx(0.005)
    assert (f.expected, f.expected_hi) == (0.01, 0.02)
    assert f.unit == "m" and f.requirement == "band"
    assert f.comparison == ("measured 5 mm outside [10 mm, 20 mm] (gr551980-pseudo)")


def test_mate_mismatch_carries_the_tolerance() -> None:
    """``mate_mismatch`` (gr334784): bore vs shaft on a ``bearing``
    mechanism — the 1 mm miss vs the relative nominal tolerance."""
    tree = SeTree()
    apply_ops(
        tree,
        [
            {"op": "add_block", "name": "hub", "envelope": "cyl:r0.008h0.03"},
            {
                "op": "add_block",
                "name": "wheel",
                "envelope": "cyl:r0.04h0.02",
                "pose": "[0.2 0 0] m",
            },
            {"op": "add_port", "block": "hub", "name": "shaft"},
            {"op": "add_port", "block": "wheel", "name": "bore"},
            {"op": "connect", "a": "wheel.bore", "b": "hub.shaft"},
            {
                "op": "set_joint",
                "a": "wheel.bore",
                "b": "hub.shaft",
                "joint": {"class": "rigid", "mechanism": "bearing"},
            },
            {"op": "add_measure", "block": "wheel", "name": "bore_d", "value": 0.017},
            {"op": "add_measure", "block": "hub", "name": "od_d", "value": 0.016},
        ],
    )
    (f,) = [f for f in se_drc.drc(tree).findings if f.rule == "mate_mismatch"]
    assert f.measured == pytest.approx(0.001)
    assert f.expected == pytest.approx(3e-3 * 0.017)
    assert f.unit == "m" and f.requirement == "max"
    assert f.comparison == "measured 1 mm > 0.051 mm (nominal mate tol 0.003×bore)"


def test_fasten_screw_too_short_carries_metres() -> None:
    """``screw_too_short``: the bolt's under-head length vs the length the
    stack-up needs — a 16 mm M6 against a 25.2 mm requirement."""
    specs = {
        "thread_size": "M6",
        "thread_pitch": 0.001,
        "outer_diameter": 0.006,
        "head_diameter": 0.010,
        "head_height": 0.006,
        "length": 0.016,
        "drive_size": 0.005,
    }
    nut_specs = {
        "thread_size": "M6",
        "thread_pitch": 0.001,
        "inner_diameter": 0.006,
        "across_flats": 0.010,
        "height": 0.0052,
    }
    tree = SeTree()
    bolt = SeBlock(name="bolt", pose=[0, 0, 0])
    bolt.bound_kind, bolt.bound = "component", "iso-4762-m6x16"
    bolt.derived = catalog.derive("fastener", specs)
    nut = SeBlock(name="nut", pose=[0, 0, 0.018])
    nut.bound_kind, nut.bound = "component", "iso-4032-m6"
    nut.derived = catalog.derive("fastener", nut_specs)
    tree.blocks["bolt"] = bolt
    tree.blocks["plate_a"] = SeBlock(
        name="plate_a", pose=[0, 0, 0.006], envelope="box:w0.05d0.05h0.006"
    )
    tree.blocks["plate_b"] = SeBlock(
        name="plate_b", pose=[0, 0, 0.012], envelope="box:w0.05d0.05h0.006"
    )
    tree.blocks["nut"] = nut
    tree.connects.append(
        ConnectSpec(
            a_block="bolt",
            a_port="thread",
            b_block="nut",
            b_port="hole",
            joint={"class": "rigid", "mechanism": "screw"},
        )
    )
    (f,) = [
        f
        for r in fasten.fasten(tree)
        for f in r.findings
        if f.rule == "screw_too_short"
    ]
    assert f.measured == pytest.approx(0.016)
    assert f.expected == pytest.approx(0.0252)
    assert f.unit == "m" and f.requirement == "min"
    assert f.comparison == "measured 16 mm < 25.2 mm (length under head)"


def test_flatpack_shelf_sag_carries_metres() -> None:
    """``flatpack_shelf_sag``: the computed sag vs the 3 mm limit — the
    flatpack layer works in mm, the finding fields are SI metres and
    render back to mm."""
    params = {
        "W": 300,
        "H": 60,
        "D": 80,
        "material": "plywood-6mm",
        "shelves": [30],
        "shelf_load_n": 200,
        "cutter_d": 3.175,
    }
    ref = SimpleNamespace(meta={"flatpack": params})
    stub = SimpleNamespace(get_ref=lambda *, kind, id, **kw: ref)
    (f,) = [
        f
        for f in flatpack_checks.findings(cast(Any, stub), SeTree(), 1)
        if f.rule == "flatpack_shelf_sag"
    ]
    assert f.measured == pytest.approx(5.189e-3, rel=1e-3)
    assert f.expected == pytest.approx(0.003)
    assert f.unit == "m" and f.requirement == "max"
    assert f.comparison == "measured 5.189 mm > 3 mm (sag limit)"


def test_layer_vs_load_carries_the_ratio() -> None:
    """``layer_vs_load``: the load fraction along the weak build-z vs the
    house ``strength_z_ratio`` — a receipt, so no ``requirement``."""
    f = se_printing._layer_vs_load_finding(
        "plate",
        cast(Any, SimpleNamespace(load_vs_layer=0.4)),
        {"strength_z_ratio": 0.5},
    )
    assert f is not None
    assert f.measured == pytest.approx(0.4)
    assert f.expected == pytest.approx(0.5)
    assert f.unit == "ratio" and f.requirement is None
    assert f.comparison == (
        "measured 0.4 ratio vs 0.5 ratio "
        "(strength_z_ratio, tensile-across-layers ÷ in-plane)"
    )


def test_axial_capacity_carries_newtons() -> None:
    """``axial_capacity``: a 150 N preload against a tie's 100 N
    tension_capacity — the figure lifts off ``se_stability``'s
    ``CheckFinding`` unchanged."""
    tree = SeTree()
    apply_ops(
        tree,
        [
            {"op": "add_block", "name": "a", "pose": [0.0, 0.0, 0.0], "pose_unit": "m"},
            {"op": "add_block", "name": "b", "pose": [1.0, 0.0, 0.0], "pose_unit": "m"},
            {"op": "add_port", "block": "a", "name": "pin"},
            {"op": "add_port", "block": "b", "name": "pin"},
            {
                "op": "connect",
                "a": "a.pin",
                "b": "b.pin",
                "joint": {
                    "class": "axial",
                    "params": {
                        "tension_capacity": 100.0,
                        "compression_capacity": 0.0,
                        "preload": 150.0,
                    },
                },
            },
        ],
    )
    f = next(
        f
        for f in se_drc.drc(tree).findings
        if f.rule == "axial_capacity" and f.measured is not None
    )
    assert f.measured == pytest.approx(150.0)
    assert f.expected == pytest.approx(100.0)
    assert f.unit == "N" and f.requirement == "max"
    assert f.comparison == "measured 150 N > 100 N (tension_capacity)"
