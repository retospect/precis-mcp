"""Exact completion wire transport: precision, identity and refusal boundaries."""

from __future__ import annotations

import json
from decimal import Decimal
from fractions import Fraction
from typing import Any, cast

import pytest

from precis.structure import completion_codec as codec


def test_round_trip_preserves_large_ids_and_distinct_exact_values() -> None:
    original = {
        "ref_id": 2**100 + 1,
        "version": 3,
        "decimal": Decimal("0.1"),
        "binary": 0.1,
        "fraction": Fraction(-7, 12),
        "scientific_integer": codec.canonical_number(3),
        "flags": (True, False, None),
        "label": 'Cα\nquoted " atom',
    }
    wire = codec.dumps_wire(original)
    parsed = json.loads(wire)
    assert parsed["ref_id"] == {"$int": str(2**100 + 1)}
    assert parsed["version"] == {"$int": "3"}
    assert parsed["scientific_integer"] == {"$rational": {"n": "3", "d": "1"}}
    restored = codec.loads_wire(wire)
    assert restored["ref_id"] == original["ref_id"]
    assert type(restored["ref_id"]) is int
    assert restored["decimal"] == Fraction(1, 10)
    assert restored["binary"] == Fraction.from_float(0.1)
    assert restored["decimal"] != restored["binary"]
    assert restored["fraction"] == Fraction(-7, 12)
    assert type(restored["scientific_integer"]) is Fraction
    assert restored["flags"] == [True, False, None]
    assert restored["label"] == original["label"]


def test_canonical_json_ignores_mapping_order_but_not_missing_values() -> None:
    first = {"z": [2, {"b": 3, "a": "é"}], "a": None}
    second = {"a": None, "z": [2, {"a": "é", "b": 3}]}
    assert codec.dumps_wire(first) == codec.dumps_wire(second)
    assert codec.dumps_wire({}) != codec.dumps_wire({"a": None})
    assert codec.dumps_wire(True) != codec.dumps_wire(1)
    assert codec.dumps_wire(1) != codec.dumps_wire(Fraction(1))
    assert codec.loads_wire(codec.dumps_wire("\ud800")) == "\ud800"


@pytest.mark.parametrize("value", [-0.0, Decimal("-0"), Decimal("0E+99999999")])
def test_signed_zero_normalizes_without_expanding_decimal_exponent(
    value: object,
) -> None:
    assert codec.to_wire(value) == {"$rational": {"n": "0", "d": "1"}}


@pytest.mark.parametrize(
    "value",
    [True, None, "1", float("inf"), float("nan"), Decimal("NaN"), Decimal("-Infinity")],
)
def test_canonical_number_rejects_non_numbers_and_nonfinite(value: object) -> None:
    with pytest.raises(codec.CodecError):
        codec.canonical_number(value)


@pytest.mark.parametrize(
    "value", [{"$int": "3"}, {"$rational": None}, {"x": {"$int": 3}}]
)
def test_ordinary_data_cannot_impersonate_reserved_tags(value: object) -> None:
    with pytest.raises(codec.CodecError, match="reserved"):
        codec.to_wire(value)


@pytest.mark.parametrize(
    "value",
    [
        {"$int": True},
        {"$int": 1},
        {"$int": "-0"},
        {"$int": "+1"},
        {"$int": "01"},
        {"$int": "١"},
        {"$int": "1\n"},
        {"$int": " 1"},
        {"$int": "1", "extra": None},
        {"$rational": {"n": "1", "d": "2", "extra": None}},
        {"$rational": {"n": "1"}},
        {"$rational": "1/2"},
        {"$rational": {"n": "1", "d": "-2"}},
        {"$rational": {"n": "1", "d": "0"}},
        {"$rational": {"n": "2", "d": "4"}},
        {"$rational": {"n": "0", "d": "2"}},
        {"$rational": {"n": "-0", "d": "1"}},
        {"$rational": {"n": "NaN", "d": "1"}},
        {"$rational": {"n": "1", "d": True}},
        {"$int": "1", "$rational": {"n": "1", "d": "1"}},
        1,
        0.5,
        Decimal("1"),
        (None,),
        {1: "non-string key"},
    ],
)
def test_from_wire_refuses_noncanonical_or_untagged_values(value: object) -> None:
    with pytest.raises(codec.CodecError):
        codec.from_wire(value)


@pytest.mark.parametrize(
    "text",
    [
        '{"x":null,"x":true}',
        '{"x":null,"\\u0078":true}',
        '{"$rational":{"n":"1","n":"2","d":"3"}}',
        "1",
        "1.25",
        "1e10000000",
        "NaN",
        "Infinity",
        "-Infinity",
        '[true,{"x":0}]',
        '{"x":}',
    ],
)
def test_json_parser_rejects_duplicates_raw_numbers_and_invalid_json(text: str) -> None:
    with pytest.raises(codec.CodecError):
        codec.loads_wire(text)


def test_numeric_digit_bound_applies_before_expansion() -> None:
    largest = 10**codec.MAX_NUMERIC_DIGITS - 1
    assert codec.loads_wire(codec.dumps_wire(largest)) == largest
    for value in (
        largest + 1,
        -(largest + 1),
        Fraction(1, largest + 1),
        Decimal("1e1000000000"),
        Decimal("1e-1000000000"),
        Decimal("1" * (codec.MAX_NUMERIC_DIGITS + 1)),
    ):
        with pytest.raises(codec.CodecError, match="limit"):
            codec.to_wire(value)
    with pytest.raises(codec.CodecError, match="digit limit"):
        codec.loads_wire('{"$int":"' + "1" * (codec.MAX_NUMERIC_DIGITS + 1) + '"}')


def test_depth_bound_includes_numeric_tag_structure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codec, "MAX_DEPTH", 2)
    assert codec.from_wire([[None]]) == [[None]]
    assert codec.from_wire([{"$int": "2"}]) == [2]
    assert codec.to_wire(Fraction(1, 3)) == {"$rational": {"n": "1", "d": "3"}}
    for call in (
        lambda: codec.from_wire([[[None]]]),
        lambda: codec.to_wire([[2]]),
        lambda: codec.to_wire([Fraction(1, 3)]),
        lambda: codec.loads_wire("[[[null]]]"),
    ):
        with pytest.raises(codec.CodecError, match="depth"):
            call()


def test_preparse_bounds_and_tag_item_accounting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codec, "MAX_ITEMS", 3)
    assert codec.to_wire(123) == {"$int": "123"}
    assert codec.from_wire([None, None]) == [None, None]
    for call in (
        lambda: codec.to_wire([123]),
        lambda: codec.from_wire([None, None, None]),
        lambda: codec.loads_wire("[null,null,null]"),
        lambda: codec.to_wire(Fraction(1, 2)),
    ):
        with pytest.raises(codec.CodecError, match="item"):
            call()


def test_text_budget_counts_aggregate_keys_and_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codec, "MAX_TEXT_BYTES", 8)
    assert codec.to_wire({"é": "é"}) == {"é": "é"}
    for call in (
        lambda: codec.to_wire("é" * 5),
        lambda: codec.to_wire("x" * 9),
        lambda: codec.to_wire({"é": "é", "x": "abcd"}),
        lambda: codec.dumps_wire({"é": "é"}),
        lambda: codec.loads_wire('"1234567"'),
    ):
        with pytest.raises(codec.CodecError, match="text"):
            call()


def test_scanner_ignores_escaped_json_structure_inside_strings() -> None:
    original = {"literal": '[{\\" : }]' * 100, "x": [True, None, Fraction(3, 4)]}
    assert codec.loads_wire(codec.dumps_wire(original)) == original


def test_unsupported_objects_and_cycles_are_bounded() -> None:
    cycle: list[object] = []
    cycle.append(cycle)
    for value in (object(), {1: "key"}, {"x"}, b"bytes", cycle):
        with pytest.raises(codec.CodecError):
            codec.to_wire(value)
    with pytest.raises(codec.CodecError, match="text"):
        codec.loads_wire(cast(Any, b"null"))
