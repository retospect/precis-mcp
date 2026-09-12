"""Named dimensions + constraints — refuse the impossible at parse.

`dim <name> =|>=|<= <mm>` bounds accumulate by intersection (one-sided
bounds are first-class: ">= 100" stays open-ended); `constrain a = b`
merges dims into an equality class. A class whose combined interval is
empty is a contradiction the kernel refuses before any geometry exists.
"""

from __future__ import annotations

import pytest

from precis.cad.scene import SceneError, parse_source, spec_to_source

_BODY = "plate add box:w60mmd40mmh10mm\n"


def test_dims_round_trip_with_one_sided_bounds():
    spec = parse_source(
        _BODY + "dim a = 200mm\ndim c >= 100mm\ndim c <= 500mm\ndim d >= 10mm"
    )
    assert spec.meta["dims"] == {
        "a": [0.2, 0.2],
        "c": [0.1, 0.5],
        "d": [0.01, None],
    }
    assert parse_source(spec_to_source(spec)) == spec
    src = spec_to_source(spec)
    assert "dim a = 0.2m" in src and "dim d >= 0.01m" in src


def test_the_users_example_is_refused():
    # a is 20cm, b is 15cm — they cannot be the same length.
    with pytest.raises(SceneError, match="empty combined range"):
        parse_source(_BODY + "dim a = 200mm\ndim b = 150mm\nconstrain a = b")


def test_per_dim_contradiction_refused_at_the_line():
    with pytest.raises(SceneError, match="empty range"):
        parse_source(_BODY + "dim a >= 300mm\ndim a <= 200mm")


def test_transitive_classes_are_checked():
    # a = c and c = b pulls a and b into one class → same contradiction.
    with pytest.raises(SceneError, match="empty combined range"):
        parse_source(
            _BODY
            + "dim a = 200mm\ndim b = 150mm\ndim c >= 0mm\n"
            + "constrain a = c\nconstrain c = b"
        )


def test_compatible_constraints_pass_and_round_trip():
    spec = parse_source(_BODY + "dim a >= 100mm\ndim b <= 500mm\nconstrain a = b")
    assert spec.meta["constraints"] == [["a", "b"]]
    assert parse_source(spec_to_source(spec)) == spec


def test_constrain_refusals():
    with pytest.raises(SceneError, match="not a declared dim"):
        parse_source(_BODY + "dim a = 5mm\nconstrain a = ghost")
    with pytest.raises(SceneError, match="tautology"):
        parse_source(_BODY + "dim a = 5mm\nconstrain a = a")
    with pytest.raises(SceneError, match="expected 'dim"):
        parse_source(_BODY + "dim a about 5mm")


# --- v2: parametrized configs (`box:w{a}...`) ------------------------------


def test_pinned_dim_drives_geometry_and_source_stays_parametric():
    from precis.cad.scene import expand_instances

    src = (
        "dim a = 40mm\ndim b >= 10mm\nconstrain a = b\n"
        "component bracket\nslab add box:w{a}d{b}h10\n"
    )
    spec = parse_source(src)
    # the stored source keeps the reference — edit the dim, geometry follows
    assert "box:w{a}d{b}h10" in spec_to_source(spec)
    ex = expand_instances(spec)
    # b is pinned through its equality class with a; the dim substitutes as
    # SI metres (40mm → 0.04), while the config's own bare `h10` literal is
    # the one grammar exemption — canonical/unconverted.
    assert ex.nodes[0].config == "box:w0.04d0.04h10"


def test_unpinned_and_unknown_dim_refs_refuse_at_parse():
    with pytest.raises(SceneError, match="dim 'c' is not pinned"):
        parse_source("dim c >= 100mm\ncomponent a\nbase add box:w{c}d5h5\n")
    with pytest.raises(SceneError, match="references unknown dim 'zz'"):
        parse_source("component a\nbase add box:w{zz}d5h5\n")


def test_bad_shape_after_substitution_still_refuses():
    with pytest.raises(Exception, match="box needs"):
        parse_source("dim a = 5mm\ncomponent x\nbase add box:w{a}\n")


def test_parametric_chamfer_keeps_the_op_rules():
    with pytest.raises(SceneError, match="cannot use op 'add'"):
        parse_source(
            "dim s = 2mm\ncomponent x\nbase add box:w9mmd9mmh9mm\nedge add chamfer:{s}x45\n"
        )


def test_payload_config_may_not_reference_dims():
    with pytest.raises(SceneError, match="payload 'pin' config cannot reference"):
        parse_source(
            "dim d = 3mm\ncomponent hub\nbase add box:w9mmd9mmh9mm\n"
            "port p @0mm,0mm,9mm of:hub\n"
            "payload pin add cyl:r{d}h5 at:p\n"
        )


def test_sub_design_dims_resolve_in_their_own_namespace():
    from precis.cad.scene import expand_instances

    sub = parse_source("dim a = 7mm\ncomponent pin\nshaft add cyl:r{a}h20\n")
    # the parent declares a DIFFERENT a — the sub must not see it
    top = parse_source("dim a = 99mm\nuse pin_lib as p1 @0mm,0mm,5mm\n")
    ex = expand_instances(top, resolve=lambda slug: sub)
    shaft = next(n for n in ex.nodes if n.name == "p1.shaft")
    # the dim substitutes as SI metres (7mm → 0.007); `h20` is the config's
    # own bare literal — the one grammar exemption, canonical/unconverted.
    assert shaft.config == "cyl:r0.007h20"
