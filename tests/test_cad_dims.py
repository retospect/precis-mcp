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


# --- v2: parametrized configs (`box:w{a}...`) ------------------------------


def test_pinned_dim_drives_geometry_and_source_stays_parametric():
    from precis.cad.scene import expand_instances

    src = (
        "dim a = 40\ndim b >= 10\nconstrain a = b\n"
        "component bracket\nslab add box:w{a}d{b}h10\n"
    )
    spec = parse_source(src)
    # the stored source keeps the reference — edit the dim, geometry follows
    assert "box:w{a}d{b}h10" in spec_to_source(spec)
    ex = expand_instances(spec)
    # b is pinned through its equality class with a
    assert ex.nodes[0].config == "box:w40d40h10"


def test_unpinned_and_unknown_dim_refs_refuse_at_parse():
    with pytest.raises(SceneError, match="dim 'c' is not pinned"):
        parse_source("dim c >= 100\ncomponent a\nbase add box:w{c}d5h5\n")
    with pytest.raises(SceneError, match="references unknown dim 'zz'"):
        parse_source("component a\nbase add box:w{zz}d5h5\n")


def test_bad_shape_after_substitution_still_refuses():
    with pytest.raises(Exception, match="box needs"):
        parse_source("dim a = 5\ncomponent x\nbase add box:w{a}\n")


def test_parametric_chamfer_keeps_the_op_rules():
    with pytest.raises(SceneError, match="cannot use op 'add'"):
        parse_source(
            "dim s = 2\ncomponent x\nbase add box:w9d9h9\nedge add chamfer:{s}x45\n"
        )


def test_payload_config_may_not_reference_dims():
    with pytest.raises(SceneError, match="payload 'pin' config cannot reference"):
        parse_source(
            "dim d = 3\ncomponent hub\nbase add box:w9d9h9\n"
            "port p @0,0,9 of:hub\n"
            "payload pin add cyl:r{d}h5 at:p\n"
        )


def test_sub_design_dims_resolve_in_their_own_namespace():
    from precis.cad.scene import expand_instances

    sub = parse_source("dim a = 7\ncomponent pin\nshaft add cyl:r{a}h20\n")
    # the parent declares a DIFFERENT a — the sub must not see it
    top = parse_source("dim a = 99\nuse pin_lib as p1 @0,0,5\n")
    ex = expand_instances(top, resolve=lambda slug: sub)
    shaft = next(n for n in ex.nodes if n.name == "p1.shaft")
    assert shaft.config == "cyl:r7h20"
