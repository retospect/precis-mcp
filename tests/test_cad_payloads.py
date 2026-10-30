"""Prim payloads — round (``rd``) and draft (``draft``/``pullz``) on
``box``/``cyl`` (gr524645 + gr529022).

The ruling: the cad DSL primitives carry these features as explicit
payloads; mold ops never derive them. Pins the draft geometry (a drafted
box's pull-side face is smaller by ``h·tan(draft)`` per side, a drafted
cylinder is a :class:`~precis.cad.primitives.CircularFrustum` with the
right radii), the refusals (negative or ≥ 45° drafts carry ``next=``;
only ``box``/``cyl`` take the payload; the taper family is told to use
``rb``/``rt``), the mold cavity carrying a part's rounding (and the
payloads through the shrink-scale), and both exports honouring the
payloads (OpenSCAD renders draft analytically and ``rd`` via minkowski,
with the ``// WARNING:`` header for what it cannot render).
"""

from __future__ import annotations

import math

import pytest

from precis.cad import export as cad_export
from precis.cad.dsl import (
    DRAFT_KEY,
    ROUND_KEY,
    DslError,
    ShapeSpec,
    build_config,
    draft_cyl_ends,
    format_spec,
    parse,
    scale_config,
)
from precis.cad.primitives import CircularFrustum, PolyFrustum
from precis.cad.scene import NodeSpec, SceneSpec, build_design, expand_instances


def _ring_area(ring: list) -> float:
    """Shoelace area of a 3-D planar ring's x/y projection."""
    return (
        abs(
            sum(
                ring[i][0] * ring[(i + 1) % len(ring)][1]
                - ring[(i + 1) % len(ring)][0] * ring[i][1]
                for i in range(len(ring))
            )
        )
        / 2.0
    )


# ── draft geometry ────────────────────────────────────────────────────


def test_drafted_box_top_face_is_smaller_by_the_expected_amount() -> None:
    prim = build_config("box:w20mmd10mmh5mmdraft2deg", require_units=True)
    assert isinstance(prim, PolyFrustum)
    inset = 0.005 * math.tan(math.radians(2.0))
    area_bottom = _ring_area(prim._bottom)
    area_top = _ring_area(prim._top)
    # bottom face is the full 20 × 10 section; the top (the pull-side
    # end, +z default) loses h·tan(draft) on each of its four sides.
    assert area_bottom == pytest.approx(0.02 * 0.01)
    assert area_top == pytest.approx((0.02 - 2.0 * inset) * (0.01 - 2.0 * inset))
    assert area_top < area_bottom
    # and the box is still base-at-z=0, full height
    lo, hi = prim.aabb_local()
    assert lo[2] == pytest.approx(0.0)
    assert hi[2] == pytest.approx(0.005)


def test_drafted_box_pullz_minus_tapers_toward_the_base() -> None:
    prim = build_config("box:w20mmd10mmh5mmdraft2degpullz-1", require_units=True)
    assert isinstance(prim, PolyFrustum)
    inset = 0.005 * math.tan(math.radians(2.0))
    assert _ring_area(prim._bottom) == pytest.approx(
        (0.02 - 2.0 * inset) * (0.01 - 2.0 * inset)
    )
    assert _ring_area(prim._top) == pytest.approx(0.02 * 0.01)


def test_drafted_cylinder_is_a_frustum_with_the_right_radii() -> None:
    prim = build_config("cyl:r5mmh10mmdraft3deg", require_units=True)
    assert isinstance(prim, CircularFrustum)
    expected = 0.005 - 0.01 * math.tan(math.radians(3.0))
    assert (prim.rb, prim.rt, prim.h) == pytest.approx((0.005, expected, 0.01))
    # pullz-1 swaps the taper ends
    flipped = build_config("cyl:r5mmh10mmdraft3degpullz-1", require_units=True)
    assert isinstance(flipped, CircularFrustum)
    assert (flipped.rb, flipped.rt) == pytest.approx((expected, 0.005))


def test_sharp_box_and_cyl_build_unchanged() -> None:
    box = build_config("box:w20mmd10mmh5mm", require_units=True)
    assert isinstance(box, PolyFrustum)
    assert _ring_area(box._top) == pytest.approx(0.02 * 0.01)
    cyl = build_config("cyl:r5mmh10mm", require_units=True)
    assert isinstance(cyl, CircularFrustum)
    assert (cyl.rb, cyl.rt) == (0.005, 0.005)


# ── refusals ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "config",
    [
        "box:w20mmd10mmh5mmdraft-1deg",
        "box:w20mmd10mmh5mmdraft0deg",
        "box:w20mmd10mmh5mmdraft45deg",
        "box:w20mmd10mmh5mmdraft50deg",
        "cyl:r5mmh10mmdraft-2deg",
        "cyl:r5mmh10mmdraft45deg",
    ],
)
def test_invalid_draft_refused_with_next(config: str) -> None:
    with pytest.raises(DslError) as exc_info:
        parse(config, require_units=True)
    assert exc_info.value.next, f"no next= on the refusal for {config}"
    assert "draft" in str(exc_info.value)


def test_over_deep_draft_refused_naming_the_dimension() -> None:
    # inset = h·tan(draft) reaching ½·min(w, d) would erase the pull-side face
    with pytest.raises(DslError, match="erasing the pull-side face"):
        parse("box:w10mmd10mmh40mmdraft30deg", require_units=True)
    with pytest.raises(DslError, match="erasing the pull-side face"):
        parse("cyl:r5mmh40mmdraft30deg", require_units=True)


def test_draft_refused_on_other_aliases() -> None:
    for config, who in [
        ("tcone:rb4mmrt2mmh8mmdraft2deg", "rb/rt"),
        ("frustum:n6rb4mmrt2mmh5mmdraft2deg", "rb/rt"),
        ("cone:r4mmh8mmdraft2deg", "rb/rt"),
        ("hex:r5mmh10mmdraft2deg", "box and cylinder"),
        ("sphere:r5mmdraft2deg", "no side faces"),
        ("torus:R10mmr2mmdraft2deg", "no side faces"),
    ]:
        with pytest.raises(DslError) as exc_info:
            parse(config, require_units=True)
        assert who in str(exc_info.value)


def test_pull_without_draft_and_x_y_pull_refused() -> None:
    with pytest.raises(DslError, match="no draft angle was given"):
        parse("box:w20mmd10mmh5mmpullz1", require_units=True)
    with pytest.raises(DslError, match="z-axis only"):
        parse("box:w20mmd10mmh5mmdraft2degpullx1", require_units=True)
    with pytest.raises(DslError, match="pullz1 or pullz-1"):
        parse("box:w20mmd10mmh5mmdraft2degpullz3", require_units=True)


def test_round_and_draft_compose_rounding_the_drafted_solid() -> None:
    from precis.cad.primitives import Rounded

    prim = build_config("box:w20mmd10mmh5mmdraft2degpullz-1rd1mm", require_units=True)
    assert isinstance(prim, Rounded)
    assert prim.lift == pytest.approx(0.001)
    # the inner body is the exact inward offset: its boundary sits at
    # distance rd from the sharp drafted solid everywhere
    sharp = build_config("box:w20mmd10mmh5mmdraft2degpullz-1", require_units=True)
    inner = prim.inner
    assert isinstance(inner, PolyFrustum)
    for _normal, ring in inner._face_polys:
        for i in range(len(ring)):
            a, b = ring[i], ring[(i + 1) % len(ring)]
            for t in (0.0, 0.5):
                q = a + t * (b - a)
                from precis.cad.vec import vec3

                qs = vec3(float(q[0]), float(q[1]), float(q[2] + prim.lift))
                assert abs(abs(sharp.distance_local(qs)) - 0.001) < 1e-9


# ── storage round-trip and scaling ────────────────────────────────────


def test_draft_round_trips_through_canonical_text() -> None:
    text = "box:w0.02d0.01h0.005draft0.03490658503988659pullz-1rd0.001"
    spec = parse(text)
    assert format_spec(spec) == text
    assert parse(format_spec(spec)).params == spec.params
    # scale_config rescales lengths, never the angle or the pull sign
    scaled = parse(scale_config(text, 2.0))
    assert scaled.params["w"] == pytest.approx(0.04)
    assert scaled.params[DRAFT_KEY] == pytest.approx(spec.params[DRAFT_KEY])
    assert scaled.params["pullz"] == -1.0
    assert scaled.params[ROUND_KEY] == pytest.approx(0.002)


# ── mold ops carry the payloads into the cavity ───────────────────────


def _mold_piece(config: str, **kwargs) -> SceneSpec:
    from precis.cad.mold import cut_mold

    spec = SceneSpec(
        nodes=[NodeSpec("body", "add", config, "part")], components=["part"]
    )
    design = build_design(spec)
    cut = cut_mold(expand_instances(spec).nodes, design, **kwargs)
    return cut.pieces[0]


def test_mold_cavity_keeps_a_rounded_parts_fillets() -> None:
    from precis.cad.fold import expr_sdf
    from precis.cad.vec import vec3

    def cavity_sdf_at(config: str, pt: tuple[float, float, float]) -> float:
        piece = build_design(_mold_piece(config, layout="axial", margin=0.004))
        return min(
            expr_sdf(e, vec3(*pt), piece.instances) for e in piece.components.values()
        )

    # at (9.5, 9.5, 10) mm a sharp 20 mm box is solid part (no cavity
    # material); a rounded one's fillet pulls its surface in, so the
    # cavity shows material there — the rounding rode into the cavity
    assert cavity_sdf_at("box:w0.02d0.02h0.02", (0.0095, 0.0095, 0.01)) > 0.0
    assert cavity_sdf_at("box:w0.02d0.02h0.02rd0.002", (0.0095, 0.0095, 0.01)) < 0.0


def test_mold_cavity_carries_payloads_through_the_shrink_scale() -> None:
    piece = _mold_piece(
        "box:w0.02d0.02h0.02draft0.03490658503988659rd0.002",
        layout="axial",
        shrink=0.1,
    )
    cavity = [n for n in piece.nodes if n.op == "cut"]
    assert len(cavity) == 1
    params = parse(cavity[0].config).params
    # both payloads ride the shrink-scaled cavity config verbatim
    assert params[DRAFT_KEY] == pytest.approx(0.03490658503988659)
    assert params[ROUND_KEY] == pytest.approx(0.0022)
    assert params["w"] == pytest.approx(0.022)


# ── exports honour the payloads ───────────────────────────────────────


def _scad(config: str) -> str:
    spec = SceneSpec(
        nodes=[NodeSpec("body", "add", config, "part")], components=["part"]
    )
    return cad_export.to_openscad(spec, name="t")


def test_openscad_renders_draft_analytically() -> None:
    scad = _scad("box:w0.02d0.01h0.005draft0.03490658503988659")
    assert "linear_extrude(height=5, scale=" in scad
    assert "square([20,10], center=true);" in scad
    scad = _scad("cyl:r0.005h0.01draft0.05235987755982989")
    assert "cylinder(h=10, r1=5, r2=4.47592, $fn=64);" in scad
    assert "WARNING" not in scad


def test_openscad_renders_rounded_leaf_via_minkowski() -> None:
    scad = _scad("box:w0.02d0.01h0.005rd0.001")
    assert "minkowski() {" in scad
    assert "translate([0,0,1]) translate([-9,-4,0]) cube([18,8,3]);" in scad
    assert "sphere(r=1, $fn=64);" in scad
    assert "WARNING" not in scad


def test_openscad_sharp_design_has_no_payload_markup() -> None:
    scad = _scad("box:w0.02d0.01h0.005")
    assert "minkowski" not in scad and "WARNING" not in scad
    assert "linear_extrude" not in scad


def test_openscad_nonconvex_roundable_warns_and_keeps_sharp() -> None:
    # Defensive: every rd-accepting alias today is convex, so no stored
    # design can reach this — a non-convex roundable must degrade to the
    # sharp leaf + a // WARNING: header, never a silently wrong minkowski.
    from precis.cad.export import _leaf_scad
    from precis.cad.scene import parse_source

    node = parse_source("t add torus:R8mmr2mm").nodes[0]
    warnings: list[str] = []
    out = _leaf_scad(
        node, ShapeSpec("torus", {"R": 8.0, "r": 2.0, "rd": 1.0}), warnings
    )
    assert "rotate_extrude(" in out  # the sharp torus, rd dropped
    assert "minkowski" not in out
    assert len(warnings) == 1
    assert "torus" in warnings[0] and "rd=1" in warnings[0]


@pytest.mark.skipif(not cad_export.manifold_available(), reason="manifold3d extra")
def test_mesh_export_honours_draft_analytically() -> None:
    import numpy as np

    from precis.cad.tessellate import mesh_config

    v, _t = mesh_config("box:w0.02d0.01h0.005draft0.03490658503988659")
    inset = 0.005 * math.tan(math.radians(2.0))
    bottom = v[v[:, 2] < 0.0025]
    top = v[v[:, 2] > 0.0025]
    assert np.abs(bottom[:, 0]).max() == pytest.approx(0.01)
    assert np.abs(top[:, 0]).max() == pytest.approx(0.01 - inset)


def test_draft_helpers_match_the_built_prims() -> None:
    p = parse("cyl:r0.005h0.01draft0.05235987755982989pullz-1").params
    rb, rt = draft_cyl_ends(p)
    prim = build_config("cyl:r0.005h0.01draft0.05235987755982989pullz-1")
    assert isinstance(prim, CircularFrustum)
    assert (prim.rb, prim.rt) == pytest.approx((rb, rt))
