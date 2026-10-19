"""Stepped rail tunnel scene: rail segments of different radius through
solver-minted washers, sites labelled per segment, junction.strain per
reducer seam -- the H2 rail side of
docs/backlog/hexfold-nanoreactor-tube.md.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis_se.atomic.generators import GeneratorError
from precis_se.atomic.generators.hexfold_scene import build_hexfold_scene


def params(**over: Any) -> dict[str, Any]:
    p: dict[str, Any] = {
        "features": [{"name": "t", "type": "tunnel-reservoirs", "sheet": [44, 44]}],
        "k_tether": 1.0,
        "tube": {"rail": [[18, 4], [8, 4]], "rails": 3, "rows": 2, "pores": []},
    }
    p.update(over)
    return p


def test_stepped_rail_tunnel_builds() -> None:
    block = build_hexfold_scene(params())
    findings = block.topology["report"]["findings"]
    # residual (spec docs/backlog/hexfold-nanoreactor-tube.md): the star
    # notch walls at a step seam sit ~1 A apart unbonded after relax --
    # geom.nearest_not_bonded/clash ERRORs confined to the washer zone;
    # anything else is a regression
    errors = [f for f in findings if f["severity"] == "ERROR"]
    assert {f["code"] for f in errors} <= {
        "geom.nearest_not_bonded",
        "geom.clash",
        "geom.contact.short",
    }
    where = {f["where"] for f in findings if f["code"] == "junction.strain"}
    assert {
        "low",
        "high",
        "reducer:seg0->seg1:outer",
        "reducer:seg0->seg1:inner",
    } <= where


def test_stepped_sites_labelled_per_segment() -> None:
    block = build_hexfold_scene(params())
    sites = block.topology["sites"]["sites"]
    segs = {e.get("segment") for e in sites if "segment" in e}
    assert segs == {0, 1}
    assert any(e["region"].startswith("seg0.") for e in sites)
    assert any(e["region"].startswith("seg1.") for e in sites)


def test_stepped_unsupported_key_refused_by_name() -> None:
    tube = {
        "rail": [[18, 4], [8, 4]],
        "rails": 3,
        "rows": 2,
        "pores": [],
        "cone": True,
    }
    with pytest.raises(GeneratorError, match="cone"):
        build_hexfold_scene(params(tube=tube))


def test_stepped_bad_pore_refused() -> None:
    tube = {
        "rail": [[18, 4], [8, 4]],
        "rails": 3,
        "rows": 2,
        "pores": [[5, 0, 1, 2]],
    }
    with pytest.raises(GeneratorError, match="seg 5"):
        build_hexfold_scene(params(tube=tube))
