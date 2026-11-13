"""The build layer over the recipe (se-flow-kernel slice 4b §Design
representation item 1): ``build(recipe) -> Built``, ``FEATURE_OPS``,
``shape_sha``, canonical group form and the per-feature step cache. The
rasterised region-change report is not built yet; only the deleted-anchor
refusal is pinned here."""

from __future__ import annotations

import dataclasses
import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from precis.errors import BadInput
from precis_geom import steps
from precis_geom.design import Recipe, build
from precis_geom.flow import domain_from_solid
from tests.test_se_flow import HOLE_R, HOLES, TIP, H, L, W, _hex_design, _tip_patch

pytestmark = pytest.mark.filterwarnings("ignore:steps:UserWarning")


@pytest.fixture(autouse=True)
def cache(tmp_path: Path) -> Any:
    steps.configure(tmp_path / "cache")
    try:
        yield
    finally:
        steps.configure()


def _hex(holes: Any = HOLES) -> tuple[Recipe, str, list[str]]:
    """The cfdtest hex footprint as a box plus two ``ngon:n4`` tips, holes
    as one drill group under the box."""
    r = Recipe()
    base = r.add("box", w=L - 2 * TIP, d=W, h=H, x=L / 2, y=W / 2)
    r.add("ngon", parents=(base,), n=4, r=W / 2, h=H, x=TIP, y=W / 2)
    r.add("ngon", parents=(base,), n=4, r=W / 2, h=H, x=L - TIP, y=W / 2)
    ids = [
        r.add(
            "drill",
            parents=(base,),
            group="holes",
            x=x,
            y=y,
            d=2 * HOLE_R,
            z=5e-4,
            depth=d + 5e-4,
        )
        for x, y, d in holes
    ]
    return r, base, ids


def _new_entries(before: list[dict[str, Any]]) -> list[str]:
    old = {row["key"] for row in before}
    return sorted(row["name"] for row in steps.ls() if row["key"] not in old)


def test_hex_footprint_builds_a_gap_domain() -> None:
    """Box + two diamond tips + the drill group is the chamber void that
    ``domain_from_solid`` reads, with no adapter between — cell for cell the
    cfdtest ``PolyFrustum`` fixture's gap."""
    built = build(_hex()[0])
    gap = domain_from_solid(
        built.solid(), _tip_patch(0.0, -1.0), _tip_patch(L, 1.0), nx=80
    )
    assert (gap.thin_axis, gap.x_axis, gap.y_axis) == (2, 0, 1)
    assert math.isclose(gap.L, L) and math.isclose(gap.h0, H)
    assert gap.depth_star.max() > 1.0
    assert gap.domain.ports.inlet.any() and gap.domain.ports.outlet.any()
    ref = domain_from_solid(
        _hex_design(), _tip_patch(0.0, -1.0), _tip_patch(L, 1.0), nx=80
    )
    assert np.array_equal(gap.domain.mask, ref.domain.mask)
    assert np.allclose(gap.depth_star, ref.depth_star, atol=1e-9)
    names = [n.name for n in built.spec.nodes]
    assert [n for n in names if n.startswith("holes#")] == [
        f"holes#{i}" for i in range(len(HOLES))
    ]


def test_build_leaves_the_recipe_alone() -> None:
    r = _hex()[0]
    doc = r.to_json()
    build(r)
    assert r.to_json() == doc


def test_hole_order_and_ids_do_not_change_the_sha() -> None:
    a = build(_hex()[0]).shape_sha
    assert build(_hex(HOLES[::-1])[0]).shape_sha == a
    # equal geometry under different ids: burn five ids first
    shifted = Recipe()
    for _ in range(5):
        shifted.remove(shifted.add("sphere", r=1.0))
    base = shifted.add("box", w=L - 2 * TIP, d=W, h=H, x=L / 2, y=W / 2)
    shifted.add("ngon", parents=(base,), n=4, r=W / 2, h=H, x=TIP, y=W / 2)
    shifted.add("ngon", parents=(base,), n=4, r=W / 2, h=H, x=L - TIP, y=W / 2)
    for x, y, d in HOLES:
        shifted.add(
            "drill",
            parents=(base,),
            group="holes",
            x=x,
            y=y,
            d=2 * HOLE_R,
            z=5e-4,
            depth=d + 5e-4,
        )
    assert build(shifted).shape_sha == a


def test_remove_restore_and_json_round_trip_keep_the_sha() -> None:
    r, _base, ids = _hex()
    a = build(r).shape_sha
    snap = r.snapshot()
    r.remove(ids[2])
    assert build(r).shape_sha != a
    r.restore(snap)
    assert build(r).shape_sha == a
    assert build(Recipe.from_json(r.to_json())).shape_sha == a


def test_hole_edit_rebuilds_its_leaf_and_combine_only() -> None:
    r, base, ids = _hex()
    first = build(r)
    before = steps.ls()
    snap = r.snapshot()
    r.modify(ids[0], x=0.032)
    build(r)
    # one new leaf and one new combine, then the union of the three sinks
    assert _new_entries(before) == ["feature.combine", "feature.drill", "feature.union"]
    # a base edit rebuilds the base and everything over it, nothing else
    before = steps.ls()
    r.modify(base, h=H * 1.1)
    build(r)
    new = _new_entries(before)
    assert "feature.drill" not in new and new.count("feature.box") == 1
    # undo is a cache hit: no new entry, the same shape
    r.restore(snap)
    before = steps.ls()
    again = build(r)
    assert _new_entries(before) == []
    assert again.shape_sha == first.shape_sha
    assert again.step_keys == first.step_keys


def test_independent_groups_hold_separate_combines() -> None:
    r = Recipe()
    base = r.add("box", w=0.1, d=0.03, h=1e-3, x=0.05, y=0.015)
    a = r.add("drill", parents=(base,), group="a", x=0.02, y=0.01, d=0.002, depth=0.001)
    r.add("drill", parents=(base,), group="b", x=0.07, y=0.01, d=0.002, depth=0.001)
    build(r)
    before = steps.ls()
    r.modify(a, y=0.02)
    build(r)
    assert _new_entries(before) == ["feature.combine", "feature.drill", "feature.union"]


def test_refusals() -> None:
    r, base, ids = _hex()
    r.region("inlet", f"patch:{ids[1]}.side1@0,0+1x1")
    r.remove(ids[1])
    with pytest.raises(BadInput, match=rf"region 'inlet'.*{ids[1]}"):
        build(r)
    bad = Recipe()
    bad.add("blob", r=1.0)
    with pytest.raises(BadInput, match="unknown op 'blob'"):
        build(bad)
    leafy = Recipe()
    b = leafy.add("box", w=1.0, d=1.0, h=1.0)
    h = leafy.add("drill", parents=(b,), group="g", x=0, y=0, d=0.1, depth=0.1)
    leafy.add("cyl", parents=(h,), r=0.1, h=0.1)
    with pytest.raises(BadInput, match="group members are leaves"):
        build(leafy)
    with pytest.raises(BadInput, match="feature op 'box'"):
        steps.configure("off")
        bare = Recipe()
        bare.add("box", w=1.0)
        build(bare)


def test_shape_sha_refuses_a_loader() -> None:
    spec = build(_hex()[0]).spec
    loaded = dataclasses.replace(spec, field_loader=lambda ref: np.zeros(1))
    with pytest.raises(BadInput, match="loader"):
        steps.shape_sha(loaded)
