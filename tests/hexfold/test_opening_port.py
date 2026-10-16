"""``opening(port=X)@site``: a sheet hole solved from a tube rim (SPEC 11.4)."""

from __future__ import annotations

import json

import numpy as np
import pytest

from hexfold.build import Net, build
from hexfold.canon import canonical_json
from hexfold.check import check
from hexfold.report import BuildError, ParseError, Profile, Report, Severity
from hexfold.stick import stick
from hexfold.text import OPENING_RING, parse, spec_from_dict, to_text

ONE = """hexfold 0.2
t: tube(6,6, len=6)
a: sheet(20,20) - opening(port=t.in)@(10,10,A)
"""

TWO = """hexfold 0.2
a: sheet(20,20) - opening(port=t.in)@(10,10,A)
t: tube(6,6, len=6)
b: sheet(20,20) - opening(port=t.out)@(10,10,A)
"""


def _codes(report: Report, sev: Severity) -> list[str]:
    return [f.code for f in report.findings if f.severity == sev]


def _components(net: Net) -> int:
    parent = list(range(len(net.atoms)))

    def root(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, j, _ in net.bonds:
        parent[root(i)] = root(j)
    return len({root(x) for x in range(len(net.atoms))})


def _cross_bond_lengths(net: Net) -> list[float]:
    """Relaxed (stick) lengths of the bonds between tube ``t`` and a sheet."""
    pos = stick(net)
    return [
        float(np.linalg.norm(pos[i] - pos[j]))
        for i, j, _ in net.bonds
        if (net.atoms[i].instance == "t") != (net.atoms[j].instance == "t")
    ]


def test_one_opening_builds_clean_and_connected() -> None:
    net = build(ONE, strict=False)
    assert _codes(net.report, Severity.ERROR) == []
    defects = [f for f in net.report.findings if f.code == "opening.defects"]
    assert len(defects) == 1
    assert dict(defects[0].data)["pairs"] == 12
    assert _components(net) == 1
    assert "opening.face.open" not in {f.code for f in net.report.findings}
    # the fused bonds relax into the validator's bond band around sigma
    lengths = _cross_bond_lengths(net)
    assert len(lengths) == 12
    sigma, tol = net.lattice.sigma_A, Profile.DEFAULT.bond_tol_A
    assert all(abs(d - sigma) <= tol for d in lengths), lengths


def test_one_opening_check_has_no_clash_or_overlap() -> None:
    r = check(ONE, geometry=True)
    assert _codes(r, Severity.ERROR) == []
    assert not [f for f in r.findings if f.code in ("geom.clash", "geom.seed_overlap")]
    summary = next(f for f in r.findings if f.code == "geom.summary")
    assert "0 bonds and 0 angles past tolerance" in summary.message


def test_two_openings_sheet_tube_sheet() -> None:
    net = build(TWO, strict=False)
    assert _codes(net.report, Severity.ERROR) == []
    defects = [f for f in net.report.findings if f.code == "opening.defects"]
    assert [dict(f.data)["pairs"] for f in defects] == [12, 12]
    assert _components(net) == 1
    lengths = _cross_bond_lengths(net)
    assert len(lengths) == 24
    sigma, tol = net.lattice.sigma_A, Profile.DEFAULT.bond_tol_A
    assert all(abs(d - sigma) <= tol for d in lengths), lengths
    r = check(TWO, geometry=True)
    assert _codes(r, Severity.ERROR) == []


def test_opening_with_fuse_is_refused() -> None:
    text = ONE + "u: tube(6,6, len=3)\nt.out --fuse k=0--> u.in\n"
    net = build(text, strict=False)
    assert "opening.combined" in _codes(net.report, Severity.ERROR)
    assert "opening.combined" in {f.code for f in check(text).findings}


def test_opening_with_fit_is_refused() -> None:
    net = build(ONE.replace("len=6", "len=fit"), strict=False)
    assert "opening.fit" in _codes(net.report, Severity.ERROR)


def test_second_opening_on_same_pair_is_a_placement_error() -> None:
    text = ONE.replace("(10,10,A)\n", "(10,10,A) - opening(port=t.out)@(5,5,A)\n")
    net = build(text, strict=False)
    assert "opening.placement" in _codes(net.report, Severity.ERROR)


def test_ring_opening_without_port_in_dict_is_refused() -> None:
    doc = json.loads(canonical_json(build(ONE, strict=False)))
    del doc["instances"]["a"]["holes"][0]["port"]
    with pytest.raises(ParseError):
        spec_from_dict(doc)


@pytest.mark.parametrize(("tube", "word"), [("9,0", "zigzag"), ("7,3", "chiral")])
def test_non_armchair_rim_refused(tube: str, word: str) -> None:
    text = (
        f"hexfold 0.2\nt: tube({tube}, len=3)\n"
        "a: sheet(20,20) - opening(port=t.in)@(10,10,A)\n"
    )
    net = build(text, strict=False)
    bad = [f for f in net.report.findings if f.code == "opening.rim.unsupported"]
    assert len(bad) == 1 and bad[0].severity == Severity.ERROR
    assert word in bad[0].message and "not supported yet" in bad[0].message
    with pytest.raises(BuildError):
        build(text)
    assert "opening.rim.unsupported" in {f.code for f in check(text).findings}


def test_unknown_port_is_an_error() -> None:
    net = build(ONE.replace("t.in", "t.bad"), strict=False)
    assert "port.unknown" in _codes(net.report, Severity.ERROR)


def test_text_round_trip() -> None:
    spec = parse(TWO)
    b = spec.instance("b")
    assert b is not None
    hole = b.holes[0]
    assert hole.ring == OPENING_RING and hole.port == "t.out"
    again = parse(to_text(spec))
    b2 = again.instance("b")
    assert b2 is not None and b2.holes == b.holes
    assert to_text(again) == to_text(spec)


def test_canonical_json_round_trip() -> None:
    doc = json.loads(canonical_json(build(ONE, strict=False)))
    h = doc["instances"]["a"]["holes"][0]
    assert h["port"] == "t.in" and h["ring"] == "opening"
    back = spec_from_dict(doc)
    a = back.instance("a")
    assert a is not None and a.holes[0].port == "t.in"
