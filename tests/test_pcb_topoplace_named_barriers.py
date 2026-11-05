"""A barrier cannot acquire copper ownership merely by carrying a net name."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest
from shapely.geometry import Polygon

from precis.pcb.topoplace.design import TopoDesign, TopoZone
from precis.pcb.topoplace.legalize import _via_zone_exempt, legalize_pose
from precis.pcb.topoplace.shove import shove_pose


def _design(kind: str, *, fixed: bool = False) -> TopoDesign:
    body = np.asarray([[-0.5, -0.5], [0.5, -0.5], [0.5, 0.5], [-0.5, 0.5]])
    body = np.vstack((body, body[0]))
    zone = TopoZone(
        kind=kind,
        ring=np.asarray(
            [[9.0, 19.0], [11.0, 19.0], [11.0, 21.0], [9.0, 21.0], [9.0, 19.0]]
        ),
        net="N0",
        name="N0",
    )
    return TopoDesign(
        parts=("U1",),
        pin_xy=np.asarray([[0.0, 0.0]]),
        pin_part=np.asarray([0]),
        pin_side=np.asarray([0]),
        pin_label=np.asarray(["1"], dtype=object),
        pin_net=np.asarray([0]),
        pin_wh=np.asarray([[1.0, 1.0]]),
        pin_role=np.asarray(["electrode"], dtype=object),
        net_pins=(np.asarray([0]),),
        net_name=("N0",),
        flex=(),
        swap_groups=(),
        fixed=np.asarray([fixed]),
        freedoms=np.asarray([3], dtype=np.uint8),
        outline=np.asarray([[0.0, 0.0], [30.0, 0.0], [30.0, 30.0], [0.0, 30.0]]),
        pose0=np.asarray([[10.0, 20.0, 0.0, 0.0]]),
        part_outline=(body,),
        zones=(zone,),
    )


@pytest.mark.parametrize("solver", [legalize_pose, shove_pose])
@pytest.mark.parametrize("kind", ["keepout", "mounting_hole"])
def test_named_barrier_is_treated_like_unnamed_barrier(solver, kind: str) -> None:
    d = _design(kind)
    result = solver(d, d.pose0)
    unnamed = replace(d, zones=(replace(d.zones[0], net="", name=""),))
    expected = solver(unnamed, unnamed.pose0)
    np.testing.assert_array_equal(result.pose, expected.pose)
    assert result.resolved == expected.resolved
    assert result.remaining == expected.remaining
    assert result.moved_mm > 0.0

    # The shove mesh's no-inversion guard can cap an exit from a zone's
    # centre. It must report that residual rather than claim resolution.
    body = Polygon(d.part_outline[0] + result.pose[0, :2])
    area = body.intersection(Polygon(d.zones[0].ring)).area
    assert result.resolved == (area < 1e-6)
    if area >= 1e-6:
        assert any(
            a == 0 and b == 1 and overlap > 0 for a, b, overlap in result.remaining
        )


@pytest.mark.parametrize("solver", [legalize_pose, shove_pose])
@pytest.mark.parametrize("kind", ["keepout", "mounting_hole"])
def test_fixed_part_reports_named_barrier(solver, kind: str) -> None:
    d = _design(kind, fixed=True)
    result = solver(d, d.pose0)
    np.testing.assert_array_equal(result.pose, d.pose0)
    assert not result.resolved
    assert any(a == 0 and b == 1 and area > 0.0 for a, b, area in result.remaining)


@pytest.mark.parametrize("solver", [legalize_pose, shove_pose])
@pytest.mark.parametrize("kind", ["fixed_copper", "fixed_via"])
def test_owned_copper_retains_exemption(solver, kind: str) -> None:
    d = _design(kind)
    result = solver(d, d.pose0)
    np.testing.assert_array_equal(result.pose, d.pose0)
    assert result.resolved
    assert result.remaining == ()

    foreign = replace(d, zones=(replace(d.zones[0], net="N1"),))
    foreign_result = solver(foreign, foreign.pose0)
    assert foreign_result.moved_mm > 0.0


@pytest.mark.parametrize("kind", ["keepout", "mounting_hole", "fixed_via"])
def test_via_exemption_requires_fixed_via_kind(kind: str) -> None:
    """The same electrode-pad overlap can exempt only an authored via."""
    d = _design(kind)
    exempt = _via_zone_exempt(
        d, d.pose0[0], 0, d.zones[0], Polygon(d.zones[0].ring), {0: "N0"}
    )
    assert exempt is (kind == "fixed_via")
