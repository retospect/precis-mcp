"""Design source parsing + Design building (ADR 0041 §3, §11)."""

from __future__ import annotations

import math

import pytest

from precis.cad.scene import (
    NodeSpec,
    SceneError,
    build_design,
    parse_source,
    spec_to_source,
)
from precis.cad.vec import vec3

_FLANGE = """
# a flange
component flange
plate     add  cyl:r25h8
hub_bore  cut  cyl:r8h10    @0,0,-1
bolts     cut  cyl:r2.5h10  @18,0,-1  polar:n6r18
"""


def test_parse_basic_counts() -> None:
    spec = parse_source(_FLANGE)
    assert spec.components == ["flange"]
    assert [n.name for n in spec.nodes] == ["plate", "hub_bore", "bolts"]
    assert spec.nodes[1].op == "cut"
    assert spec.nodes[1].loc == (0.0, 0.0, -1.0)
    assert spec.nodes[2].pattern == {"kind": "polar", "n": 6.0, "r": 18.0}


def test_default_component() -> None:
    spec = parse_source("a add cyl:r2h2")
    assert spec.components == ["part"]
    assert spec.nodes[0].component == "part"


def test_meta_roundtrip() -> None:
    spec = parse_source(_FLANGE)
    for n in spec.nodes:
        rebuilt = NodeSpec.from_meta(n.name, n.to_meta())
        assert rebuilt == n


def test_bad_op_rejected() -> None:
    with pytest.raises(SceneError):
        parse_source("a frobnicate cyl:r2h2")


def test_bad_config_rejected() -> None:
    with pytest.raises(ValueError):
        parse_source("a add notashape:99")


def test_duplicate_name_rejected() -> None:
    with pytest.raises(SceneError):
        parse_source("a add cyl:r2h2\na cut cyl:r1h3")


def test_build_design_flange_probe() -> None:
    # The built design must behave like the hand-built flange: solid in the
    # plate, void in the bore.
    design = build_design(parse_source(_FLANGE))
    assert "flange" in design.components
    # a point in the plate annulus (r=23: outside the bolt circle r18±2.5,
    # outside the bore r8, inside the plate r25)
    assert design.classify_point(vec3(23, 0, 4), component="flange").inside
    # a point in the central bore is carved away
    assert not design.classify_point(vec3(0, 0, 4), component="flange").inside


def test_build_polar_pattern_places_six() -> None:
    design = build_design(parse_source("b cut cyl:r2.5h10 @18,0,-1 polar:n6r18"))
    # six bolt instances at radius 18, 60° apart
    labels = {inst.label for inst in design.instances.values()}
    assert sum(1 for x in labels if x.startswith("b#")) == 6
    # a probe point at the first bolt centre is inside that cylinder
    from precis.cad.scene import build_design as _b

    d2 = _b(parse_source("plate add cyl:r25h8\nb cut cyl:r2.5h10 @18,0,-1 polar:n6r18"))
    # second bolt at 60°: (18cos60, 18sin60) = (9, 15.588)
    x, y = 18 * math.cos(math.radians(60)), 18 * math.sin(math.radians(60))
    assert not d2.classify_point(vec3(x, y, 4), component="part").inside  # carved


_ASSEMBLY = """
desc: a two part assembly
use: bench testing
component shaft
rod   add  cyl:r5h40   @0,0,-20
component hub
plate add  cyl:r20h10
bore  cut  cyl:r5.1h12 @0,0,-1
lin   add  box:w2d2h2  @1,0,0  linear:n3dx5
rimr  add  box:w4d4h4  @20,0,0 rot:0,0,45
"""


@pytest.mark.parametrize("src", [_FLANGE, _ASSEMBLY])
def test_spec_to_source_round_trips(src: str) -> None:
    """parse_source ∘ spec_to_source is identity on authored designs — so the
    web editor can show an editable source and re-parse an LLM's rewrite."""
    spec = parse_source(src)
    assert parse_source(spec_to_source(spec)) == spec


def test_spec_to_source_carries_meta_and_components() -> None:
    out = spec_to_source(parse_source(_ASSEMBLY))
    assert "desc: a two part assembly" in out
    assert "use: bench testing" in out
    assert "component shaft" in out and "component hub" in out
    # loc/rot/pattern tokens survive
    assert "@0,0,-20" in out
    assert "linear:n3dx5" in out
    assert "rot:0,0,45" in out
