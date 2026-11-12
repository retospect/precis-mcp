"""Fit families (SPEC 12.1, 0.2): every fit site enumerates its family,
ranks by (seam ring, |residual|, lattice index) and reports the ranked
remainder as ``fit.alternatives``, without changing the 0.1 winner."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import numpy as np
import pytest

from hexfold.build import (
    Net,
    _apply_hole,
    _fit_alternatives_finding,
    _rank_fit,
    _sheet_sites,
    _solve_collar,
    build,
)
from hexfold.defects import Defect, Patch, Vid
from hexfold.lattice import Lattice, Site
from hexfold.report import Finding, Report

_ROOT = Path(__file__).resolve().parents[2] / "hexfold"


def _net(text: str) -> Net:
    return build(text, strict=False)


def _alt_findings(net: Net) -> list:
    return [f for f in net.report.findings if f.code == "fit.alternatives"]


# ---------- len=fit ----------


def test_len_fit_family_applied_unchanged() -> None:
    # same example as test_len_fit_picks_smallest_clean_len: applied len
    # must stay 7, the 0.1 winner.
    net = _net(
        """hexfold 0.1
origin t
t: tube(10,10,len=fit) - hexagon@(7,0,A):0
"""
    )
    fs = _alt_findings(net)
    assert len(fs) == 1
    data = dict(fs[0].data)
    assert data["param"] == "len"
    assert data["applied"] == 7
    inst = net.spec.instance("t")
    assert inst is not None
    got = int(dict(inst.params)["len"])
    assert got == 7


def test_len_fit_alternatives_are_further_clean_lens_bounded() -> None:
    net = _net(
        """hexfold 0.1
origin t
t: tube(10,10,len=fit) - hexagon@(7,0,A):0
"""
    )
    data = dict(_alt_findings(net)[0].data)
    values = [a["value"] for a in data["alternatives"]]
    # every len from 8 upward is clean too (once the hole clears, it stays
    # clear); the family is capped at the winner (7) plus <=7 further
    # members, so the alternatives are exactly 8..14.
    assert values == [8, 9, 10, 11, 12, 13, 14]
    costs = [tuple(a["cost"]) for a in data["alternatives"]]
    assert costs == sorted(costs)


# ---------- fuse k=fit ----------


def test_fuse_k_fit_applied_unchanged_da_neck() -> None:
    # the DA-neck menu (Baowan, Cox & Hill 2010) synthesises a
    # `neck.out --fuse k=fit--> host_hole` connect (menus.py host_k=-1);
    # its 5-dangling host opening gives a family of 5 registrations.
    text = (_ROOT / "examples" / "nanobud_da_neck.hx").read_text(encoding="utf-8")
    net = _net(text)
    fs = _alt_findings(net)
    assert len(fs) == 1
    data = dict(fs[0].data)
    assert data["param"] == "k"
    assert data["applied"] == 0  # unchanged from before this slice
    alts = data["alternatives"]
    assert [a["value"] for a in alts] == [1, 2, 3, 4]  # N-1 = 4 entries
    costs = [tuple(a["cost"]) for a in alts]
    assert costs == sorted(costs)
    # every registration of this 5-fold seam yields the same ring-size
    # multiset {6,7,7,8,8} (max 8, defect charge sum|6-n| = 6), so the
    # applied k's own cost equals the first alternative's — verifying
    # "first alternative's cost >= applied cost" for the tie case.
    assert costs[0] == (8.0, 6.0, 1)


def test_fuse_k_fit_family_ties_broken_by_k() -> None:
    text = (_ROOT / "examples" / "nanobud_da_neck.hx").read_text(encoding="utf-8")
    net = _net(text)
    data = dict(_alt_findings(net)[0].data)
    # (2) max seam ring and (3) |residual| are identical for every kc of
    # this symmetric seam; only (4) the lattice index (k itself) differs.
    for a in data["alternatives"]:
        assert a["cost"][0] == 8.0
        assert a["cost"][1] == 6.0


# ---------- collar {Rxk @fit} ----------


def test_collar_fit_applied_unchanged_and_alternatives_nonempty() -> None:
    net = _net(
        """hexfold 0.1
h: tube(5,5, len=14) - hexagon@(7,0,A):0
t: tube(6,0, len=4)
t.out --fuse{7x5 @fit} k=0--> h.hole
"""
    )
    fs = _alt_findings(net)
    assert len(fs) == 1
    data = dict(fs[0].data)
    assert data["param"] == "collar"
    # the 0.1 winner: the smallest-radius admissible orbit, base (3,3,B).
    assert data["applied"] == ["(0,0,B)", "(1,1,B)", "(2,2,B)", "(3,3,B)", "(4,4,B)"]
    assert data["alternatives"]  # non-empty: a second admissible orbit exists
    assert len(data["alternatives"]) == 1
    assert data["alternatives"][0]["cost"] == [0.0, 0.0, 1]
    assert "fit.unsolvable" not in [f.code for f in net.report.findings]


# ---------- collar solver copies + preserved results (gr647531) ----------


def _holed_sheet_patch() -> tuple[
    Patch, frozenset[Vid], frozenset[Site], list[Finding]
]:
    """A 10x10 sheet minus one hexagon ring — the smallest patch whose 7x3
    collar family stays cheap enough to solve in a unit test."""
    patch = Patch(Lattice(), _sheet_sites(10, 10))
    findings: list[Finding] = []
    got = _apply_hole(patch, 6, Site(5, 5, 0), 0, "s", findings)
    assert got is not None
    rim_fs, disk = got
    return patch, rim_fs, disk, findings


def test_collar_solve_leaves_the_input_patch_untouched() -> None:
    """Every candidate orbit is tried on a deepcopy of the patch being
    built: a mutable container ``Patch.__deepcopy__`` forgot to copy would
    leak trial surgery into the real net."""
    patch, rim_fs, disk, findings = _holed_sheet_patch()
    flat0 = dict(patch.flatpos)
    edges0 = set(patch.edges)
    dirs0 = dict(patch.dirs)
    hole_b0 = dict(patch.hole_b)
    hole_bexp0 = dict(patch.hole_bexp)
    solved = _solve_collar(patch, rim_fs, disk, 7, 3, "s", findings)
    assert len(solved) == 3
    assert patch.flatpos.keys() == flat0.keys()
    assert all(np.array_equal(patch.flatpos[v], p0) for v, p0 in flat0.items())
    assert patch.edges == edges0
    assert patch.dirs.keys() == dirs0.keys()
    assert all(np.array_equal(patch.dirs[e], d0) for e, d0 in dirs0.items())
    assert patch.hole_b == hole_b0
    assert patch.hole_bexp == hole_bexp0
    assert patch.discl == []


def test_patch_deepcopy_surgery_does_not_leak_into_source() -> None:
    """``__deepcopy__`` copies every mutable container and shares only the
    frozen leaves: inserting on the copy grows its flatpos *and* back-fills
    phi on the copy's own version of the earlier defect's chart — neither
    write may reach the original."""
    patch = Patch(Lattice(), _sheet_sites(10, 10))
    patch.insert(Defect(7, Site(3, 3, 0), 0), 0)
    flat0 = set(patch.flatpos)
    edges0 = set(patch.edges)
    dirs0 = set(patch.dirs)
    phi0 = set(patch.discl[0].phi)
    work = copy.deepcopy(patch)
    # the frozen lattice is the one leaf the copy shares on purpose
    assert work.lat is patch.lat
    work.insert(Defect(7, Site(7, 7, 0), 0), 1)
    # sanity: the surgery really ran on the copy
    assert len(work.flatpos) > len(flat0)
    assert len(work.discl) == 2
    assert set(work.discl[0].phi) > phi0
    assert set(patch.flatpos) == flat0
    assert patch.edges == edges0
    assert set(patch.dirs) == dirs0
    assert len(patch.discl) == 1
    assert set(patch.discl[0].phi) == phi0


def test_collar_sheet_fit_preserves_winner_and_alternatives() -> None:
    """The vectorized sheet-orbit enumeration (gr647531) must report the
    same family the per-site scan did: this pins the 0.1 winner, the full
    ranked remainder, and that the winner — not an alternative — is what
    the fuse records."""
    net = _net(
        """hexfold 0.1
s: sheet(10,10) - hexagon@(5,5,A):0
t: tube(6,0, len=4)
t.out --fuse{7x3 @fit} k=0--> s.hole
"""
    )
    fs = _alt_findings(net)
    assert len(fs) == 1
    data = dict(fs[0].data)
    assert data["param"] == "collar"
    # the 0.1 winner: the first admissible orbit in enumeration order.
    assert data["applied"] == ["(1,8,B)", "(6,0,B)", "(9,5,B)"]
    assert [a["value"] for a in data["alternatives"]] == [
        ["(1,9,A)", "(6,0,A)", "(9,5,B)"],
        ["(1,9,B)", "(5,0,B)", "(9,5,B)"],
        ["(1,9,B)", "(5,0,B)", "(9,4,B)"],
        ["(0,9,B)", "(6,0,A)", "(9,6,B)"],
        ["(1,9,B)", "(5,0,A)", "(9,4,B)"],
        ["(0,9,A)", "(7,0,A)", "(9,6,B)"],
        ["(0,8,A)", "(7,0,A)", "(9,7,B)"],
        ["(0,5,A)", "(6,9,B)", "(9,0,B)"],
        ["(0,6,A)", "(7,9,B)", "(9,0,A)"],
        ["(2,9,B)", "(4,0,A)", "(9,4,B)"],
        ["(0,4,A)", "(6,9,B)", "(9,0,B)"],
        ["(0,8,A)", "(8,0,A)", "(9,8,B)"],
        ["(0,7,A)", "(8,9,B)", "(9,0,A)"],
        ["(2,9,B)", "(3,0,A)", "(9,3,B)"],
        ["(0,3,A)", "(5,9,B)", "(9,0,B)"],
        ["(0,7,A)", "(8,0,A)", "(9,9,B)"],
        ["(2,0,A)", "(3,9,B)", "(9,3,B)"],
        ["(0,2,A)", "(5,9,B)", "(9,1,B)"],
        ["(1,0,A)", "(3,9,B)", "(9,2,B)"],
        ["(0,1,A)", "(4,9,B)", "(9,1,B)"],
        ["(0,0,A)", "(4,9,B)", "(9,2,B)"],
    ]
    costs = [a["cost"] for a in data["alternatives"]]
    assert costs == [[0.0, 0.0, i] for i in range(1, 22)]
    assert "fit.unsolvable" not in [f.code for f in net.report.findings]
    # the applied winner lands on the connect's expansion record.
    [conn] = [c for c in net.spec.connects if (c.expanded or {}).get("defects")]
    defects = (conn.expanded or {})["defects"]
    assert [(d["site"], d["dir"]) for d in defects] == [
        ("(9,5,B)", 0),
        ("(1,8,B)", 1),
        ("(6,0,B)", 5),
    ]


# ---------- one-member family ----------


def test_single_member_family_reports_empty_alternatives() -> None:
    # exercises the shared mechanism every fit site calls: with exactly
    # one candidate, fit.alternatives still fires (consumers rely on the
    # code to know a fit happened) but its alternatives list is empty.
    ranked = _rank_fit([(7, 0.0, 0.0, 7)])
    assert len(ranked) == 1
    finding = _fit_alternatives_finding("len", "t", None, ranked[0][0], ranked[1:])
    assert finding.code == "fit.alternatives"
    data = dict(finding.data)
    assert data["alternatives"] == []
    assert data["applied"] == 7


# ---------- determinism ----------


def test_fit_alternatives_report_json_stable_across_builds() -> None:
    text = (_ROOT / "examples" / "nanobud_da_neck.hx").read_text(encoding="utf-8")
    net1 = _net(text)
    net2 = _net(text)
    d1 = net1.report.to_dict()
    d2 = net2.report.to_dict()
    assert "fit.alternatives" in {f["code"] for f in d1["findings"]}
    s1 = json.dumps(d1, sort_keys=True)
    s2 = json.dumps(d2, sort_keys=True)
    assert s1 == s2


def test_fit_alternatives_finding_data_is_sorted_tuple_of_pairs() -> None:
    net = _net(
        """hexfold 0.1
origin t
t: tube(10,10,len=fit) - hexagon@(7,0,A):0
"""
    )
    f = [x for x in net.report.findings if x.code == "fit.alternatives"][0]
    keys = [k for k, _ in f.data]
    assert keys == sorted(keys)


def test_rank_fit_asserts_no_tie_past_lattice_index() -> None:
    with pytest.raises(AssertionError):
        _rank_fit([(1, 0.0, 0.0, 0), (2, 0.0, 0.0, 0)])


def test_report_merge_keeps_sorted_and_serialisable() -> None:
    net = _net(
        """hexfold 0.1
origin t
t: tube(10,10,len=fit) - hexagon@(7,0,A):0
"""
    )
    merged = Report(net.report.findings).merge(Report(()))
    assert merged.findings == net.report.sorted().findings
    assert json.dumps(merged.to_dict())
