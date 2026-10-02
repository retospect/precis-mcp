"""``geom.clash`` (gr459567): non-bonded atoms closer than ``Profile.clash_A``
in the stick geometry.  Before it, ``geom.*`` covered bonded terms only, so
a fullerene sunk into its host and a crumpled lid both checked clean."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from hexfold.check import _clash_pairs, check

_EX = Path(__file__).resolve().parents[2] / "hexfold" / "examples"

#: gr459595's repro: an unholed cap(36,0) lid once crumpled to 0.63 A
#: self-clashes in stick(); flat (24.7 x 27 x 0.5 A) and clash-free since the
#: winding-signed flat-rim seed normal.
LARGE_LID = """hexfold 0.2
g: tube(36,0, len=7)
l: cap(36,0)
g.out --fuse k=0--> l.in
"""


def _clash(spec: str) -> tuple[list[dict], dict]:
    r = check(spec, geometry=True)
    clashes = [dict(f.data) for f in r.findings if f.code == "geom.clash"]
    summary = next(dict(f.data) for f in r.findings if f.code == "geom.summary")
    return clashes, summary


def test_sunk_bud_reports_a_clash_naming_both_instances() -> None:
    clashes, summary = _clash((_EX / "sheet_bud_22.hx").read_text(encoding="utf-8"))
    assert clashes, "a [2+2] bud seeded into its host must not check clean"
    assert summary["clash_count"] >= len(clashes)
    assert len(clashes) <= 10  # capped like the other geom.* codes
    assert summary["clash_min"] < 1.8
    assert {"b", "h"} in [set(c["instances"]) for c in clashes]
    worst = min(c["distance"] for c in clashes)
    assert worst == summary["clash_min"]


@pytest.mark.parametrize(
    "spec",
    [
        (_EX / "pillar.hx").read_text(encoding="utf-8"),
        (_EX / "valve_shell.hx").read_text(encoding="utf-8"),
        LARGE_LID,
    ],
    ids=["pillar", "valve_shell", "large_lid_gr459595"],
)
def test_clean_builds_report_no_clash(spec: str) -> None:
    clashes, summary = _clash(spec)
    assert clashes == []
    assert summary["clash_count"] == 0
    assert summary["clash_min"] is None


def test_clash_pairs_skip_bonded_and_shared_neighbour_pairs() -> None:
    # a 4-chain 0-1-2-3 folded so every pair sits 1.0 A apart, plus a
    # free atom 4 near atom 0: only the 1-4 pair (0,3) and the unbonded
    # (0,4) count; 1-2 and 1-3 pairs are left to bond/angle checks
    coords = np.array(
        [[0, 0, 0], [1, 0, 0], [0.5, 0.8, 0], [0, 0.9, 0.3], [0, 0, 1.2]],
        dtype=np.float64,
    )
    bonds = [(0, 1, 1), (1, 2, 1), (2, 3, 1)]
    pairs = _clash_pairs(coords, bonds, 1.8)
    found = {(i, j) for _d, i, j in pairs}
    assert (0, 1) not in found and (0, 2) not in found and (1, 3) not in found
    assert (0, 3) in found and (0, 4) in found
    assert [d for d, *_ in pairs] == sorted(d for d, *_ in pairs)


def test_clash_bar_comes_from_the_profile() -> None:
    from hexfold.report import Profile

    spec = (_EX / "sheet_bud_22.hx").read_text(encoding="utf-8")
    loose = check(spec, geometry=True, profile=Profile(clash_A=0.1))
    assert not [f for f in loose.findings if f.code == "geom.clash"]
