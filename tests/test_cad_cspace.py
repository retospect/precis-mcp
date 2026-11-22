"""Continuous sweep contracts, independent of the store and handler."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import replace

import numpy as np
import pytest

from precis.cad.bulk import expr_aabb
from precis.cad.cspace import SweepLimit, collision_windows, motion_bounds
from precis.cad.graph import Design
from precis.cad.scene import build_design, expand_instances, parse_source
from precis.cad.vec import Transform


def _oracle_walk(
    function: Callable[[float], float],
    *,
    speed: float = 1.0,
    resolution: float = 0.001,
    budget: int = 10000,
    limits: tuple[float, float] = (0.0, 1.0),
):
    # Overlapping bounds deliberately force the injected narrow phase.
    design = build_design(
        parse_source(
            "component a\na add box:w1md1mh1m\ncomponent b\nb add box:w1md1mh1m"
        )
    )
    current = 0.0

    def build(q: float) -> Design:
        nonlocal current
        current = q
        return design

    return collision_windows(
        build,
        ("a", "b"),
        limits,
        speed=speed,
        resolution=resolution,
        max_evaluations=budget,
        gap=lambda _d, _a, _b: function(current),
    )


def test_window_between_old_samples_and_two_disconnected_windows():
    result = _oracle_walk(lambda q: min(abs(q - 0.31) - 0.012, abs(q - 0.72) - 0.02))
    assert len(result.windows) == 2
    for actual, expected in zip(
        result.windows, ((0.298, 0.322), (0.70, 0.74)), strict=True
    ):
        assert actual == pytest.approx(expected, abs=result.tolerance)
    assert not result.contact_throughout


@pytest.mark.parametrize(
    "limits,expected",
    [((0.3, 0.32), (0.3, 0.32)), ((0.3, 0.5), (0.3, 0.4)), ((0.0, 0.3), (0.2, 0.3))],
)
def test_windows_clipped_to_range(limits, expected):
    result = _oracle_walk(lambda q: abs(q - 0.3) - 0.1, limits=limits)
    assert len(result.windows) == 1
    assert result.windows[0] == pytest.approx(expected, abs=result.tolerance)


def test_persistent_contact_sampling_is_bounded_and_not_a_collision():
    result = _oracle_walk(lambda _q: 0.0, resolution=0.000001, budget=64)
    assert result.contact_throughout
    assert result.windows == ()
    assert result.sampled_contact
    assert result.evaluations == 64
    assert result.exact_evaluations == result.evaluations


def test_contact_pair_can_develop_interference_between_coarse_probes():
    result = _oracle_walk(lambda q: min(0.0, abs(q - 0.317) - 0.008))
    assert not result.contact_throughout
    assert len(result.windows) == 1
    assert result.windows[0] == pytest.approx((0.309, 0.325), abs=result.tolerance)


def test_contact_sampling_does_not_certify_between_probes():
    # This narrow excursion lies between all 64 probes. The result must
    # explicitly say sampled, even though its depth exceeds resolution.
    result = _oracle_walk(lambda q: min(0.0, abs(q - 0.31) - 0.002))
    assert result.contact_throughout
    assert result.sampled_contact
    assert result.windows == ()


def test_contact_sampling_still_obeys_the_budget():
    with pytest.raises(SweepLimit, match="exhausted 8 exact gap calls"):
        _oracle_walk(lambda _q: 0.0, budget=8)


def test_contact_band_is_not_reported_as_deep_interference():
    result = _oracle_walk(lambda q: -0.0005 * (1 + math.sin(q)) / 2)
    assert result.windows == ()
    assert result.contact_throughout


def test_cap_raises_instead_of_returning_a_partial_window():
    calls = 0

    def gap(q: float) -> float:
        nonlocal calls
        calls += 1
        return abs(q - 0.3) - 0.1

    with pytest.raises(SweepLimit, match="exhausted 2 exact gap calls"):
        _oracle_walk(gap, budget=2)
    assert calls == 2


def test_separated_boxes_never_call_exact_gap():
    design = build_design(
        parse_source(
            "component a\na add box:w1md1mh1m\ncomponent b\nb add box:w1md1mh1m @10m,0m,0m"
        )
    )

    def unexpected_gap(_design: Design, _a: str, _b: str) -> float:
        pytest.fail("disjoint AABBs must supply the gap lower bound")

    result = collision_windows(
        lambda _q: design,
        ("a", "b"),
        (0, 1),
        speed=1,
        resolution=0.001,
        max_evaluations=1,
        gap=unexpected_gap,
    )
    assert result.windows == ()
    assert result.evaluations == 2
    assert result.exact_evaluations == 0


_DISC_RIG = """
component plate
base add box:w80mmd80mmh2mm
component disc
rotor add cyl:r30mmh2mm @0mm,0mm,2.5mm
port axis of:disc
joint disc revolute at:axis limits:-180deg..180deg
"""


def test_clear_disc_sweep_passes_with_default_exact_budget(monkeypatch):
    import precis.handlers.cad as handler

    spec = parse_source(_DISC_RIG)
    cad = object.__new__(handler.CadHandler)
    cad._resolve = lambda _slug: spec
    results = []

    def measured(*args, **kwargs):
        result = collision_windows(*args, **kwargs)
        results.append(result)
        return result

    def unexpected_gap(*_args, **_kwargs):
        pytest.fail("the 0.5 mm gap keeps the disc and plate AABBs disjoint")

    monkeypatch.setattr(handler, "collision_windows", measured)
    monkeypatch.setattr(handler, "sdf_clearance", unexpected_gap)
    response = cad._render_sweep(spec, {})
    assert "up to 512 exact gap calls/pair" in response.body
    assert "no interference resolved in the travel" in response.body
    assert len(results) == 1
    assert results[0].exact_evaluations == 0
    assert results[0].evaluations > 512


def test_cheap_probe_cap_refuses_with_a_useful_next_step(monkeypatch):
    import precis.cad.cspace as cspace
    import precis.handlers.cad as handler
    from precis.errors import BadInput

    spec = parse_source(_DISC_RIG)
    cad = object.__new__(handler.CadHandler)
    cad._resolve = lambda _slug: spec
    monkeypatch.setattr(cspace, "AABB_PROBE_LIMIT", 2)
    with pytest.raises(BadInput, match="exhausted 2 cheap AABB-only probes") as exc:
        cad._render_sweep(spec, {"n": 1000000})
    assert "range not covered — no partial verdict" in str(exc.value)
    assert "narrow the joint limits:" in str(exc.value.next)
    assert "increase args.n" not in str(exc.value.next)


@pytest.mark.parametrize(
    "value,windows,contact", [(1, (), False), (-1, ((0, 1),), False), (0, (), True)]
)
def test_stationary_pair_needs_one_evaluation(value, windows, contact):
    result = _oracle_walk(lambda _q: value, speed=0, budget=1)
    assert result.windows == windows
    assert result.contact_throughout == contact
    assert result.evaluations == 1


@pytest.mark.parametrize("scale", [1e-9, 1.0, 1e9])
def test_window_resolution_is_scale_relative(scale):
    result = _oracle_walk(
        lambda q: scale * (abs(q - 0.3) - 0.1), speed=scale, resolution=scale * 0.001
    )
    assert result.windows[0] == pytest.approx((0.2, 0.4), abs=0.001)


def test_nonfinite_oracle_refuses():
    with pytest.raises(ValueError, match="non-finite gap"):
        _oracle_walk(lambda _q: math.nan)


@pytest.mark.parametrize("union", [False, True])
def test_sweep_unbounded_component_refuses_with_next_step(monkeypatch, union):
    import precis.handlers.cad as handler
    from precis.cad.primitives import HalfSpace
    from precis.errors import BadInput

    spec = parse_source("""
component arm
bar add box:w10mmd2mmh2mm
port pivot of:arm
joint arm revolute at:pivot limits:-180deg..180deg
""")
    original = handler.build_design

    def with_unbounded_component(expanded):
        design = original(expanded)
        plane = design.prim(
            "plane", HalfSpace(point=np.zeros(3), normal=np.array([0, 0, 1]))
        )
        material = design.merge(design.components["arm"], plane) if union else plane
        design.add_component("unbounded", material)
        return design

    monkeypatch.setattr(handler, "build_design", with_unbounded_component)
    cad = object.__new__(handler.CadHandler)
    cad._resolve = lambda _slug: spec
    with pytest.raises(
        BadInput, match="component 'unbounded' has no finite motion bound"
    ) as exc:
        cad._render_sweep(spec, {})
    assert "bounded finite geometry" in str(exc.value.next)


@pytest.mark.parametrize("listing", ["mates_of", "joints_of", "couples_of"])
def test_sweep_malformed_joint_listing_refuses_with_next_step(monkeypatch, listing):
    import precis.handlers.cad as handler
    from precis.cad.scene import SceneError
    from precis.errors import BadInput

    def malformed(_spec):
        raise SceneError("malformed stored joint")

    monkeypatch.setattr(handler, listing, malformed)
    cad = object.__new__(handler.CadHandler)
    with pytest.raises(BadInput, match="malformed stored joint") as exc:
        cad._render_sweep(parse_source("component a\na add box:w1md1mh1m"), {})
    assert "check joint, mate and coupling declarations" in str(exc.value.next)


@pytest.mark.parametrize("exact", [False, True])
def test_field_gap_cannot_silently_skip_a_collision(exact):
    from precis.cad.primitives import Field
    from precis.errors import Unsupported
    from precis.handlers.cad import CadHandler

    # The two wide field boxes overlap at q=0. Scaled distance samples
    # would produce a step past the entire q=1..5 collision window if
    # treated as a 1-Lipschitz distance. The exact flag is metadata only.
    points = np.indices((13, 13, 13), dtype=float) - 6
    field = Field(
        100 * (np.linalg.norm(points, axis=0) - 1), 1, np.full(3, -6.0), exact=exact
    )
    spec = parse_source("""
component fixed
a add field:abc123abc123 @3m,0m,0m
component moving
b add field:abc123abc123
port axis of:moving rot:0deg,90deg,0deg
joint moving prismatic at:axis limits:0m..10m
""")
    spec = replace(spec, field_loader=lambda _ref: field)
    cad = object.__new__(CadHandler)
    cad._resolve = lambda _slug: spec
    with pytest.raises(Unsupported, match="sampled field") as exc:
        cad._render_sweep(spec, {})
    assert "analytic primitives" in str(exc.value.next)


def test_motion_bounds_include_negative_coupling_chain_and_rigid_descendants():
    sub = parse_source(
        "component body\nbar add box:w10mmd2mmh2mm @5mm,0mm,0mm\nport pivot of:body"
    )
    spec = parse_source("""
component a
bar add box:w10mmd2mmh2mm @5mm,0mm,0mm
port pivot of:a
port end @10mm,0mm,0mm of:a
joint a revolute at:pivot limits:0deg..90deg
use sub as b
joint b.pivot to end revolute limits:-180deg..180deg
use sub as c
mate c.pivot to b.pivot
gear a to b ratio:-2
""")
    frames: dict[str, Transform] = {}
    initial = build_design(
        expand_instances(spec, lambda _slug: sub, {"a": 0.0}, joint_frames=frames)
    )
    bounds = motion_bounds(spec, initial, frames, "a", math.pi / 2)
    assert bounds["a"].group == ("a",)
    assert bounds["b.body"].group == ("a", "b")
    assert bounds["c.body"].group == bounds["b.body"].group
    assert bounds["b.body"].speed > 2 * math.hypot(0.01, 0.001)
    for q in np.linspace(0, math.pi / 2, 37):
        posed = build_design(expand_instances(spec, lambda _slug: sub, {"a": float(q)}))
        for name, bound in bounds.items():
            low, high = expr_aabb(posed, posed.components[name])
            assert np.all(low >= bound.envelope[0] - 1e-12)
            assert np.all(high <= bound.envelope[1] + 1e-12)


@pytest.mark.parametrize(
    "kind,options,travel",
    [
        ("prismatic", "limits:0mm..10mm", 0.01),
        ("screw", "pitch:10mm limits:0deg..360deg", 2 * math.pi),
        ("cylindrical", "limits:0deg..360deg", 2 * math.pi),
    ],
)
def test_motion_bounds_for_tilted_slide_screw_and_cylindrical(kind, options, travel):
    spec = parse_source(f"""
component arm
bar add box:w10mmd2mmh2mm @5mm,0mm,0mm
port pivot of:arm rot:90deg,0deg,0deg
joint arm {kind} at:pivot {options}
""")
    frames: dict[str, Transform] = {}
    state: dict[str, object] = {"arm": [0.0, 0.0] if kind == "cylindrical" else 0.0}
    initial = build_design(expand_instances(spec, state=state, joint_frames=frames))
    bound = motion_bounds(spec, initial, frames, "arm", travel)["arm"]
    if kind == "prismatic":
        assert bound.speed == 1
    for q in np.linspace(0, travel, 17):
        state = {"arm": [float(q), 0.0] if kind == "cylindrical" else float(q)}
        design = build_design(expand_instances(spec, state=state))
        low, high = expr_aabb(design, design.components["arm"])
        assert np.all(low >= bound.envelope[0] - 1e-12)
        assert np.all(high <= bound.envelope[1] + 1e-12)
