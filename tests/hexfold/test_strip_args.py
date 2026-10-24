"""``sheetnet.strip`` argument validation (gr513669): a nonpositive
``periods``/``rows`` must refuse at the call with a named ``ValueError``
— never a bare ``IndexError`` out of ``_zigzag_rims`` (an empty
``top_full`` walk read ``walk[0]``)."""

from __future__ import annotations

import pytest

from hexfold.sheetnet import strip


def test_strip_refuses_nonpositive_periods() -> None:
    with pytest.raises(ValueError, match="strip periods must be >= 1; got -3"):
        strip(rows=8, periods=-3)


def test_strip_refuses_zero_periods() -> None:
    with pytest.raises(ValueError, match="strip periods must be >= 1; got 0"):
        strip(rows=8, periods=0)


def test_strip_refuses_nonpositive_rows() -> None:
    with pytest.raises(ValueError, match="strip rows must be >= 1; got 0"):
        strip(rows=0, periods=4)


def test_strip_validation_runs_for_armchair_too() -> None:
    with pytest.raises(ValueError, match="strip periods must be >= 1; got -1"):
        strip(rows=4, periods=-1, edge="armchair")


def test_strip_still_builds_and_bad_edge_still_refused() -> None:
    st = strip(rows=4, periods=3)
    assert st.rims["rim"].atoms
    with pytest.raises(ValueError, match="strip edge"):
        strip(rows=4, periods=3, edge="bogus")
