"""gr336344: awkward-mantissa advisory on the strict units ingest path."""

from __future__ import annotations

import pytest

from precis.cad.dsl import parse
from precis.utils.units import parse_quantity


@pytest.mark.parametrize(
    "text, dim, expect",
    [
        ("0.000000003 m", "length", "parsed as 3 nm"),
        ("3e-9 m", "length", "parsed as 3 nm"),
        ("0.000003 mm", "length", "parsed as 3 nm"),
        ("5000000 mm", "length", "parsed as 5 km"),
        ("0.000002 kg", "mass", "parsed as 2 mg"),
    ],
)
def test_awkward_warns_but_still_parses(text, dim, expect):
    warns: list[str] = []
    val = parse_quantity(text, dim, warnings=warns, arg_name="x")
    assert len(warns) == 1 and expect in warns[0] and warns[0].startswith("x: ")
    assert val == parse_quantity(text, dim)  # same value as without the channel


@pytest.mark.parametrize(
    "text, dim",
    [
        ("3 nm", "length"),
        ("0.1 mm", "length"),
        ("0.05 mm", "length"),
        ("20000 mm", "length"),
        ("0 m", "length"),
        ("1e-30 m", "length"),  # beyond the prefix ladder: no neat form
        ("0.0000001 deg", "angle"),
    ],
)
def test_sane_forms_stay_silent(text, dim):
    warns: list[str] = []
    parse_quantity(text, dim, warnings=warns)
    assert warns == []


def test_permissive_path_never_warns():
    warns: list[str] = []
    assert parse_quantity(
        "0.000000003 m", "length", require_unit=False, warnings=warns
    ) == pytest.approx(3e-9)
    assert parse_quantity(3e-9, "length", require_unit=False, warnings=warns) == 3e-9
    assert warns == []


def test_dsl_boundary_collects_warning_permissive_does_not():
    warns: list[str] = []
    spec = parse("box:w0.000000003md20mmh10mm", require_units=True, warnings=warns)
    assert spec.params["w"] == pytest.approx(3e-9)
    assert len(warns) == 1 and "box:w" in warns[0] and "3 nm" in warns[0]
    quiet: list[str] = []
    parse("box:w0.000000003d0.02h0.01", warnings=quiet)
    assert quiet == []
