"""Fit families (SPEC 12.1, 0.2): every fit site enumerates its family,
ranks by (seam ring, |residual|, lattice index) and reports the ranked
remainder as ``fit.alternatives``, without changing the 0.1 winner."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hexfold.build import Net, _fit_alternatives_finding, _rank_fit, build
from hexfold.report import Report

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
    # multiset {6,7,7,8,8} (max 8, local residual contribution 6), so the
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
