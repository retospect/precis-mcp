"""Enclosure solids: real assembled fit, feature cuts, and early refusals."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from typing import Any

import pytest

from precis.cad.dsl import format_spec, parse
from precis.cad.fit import tessellate_component
from precis.cad.fold import expr_sdf
from precis.cad.scene import SceneSpec, build_design
from precis.cad.vec import vec3
from precis.errors import BadInput
from precis_se.enclosure import generator
from precis_se.enclosure.generator import enclosure_box, lid_overlap_mm3

PARAMS: dict[str, Any] = {"x": 80, "y": 60, "z": 30}
CHORD = 1e-5  # 0.01 mm, comfortably below the 0.3 mm lip clearance.


def _solid(spec: SceneSpec, component: str) -> Any:
    solid = tessellate_component(spec, component, CHORD)
    assert solid is not None
    assert solid.volume() > 0
    return solid


def _distance(spec: SceneSpec, component: str) -> Any:
    design = build_design(spec)
    expr = design.components[component]

    def at(x: float, y: float, z: float) -> float:
        return expr_sdf(expr, vec3(x / 1000, y / 1000, z / 1000), design.instances)

    return at


def test_params_round_trip_and_si_components() -> None:
    result = enclosure_box(**PARAMS)
    replay = enclosure_box(**json.loads(json.dumps(result.params)))
    assert replay.params == result.params
    assert replay.parts == result.parts
    assert set(result.parts) == {"box", "lid"}
    for name, spec in result.parts.items():
        assert spec.meta["enclosure"] == result.params
        assert spec.components == [name]
        assert {node.component for node in spec.nodes} == {name}
    box = _distance(result.parts["box"], "box")
    lid = _distance(result.parts["lid"], "lid")
    assert box(0, 0, 1) < 0  # floor material, SI conversion included
    assert box(0, 0, -0.5) > 0
    assert box(0, 0, 15) > 0  # hollow interior
    assert lid(0, 0, 31.5) > 0
    assert lid(0, 0, 33.5) < 0  # underside at floor + internal height


@pytest.mark.parametrize("draft_angle", [0, 3])
def test_generated_configs_are_canonical_and_dimensions_are_si(
    draft_angle: float,
) -> None:
    result = enclosure_box(**PARAMS, draft_angle=draft_angle)
    for spec in result.parts.values():
        for node in spec.nodes:
            shape = parse(node.config)
            assert format_spec(shape) == node.config
    box = {node.name: node for node in result.parts["box"].nodes}
    lid = {node.name: node for node in result.parts["lid"].nodes}
    assert parse(box["cavity-x"].config).params == pytest.approx(
        {"w": 0.080, "d": 0.052, "h": 0.033}
    )
    assert parse(box["pilot-1"].config).params == pytest.approx(
        {"r": 0.00125, "h": 0.033}
    )
    assert parse(lid["clearance-1"].config).params == pytest.approx(
        {"r": 0.0016, "h": 0.009}
    )
    assert box["cavity-x"].loc[2] == pytest.approx(0.002)
    assert lid["plate-x"].loc[2] == pytest.approx(0.032)


@pytest.mark.parametrize("boss_count", [1, 2])
def test_narrow_automatic_layout_accepts_bosses_that_physically_fit(
    boss_count: int,
) -> None:
    result = enclosure_box(20, 80, 30, boss_count=boss_count)
    assert len(result.boss_centers) == boss_count
    assert all(abs(x) == pytest.approx(1.7) for x, _ in result.boss_centers)
    assert abs(lid_overlap_mm3(result)) < 1e-3


def test_narrow_four_boss_layout_refuses_actual_boss_overlap() -> None:
    with pytest.raises(BadInput, match="placements overlap") as error:
        enclosure_box(20, 80, 30, boss_count=4)
    assert error.value.next


def test_unknown_screw_recovery_names_all_supported_sizes() -> None:
    with pytest.raises(BadInput, match="screw_size") as error:
        enclosure_box(**PARAMS, screw_size="M99")
    assert isinstance(error.value.next, str)
    assert "M2, M2.5, M3, M4" in error.value.next


@pytest.mark.parametrize("draft_angle", [0, 3], ids=["straight", "drafted"])
def test_assembled_solids_fit_and_shifted_lid_really_interferes(
    draft_angle: float,
) -> None:
    result = enclosure_box(**PARAMS, draft_angle=draft_angle)
    box = _solid(result.parts["box"], "box")
    lid = _solid(result.parts["lid"], "lid")
    assert abs((box ^ lid).volume()) < 1e-12
    assert abs(lid_overlap_mm3(result)) < 1e-3
    # A no-op/empty fit implementation must not pass this test.
    displaced = lid.translate((0.002, 0, 0))
    overlap = (box ^ displaced).volume()
    assert overlap > 1e-9
    shifted_lid = replace(
        result.parts["lid"],
        nodes=[
            replace(node, loc=(node.loc[0] + 0.002, node.loc[1], node.loc[2]))
            for node in result.parts["lid"].nodes
        ],
    )
    shifted = replace(result, parts={**result.parts, "lid": shifted_lid})
    assert lid_overlap_mm3(shifted) == pytest.approx(overlap * 1e9, abs=1e-3)


@pytest.mark.parametrize("draft_angle", [0, 3])
def test_lip_has_clearance_and_material_at_multiple_depths(draft_angle: float) -> None:
    result = enclosure_box(**PARAMS, boss_count=0, ribs=False, draft_angle=draft_angle)
    box = _distance(result.parts["box"], "box")
    lid = _distance(result.parts["lid"], "lid")
    for z in (29.25, 30.5, 31.75):
        # Draft follows the cavity slope; clearance holds throughout insertion.
        cavity_x = 40 + (z - 2) * math.tan(math.radians(draft_angle))
        assert box(cavity_x + 0.5, 0, z) < 0
        assert box(cavity_x - 0.15, 0, z) > 0
        assert lid(cavity_x - 0.15, 0, z) > 0
        assert lid(cavity_x - 0.8, 0, z) < 0
        assert lid(0, 0, z) > 0


@pytest.mark.parametrize("draft_angle", [0, 3], ids=["straight", "drafted"])
def test_wall_thickness_at_side_and_rounded_corner_throughout_depth(
    draft_angle: float,
) -> None:
    result = enclosure_box(**PARAMS, draft_angle=draft_angle, boss_count=0, ribs=False)
    box = _distance(result.parts["box"], "box")
    angle = math.radians(draft_angle)
    horizontal_wall = 2 / math.cos(angle)
    # One micrometre resolves the normal-vs-horizontal offset even at 3 degrees.
    epsilon = 0.001
    for z in (5, 15, 27):
        inner_x = 40 + (z - 2) * math.tan(angle)
        assert box(inner_x - epsilon, 0, z) > 0
        assert box(inner_x + epsilon, 0, z) < 0
        assert box(inner_x + horizontal_wall - epsilon, 0, z) < 0
        assert box(inner_x + horizontal_wall + epsilon, 0, z) > 0

        # The NE arc shares the same normal thickness as the straight side.
        inner_radius = 4 + (z - 2) * math.tan(angle)
        for radius, inside in (
            (inner_radius - epsilon, False),
            (inner_radius + epsilon, True),
            (inner_radius + horizontal_wall - epsilon, True),
            (inner_radius + horizontal_wall + epsilon, False),
        ):
            offset = radius / math.sqrt(2)
            distance = box(36 + offset, 26 + offset, z)
            assert distance < 0 if inside else distance > 0


@pytest.mark.parametrize(
    "screw_size,pilot,clearance",
    [("M2", 1.6, 2.2), ("M2.5", 2.05, 2.7), ("M3", 2.5, 3.2), ("M4", 3.3, 4.2)],
)
def test_table_driven_pilot_and_clearance_are_actual_holes(
    screw_size: str, pilot: float, clearance: float
) -> None:
    result = enclosure_box(**PARAMS, screw_size=screw_size)
    assert result.pilot_diameter == pilot
    assert result.clearance_diameter == clearance
    box = _distance(result.parts["box"], "box")
    lid = _distance(result.parts["lid"], "lid")
    for x, y in result.boss_centers:
        length = math.hypot(x, y)
        ux, uy = -x / length, -y / length
        for at, radius, z in (
            (box, pilot / 2, 15),
            (lid, clearance / 2, 33.5),
        ):
            assert at(x, y, z) > 0
            assert at(x + ux * (radius - 0.15), y + uy * (radius - 0.15), z) > 0
            assert at(x + ux * (radius + 0.15), y + uy * (radius + 0.15), z) < 0


@pytest.mark.parametrize("count", range(5))
def test_boss_count_controls_bosses_holes_and_ribs(count: int) -> None:
    result = enclosure_box(**PARAMS, boss_count=count)
    assert len(result.boss_centers) == count
    for part, prefix in (("box", "pilot"), ("box", "rib"), ("lid", "clearance")):
        names = {
            n.name for n in result.parts[part].nodes if n.name.startswith(prefix + "-")
        }
        assert names == {f"{prefix}-{i}" for i in range(1, count + 1)}


def test_custom_boss_placement_is_preserved() -> None:
    positions = [[-20, -10], [20, 10]]
    result = enclosure_box(**PARAMS, boss_count=2, boss_placement=positions)
    assert result.boss_centers == ((-20, -10), (20, 10))
    assert result.params["boss_placement"] == positions


@pytest.mark.parametrize(
    "reverse", [False, True], ids=["near-wall-first", "center-first"]
)
def test_later_boss_ribs_cannot_refill_an_earlier_pilot(reverse: bool) -> None:
    positions = [[0, -15], [0, 0]]
    if reverse:
        positions.reverse()
    result = enclosure_box(80, 50, 25, boss_count=2, boss_placement=positions)
    box = _distance(result.parts["box"], "box")
    for x, y in positions:
        # The center boss's rib crosses the near-wall boss below the lip.
        assert box(x, y, 10) > 0
        assert box(x + result.pilot_diameter / 2 - 0.1, y, 10) > 0


def test_ribs_add_material_below_lip_without_filling_pilots() -> None:
    ribbed = enclosure_box(**PARAMS)
    plain = enclosure_box(**PARAMS, ribs=False)
    assert not any(n.name.startswith("rib-") for n in plain.parts["box"].nodes)
    with_ribs = _solid(ribbed.parts["box"], "box")
    without_ribs = _solid(plain.parts["box"], "box")
    assert (with_ribs - without_ribs).volume() > 1e-9
    assert abs((without_ribs - with_ribs).volume()) < 1e-12
    for node in ribbed.parts["box"].nodes:
        if node.name.startswith("rib-"):
            rib = _solid(SceneSpec(nodes=[node], components=["box"]), "box")
            assert rib.bounding_box()[5] <= (32 - 3) / 1000


def test_counterbores_only_cut_the_lid_and_leave_the_seat() -> None:
    result = enclosure_box(**PARAMS, counterbore=True, lid_thickness=6)
    assert not any(n.name.startswith("counterbore-") for n in result.parts["box"].nodes)
    bores = [n for n in result.parts["lid"].nodes if n.name.startswith("counterbore-")]
    assert len(bores) == 4 and all(n.op == "cut" for n in bores)
    lid = _distance(result.parts["lid"], "lid")
    probe_radius = (result.clearance_diameter + result.head_diameter) / 4
    for x, y in result.boss_centers:
        length = math.hypot(x, y)
        px, py = x - x / length * probe_radius, y - y / length * probe_radius
        assert lid(px, py, 38 - result.head_height / 2) > 0
        assert lid(px, py, 32.5) < 0


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"boss_outer_diameter": 5},
            "pilot hole leaves less than one wall thickness of boss wall",
        ),
        (
            {"counterbore": True, "lid_thickness": 4},
            "counterbore needs lid thickness greater than head height plus a 1 mm floor",
        ),
        (
            {"boss_count": 1, "boss_placement": [[39, 0]]},
            "screw circle must stay inside the walls",
        ),
        (
            {"boss_count": 1, "boss_placement": [[35, 0]]},
            "lip clears bosses only when boss outer diameter plus lip clearance fits",
        ),
    ],
    ids=["pilot-wall", "counterbore-floor", "screw-circle", "lip-boss-clearance"],
)
def test_geometric_refusals_explain_the_failed_constraint_before_nodes(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any], message: str
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("invalid enclosure reached geometry construction")

    monkeypatch.setattr(generator, "NodeSpec", forbidden)
    with pytest.raises(BadInput, match=message) as error:
        enclosure_box(**{**PARAMS, **overrides})
    assert error.value.next


@pytest.mark.parametrize("screw_size", ["M2", "M2.5", "M3", "M4"])
def test_counterbore_rejects_exact_head_height_plus_one_mm(screw_size: str) -> None:
    reference = enclosure_box(**PARAMS, screw_size=screw_size)
    with pytest.raises(BadInput, match="greater than head height plus a 1 mm floor"):
        enclosure_box(
            **PARAMS,
            screw_size=screw_size,
            counterbore=True,
            lid_thickness=reference.head_height + 1,
        )
    enclosure_box(
        **PARAMS,
        screw_size=screw_size,
        counterbore=True,
        lid_thickness=reference.head_height + 1.01,
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"x": 0},
        {"y": -1},
        {"z": float("nan")},
        {"wall": 0},
        {"floor": -1},
        {"lid_thickness": 0},
        {"corner_radius": -1},
        {"draft_angle": float("inf")},
        {"lip_height": 30},
        {"lip_clearance": -0.1},
        {"boss_count": 5},
        {"boss_count": -1},
        {"boss_count": 1.5},
        {"boss_outer_diameter": 5},  # pilot leaves less than 2 mm wall
        {"screw_size": "M99"},
        {"counterbore": True, "lid_thickness": 3},
        {"rib_ratio": 0},
        {"boss_count": 1, "boss_placement": []},
        {"boss_count": 1, "boss_placement": [[100, 0]]},
        {"boss_count": 1, "boss_placement": [[38, 0]]},
        {"boss_count": 1, "boss_placement": [[35, 0]]},
        {"boss_count": 1, "boss_placement": [[float("nan"), 0]]},
    ],
)
def test_invalid_parameters_refuse_before_any_geometry(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any]
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("invalid enclosure reached geometry construction")

    monkeypatch.setattr(generator, "NodeSpec", forbidden)
    with pytest.raises(BadInput) as error:
        enclosure_box(**{**PARAMS, **overrides})
    assert error.value.next
