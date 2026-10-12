"""parse_vector_quantity / format_vector (pose-vector-units): three accepted
forms, strict rejection with a hint echoing the caller's vector, and the
shared se/nm ``pose``/``rot`` op ingest riding it."""

from __future__ import annotations

import math

import pytest

from precis.blocktree.ops import OpError, _pose_vec
from precis.errors import BadInput
from precis.utils.units import format_vector, parse_vector_quantity


@pytest.mark.parametrize(
    "value",
    [
        "[3 0 0] nm",
        "3 0 0 nm",
        "[3, 0, 0] nm",
        "3, 0, 0 nm",
        ["3nm", "0nm", "0nm"],
        ["3 nm", 0, "0 nm"],
        "[3nm 0 0]",
        "[3e-9 0 0] m",
    ],
)
def test_forms_agree(value: object) -> None:
    assert parse_vector_quantity(value, "length") == pytest.approx([3e-9, 0, 0])


def test_external_unit_arg() -> None:
    assert parse_vector_quantity([4, 0, 0], "length", unit="km") == pytest.approx(
        [4000.0, 0, 0]
    )
    assert parse_vector_quantity([0, 0, 90], "angle", unit="deg") == pytest.approx(
        [0, 0, math.pi / 2]
    )


def test_per_component_mixed_units() -> None:
    got = parse_vector_quantity(["3nm", "0nm", "2Å"], "length")
    assert got == pytest.approx([3e-9, 0, 2e-10])


def test_zero_vector_is_legal_bare() -> None:
    assert parse_vector_quantity([0, 0, 0], "length") == [0.0, 0.0, 0.0]
    assert parse_vector_quantity("0 0 0", "angle") == [0.0, 0.0, 0.0]


def test_bare_nonzero_rejects_with_echoed_hint() -> None:
    with pytest.raises(BadInput) as ei:
        parse_vector_quantity([3, 0, 0], "length", arg_name="pose")
    assert '"[3 0 0] nm"' in str(ei.value.next)
    assert '"[3 0 0] m"' in str(ei.value.next)


def test_rot_hint_offers_deg_rad() -> None:
    with pytest.raises(BadInput) as ei:
        parse_vector_quantity([0, 0, 1.5], "angle")
    assert '"[0 0 1.5] deg"' in str(ei.value.next)
    assert '"[0 0 1.5] rad"' in str(ei.value.next)


def test_mixing_united_and_bare_nonzero_rejects() -> None:
    with pytest.raises(BadInput, match="mixes"):
        parse_vector_quantity(["3nm", 2, 0], "length")


def test_bare_zero_beside_united_components_ok() -> None:
    assert parse_vector_quantity(["3nm", 0, "0nm"], "length") == pytest.approx(
        [3e-9, 0, 0]
    )


def test_trailing_plus_per_component_rejects() -> None:
    with pytest.raises(BadInput, match="AND per-component"):
        parse_vector_quantity(["3nm", 0, 0], "length", unit="m")
    with pytest.raises(BadInput, match="twice"):
        parse_vector_quantity("[3 0 0] nm", "length", unit="m")


@pytest.mark.parametrize("bad", [[1, 2], "1 2 3 4 m", 5, [True, 0, 0], ["a", 0, 0]])
def test_shape_and_type_errors(bad: object) -> None:
    with pytest.raises(BadInput):
        parse_vector_quantity(bad, "length")


def test_wrong_dimension_rejects() -> None:
    with pytest.raises(BadInput):
        parse_vector_quantity("[1 0 0] kg", "length")


def test_format_vector_common_unit() -> None:
    assert format_vector([3e-9, 0, 0], "length") == "[3 0 0] nm"
    assert format_vector([0, 0, math.pi / 2], "angle") == "[0 0 90] deg"
    assert format_vector([0, 0, 0], "length") == "[0 0 0] m"
    assert format_vector([1.5, 0, -2], "length") == "[1.5 0 -2] m"


def test_format_roundtrips_through_parse() -> None:
    v = [2e-10, 0, 3e-9]
    assert parse_vector_quantity(format_vector(v, "length"), "length") == pytest.approx(
        v, rel=1e-3
    )


def test_pose_vec_op_ingest() -> None:
    assert _pose_vec({"pose": "[1 2 3] mm"}, "pose", "p") == pytest.approx(
        [1e-3, 2e-3, 3e-3]
    )
    assert _pose_vec({"rot": "[0 0 90] deg"}, "rot", "r") == pytest.approx(
        [0, 0, math.pi / 2]
    )
    assert _pose_vec({"pose": [4, 0, 0], "pose_unit": "km"}, "pose", "p") == [
        4000.0,
        0.0,
        0.0,
    ]
    assert _pose_vec({}, "pose", "p") == [0.0, 0.0, 0.0]


def test_pose_vec_bare_rejects_as_operror_with_hint() -> None:
    with pytest.raises(OpError, match=r"\[3 0 0\] nm"):
        _pose_vec({"pose": [3, 0, 0]}, "pose", "pose")
