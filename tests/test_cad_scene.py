"""Design source parsing + Design building."""

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
plate     add  cyl:r25mmh8mm
hub_bore  cut  cyl:r8mmh10mm    @0mm,0mm,-1mm
bolts     cut  cyl:r2.5mmh10mm  @18mm,0mm,-1mm  polar:n6r18mm
"""


def test_parse_basic_counts() -> None:
    spec = parse_source(_FLANGE)
    assert spec.components == ["flange"]
    assert [n.name for n in spec.nodes] == ["plate", "hub_bore", "bolts"]
    assert spec.nodes[1].op == "cut"
    assert spec.nodes[1].loc == (0.0, 0.0, -0.001)
    pattern = spec.nodes[2].pattern
    assert pattern is not None
    assert pattern["kind"] == "polar" and pattern["n"] == 6.0
    assert pattern["r"] == pytest.approx(0.018)


def test_default_component() -> None:
    spec = parse_source("a add cyl:r2mmh2mm")
    assert spec.components == ["part"]
    assert spec.nodes[0].component == "part"


def test_meta_roundtrip() -> None:
    spec = parse_source(_FLANGE)
    for n in spec.nodes:
        rebuilt = NodeSpec.from_meta(n.name, n.to_meta())
        assert rebuilt == n


def test_bad_op_rejected() -> None:
    with pytest.raises(SceneError):
        parse_source("a frobnicate cyl:r2mmh2mm")


def test_bad_config_rejected() -> None:
    with pytest.raises(ValueError):
        parse_source("a add notashape:99")


def test_duplicate_name_rejected() -> None:
    with pytest.raises(SceneError):
        parse_source("a add cyl:r2mmh2mm\na cut cyl:r1mmh3mm")


def test_build_design_flange_probe() -> None:
    # The built design must behave like the hand-built flange: solid in the
    # plate, void in the bore.
    design = build_design(parse_source(_FLANGE))
    assert "flange" in design.components
    # a point in the plate annulus (r=23mm: outside the bolt circle r18±2.5mm,
    # outside the bore r8mm, inside the plate r25mm) — internal storage is SI
    # metres, so probe points are the mm-authored geometry ÷ 1000.
    assert design.classify_point(vec3(0.023, 0, 0.004), component="flange").inside
    # a point in the central bore is carved away
    assert not design.classify_point(vec3(0, 0, 0.004), component="flange").inside


def test_build_polar_pattern_places_six() -> None:
    design = build_design(
        parse_source("b cut cyl:r2.5mmh10mm @18mm,0mm,-1mm polar:n6r18mm")
    )
    # six bolt instances at radius 18mm, 60° apart
    labels = {inst.label for inst in design.instances.values()}
    assert sum(1 for x in labels if x.startswith("b#")) == 6
    # a probe point at the first bolt centre is inside that cylinder
    from precis.cad.scene import build_design as _b

    d2 = _b(
        parse_source(
            "plate add cyl:r25mmh8mm\n"
            "b cut cyl:r2.5mmh10mm @18mm,0mm,-1mm polar:n6r18mm"
        )
    )
    # second bolt at 60°: (18cos60, 18sin60) mm = (9, 15.588) mm
    x, y = 0.018 * math.cos(math.radians(60)), 0.018 * math.sin(math.radians(60))
    assert not d2.classify_point(vec3(x, y, 0.004), component="part").inside  # carved


_ASSEMBLY = """
desc: a two part assembly
use: bench testing
component shaft
rod   add  cyl:r5mmh40mm   @0mm,0mm,-20mm
component hub
plate add  cyl:r20mmh10mm
bore  cut  cyl:r5.1mmh12mm @0mm,0mm,-1mm
lin   add  box:w2mmd2mmh2mm  @1mm,0mm,0mm  linear:n3dx5mm
rimr  add  box:w4mmd4mmh4mm  @20mm,0mm,0mm rot:0deg,0deg,45deg
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
    # loc/rot/pattern tokens survive — re-serialised with the explicit `m`
    # unit the strict boundary requires (stored value is SI metres already).
    assert "@0m,0m,-0.02m" in out
    assert "linear:n3dx0.005m" in out
    assert f"rot:0rad,0rad,{math.radians(45)}rad" in out


# ---------------------------------------------------------------------------
# chamfer
# ---------------------------------------------------------------------------

_BEVEL_BOX = """
component part
body  add cyl:r25mmh8mm
bevel cut chamfer:2mmx45deg @20mm,0mm,10mm
"""


def test_chamfer_cut_source_round_trips() -> None:
    spec = parse_source(_BEVEL_BOX)
    assert spec.nodes[1].config == f"chamfer:0.002x{math.radians(45)}"
    assert parse_source(spec_to_source(spec)) == spec


def test_chamfer_as_first_node_rejected() -> None:
    with pytest.raises(SceneError, match="first"):
        parse_source("bevel cut chamfer:2mmx45deg @0mm,0mm,0mm")


def test_chamfer_with_add_rejected() -> None:
    with pytest.raises(SceneError, match="add"):
        parse_source("body add cyl:r25mmh8mm\nbevel add chamfer:2mmx45deg @0mm,0mm,0mm")


def test_chamfer_bevels_box_corner_keeps_deep_body() -> None:
    # bevel cut chamfer:2x45 @20,0,10 on box:w40d20h10 (mm) shaves the +x
    # top edge; probe points are the mm-authored geometry ÷ 1000.
    design = build_design(
        parse_source(
            "component part\nbody  add box:w40mmd20mmh10mm\n"
            "bevel cut chamfer:2mmx45deg @20mm,0mm,10mm\n"
        )
    )
    # corner-region points beyond the bevel plane are removed
    assert not design.classify_point(vec3(0.0199, 0, 0.0099), component="part").inside
    # deep-body points are retained (including one right at the notional
    # bevel boundary distance)
    assert design.classify_point(vec3(0, 0, 0.005), component="part").inside
    assert design.classify_point(vec3(0.019, 0, 0.008), component="part").inside


def test_chamfer_truncates_cylinder_top() -> None:
    # chamfer:0x0 @0,0,5 (mm) cut on cyl:r5h10 (mm) is a flat cut: nothing
    # above z=5mm=0.005m.
    design = build_design(
        parse_source(
            "component part\nbody add cyl:r5mmh10mm\n"
            "cap  cut chamfer:0mmx0deg @0mm,0mm,5mm\n"
        )
    )
    assert design.classify_point(vec3(0, 0, 0.0049), component="part").inside
    assert not design.classify_point(vec3(0, 0, 0.0051), component="part").inside


# ---------------------------------------------------------------------------
# weld — declared intended overlap (docs/backlog/cad-intended-overlap-weld.md)
# ---------------------------------------------------------------------------

_TWO_PARTS = """
component a
abox add box:w10mmd10mmh10mm
component b
bbox add box:w10mmd10mmh10mm @8mm,0mm,0mm
"""

_THREE_PARTS = _TWO_PARTS + "component c\ncbox add box:w10mmd10mmh10mm @30mm,0mm,0mm\n"


def test_weld_two_names_stores_sorted_group() -> None:
    spec = parse_source(_TWO_PARTS + "weld a b\n")
    assert spec.meta["welds"] == [["a", "b"]]


def test_weld_order_of_names_does_not_matter() -> None:
    spec = parse_source(_TWO_PARTS + "weld b a\n")
    assert spec.meta["welds"] == [["a", "b"]]


def test_weld_three_names_is_one_clique_group() -> None:
    spec = parse_source(_THREE_PARTS + "weld a b c\n")
    assert spec.meta["welds"] == [["a", "b", "c"]]


def test_weld_star_stores_star_form() -> None:
    spec = parse_source(_TWO_PARTS + "weld *\n")
    assert spec.meta["welds"] == [["*"]]


def test_weld_no_line_leaves_meta_absent() -> None:
    spec = parse_source(_TWO_PARTS)
    assert "welds" not in spec.meta


def test_weld_single_name_is_scene_error_with_line() -> None:
    with pytest.raises(SceneError, match="line 6"):
        parse_source(_TWO_PARTS + "weld a\n")


def test_weld_repeated_name_is_scene_error_with_line() -> None:
    with pytest.raises(SceneError, match="line 6"):
        parse_source(_TWO_PARTS + "weld a a\n")


def test_weld_unknown_name_is_scene_error_with_line() -> None:
    with pytest.raises(SceneError, match="line 6"):
        parse_source(_TWO_PARTS + "weld a nosuch\n")


def test_weld_naming_an_instance_parses() -> None:
    # `use … as g` declares an instance; a weld may name it just like a
    # plain component — name resolution happens after expansion.
    spec = parse_source(
        "component base\nb add box:w10mmd10mmh10mm\nuse standoff as g\nweld g base\n"
    )
    assert spec.meta["welds"] == [["base", "g"]]


def test_weld_round_trips() -> None:
    spec = parse_source(_TWO_PARTS + "weld a b\n")
    assert parse_source(spec_to_source(spec)) == spec


def test_weld_star_round_trips() -> None:
    spec = parse_source(_TWO_PARTS + "weld *\n")
    assert parse_source(spec_to_source(spec)) == spec


def test_two_separate_weld_lines_stay_two_groups() -> None:
    spec = parse_source(_THREE_PARTS + "weld a b\nweld b c\n")
    # a–c is never implied — only the two declared groups
    assert spec.meta["welds"] == [["a", "b"], ["b", "c"]]
    assert parse_source(spec_to_source(spec)) == spec


def test_duplicate_weld_line_is_dropped() -> None:
    spec = parse_source(_TWO_PARTS + "weld a b\nweld b a\n")
    assert spec.meta["welds"] == [["a", "b"]]


def test_weld_round_trips_after_mates_joints_couples() -> None:
    # weld lines follow mates/joints/couples in the trailing relation block.
    src = (
        "component post\n"
        "pillar add box:w6mmd6mmh20mm @0mm,28mm,0mm\n"
        "component a1\n"
        "bar1 add box:w20mmd6mmh6mm @10mm,0mm,3mm\n"
        "port h1 of:a1\n"
        "component a2\n"
        "bar2 add box:w30mmd6mmh6mm @15mm,0mm,13mm\n"
        "port h2 of:a2\n"
        "joint a1 revolute at:h1 limits:-180deg..180deg\n"
        "joint a2 revolute at:h2 limits:-180deg..180deg\n"
        "gear a1 to a2 ratio:1\n"
        "weld post a1\n"
    )
    spec = parse_source(src)
    assert spec.meta["welds"] == [["a1", "post"]]
    out = spec_to_source(spec)
    # weld comes after the gear/belt coupling
    assert out.index("gear a1 to a2") < out.index("weld a1 post")
    assert parse_source(out) == spec
