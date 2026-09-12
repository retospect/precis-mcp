"""config mini-DSL — parse / build / round-trip tests."""

from __future__ import annotations

import math
import re

import pytest

from precis.cad.dsl import (
    DslError,
    ShapeSpec,
    build_config,
    format_spec,
    parse,
)
from precis.cad.primitives import (
    CircularFrustum,
    HalfSpace,
    PolyFrustum,
    Sphere,
    Torus,
)
from precis.cad.vec import vec3
from precis.utils.units import UnitRequiredError


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


def test_parse_scientific_notation() -> None:
    """nm-scale dims are legal without ten zeros (gr332020): numbers take
    an optional exponent — unambiguous, no DSL key is ``e``."""
    spec = parse("box:w3e-9d1.5E-9h3e-10")
    assert spec.params["w"] == pytest.approx(3e-9)
    assert spec.params["d"] == pytest.approx(1.5e-9)
    assert spec.params["h"] == pytest.approx(3e-10)
    assert parse("cyl:r2e3h1e+2").params == {"r": 2000.0, "h": 100.0}
    assert parse("chamfer:1e-6x45").params["size"] == pytest.approx(1e-6)


def test_parse_bare_exponent_is_garbage() -> None:
    with pytest.raises(DslError, match="unexpected text"):
        parse("cyl:r3eh12")


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


def test_no_colon_error_examples_parse_under_boundary_grammar() -> None:
    """The malformed-config error's own quoted ``e.g.`` examples must parse
    cleanly under boundary mode (``require_units=True``) — this exact
    message is what an agent sees on a bad node line, so a stale unitless
    example here would be self-defeating (units-cutover prompt-surface
    audit, item 5/2.2 — same class as the retired ``'cyl:r3h12'`` bug)."""
    with pytest.raises(DslError) as exc:
        parse("cylr3h12")
    msg = str(exc.value)
    eg = msg.split("e.g.", 1)[1]
    examples = re.findall(r"'([^']+)'", eg)
    assert examples, f"no quoted examples found in: {msg!r}"
    for example in examples:
        parse(example, require_units=True)  # must not raise


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


def test_build_chamfer_is_local_frame_halfspace() -> None:
    # size=2, angle=45° (canonical-mode params are radians per the angle
    # ruling): n̂ = (sin45°, 0, cos45°); point = -size·n̂; constructed
    # HalfSpace negates the normal (material on the +n̂ side).
    c = build_config(f"chamfer:2x{math.radians(45)}")
    assert isinstance(c, HalfSpace)
    s2 = math.sqrt(2.0) / 2.0
    assert c.point == pytest.approx(vec3(-2 * s2, 0.0, -2 * s2))
    assert c.normal == pytest.approx(vec3(-s2, 0.0, -s2))


def test_build_chamfer_zero_angle_is_horizontal_plane_at_minus_size() -> None:
    # A=0 → n̂ = +z, plane at local z = -size, material at z >= -size.
    c = build_config("chamfer:3x0")
    assert isinstance(c, HalfSpace)
    assert c.point == pytest.approx(vec3(0.0, 0.0, -3.0))
    assert c.normal == pytest.approx(vec3(0.0, 0.0, -1.0))
    # material is the +n̂ side: local z >= -size
    assert c.contains_local(vec3(0, 0, -2.9))  # above the plane: material
    assert not c.contains_local(vec3(0, 0, -3.1))  # below the plane: air


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
        "box:w3e-09d3e-09h3e-10",
    ],
)
def test_round_trip(config: str) -> None:
    assert format_spec(parse(config)) == config


def test_format_spec_compacts_floats() -> None:
    assert format_spec(ShapeSpec("cyl", {"r": 3.0, "h": 12.0})) == "cyl:r3h12"


def test_format_spec_nm_scale_not_zero() -> None:
    """Sub-µm dims render in scientific notation — the 6-decimal rounding
    used for ordinary magnitudes would collapse them to '0' (gr332020)."""
    assert format_spec(ShapeSpec("sphere", {"r": 3e-9})) == "sphere:r3e-09"


# ---------------------------------------------------------------------------
# boundary mode (require_units=True) — units-policy-cutover two-mode contract
# ---------------------------------------------------------------------------


def test_boundary_mode_converts_units_to_si_metres() -> None:
    spec = parse("box:w40mmd20mmh10mm", require_units=True)
    assert spec.params == pytest.approx({"w": 0.04, "d": 0.02, "h": 0.01})


def test_boundary_mode_accepts_the_acceptance_criteria_unit_set() -> None:
    assert parse("sphere:r1000mm", require_units=True).params["r"] == pytest.approx(1.0)
    assert parse("sphere:r1cm", require_units=True).params["r"] == pytest.approx(0.01)
    assert parse("sphere:r1m", require_units=True).params["r"] == pytest.approx(1.0)
    assert parse("sphere:r1km", require_units=True).params["r"] == pytest.approx(1000.0)
    assert parse("sphere:r1nm", require_units=True).params["r"] == pytest.approx(1e-9)
    assert parse("sphere:r1Å", require_units=True).params["r"] == pytest.approx(1e-10)
    assert parse("sphere:r1in", require_units=True).params["r"] == pytest.approx(0.0254)
    assert parse("sphere:r1ft", require_units=True).params["r"] == pytest.approx(0.3048)


def test_boundary_mode_rejects_bare_number_with_hint() -> None:
    with pytest.raises(UnitRequiredError) as exc_info:
        parse("box:w40d20h10", require_units=True)
    err = exc_info.value
    assert err.value == 40.0
    assert "state units" in err.hint


def test_boundary_mode_count_key_forbids_a_unit() -> None:
    with pytest.raises(DslError, match="dimensionless count"):
        parse("ngon:n6mmr5mmh10mm", require_units=True)


def test_boundary_mode_count_key_stays_bare() -> None:
    spec = parse("ngon:n6r5mmh10mm", require_units=True)
    assert spec.params["n"] == 6


def test_boundary_mode_chamfer_requires_units_on_both_size_and_angle() -> None:
    spec = parse("chamfer:1mmx45deg", require_units=True)
    assert spec.params == pytest.approx({"size": 1e-3, "angle": math.radians(45.0)})


def test_boundary_mode_chamfer_accepts_radians_on_angle() -> None:
    spec = parse(f"chamfer:1mmx{math.pi / 4}rad", require_units=True)
    assert spec.params == pytest.approx({"size": 1e-3, "angle": math.pi / 4})


def test_boundary_mode_chamfer_bare_size_rejected_with_hint() -> None:
    with pytest.raises(UnitRequiredError) as exc_info:
        parse("chamfer:1x45", require_units=True)
    assert exc_info.value.value == 1.0


def test_boundary_mode_chamfer_bare_angle_rejected_with_hint() -> None:
    # Size carries a unit, angle doesn't — angle is checked second, so this
    # is the angle-specific rejection (not the size one above).
    with pytest.raises(UnitRequiredError) as exc_info:
        parse("chamfer:1mmx45", require_units=True)
    assert exc_info.value.value == 45.0
    assert exc_info.value.dimension == "angle"


def test_boundary_mode_build_config_threads_the_flag() -> None:
    c = build_config("cyl:r3mmh12mm", require_units=True)
    assert isinstance(c, CircularFrustum)
    assert (c.rb, c.rt, c.h) == pytest.approx((0.003, 0.003, 0.012))


def test_canonical_mode_is_the_default_and_unaffected() -> None:
    """Storage reload (se/nm envelope columns, cad's own already-stored
    text) never requires units — this is the permissive-forever mode."""
    assert parse("box:w40d20h10").params == {"w": 40, "d": 20, "h": 10}
    assert build_config("cyl:r3h12").__class__ is CircularFrustum
