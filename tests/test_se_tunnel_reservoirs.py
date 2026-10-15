"""Tunnel-reservoirs scene: sheet | tube | sheet with opening-solver
joints, the nanoreactor's reservoir stage (slice H1).  Admission refusals,
the analytic composite and its defect-ring findings."""

from __future__ import annotations

import functools
from typing import Any

import numpy as np
import pytest

from precis_se.atomic.generators import GeneratorError
from precis_se.atomic.generators.hexfold_scene import build_hexfold_scene
from precis_se.atomic.generators.tunnel import tunnel_strain


def params(
    tube: Any = (6, 4),
    sheet: list[int] | None = None,
) -> dict[str, Any]:
    return {
        "tube": tube if isinstance(tube, dict) else list(tube),
        "features": [
            {
                "name": "t",
                "type": "tunnel-reservoirs",
                "sheet": sheet or [30, 30],
            }
        ],
        "k_tether": 1.0,
    }


def test_armchair_tunnel_builds_and_reports_defect_ring() -> None:
    block = build_hexfold_scene(params())
    topo = block.topology
    assert topo["scene"]["features"][0]["type"] == "tunnel-reservoirs"
    regions = dict(topo["regions"]) if "regions" in topo else {}
    findings = [f["code"] for f in topo["report"]["findings"]]
    assert findings.count("opening.defects") == 2
    assert "geom.clash" not in findings
    # H termination: every carbon short of valence was capped
    assert topo["terminated"]["count"] > 0


def test_sites_label_mouth_and_junction_atoms() -> None:
    block = build_hexfold_scene(params())
    sites = block.topology["sites"]["sites"]
    by_region: dict[str, list[dict]] = {}
    for e in sites:
        by_region.setdefault(e["region"], []).append(e)
    assert set(by_region) == {"junction_lo", "junction_hi"}
    roles = {e["role"] for e in sites}
    assert "fused" in roles and "defect" in roles
    for e in sites:
        assert {
            "ordinal",
            "region",
            "role",
            "normal",
            "z_A",
            "r_A",
            "theta_deg",
        } <= set(e)
        assert len(e["normal"]) == 3
    # fused pairs come in twos: sheet atom + tube atom
    fused = [e for e in sites if e["role"] == "fused"]
    assert len(fused) % 2 == 0 and fused
    # round-trip frame exists
    assert "to_stored" in block.topology["sites"]["frame"]


def test_validation_findings_on_small_instance() -> None:
    block = build_hexfold_scene(params(tube=(4, 3), sheet=[24, 24]))
    codes = [f["code"] for f in block.topology["report"]["findings"]]
    assert "termination.covered" in codes or "termination.uncovered" in codes
    assert "geom.contacts" in codes or "geom.contact.short" in codes
    assert codes.count("junction.strain") == 2
    # the clean fast path: every dangling C capped, nothing under 1.2 A
    assert "termination.uncovered" not in codes
    assert "geom.contact.short" not in codes


def test_junction_spacing_reports_short_tunnel() -> None:
    # a len-3 tube puts the mouths well inside the 25 A decay margin:
    # reported, not refused (relax research: decay length ~10-12 A)
    block = build_hexfold_scene(params(tube=(4, 3), sheet=[24, 24]))
    spacing = [
        f
        for f in block.topology["report"]["findings"]
        if f["code"] == "junction.spacing"
    ]
    assert len(spacing) == 1
    assert spacing[0]["severity"] == "WARN"
    assert "25" in spacing[0]["message"]


def test_junction_spacing_long_tunnel_is_clean() -> None:
    # len-12 tube: ~30 A between mouths
    block = build_hexfold_scene(params(tube=(4, 12), sheet=[24, 24]))
    spacing = [
        f
        for f in block.topology["report"]["findings"]
        if f["code"] == "junction.spacing"
    ]
    assert len(spacing) == 1
    assert spacing[0]["severity"] == "INFO"


def test_rail_seam_ends_terminated() -> None:
    block = build_hexfold_scene(
        params(
            tube={"rail": [14, 4], "rails": 2, "rows": 3, "pores": []},
            sheet=[46, 46],
        )
    )
    codes = [f["code"] for f in block.topology["report"]["findings"]]
    assert "seam.end.open" not in codes
    assert "seam.ends" in codes


def test_sites_rail_tunnel_labels_lumen() -> None:
    block = build_hexfold_scene(
        params(
            tube={"rail": [14, 4], "rails": 2, "rows": 3, "pores": []}, sheet=[46, 46]
        )
    )
    sites = block.topology["sites"]["sites"]
    roles = {e["region"]: e["role"] for e in sites}
    assert any(r == "lumen" for r in roles.values())
    assert any(r == "seam" for r in roles.values())
    assert "pairs" in block.topology["sites"]


def test_rail_tunnel_builds() -> None:
    block = build_hexfold_scene(
        params(
            tube={
                "rail": [14, 4],
                "rails": 2,
                "rows": 3,
                "pores": [],
            },
            sheet=[46, 46],
        )
    )
    findings = [f["code"] for f in block.topology["report"]["findings"]]
    assert findings.count("opening.defects") == 2
    # rail ends cannot mint closed arcs across the seam gaps
    assert "opening.face.open" in findings


RAIL = {"rail": [14, 4], "rails": 2, "rows": 3, "pores": []}


@functools.cache
def _rail_inputs() -> tuple[Any, Any, Any]:
    """One shared rail build for the strain tests (read-only; callers copy)."""
    from hexfold.tunnel import tunnel_reservoirs
    from precis_se.atomic.generators.tunnel import _normalize

    tspec, sheet, _name, _k, kind = _normalize(params(tube=RAIL, sheet=[46, 46]))
    net, tun = tunnel_reservoirs(tspec, sheet, kind=kind)
    return build_hexfold_scene(params(tube=RAIL, sheet=[46, 46])), net, tun


def _by_code(block: Any, code: str) -> list[dict[str, Any]]:
    return [f for f in block.topology["report"]["findings"] if f["code"] == code]


def test_rail_tunnel_emits_seam_strain_per_seam() -> None:
    block, _net, tun = _rail_inputs()
    seams = _by_code(block, "seam.strain")
    assert len(seams) == len(tun.seam_atoms) > 0
    for k, f in enumerate(seams):
        assert f["where"] == f"seam{k}"
        d = dict(f["data"]) if not isinstance(f["data"], dict) else f["data"]
        assert d["atoms"] == len(tun.seam_atoms[k])
        for key in ("bonds_min_A", "bonds_max_A", "bonds_mean_A"):
            assert 1.2 <= d[key] <= 1.7
        assert np.isfinite(d["angle_dev_max_deg"])
        assert np.isfinite(d["pyramidal_max_A"])


def test_tunnel_strain_reproduces_build_junction_strain() -> None:
    block, net, tun = _rail_inputs()
    fresh = tunnel_strain(
        block.coords,
        block.elements,
        block.bonds,
        tun.junctions,
        tun.seam_atoms,
        net.lattice.sigma_A,
    )
    stored = {f["where"]: f for f in _by_code(block, "junction.strain")}
    got = [f for f in fresh if f.code == "junction.strain"]
    assert len(got) == 2
    for f in got:
        want = stored[f.where]
        assert f.message == want["message"]
        d = want["data"]
        assert dict(f.data) == (dict(d) if not isinstance(d, dict) else d)


def test_seam_strain_rises_when_a_seam_atom_is_displaced() -> None:
    block, net, tun = _rail_inputs()

    def read(coords: Any) -> list[Any]:
        return [
            dict(f.data)
            for f in tunnel_strain(
                coords,
                block.elements,
                block.bonds,
                tun.junctions,
                tun.seam_atoms,
                net.lattice.sigma_A,
            )
            if f.code == "seam.strain"
        ]

    base = read(block.coords)[0]
    raised = False
    for atom in tun.seam_atoms[0]:
        moved = np.array(block.coords, dtype=float)
        moved[atom, 0] += 0.3
        after = read(moved)[0]
        raised |= (
            after["angle_dev_max_deg"] > base["angle_dev_max_deg"]
            or after["pyramidal_max_A"] > base["pyramidal_max_A"]
        )
    assert raised


@pytest.mark.parametrize(
    "change",
    [
        {"tube": [2, 4]},
        {"tube": [6, 1]},
        {"tube": [6.5, 4]},
        {"tube": "tube(6,6)"},
        {"tube": {"rail": [14]}},
        {"tube": {"rail": [14, 4], "rails": 2}},
        {"features": [{"name": "t", "type": "tunnel-reservoirs"}]},
        {
            "features": [
                {
                    "name": "t",
                    "type": "tunnel-reservoirs",
                    "sheet": [30, 30],
                    "extra": 1,
                }
            ]
        },
        {"features": [{"name": "t", "type": "tunnel-reservoirs", "sheet": [3, 30]}]},
        {"features": [{"name": "t", "type": "rail-k3-120-z", "sheet": [30, 30]}]},
        {"k_tether": 0},
        {"k_tether": "x"},
        {"sheet": [30, 30]},
        {"bogus": 1},
    ],
)
def test_tunnel_reservoirs_invalid_input_refuses(change: dict[str, Any]) -> None:
    with pytest.raises(GeneratorError):
        build_hexfold_scene({**params(), **change})


def test_sheet_too_small_for_mouth_refuses() -> None:
    # a (20,20) tube is far wider than a [8,8] reservoir sheet
    with pytest.raises(GeneratorError, match="too small"):
        build_hexfold_scene(params(tube=(20, 4), sheet=[8, 8]))
