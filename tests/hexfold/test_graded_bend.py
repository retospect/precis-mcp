"""Graded bends (gr459928): a sheet-to-tube foot whose six heptagons sit in
two or three seam rows instead of one.  Each row is a flat lid with a
centred p-wedge cut (a cone frustum, rim terms 6-p and -(6-p)), so each
seam mints only the step in turning between neighbours.  Also pins the two
surgery bugs that blocked it: heptagon insertion merging A/B wedge copies,
and a ``hex(r)`` hole centred one ring off a disclination core."""

from __future__ import annotations

from collections import Counter

import pytest

from hexfold.build import Net, build
from hexfold.check import check


def _net(text: str) -> Net:
    return build(text, strict=False)


def _errors(net: Net) -> list[str]:
    return [f.code for f in net.report.findings if f.severity.name == "ERROR"]


def _seam_census(net: Net) -> Counter[tuple[int, str]]:
    inst = [a.instance for a in net.atoms]
    return Counter(
        (len(r), "+".join(sorted({inst[i] for i in r})))
        for r in net.rings
        if len(r) != 6
    )


# ---------- heptagon insertion ----------


@pytest.mark.parametrize(
    "defects",
    [
        "+ heptagon@(15,15,A):1",  # odd dir: wedge frame turned 60 deg
        "+ heptagon@(15,15,B):0",  # B-site apex
        "+ heptagon@(20,20,A):1 + heptagon@(8,8,A):4",
    ],
)
def test_sheet_heptagons_insert_cleanly(defects: str) -> None:
    net = _net(f"hexfold 0.2\norigin s\ns: sheet(30,30) {defects}\n")
    assert _errors(net) == []
    k = defects.count("heptagon")
    assert Counter(len(r) for r in net.rings).keys() <= {6, 7}
    assert Counter(len(r) for r in net.rings)[7] == k
    assert dict(net.ports)["rim"].b_expected == 6 + k


# ---------- cone-frustum lids ----------


@pytest.mark.parametrize(
    ("lid", "p", "hole_r", "outer", "inner"),
    [
        ("cap(36,0)", "square", 3, 24, 16),  # p=2: 4/6 of the disc
        ("cap(36,0)", "3", 3, 18, 12),  # p=3
        ("cap(48,0)", "2", 5, 16, 12),  # p=4: core collapses to a digon
    ],
)
def test_frustum_lid(lid: str, p: str, hole_r: int, outer: int, inner: int) -> None:
    wedges = {"square": 2, "3": 3, "2": 4}[p]
    net = _net(
        f"hexfold 0.2\norigin b\nb: {lid} + {p}@(-1,0,A):2 - hex({hole_r})@(-1,0,A):0\n"
    )
    assert _errors(net) == []
    assert {len(r) for r in net.rings} == {6}
    ports = dict(net.ports)
    assert len(ports["in"].dangling) == outer
    assert len(ports["hole"].dangling) == inner
    assert ports["in"].b_expected == 6 - wedges
    assert ports["hole"].b_expected == -(6 - wedges)


# ---------- graded feet ----------

FOOT_33 = """hexfold 0.2
origin s
s: sheet(30,30) - hex(2)@(14,14,A):0
b: cap(36,0) + 3@(-1,0,A):2 - hex(3)@(-1,0,A):0
t: tube(12,0, len=6)
s.hole --fuse k=0--> b.in
b.hole --fuse k=0--> t.in
"""

FOOT_222 = """hexfold 0.2
origin s
s: sheet(30,30) - hex(3)@(14,14,A):0
b: cap(36,0) + square@(-1,0,A):2 - hex(3)@(-1,0,A):0
c: cap(48,0) + 2@(-1,0,A):2 - hex(5)@(-1,0,A):0
t: tube(12,0, len=6)
s.hole --fuse k=0--> b.in
b.hole --fuse k=0--> c.in
c.hole --fuse k=0--> t.in
"""


def test_foot_3_plus_3() -> None:
    net = _net(FOOT_33)
    assert _errors(net) == []
    assert _seam_census(net) == Counter({(7, "b+s"): 3, (7, "b+t"): 3})
    res = [f for f in check(FOOT_33).findings if f.code == "euler.residual"]
    assert res and all(dict(f.data)["residual"] == 0 for f in res)


def test_foot_2_plus_2_plus_2() -> None:
    net = _net(FOOT_222)
    assert _errors(net) == []
    census = _seam_census(net)
    # the two cone-to-cone/tube seams are clean: two heptagons each
    assert census[(7, "b+c")] == 2
    assert census[(7, "c+t")] == 2
    # flat hole (6 corners) onto the p=2 frustum (4): net -2, but a regular
    # hex hole lines up only 2 corners, so two 5-7 pairs ride along
    assert census[(7, "b+s")] - census[(5, "b+s")] == 2
    res = [f for f in check(FOOT_222).findings if f.code == "euler.residual"]
    assert res and all(dict(f.data)["residual"] == 0 for f in res)
