"""Shared angle helpers (gr336352): TeX degree spelling, parse_angle_deg
policy, respond-in-kind / both-forms display."""

from __future__ import annotations

import math

import pytest

from precis.errors import BadInput
from precis.utils.units import (
    format_angle_both,
    format_angle_in_kind,
    parse_angle_deg,
    parse_quantity,
)


@pytest.mark.parametrize(
    "text", ["30\\circ", "30^\\circ", "30^{\\circ}", "30°", "30 deg"]
)
def test_degree_spellings_parse_alike(text: str) -> None:
    assert parse_quantity(text, "angle") == pytest.approx(math.radians(30))


def test_parse_angle_deg_units_and_zero() -> None:
    assert parse_angle_deg("90deg", arg_name="a") == pytest.approx(90)
    assert parse_angle_deg("1.5707963267948966 rad", arg_name="a") == pytest.approx(90)
    assert parse_angle_deg(0, arg_name="a") == 0.0
    assert parse_angle_deg(None, arg_name="a") == 0.0


def test_parse_angle_deg_rejects_bare_nonzero_with_two_readings() -> None:
    with pytest.raises(BadInput) as ei:
        parse_angle_deg(1.5, arg_name="rot")
    assert "1.5 deg" in str(ei.value.next) and "1.5 rad" in str(ei.value.next)


def test_parse_angle_deg_enclave_bare_is_degrees() -> None:
    assert parse_angle_deg(45, arg_name="rot", bare="degrees") == 45.0
    assert parse_angle_deg("45", arg_name="rot", bare="degrees") == 45.0


def test_parse_angle_deg_rejects_non_angle_unit() -> None:
    with pytest.raises(BadInput):
        parse_angle_deg("3 mm", arg_name="rot")


def test_respond_in_kind_and_both_forms() -> None:
    half_pi = math.pi / 2
    assert format_angle_both(half_pi) == "90° (1.5708 rad)"
    assert format_angle_in_kind(half_pi, "deg") == "90°"
    assert format_angle_in_kind(half_pi, "rad") == "1.5708 rad"
    assert format_angle_in_kind(half_pi, None) == "90° (1.5708 rad)"
