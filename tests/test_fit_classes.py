"""`precis.fit_classes` — clearance-hole lookup as a unit.

Three modules split this subject deliberately, so each assertion has one
home: **this** one is the loader's own behaviour (both class shapes, the
default, what absence looks like); ``test_standards_table_agreement.py``
owns every claim that two transcribed tables agree; ``test_se_fasten.py``
owns what the se pass does with the answer.
"""

from __future__ import annotations

import pytest

from precis import fit_classes as core_fit


def test_the_house_rule_is_the_default_and_is_d_plus_02() -> None:
    fit = core_fit.clearance_hole("M6")
    assert fit is not None
    assert fit.fit_class == core_fit.default_class() == "house"
    assert fit.hole_mm == pytest.approx(6.2)
    assert fit.hole_m == pytest.approx(0.0062)


@pytest.mark.parametrize(
    ("fit_class", "expected"),
    [("fine", 6.4), ("medium", 6.6), ("coarse", 7.0)],
)
def test_the_iso_273_columns(fit_class: str, expected: float) -> None:
    fit = core_fit.clearance_hole("M6", fit_class)
    assert fit is not None and fit.hole_mm == pytest.approx(expected)


def test_the_house_rule_is_tighter_than_iso_fine() -> None:
    """The fact se's hole-pattern finding exists for: 0.1 mm of radial
    slack per hole against ISO fine's 0.2 mm."""
    house = core_fit.clearance_hole("M6", "house")
    fine = core_fit.clearance_hole("M6", "fine")
    assert house is not None and fine is not None
    assert house.hole_mm < fine.hole_mm
    assert house.radial_slack_mm == pytest.approx(0.1)
    assert fine.radial_slack_mm == pytest.approx(0.2)


def test_a_rule_class_and_a_tabulated_class_both_resolve() -> None:
    """The two shapes a class can take: `house` carries an `offset_mm`
    rule applied to the nominal diameter, the ISO classes carry a
    per-size column. A regression that handled only one would still look
    healthy at M6, where both exist."""
    assert core_fit.classes()["house"].get("offset_mm") == pytest.approx(0.2)
    assert "offset_mm" not in core_fit.classes()["medium"]
    for size in core_fit.sizes():
        rule = core_fit.clearance_hole(size, "house")
        tabulated = core_fit.clearance_hole(size, "medium")
        assert rule is not None and tabulated is not None
        assert rule.hole_mm == pytest.approx(rule.nominal_mm + 0.2)


def test_the_source_distinguishes_a_shop_rule_from_a_standard() -> None:
    """A number a shop made up and a number ISO published must not read
    the same downstream."""
    house = core_fit.clearance_hole("M6", "house")
    fine = core_fit.clearance_hole("M6", "fine")
    assert house is not None and fine is not None
    assert "NOT a standard" in house.source
    assert "ISO 273" in fine.source


def test_size_is_matched_case_insensitively() -> None:
    assert core_fit.clearance_hole("m6") == core_fit.clearance_hole(" M6 ")


def test_an_unknown_size_or_class_is_none_not_a_guess() -> None:
    # M14 is a real thread this table does not list; a *rule* class
    # cannot rescue it either, because the nominal diameter it would
    # apply to lives in the same size table.
    assert core_fit.clearance_hole("M14") is None
    assert core_fit.clearance_hole("M14", "house") is None
    assert core_fit.clearance_hole("M6", "snug") is None


def test_sizes_come_back_smallest_first() -> None:
    listed = core_fit.sizes()
    assert listed[0] == "M3" and listed[-1] == "M20"
    assert listed == sorted(listed, key=lambda s: float(s[1:]))
