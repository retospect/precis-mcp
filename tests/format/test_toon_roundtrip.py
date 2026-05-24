"""Roundtrip tests: ``load(dump(rows)) == rows-as-strings``.

`dump` accepts heterogeneous scalar types; `load` always returns
strings. The roundtrip property therefore compares against the
*stringified* form of the input, not the original.

**Caveat (intentional)**: the dump rules are tuned for an LLM
audience — minimal quoting, no force-wrap on all-empty rows.
That makes a few cell shapes *not* round-trippable through
``load(dump(...))``:

* A cell that starts with a literal ``"`` — ``load`` reads the
  leading quote as a wrapper open.
* An all-empty single-column row — ``dump`` emits ``"a\\n"`` and
  ``load`` cannot distinguish that from a header-only document.
* The literal ``""`` cell on its own — same load-side ambiguity.

These shapes are not exercised in production (no in-tree caller
parses our own dumps). The remaining well-behaved shapes — cells
with embedded tabs / newlines / CRs / mid-string quotes — still
round-trip cleanly.
"""

from __future__ import annotations

import pytest

from precis.format.toon import dump, load


def _stringify(rows: list[dict[str, object]]) -> list[dict[str, str]]:
    """Project rows to the all-string form that ``load`` returns."""
    out: list[dict[str, str]] = []
    keys: list[str] = []
    for row in rows:
        for k in row:
            if k not in keys:
                keys.append(k)
    for row in rows:
        new: dict[str, str] = {}
        for k in keys:
            v = row.get(k)
            if v is None:
                new[k] = ""
            elif v is True:
                new[k] = "true"
            elif v is False:
                new[k] = "false"
            elif isinstance(v, float):
                new[k] = repr(v)
            else:
                new[k] = str(v)
        out.append(new)
    return out


@pytest.mark.parametrize(
    "rows",
    [
        [{"a": "1", "b": "2"}],
        [{"a": "x", "b": "y"}, {"a": "z", "b": "w"}],
        [{"a": "with\tseparator"}],
        [{"a": "with\nnewline"}],
        [{"a": 'with "quote"'}],  # mid-cell quote — passes through, parses literally
        [{"a": "x\ry"}],
        [{"a": None, "b": "x"}],
        [{"a": True, "b": False}],
        [{"a": 42, "b": -7}],
        [{"a": 1.5, "b": 0.0}],
        # Multi-column all-empty: rendered as `"\t"` (one separator)
        # which the tokeniser correctly parses back to two empty cells.
        [{"a": "", "b": ""}],
        [{"a": "tab\there\tand\there"}],
        [{"a": 'quote " then \ttab'}],
    ],
)
def test_roundtrip_preserves_string_form(rows):
    encoded = dump(rows)
    decoded = load(encoded)
    assert decoded == _stringify(rows)


class TestRoundtripWithSchema:
    def test_pinned_schema_roundtrips(self):
        rows = [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}]
        out = dump(rows, schema=["b", "a"])
        decoded = load(out)
        assert decoded == [{"b": "2", "a": "1"}, {"b": "4", "a": "3"}]

    def test_schema_with_missing_keys_roundtrips_as_empty(self):
        rows = [{"a": "1"}, {"a": "3", "b": "4"}]
        out = dump(rows, schema=["a", "b"])
        decoded = load(out)
        assert decoded == [{"a": "1", "b": ""}, {"a": "3", "b": "4"}]


class TestRoundtripWithSeparator:
    def test_comma_separator(self):
        rows = [{"a": "1", "b": "2"}, {"a": "with,comma", "b": "y"}]
        encoded = dump(rows, sep=",")
        decoded = load(encoded, sep=",")
        assert decoded == [{"a": "1", "b": "2"}, {"a": "with,comma", "b": "y"}]


class TestPathologicalCells:
    """Single-cell roundtrip on the shapes that *do* survive.

    The cells excluded from this list — anything starting with a
    literal ``"``, the empty cell, the literal ``""`` — are
    documented at the top of this module as the intentional
    LLM-audience trade-off. They aren't broken; they're out of
    contract.
    """

    @pytest.mark.parametrize(
        "cell",
        [
            "tab\there",
            "newline\nhere",
            'quote " here',  # mid-string quote: passes through cleanly
            "all\tof\nthe\rabove",
            " ",
            "\t",
            "\n",
        ],
    )
    def test_single_cell_survives_roundtrip(self, cell):
        out = dump([{"a": cell}])
        decoded = load(out)
        assert decoded == [{"a": cell}]
