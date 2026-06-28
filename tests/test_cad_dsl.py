"""config mini-DSL — parse / build / round-trip tests (ADR 0041 §11)."""

from __future__ import annotations

import math

import pytest

from precis.cad.dsl import (
    DslError,
    ShapeSpec,
    build,
    build_config,
    format_spec,
    parse,
)
from precis.cad.primitives import (
    CircularFrustum,
    PolyFrustum,
    Sphere,
    Torus,
)
from precis.cad.vec import vec3


@pytest.mark.parametrize(
    ("config", "alias", "params"),
    [
        ("box:w40d20h10", "box", {"w": 40, "d": 20, "h": 10}),
        ("cyl:r3h12", "cyl", {"r": 3, "h": 12}),
        ("cone:r4h8", "cone", {"r": 4, "h": 8}),
        ("tcone:rb4rt2h8", "tcone", {"rb": 4, "rt": 2, "h": 8}),
        ("sphere:r5", "sphere", {"r": 5}),
        ("torus:R10r2", "torus", {"R": 10, "r": 2}),
        ("frustum:n6rb4rt2h5", "frustum", {"n": 6, "rb": 4, "rt": 2, "h": 5}),
        ("pyramid:n4r5h8", "pyramid", {"n": 4, "r": 5, "h": 8}),
        ("chamfer:1x45", "chamfer", {"size": 1, "angle": 45}),
    ],
)
def test_parse_aliases(config: str, alias: str, params: dict[str, float]) -> None:
    spec = parse(config)
    assert spec.alias == alias
    assert spec.params == pytest.approx(params)


def test_parse_distinguishes_R_from_r() -> None:
    spec = parse("torus:R10r2")
    assert spec.params["R"] == 10
    assert spec.params["r"] == 2


def test_parse_rejects_rb_rt_ambiguity() -> None:
    # rb/rt must out-match r — frustum:n6rb4rt2h5 already covers this.
    spec = parse("frustum:n6rb4rt2h5")
    assert spec.params == {"n": 6, "rb": 4, "rt": 2, "h": 5}


def test_parse_decimal() -> None:
    spec = parse("cyl:r2.5h12")
    assert spec.params["r"] == pytest.approx(2.5)


def test_parse_missing_key() -> None:
    with pytest.raises(DslError, match="missing"):
        parse("cyl:r3")


def test_parse_unknown_shape() -> None:
    with pytest.raises(DslError, match="unknown shape"):
        parse("widget:r3h4")


def test_parse_unexpected_key() -> None:
    with pytest.raises(DslError, match="unexpected"):
        parse("sphere:r5h2")


def test_parse_no_colon() -> None:
    with pytest.raises(DslError):
        parse("cylr3h12")


def test_parse_ngon_requires_int_ge_3() -> None:
    with pytest.raises(DslError, match="n must be"):
        parse("ngon:n2r5h3")


def test_parse_trailing_garbage() -> None:
    with pytest.raises(DslError, match="unexpected text"):
        parse("cyl:r3h12xyz")


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


def test_build_cyl() -> None:
    c = build_config("cyl:r3h12")
    assert isinstance(c, CircularFrustum)
    assert (c.rb, c.rt, c.h) == (3, 3, 12)


def test_build_cone_tip_is_zero_top() -> None:
    c = build_config("cone:r4h8")
    assert isinstance(c, CircularFrustum)
    assert c.rt == 0


def test_build_sphere_and_torus() -> None:
    assert isinstance(build_config("sphere:r5"), Sphere)
    assert isinstance(build_config("torus:R10r2"), Torus)


def test_build_box_dimensions() -> None:
    b = build_config("box:w40d20h10")
    assert isinstance(b, PolyFrustum)
    lo, hi = b.aabb_local()
    assert math.isclose(hi[0] - lo[0], 40)
    assert math.isclose(hi[1] - lo[1], 20)
    assert math.isclose(hi[2] - lo[2], 10)


def test_build_pyramid_narrows() -> None:
    p = build_config("pyramid:n4r5h8")
    assert p.contains_local(vec3(0, 0, 7.9))
    assert not p.contains_local(vec3(4, 4, 7.9))


def test_build_chamfer_needs_anchor() -> None:
    with pytest.raises(DslError, match="anchor"):
        build(parse("chamfer:1x45"))


# ---------------------------------------------------------------------------
# round-trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "config",
    [
        "box:w40d20h10",
        "cyl:r3h12",
        "cone:r4h8",
        "tcone:rb4rt2h8",
        "sphere:r5",
        "torus:R10r2",
        "ngon:n6r5h10",
        "frustum:n6rb4rt2h5",
        "pyramid:n4r5h8",
        "chamfer:1x45",
        "cyl:r2.5h12",
    ],
)
def test_round_trip(config: str) -> None:
    assert format_spec(parse(config)) == config


def test_format_spec_compacts_floats() -> None:
    assert format_spec(ShapeSpec("cyl", {"r": 3.0, "h": 12.0})) == "cyl:r3h12"
