"""Build-phase topology: tube/sheet/cone/C60 counts, valences, counting law."""

from __future__ import annotations

from collections import Counter

import pytest

from hexfold.build import Net, build
from hexfold.defects import counting_residual
from hexfold.lattice import n_cells


def _net(text: str) -> Net:
    return build(text, strict=False)


def _valences(net: Net) -> Counter[int]:
    deg: Counter = Counter()
    for i, j, _ in net.bonds:
        deg[i] += 1
        deg[j] += 1
    return Counter(deg.values())


def _counting_ok(net: Net) -> bool:
    pn = Counter(len(r) for r in net.rings)
    b = sum(p.b for _, p in net.ports)
    chi = len(net.atoms) - len(net.bonds) + len(net.rings)
    return counting_residual(dict(pn), b, chi) == 0


def test_sheet_patch() -> None:
    net = _net("hexfold 0.1\norigin s\ns: sheet(4, 3)\n")
    assert len(net.atoms) == 2 * 4 * 3
    assert all(v <= 3 for v in _valences(net))
    assert all(len(r) == 6 for r in net.rings)
    names = [n for n, _ in net.ports]
    assert names == ["rim"]
    assert net.ports[0][1].b == 6
    assert _counting_ok(net)


def test_tube_counts() -> None:
    n, m, length = 5, 5, 4
    net = _net(f"hexfold 0.1\norigin t\nt: tube({n}, {m}, len={length})\n")
    assert len(net.atoms) == 2 * n_cells(n, m) * length
    # every bond is order 1 in phase 1
    assert all(o == 1 for _, _, o in net.bonds)
    # two rim ports, normal "axis", B = 0 (cylinder has chi 0)
    assert [n_ for n_, _ in net.ports] == ["in", "out"]
    assert all(p.normal == "axis" for _, p in net.ports)
    assert all(p.b == 0 for _, p in net.ports)
    # interior (non-rim) atoms valence 3; rim walk contains the
    # valence-2 tip atoms of the sawtooth cut (10 per end for (5,5))
    rim = {o for _, p in net.ports for o in p.atoms}
    deg: Counter[int] = Counter()
    for i, j, _ in net.bonds:
        deg[i] += 1
        deg[j] += 1
    assert all(deg[a.ord] == 3 for a in net.atoms if a.ord not in rim)
    assert sum(1 for a in net.atoms if deg[a.ord] == 2) == 20
    # rings are all hexagons; Euler for cylinder: V-E+F = 0
    assert all(len(r) == 6 for r in net.rings)
    assert len(net.atoms) - len(net.bonds) + len(net.rings) == 0


def test_tube_zigzag() -> None:
    net = _net("hexfold 0.1\norigin t\nt: tube(10, 0, len=3)\n")
    assert len(net.atoms) == 2 * n_cells(10, 0) * 3
    assert len(net.atoms) - len(net.bonds) + len(net.rings) == 0


@pytest.mark.parametrize("p", [1, 2, 3, 4, 5])
def test_cone_apex_pentagons(p: int) -> None:
    net = _net(f"hexfold 0.1\norigin c\nc: cone({p}, rad=12)\n")
    sizes = Counter(len(r) for r in net.rings)
    assert sizes[5] == p
    assert set(sizes) == {5, 6}
    # chi = 1 (disk): sum(6-n)Pn + B = 6
    b = sum(pt.b for _, pt in net.ports)
    assert p + b == 6


def test_c60() -> None:
    net = _net("hexfold 0.1\norigin c\nc: fullerene(C60)\n")
    assert len(net.atoms) == 60
    assert len(net.bonds) == 90
    assert Counter(len(r) for r in net.rings) == Counter({5: 12, 6: 20})
    assert not net.ports
    assert len(net.atoms) - len(net.bonds) + len(net.rings) == 2


def test_c60_pentagon_hole() -> None:
    net = _net("hexfold 0.1\norigin c\nc: fullerene(C60) - pentagon@(0,0,A)\n")
    assert len(net.atoms) == 55
    sizes = Counter(len(r) for r in net.rings)
    assert sizes[5] == 11
    ((name, port),) = net.ports
    assert name == "hole"
    # counting law: 11 pentagons + B = 6
    assert 11 + port.b == 6


def test_sw_glyph_ring_sizes() -> None:
    net = _net("hexfold 0.1\norigin s\ns: sheet(12, 12) + sw@(4,4,A):0\n")
    sizes = Counter(len(r) for r in net.rings)
    assert sizes[5] == 2 and sizes[7] == 2


def test_57_glyph() -> None:
    net = _net("hexfold 0.1\norigin s\ns: sheet(12, 12) + 57@(4,4,A):0\n")
    sizes = Counter(len(r) for r in net.rings)
    assert sizes[5] == 1 and sizes[7] == 1


def test_sheet_hole_port() -> None:
    net = _net("hexfold 0.1\norigin s\ns: sheet(12, 12) - hexagon@(4,4,A)\n")
    names = [n for n, _ in net.ports]
    assert "hole" in names
    assert _counting_ok(net)


def test_cut_overlap_fires() -> None:
    net = _net(
        "hexfold 0.1\norigin s\n"
        "s: sheet(12, 12) + pentagon@(4,4,A):0 + pentagon@(4,4,A):1\n"
    )
    assert any(f.code == "cut.overlap" for f in net.report.errors())


def test_cone_rad_too_small_cut_overlap() -> None:
    net = _net("hexfold 0.1\norigin c\nc: cone(5, rad=8)\n")
    assert any(f.code == "cut.overlap" for f in net.report.findings)


def test_sheet_named_params() -> None:
    a = _net("hexfold 0.1\norigin s\ns: sheet(W=12, H=12)\n")
    b = _net("hexfold 0.1\norigin s\ns: sheet(12, 12)\n")
    assert len(a.atoms) == len(b.atoms)
    assert a.bonds == b.bonds
