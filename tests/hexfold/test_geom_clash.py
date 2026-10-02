"""``geom.clash`` (gr459567): non-bonded atoms closer than ``Profile.clash_A``
in the stick geometry.  Before it, ``geom.*`` covered bonded terms only, so
a fullerene sunk into its host and a crumpled lid both checked clean."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from hexfold.check import _clash_pairs, check
from hexfold.report import Profile, Severity

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


def test_squeezed_bud_neck_reports_a_warn_clash_naming_both_instances() -> None:
    # a [9-6] neck squeezes to ~1.5 A under stick: a clash, not an overlap
    r = check((_EX / "nanobud_96.hx").read_text(encoding="utf-8"), geometry=True)
    found = [f for f in r.findings if f.code == "geom.clash"]
    clashes = [dict(f.data) for f in found]
    summary = next(dict(f.data) for f in r.findings if f.code == "geom.summary")
    assert clashes
    assert {f.severity for f in found} == {Severity.WARN}
    assert summary["clash_count"] >= len(clashes)
    assert len(clashes) <= 10  # capped like the other geom.* codes
    assert {"b", "h"} in [set(c["instances"]) for c in clashes]
    worst = min(c["distance"] for c in clashes)
    assert worst == summary["clash_min"]


def test_clash_under_the_overlap_bar_is_an_error() -> None:
    spec = (_EX / "nanobud_96.hx").read_text(encoding="utf-8")
    r = check(spec, geometry=True, profile=Profile(clash_error_A=1.6))
    found = [f for f in r.findings if f.code == "geom.clash"]
    by_sev = {
        sev: [dict(f.data)["distance"] for f in found if f.severity == sev]
        for sev in (Severity.ERROR, Severity.WARN)
    }
    assert by_sev[Severity.ERROR] and all(d < 1.6 for d in by_sev[Severity.ERROR])
    assert all(d >= 1.6 for d in by_sev[Severity.WARN])


# ---------- nanobud placement (gr459567 gap 3) ----------

#: se design nanobud-review-figs' five blocks, verbatim (prod, 2026-10-02):
#: before the placement fix they measured 0.62-0.85 A ([2+2] ball lying on
#: the wall, [9-6]/[8-7] ball inside its tube or under its sheet)
REVIEW_FIGS = {
    "bud22": ("tube(8,8, len=12)", "h", "(6,0,A):0 [2+2]"),
    "bud87": ("tube(8,8, len=12)", "h", "(6,0,A):0 [8-7]"),
    "bud96": ("tube(8,8, len=12)", "h", "(6,0,A):0 [9-6]"),
    "gsheet22": ("sheet(25A, 12)", "s", "(5,5,A):0 [2+2]"),
    "gsheet96": ("sheet(25A, 12)", "s", "(5,5,A):0 [9-6]"),
}


def _fig(host: str, h: str, site: str, origin: str | None = None) -> str:
    return (
        f"hexfold 0.2\norigin {origin or h}\n{h}: {host}\n"
        f"b: fullerene(C60)\nb @ {h}/{site}\n"
    )


@pytest.mark.parametrize("name", sorted(REVIEW_FIGS))
def test_review_figure_buds_do_not_overlap_their_host(name: str) -> None:
    host, h, site = REVIEW_FIGS[name]
    r = check(_fig(host, h, site), geometry=True)
    errors = [
        f for f in r.findings if f.code == "geom.clash" and f.severity == Severity.ERROR
    ]
    assert errors == []
    summary = next(dict(f.data) for f in r.findings if f.code == "geom.summary")
    if "[2+2]" in site:
        # the cycloadduct leaves no squeezed neck
        assert summary["clash_count"] == 0, summary["clash_min"]


def _summary_and_place(spec: str) -> tuple[dict, set[str]]:
    r = check(spec, geometry=True)
    summary = next(dict(f.data) for f in r.findings if f.code == "geom.summary")
    return summary, {f.code for f in r.findings if f.code.startswith("place.")}


@pytest.mark.parametrize(
    ("host", "h", "site"),
    [
        REVIEW_FIGS["bud22"],  # bond path, tube
        REVIEW_FIGS["gsheet22"],  # bond path, sheet: seated edge-on if wrong
        ("tube(8,8, len=12)", "h", "(1,0,A):0 [2+2]"),  # 12.9 of 14.1 A to the end
        REVIEW_FIGS["bud96"],  # menu path
    ],
    ids=["bud22", "gsheet22", "tube_end_22", "bud96"],
)
def test_bud_placement_does_not_depend_on_the_bfs_origin(
    host: str, h: str, site: str
) -> None:
    # with the bud as origin the host is the moved side: the bond path
    # orients it by its own surface normal and the menu path reflects the
    # bud's seed up front, so both roots give the same geometry
    by_root = {o: _summary_and_place(_fig(host, h, site, o)) for o in (h, "b")}
    (s_h, p_h), (s_b, p_b) = by_root[h], by_root["b"]
    assert s_h["clash_count"] == s_b["clash_count"], by_root
    assert p_h == p_b, by_root
    assert s_b["clash_min"] is None or s_b["clash_min"] > Profile.DEFAULT.clash_error_A


def test_mirrored_bud_is_reported() -> None:
    _s, menu = _summary_and_place(_fig(*REVIEW_FIGS["bud96"]))
    assert menu == {"place.mirrored"}
    _s, bond = _summary_and_place(_fig(*REVIEW_FIGS["bud22"]))
    assert bond == set()


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
    spec = (_EX / "sheet_bud_22.hx").read_text(encoding="utf-8")
    loose = check(spec, geometry=True, profile=Profile(clash_A=0.1))
    assert not [f for f in loose.findings if f.code == "geom.clash"]
