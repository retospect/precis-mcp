"""``geom.nearest_not_bonded`` (gr459567, Reto 2026-10-12): every atom's
nearest non-bonded atom must lie farther than its nearest bonded one, or
the bond list disagrees with the spatial neighbours."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from hexfold.build import build
from hexfold.check import _nearest_not_bonded, check
from hexfold.report import Severity
from hexfold.stick import stick_info

_EX = Path(__file__).resolve().parents[2] / "hexfold" / "examples"

#: tangled by construction (gr462074) or regressed by 5c692aad3's 1-3
#: repulsion exclusion (cone5's degree-1 rim atom folds onto its neighbour's
#: other neighbour, 0.99 A); everything else must pass
_FAILING = {"tube_ring_closure", "cone5"}

_JUNCTIONS = {
    "skill_tube1010_96": (
        "hexfold 0.2\norigin h\nh: tube(10,10,len=14)\nb: fullerene(C60)\n"
        "b @ h/(7,0,A):0 [9-6]\n"
    ),
    "hexa_nanobud_pillar": (
        "hexfold 0.2\norigin s\ns: sheet(24,24) - hex(1)@(11,11,A):0\n"
        "t: tube(12,0, len=6)\nc: cap(12,0)\nb: fullerene(C60)\n"
        "s.hole --fuse k=0--> t.in\nt.out --fuse k=0--> c.in\n"
        "b @ c/(0,0,A):0 [9-6]\n"
    ),
}


def _stacked_sheets(gap: float) -> tuple[np.ndarray, list[tuple[int, int, int]]]:
    net = build("hexfold 0.2\norigin s\ns: sheet(8,8)\n", strict=False)
    coords, _ = stick_info(net)
    one = np.asarray(coords)
    k = len(one)
    bonds = list(net.bonds) + [(i + k, j + k, o) for i, j, o in net.bonds]
    return np.vstack([one, one + [0.0, 0.0, gap]]), bonds


def test_two_sheets_one_angstrom_apart_fail_the_rule() -> None:
    coords, bonds = _stacked_sheets(1.0)
    nearest, longest, margin, _ = _nearest_not_bonded(coords, bonds)
    k = len(coords) // 2
    assert len(nearest) == len(coords)  # every atom sees the other sheet first
    assert len(longest) == len(coords)
    worst = nearest[0]
    assert (worst.atom < k) != (worst.other < k)  # across the two sheets
    assert worst.distance == pytest.approx(1.0, abs=1e-6)
    assert margin is not None and margin < -0.3


def test_two_sheets_at_graphite_spacing_pass() -> None:
    coords, bonds = _stacked_sheets(3.35)
    nearest, longest, _, _ = _nearest_not_bonded(coords, bonds)
    assert nearest == [] and longest == []


def _report(spec: str) -> tuple[list, dict]:
    r = check(spec, geometry=True)
    found = [f for f in r.findings if f.code == "geom.nearest_not_bonded"]
    summary = next(dict(f.data) for f in r.findings if f.code == "geom.summary")
    return found, summary


@pytest.mark.parametrize(
    "name",
    sorted(p.stem for p in _EX.glob("*.hx") if p.stem not in _FAILING),
)
def test_shipped_examples_pass(name: str) -> None:
    found, summary = _report((_EX / f"{name}.hx").read_text(encoding="utf-8"))
    assert found == [], [f.message for f in found]
    assert summary["nnb_count"] == 0


@pytest.mark.parametrize("name", sorted(_JUNCTIONS))
def test_bud_junctions_pass_against_the_nearest_bond(name: str) -> None:
    # gr459567: before 0.3.5's repulsion the pillar bud failed (1.37 A
    # non-bonded against a 1.47 A bond); the stretched junction bonds still
    # fail the longest-bond variant, which stays a summary count
    found, summary = _report(_JUNCTIONS[name])
    assert found == [], [f.message for f in found]
    assert summary["nnb_count"] == 0


def test_tangled_ring_closure_is_an_error_naming_both_atoms() -> None:
    found, summary = _report((_EX / "tube_ring_closure.hx").read_text(encoding="utf-8"))
    assert found and {f.severity for f in found} == {Severity.ERROR}
    assert len(found) <= 10
    assert summary["nnb_count"] >= len(found)
    rows = [dict(f.data) for f in found]
    for data in rows:
        assert data["distance"] <= data["nearest_bond"] <= data["longest_bond"]
        assert len(data["atoms"]) == len(data["instances"]) == 2
    # the report is sorted by location, the listed ten are the worst margins
    worst = min(d["distance"] - d["nearest_bond"] for d in rows)
    assert worst == pytest.approx(summary["nnb_margin"], abs=2e-3)


@pytest.mark.xfail(
    strict=True,
    reason="5c692aad3 stopped repelling 1-3 pairs; cone5's degree-1 rim atom "
    "460 folds to 0.99 A of atom 497 (gr479847)",
)
def test_cone5_rim_atom_keeps_its_angle_open() -> None:
    found, _ = _report((_EX / "cone5.hx").read_text(encoding="utf-8"))
    assert found == []
