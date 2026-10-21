"""Stepped rail tunnel scene: rail segments of different radius through
solver-minted washers, sites labelled per segment, junction.strain per
reducer seam -- the H2 rail side of
docs/backlog/hexfold-nanoreactor-tube.md.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from hexfold.check import Relaxed, geometry_findings
from hexfold.tunnel import tunnel_steps
from precis_se.atomic.generators import GeneratorError
from precis_se.atomic.generators.hexfold_scene import build_hexfold_scene
from precis_se.atomic.generators.hexfold_spec import surviving_ordinals
from precis_se.atomic.generators.tunnel import washer_zones
from precis_se.atomic.relax_block import relax_block
from tests.test_se_hexfold_relax import _scene_of, _stub_xtb_with_emt


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
    # every geometry pair finding names the washer zone it falls in
    geom = [
        f for f in findings if f["code"] in ("geom.clash", "geom.nearest_not_bonded")
    ]
    assert all(
        dict(f["data"]).get("zone") in ("reducer:seg0->seg1", "none") for f in geom
    )
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
    # ``step`` is a tube key now; an unknown key and an unknown step kind
    # are each refused by name
    tube = {
        "rail": [[18, 4], [8, 4]],
        "rails": 3,
        "rows": 2,
        "pores": [],
        "cone": True,
    }
    with pytest.raises(GeneratorError, match="'cone' is unsupported"):
        build_hexfold_scene(params(tube=tube))
    tube = {
        "rail": [[18, 4], [8, 4]],
        "rails": 3,
        "rows": 2,
        "pores": [],
        "step": "funnel",
    }
    with pytest.raises(GeneratorError, match="'funnel' is not a step kind"):
        build_hexfold_scene(params(tube=tube))


def test_stepped_cone_refused_for_flat_symmetry() -> None:
    # p = rails pentagons; p = 6 is flat, no cone exists
    tube = {
        "rail": [[18, 4], [8, 4]],
        "rails": 6,
        "rows": 2,
        "pores": [],
        "step": "cone",
    }
    with pytest.raises(GeneratorError, match="step.cone.symmetry"):
        build_hexfold_scene(params(tube=tube))


def test_stepped_cone_18_to_12_builds() -> None:
    block = build_hexfold_scene(
        params(
            tube={
                "rail": [[18, 4], [12, 4]],
                "rails": 3,
                "rows": 2,
                "pores": [],
                "step": "cone",
            }
        )
    )
    assert block.topology["scene"]["tube"]["step"] == "cone"
    findings = block.topology["report"]["findings"]
    where = {f["where"] for f in findings if f["code"] == "junction.strain"}
    assert {"reducer:seg0->seg1:outer", "reducer:seg0->seg1:inner"} <= where
    sites = block.topology["sites"]["sites"]
    assert {e.get("segment") for e in sites if "segment" in e} == {0, 1}


def test_stepped_bad_pore_refused() -> None:
    tube = {
        "rail": [[18, 4], [8, 4]],
        "rails": 3,
        "rows": 2,
        "pores": [[5, 0, 1, 2]],
    }
    with pytest.raises(GeneratorError, match="seg 5"):
        build_hexfold_scene(params(tube=tube))


#: the H2 cone gate fixture (spec docs/backlog/hexfold-nanoreactor-tube.md):
#: a 16 -> 8 rail step, rails 3 so the cone carries p = 3 pentagons
GATE_TUBE: dict[str, Any] = {
    "rail": [[16, 4], [8, 4]],
    "rails": 3,
    "rows": 2,
    "pores": [],
}
ZONE = "reducer:seg0->seg1"


def _relaxed_step(
    step: str, monkeypatch: pytest.MonkeyPatch, rail: list[list[int]] | None = None
) -> tuple[Any, Any]:
    """Build the gate fixture, relax it through ``relax_block`` (EMT
    polish + stubbed-xtb fragments, the relax test's settings) and judge
    the relaxed atoms with the washer-zone-tagged geometry tier.  The
    terminator's trimmed rim atoms do not exist in the relaxed block, so
    they are parked far away instead of judged."""
    _stub_xtb_with_emt(monkeypatch)
    rail = rail or GATE_TUBE["rail"]
    tube = {**GATE_TUBE, "rail": rail, "step": step}
    block = build_hexfold_scene(params(tube=tube))
    scene = _scene_of(block)
    report = relax_block(
        scene,
        block.topology,
        polish="emt",
        fragment="xtb",
        buffer_A=5.0,
        core_A=2.0,
        steps=30,
    )
    net, tun = tunnel_steps(
        tuple((r, p, 2, 3, None) for r, p in rail),
        (44, 44),
        step=step,
    )
    relaxed = np.array([scene.cell.frac_to_cart(a.frac) for a in scene.atoms.values()])
    remap = surviving_ordinals(net, block)
    pos = np.zeros((len(net.atoms), 3))
    for k, o in enumerate(sorted(set(range(len(net.atoms))) - set(remap))):
        pos[o] = [1e4 + 10.0 * k, 0.0, 0.0]
    for o, stored in remap.items():
        pos[o] = relaxed[stored]
    geom = geometry_findings(
        net, relaxed=Relaxed(pos, 0.0, "relax_block"), zones=washer_zones(tun)
    )
    return report, geom


@pytest.mark.slow
@pytest.mark.xfail(
    strict=True,
    reason=(
        "H2 cone stop condition (2026-10-20, measured in the test "
        "container): after relax_block (EMT polish, 30 steps) the 16->8 "
        "p=3 cone reads outer seam max 2.09 A / angle dev 20.4 deg, inner "
        "1.63 A / 59.8 deg (> 45: step.overstrained); the washer zone keeps "
        "4 geom.clash WARN + 2 geom.nearest_not_bonded ERROR of the "
        "ten-capped findings (uncapped: 19 clash pairs, 8 nnb atoms); "
        "margins and bars are not widened to close it"
    ),
)
def test_stepped_cone_gate_after_relax(monkeypatch: pytest.MonkeyPatch) -> None:
    report, geom = _relaxed_step("cone", monkeypatch)
    strain = {
        f["where"]: dict(f["data"])
        for f in report.after
        if f["code"] == "junction.strain"
    }
    read = {
        tag: (
            round(strain[f"{ZONE}:{tag}"]["seam_bonds_max_A"], 3),
            round(strain[f"{ZONE}:{tag}"]["angle_dev_max_deg"], 1),
        )
        for tag in ("outer", "inner")
    }
    in_zone = [
        f.code
        for f in geom
        if f.code in ("geom.clash", "geom.nearest_not_bonded")
        and dict(f.data).get("zone") == ZONE
    ]
    over = {tag: v for tag, v in read.items() if v[0] > 3.0 or v[1] > 45.0}
    assert not over and not in_zone, (read, sorted(in_zone))


def test_stepped_washer_16_to_8_refused_at_the_throat() -> None:
    # the gate fixture under step: washer is refused today: the 8-wide
    # rim sits 3.9 A inside the 16-wide star notches (4.5 A needed), so
    # the washer residual is read on the 18 -> 8 fixture of params()
    with pytest.raises(GeneratorError, match="pinch shut"):
        build_hexfold_scene(params(tube={**GATE_TUBE, "step": "washer"}))
