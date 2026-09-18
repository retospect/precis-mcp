"""cap(n,m) flat-lid family: cap(6k,0), k >= 1 -- the hex(k-1) flake seated
on a zigzag tube end (SPEC 6.1, 28.3).  The C60 hemisphere cap(5,5) keeps
its own tests in test_phase3.py; this file is the lid family only."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest

from hexfold.build import Net, build
from hexfold.check import check
from hexfold.defects import counting_residual
from hexfold.stick import stick_info

ROOT = Path(__file__).resolve().parents[2] / "hexfold"


def _net(text: str) -> Net:
    return build(text, strict=False)


def _counting_ok(net: Net) -> bool:
    pn = Counter(len(r) for r in net.rings)
    b = sum(p.b for _, p in net.ports)
    chi = len(net.atoms) - len(net.bonds) + len(net.rings)
    return counting_residual(dict(pn), b, chi) == 0


# ---------- standalone lid ----------


@pytest.mark.parametrize(
    "n, natoms, ndangling", [(6, 6, 6), (12, 24, 12), (18, 54, 18)]
)
def test_flat_lid_standalone(n: int, natoms: int, ndangling: int) -> None:
    net = _net(f"hexfold 0.1\norigin b\nb: cap({n},0)\n")
    assert not any(f.severity.name == "ERROR" for f in net.report.findings)
    assert len(net.atoms) == natoms
    assert [nm for nm, _ in net.ports] == ["in"]
    port = dict(net.ports)["in"]
    assert len(port.dangling) == ndangling
    assert set(re.sub(r"\d+", "", port.word)) == {"z"}  # edge-word all zigzag
    assert port.b_expected == 6
    assert all(len(r) == 6 for r in net.rings)
    assert _counting_ok(net)


def test_flat_lid_edge_word_all_zigzag() -> None:
    # run_length collapses same-turn runs; an all-zigzag rim is one run
    net = _net("hexfold 0.1\norigin b\nb: cap(12,0)\n")
    port = dict(net.ports)["in"]
    assert port.word == "z18"


def test_flat_lid_unsupported_pairs_rejected() -> None:
    for n, m in ((6, 3), (9, 0), (7, 0), (0, 0)):
        net = _net(f"hexfold 0.1\norigin b\nb: cap({n},{m})\n")
        kinds = [f for f in net.report.findings if f.code == "build.kind"]
        assert kinds, (n, m)
        assert "(5,5)" in kinds[0].message and "(6k,0)" in kinds[0].message


# ---------- fused to a zigzag tube ----------


def test_flat_lid_caps_one_tube_end() -> None:
    net = _net(
        """hexfold 0.1
origin a
a: tube(6,0,len=3)
b: cap(6,0)
a.out --fuse k=0--> b.in
"""
    )
    assert not any(f.severity.name == "ERROR" for f in net.report.findings)
    pn = Counter(len(r) for r in net.rings)
    assert pn == {5: 6, 6: 31}
    assert len(net.atoms) - len(net.bonds) + len(net.rings) == 1
    assert dict(net.ports).keys() == {"a.in"}


def test_flat_lid_pillbox_rotor_both_ends() -> None:
    # a lid pair on a (12,0) tube: closed pillbox rotor, 12 pentagons at
    # the two lids' corners, no remaining ports
    net = _net(
        """hexfold 0.1
origin a
a: tube(12,0,len=4)
b: cap(12,0)
c: cap(12,0)
a.out --fuse k=0--> b.in
a.in --fuse k=0--> c.in
"""
    )
    assert not any(f.severity.name == "ERROR" for f in net.report.findings)
    pn = Counter(len(r) for r in net.rings)
    assert pn[5] == 12
    assert len(net.atoms) - len(net.bonds) + len(net.rings) == 2
    assert dict(net.ports) == {}


def test_flat_lid_tube_fuse_stick_no_error_geometry() -> None:
    # fidelity: stick relaxes tube+lid with no ERROR-level geometry
    # finding (a WARNING-level angle deviation at the corner pentagons
    # is acceptable)
    text = """hexfold 0.1
origin a
a: tube(6,0,len=3)
b: cap(6,0)
a.out --fuse k=0--> b.in
"""
    rep = check(text, geometry=True)
    assert not any(f.severity.name == "ERROR" for f in rep.findings)
    net = _net(text)
    coords, _max_force = stick_info(net)
    assert coords.shape == (len(net.atoms), 3)


# ---------- example files ----------


def test_capped_pill_example_checks_clean() -> None:
    text = (ROOT / "examples" / "sheet_pill_bump.hx").read_text(encoding="utf-8")
    rep = check(text)
    assert not any(f.severity.name == "ERROR" for f in rep.findings), [
        f for f in rep.findings if f.severity.name == "ERROR"
    ]
    res = [f for f in rep.findings if f.code == "euler.residual"]
    assert res and all(dict(f.data)["residual"] == 0 for f in res)


def test_lid_pillbox_example_checks_clean() -> None:
    text = (ROOT / "examples" / "lid_pillbox.hx").read_text(encoding="utf-8")
    rep = check(text)
    assert not any(f.severity.name == "ERROR" for f in rep.findings), [
        f for f in rep.findings if f.severity.name == "ERROR"
    ]
