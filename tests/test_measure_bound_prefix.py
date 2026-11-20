"""Bound prefix: inclusive wording renders ≥/≤, bare strict signs stay strict."""

from __future__ import annotations

import pytest

from precis.handlers._measure_render import bound_sign, value_text


@pytest.mark.parametrize(
    ("form", "literal", "want"),
    [
        ("lower_bound", "700 or more", "≥ "),
        ("lower_bound", ">=5", "≥ "),
        ("lower_bound", ">5", "> "),
        ("lower_bound", None, "> "),
        ("upper_bound", "at most 3", "≤ "),
        ("upper_bound", "<3", "< "),
        ("approximate_point", "~3", "~"),
        ("point", "3", ""),
    ],
)
def test_bound_prefix(form: str, literal: str | None, want: str) -> None:
    assert bound_sign({"value_form": form, "literal": literal}) == want


def test_epoch_lower_bound() -> None:
    """gr717093: the source includes the endpoint of the epoch bound."""
    row = {"value_form": "lower_bound", "value_low": 700, "literal": "700 or more"}
    assert value_text(row) == "≥ 700"
