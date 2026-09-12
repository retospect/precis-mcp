"""Tests for the shared units utility (``precis.utils.units``).

Covers the units-policy-cutover acceptance criteria for this module:
ingest-any-unit parsing, the bare-number zero-counting guard + hint,
the neat display formatter's goldens, and the DSL-safe canonical
emitter's round-trip through ``cad.dsl._NUM``.
"""

from __future__ import annotations

import math
import re

import pytest

from precis.cad.dsl import _NUM
from precis.errors import BadInput
from precis.utils.units import (
    Dimension,
    UnitRequiredError,
    format_dsl_number,
    format_quantity,
    parse_quantity,
)

_NUM_RE = re.compile(rf"^{_NUM}$")


# ── ingest boundary: parse_quantity ─────────────────────────────────


@pytest.mark.parametrize(
    ("text", "dimension", "expected_si"),
    [
        ("3 mm", "length", 3e-3),
        ("1 m", "length", 1.0),
        ("2 cm", "length", 2e-2),
        ("1 km", "length", 1e3),
        ("1 nm", "length", 1e-9),
        ("1.4 Å", "length", 1.4e-10),
        ("1 in", "length", 0.0254),
        ("1 ft", "length", 0.3048),
        ("12 N", "force", 12.0),
        ("1 kN", "force", 1e3),
        ("1 lbf", "force", 4.4482216152605),
        ("5 kg", "mass", 5.0),
        ("500 g", "mass", 0.5),
        ("1 l", "volume", 1e-3),
        ("350 ml", "volume", 3.5e-4),
    ],
)
def test_parse_quantity_supported_units(
    text: str, dimension: Dimension, expected_si: float
) -> None:
    got = parse_quantity(text, dimension)
    assert math.isclose(got, expected_si, rel_tol=1e-9)


def test_parse_quantity_pint_long_tail_unit() -> None:
    # pint's curated registry goes well past the AC's minimum list —
    # a whimsical-but-real unit still ingests cleanly.
    got = parse_quantity("1 furlong", "length")
    assert math.isclose(got, 201.168, rel_tol=1e-5)


@pytest.mark.parametrize(
    ("value", "unit", "dimension"),
    [
        (3, "mm", "length"),
        (1.4, "Å", "length"),
        (12, "N", "force"),
        (5, "kg", "mass"),
        (350, "ml", "volume"),
        (90, "deg", "angle"),
        (1.57, "rad", "angle"),
    ],
)
def test_parse_quantity_round_trip_via_canonical_emitter(
    value: float, unit: str, dimension: Dimension
) -> None:
    """Ingest in a supported unit -> stored SI -> the DSL-safe canonical
    emitter (full float64 precision) + the dimension's SI unit symbol
    re-parses to the same SI quantity within 1e-12 relative — the round-
    trip property from the acceptance criteria. This is the *canonical*
    (dossier §4 context (b)) formatter, not the lossy neat display one —
    see the module docstring's two-formatter-context split.
    """
    si_unit = {
        "length": "m",
        "force": "N",
        "mass": "kg",
        "volume": "m**3",
        "angle": "rad",
    }[dimension]
    si = parse_quantity(f"{value} {unit}", dimension)
    canonical = f"{format_dsl_number(si)} {si_unit}"
    reparsed = parse_quantity(canonical, dimension)
    assert math.isclose(reparsed, si, rel_tol=1e-12)


def test_parse_quantity_numeric_input_bypasses_text_parsing() -> None:
    assert parse_quantity(3.0, "length", require_unit=False) == 3.0
    assert parse_quantity(3, "length", require_unit=False) == 3.0


def test_parse_quantity_bare_number_string_without_unit_ok_when_allowed() -> None:
    assert parse_quantity("3", "length", require_unit=False) == 3.0


# ── zero-counting guard: bare numbers rejected + structured hint ───


def test_parse_quantity_rejects_bare_numeric_input_by_default() -> None:
    with pytest.raises(UnitRequiredError) as exc_info:
        parse_quantity(3, "length")
    err = exc_info.value
    assert err.dimension == "length"
    assert err.value == 3.0


def test_parse_quantity_rejects_bare_number_string_by_default() -> None:
    with pytest.raises(UnitRequiredError) as exc_info:
        parse_quantity("3", "length")
    assert exc_info.value.value == 3.0


def test_parse_quantity_rejects_boolean_input() -> None:
    # bool is an int subclass in Python — must not silently become 1.0/0.0.
    with pytest.raises(BadInput):
        parse_quantity(True, "length")


@pytest.mark.parametrize("dimension", ["length", "force", "mass", "volume"])
def test_bare_number_hint_shape(dimension: Dimension) -> None:
    with pytest.raises(UnitRequiredError) as exc_info:
        parse_quantity(3, dimension)
    err = exc_info.value
    # Hint echoes the value and offers 2-3 plausible unit readings, each
    # a quoted "<value> <unit>" the agent can paste back verbatim.
    assert "3" in err.hint
    assert "state units" in err.hint
    readings = re.findall(r"'([^']+)'", err.hint)
    assert 2 <= len(readings) <= 3
    for reading in readings:
        assert reading.startswith("3 ")
    # The hint is also the structured error's one breaking `next=` action.
    assert err.next == err.hint


def test_bare_number_hint_matches_decisions_log_example() -> None:
    with pytest.raises(UnitRequiredError) as exc_info:
        parse_quantity(3, "length")
    assert exc_info.value.hint == "3 — state units: '3 mm'? '3 m'? '3 in'?"


# ── dimensionality / parse-failure errors ───────────────────────────


def test_parse_quantity_wrong_dimension_rejected() -> None:
    with pytest.raises(BadInput):
        parse_quantity("5 kg", "length")


def test_parse_quantity_unknown_unit_rejected() -> None:
    with pytest.raises(BadInput):
        parse_quantity("5 zorkmids", "length")


def test_parse_quantity_empty_string_rejected() -> None:
    with pytest.raises(BadInput):
        parse_quantity("   ", "length")


# ── neat formatter goldens ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "dimension", "expected"),
    [
        (2.3e-9, "length", "2.3 nm"),
        (1.2e3, "force", "1.2 kN"),
        (3.5e-4, "volume", "350 ml"),
        (1.4e-10, "length", "140 pm"),
        (0.0, "length", "0 m"),
        (2.5, "mass", "2.5 kg"),
        (0.0025, "mass", "2.5 g"),
    ],
)
def test_format_quantity_goldens(
    value: float, dimension: Dimension, expected: str
) -> None:
    assert format_quantity(value, dimension) == expected


def test_format_quantity_angle_golden() -> None:
    # The AC's angle golden: π/2 rad -> "90°" — degrees on display, never
    # the SI-prefix ladder (nobody reads femto-degrees).
    assert format_quantity(math.pi / 2, "angle") == "90°"


@pytest.mark.parametrize(
    ("value", "dimension"),
    [
        (1e-20, "length"),  # below the prefix ladder floor
        (1e20, "length"),  # above the prefix ladder ceiling
    ],
)
def test_format_quantity_out_of_range_falls_back_to_e_notation(
    value: float, dimension: Dimension
) -> None:
    out = format_quantity(value, dimension)
    assert "e" in out
    # Still re-parseable by the ingest side, round-tripping the value.
    reparsed = parse_quantity(out, dimension)
    assert math.isclose(reparsed, value, rel_tol=1e-3)


# ── DSL-safe canonical emitter ───────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    [
        4.0,
        -4.0,
        4.5,
        0.00035,
        3e-9,
        -3e-9,
        1.5e20,
        1.4e-10,
        0.1,
        100.0,
        1e16,
        1e-16,
        123456.789,
        0.0,
        -0.0,
    ],
)
def test_format_dsl_number_reparses_under_dsl_num_grammar(value: float) -> None:
    text = format_dsl_number(value)
    assert _NUM_RE.match(text), f"{text!r} does not match cad.dsl._NUM"
    assert float(text) == value


def test_format_dsl_number_trims_whole_numbers() -> None:
    assert format_dsl_number(4.0) == "4"
    assert format_dsl_number(-4.0) == "-4"


def test_format_dsl_number_rejects_non_finite() -> None:
    with pytest.raises(BadInput):
        format_dsl_number(float("nan"))
    with pytest.raises(BadInput):
        format_dsl_number(float("inf"))
