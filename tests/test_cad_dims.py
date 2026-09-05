"""Named dimensions + constraints — refuse the impossible at parse.

`dim <name> =|>=|<= <mm>` bounds accumulate by intersection (one-sided
bounds are first-class: ">= 100" stays open-ended); `constrain a = b`
merges dims into an equality class. A class whose combined interval is
empty is a contradiction the kernel refuses before any geometry exists.
"""

from __future__ import annotations

import pytest

from precis.cad.scene import SceneError, parse_source, spec_to_source

_BODY = "plate add box:w60d40h10\n"


def test_dims_round_trip_with_one_sided_bounds():
    spec = parse_source(_BODY + "dim a = 200\ndim c >= 100\ndim c <= 500\ndim d >= 10")
    assert spec.meta["dims"] == {
        "a": [200.0, 200.0],
        "c": [100.0, 500.0],
        "d": [10.0, None],
    }
    assert parse_source(spec_to_source(spec)) == spec
    src = spec_to_source(spec)
    assert "dim a = 200" in src and "dim d >= 10" in src


def test_the_users_example_is_refused():
    # a is 20cm, b is 15cm — they cannot be the same length.
    with pytest.raises(SceneError, match="empty combined range"):
        parse_source(_BODY + "dim a = 200\ndim b = 150\nconstrain a = b")


def test_per_dim_contradiction_refused_at_the_line():
    with pytest.raises(SceneError, match="empty range"):
        parse_source(_BODY + "dim a >= 300\ndim a <= 200")


def test_transitive_classes_are_checked():
    # a = c and c = b pulls a and b into one class → same contradiction.
    with pytest.raises(SceneError, match="empty combined range"):
        parse_source(
            _BODY
            + "dim a = 200\ndim b = 150\ndim c >= 0\n"
            + "constrain a = c\nconstrain c = b"
        )


def test_compatible_constraints_pass_and_round_trip():
    spec = parse_source(_BODY + "dim a >= 100\ndim b <= 500\nconstrain a = b")
    assert spec.meta["constraints"] == [["a", "b"]]
    assert parse_source(spec_to_source(spec)) == spec


def test_constrain_refusals():
    with pytest.raises(SceneError, match="not a declared dim"):
        parse_source(_BODY + "dim a = 5\nconstrain a = ghost")
    with pytest.raises(SceneError, match="tautology"):
        parse_source(_BODY + "dim a = 5\nconstrain a = a")
    with pytest.raises(SceneError, match="expected 'dim"):
        parse_source(_BODY + "dim a about 5")
