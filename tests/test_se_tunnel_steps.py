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


@pytest.mark.timeout(600)  # CI worst 230s x2.5 for runner variance (gr503084)
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
    # geom.summary counts every pair per zone, past the ten-finding cap
    (summary,) = [f for f in findings if f["code"] == "geom.summary"]
    zones = dict(summary["data"])["zones"]
    assert set(zones) == {"reducer:seg0->seg1", "none"}
    for name, counts in zones.items():
        listed = [f for f in geom if dict(f["data"])["zone"] == name]
        assert counts["clash"] >= sum(f["code"] == "geom.clash" for f in listed)
        assert counts["nnb"] >= sum(
            f["code"] == "geom.nearest_not_bonded" for f in listed
        )
    where = {f["where"] for f in findings if f["code"] == "junction.strain"}
    assert {
        "low",
        "high",
        "reducer:seg0->seg1:outer",
        "reducer:seg0->seg1:inner",
    } <= where


@pytest.mark.timeout(600)  # CI worst 220s x2.5 for runner variance (gr503084)
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


@pytest.mark.timeout(540)  # CI worst 215s x2.5 for runner variance (gr503084)
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
#: an 18 -> 8 rail step, rails 3 so the cone carries p = 3 pentagons; the
#: same tube builds under step: washer, so the two read side by side
GATE_TUBE: dict[str, Any] = {
    "rail": [[18, 4], [8, 4]],
    "rails": 3,
    "rows": 2,
    "pores": [],
}
ZONE = "reducer:seg0->seg1"


def _relaxed_step(step: str, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any]:
    """Build the gate fixture, relax it through ``relax_block`` (EMT
    polish + stubbed-xtb fragments, the relax test's settings) and judge
    the relaxed atoms with the washer-zone-tagged geometry tier.  The
    terminator's trimmed rim atoms do not exist in the relaxed block, so
    they are parked far away instead of judged."""
    _stub_xtb_with_emt(monkeypatch)
    rail = GATE_TUBE["rail"]
    tube = {**GATE_TUBE, "step": step}
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


def _gate_read(
    step: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, tuple[float, float]], dict[str, int]]:
    """The gate's numbers for one step kind on the relaxed fixture: the
    step's (seam bond max A, face-angle dev max deg) per seam, and the
    washer zone's uncapped clash-pair / nearest-not-bonded counts from
    ``geom.summary``."""
    report, geom = _relaxed_step(step, monkeypatch)
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
    (summary,) = [f for f in geom if f.code == "geom.summary"]
    return read, dict(summary.data)["zones"][ZONE]


@pytest.mark.slow
@pytest.mark.xfail(
    strict=True,
    reason=(
        "H2 cone stop condition (2026-10-21, measured in the test "
        "container): after relax_block (EMT polish, 30 steps) the 18->8 "
        "p=3 cone reads outer seam max 2.00 A / angle dev 46.8 deg "
        "(> 45: step.overstrained), inner 1.66 A / 35.1 deg; the washer "
        "zone keeps 22 clash pairs + 12 nearest-not-bonded atoms "
        "(uncapped, geom.summary zones). Washer beside it: outer "
        "1.98 A / 42.6 deg, inner 1.49 A / 103.7 deg, zone 28 + 8. "
        "Margins and bars are not widened to close it"
    ),
)
@pytest.mark.timeout(1440)  # CI worst 570s x2.5 for runner variance (gr503084)
def test_stepped_cone_gate_after_relax(monkeypatch: pytest.MonkeyPatch) -> None:
    # pass (spec §"H2 cone gate follow-up"): both step seams under the
    # step.overstrained bars and a clean washer zone under step: cone;
    # the washer reading of the same tube is carried beside it
    read, zone = _gate_read("cone", monkeypatch)
    washer = _gate_read("washer", monkeypatch)
    over = {tag: v for tag, v in read.items() if v[0] > 3.0 or v[1] > 45.0}
    assert not over and zone == {"clash": 0, "nnb": 0}, (read, zone, washer)


def test_stepped_washer_16_to_8_refused_at_the_throat() -> None:
    # 16 -> 8 under step: washer is refused: the 8-wide rim sits 3.9 A
    # inside the 16-wide star notches (4.5 A needed), so the gate fixture
    # is 18 -> 8, which builds under both step kinds
    tube = {**GATE_TUBE, "rail": [[16, 4], [8, 4]], "step": "washer"}
    with pytest.raises(GeneratorError, match="pinch shut"):
        build_hexfold_scene(params(tube=tube))
