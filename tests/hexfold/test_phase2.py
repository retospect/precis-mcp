"""Phase-2 attachments: fuse/bond connects, collars, nanobud menus."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hexfold.build import Net, build
from hexfold.canon import canonical_json
from hexfold.check import check
from hexfold.stick import stick
from hexfold.text import parse, to_text

_ROOT = Path(__file__).resolve().parents[2] / "hexfold"


def _net(text: str) -> Net:
    return build(text, strict=False)


def _codes(text: str) -> dict[str, list]:
    r = check(text)
    out: dict[str, list] = {}
    for f in r.findings:
        out.setdefault(f.code, []).append(f)
    return out


FUSE2 = """hexfold 0.1
a: tube(5,0, len=3)
b: tube(5,0, len=3)
a.out --fuse k=0--> b.in
"""


def test_fuse_tubes_all_hexagon_seam() -> None:
    net = _net(FUSE2)
    # one component: every atom reachable; ports consumed
    assert all(n != "a.out" and n != "b.in" for n, _ in net.ports)
    assert {n for n, _ in net.ports} == {"a.in", "b.out"}
    # seam rings all hexagons: interior ring census has only 6-rings
    assert set(len(r) for r in net.rings) == {6}
    # fused dangling atoms gained a bond -> degree 3
    deg = {a.ord: 0 for a in net.atoms}
    for i, j, _ in net.bonds:
        deg[i] += 1
        deg[j] += 1
    assert max(deg.values()) == 3


def test_fuse_port_mismatch() -> None:
    codes = _codes(
        """hexfold 0.1
a: tube(5,0, len=2)
b: tube(6,0, len=2)
a.out --fuse k=0--> b.in
"""
    )
    assert "port.mismatch" in codes
    assert codes["port.mismatch"][0].severity.name == "ERROR"


def test_bond_sp3_and_valence_over() -> None:
    # one authored bond onto an interior atom: degree 4 -> sp3, no error
    codes = _codes(
        """hexfold 0.1
h: sheet(12,12)
f: sheet(12,12)
h/(3,3,A) --bond--> f/(3,3,A)
"""
    )
    assert "valence.over" not in codes
    # two authored bonds onto the same atom: degree 5 -> valence.over
    codes2 = _codes(
        """hexfold 0.1
h: sheet(12,12)
f: sheet(12,12)
h/(3,3,A) --bond--> f/(3,3,A)
h/(3,4,B) --bond--> f/(3,3,A)
"""
    )
    assert "valence.over" in codes2
    assert "5 bonds" in codes2["valence.over"][0].message


def test_bond_records_sp3() -> None:
    net = _net(
        """hexfold 0.1
h: sheet(12,12)
f: sheet(12,12)
h/(3,3,A) --bond--> f/(3,3,A)
"""
    )
    # both endpoints were interior deg-3 atoms -> now deg 4 -> sp3
    sp3 = [a for a in net.atoms if a.hyb == "sp3"]
    assert len(sp3) == 2


def test_sublattice_annot() -> None:
    codes = _codes(
        """hexfold 0.1
h: sheet(12,12)
f: sheet(12,12)
h/(3,3,A) --bond--> f/(3,3,A)
h/(3,3,A) --bond--> f/(3,3,B)
"""
    )
    par = sorted(dict(f.data)["parity"] for f in codes["annot.sublattice"])
    assert par == ["cross", "same"]


def test_hole_dir_disambiguates() -> None:
    # three hexagons share (5,5,A); :d picks the ring toward direction d
    seen = set()
    for d in (0, 2, 4):
        net = _net(
            f"""hexfold 0.1
s: sheet(16,16) - hexagon@(5,5,A):{d}
"""
        )
        hole = next(p for n, p in net.ports if n.endswith("hole"))
        seen.add(tuple(sorted(hole.atoms)))
    assert len(seen) == 3


def test_hole_dir_roundtrip() -> None:
    sp = parse("hexfold 0.1\ns: sheet(16,16) - hexagon@(5,5,A):2\n")
    assert "hexagon@(5,5,A):2" in to_text(sp)
    j = json.loads(canonical_json(sp))
    assert j["instances"]["s"]["holes"][0]["dir"] == 2


def test_collar_7x5_solves_on_55() -> None:
    codes = _codes(
        """hexfold 0.1
h: tube(5,5, len=14) - hexagon@(7,0,A):0
t: tube(6,0, len=4)
t.out --fuse{7x5 @fit} k=0--> h.hole
"""
    )
    assert "fit.unsolvable" not in codes
    assert "port.symmetry" not in codes


def test_collar_symmetry_error() -> None:
    codes = _codes(
        """hexfold 0.1
h: tube(5,5, len=14) - hexagon@(7,0,A):0
t: tube(6,0, len=4)
t.out --fuse{7x4 @fit} k=0--> h.hole
"""
    )
    assert "port.symmetry" in codes
    assert codes["port.symmetry"][0].severity.name == "ERROR"


def test_22_bud() -> None:
    net = _net(
        """hexfold 0.1
h: tube(5,5, len=8)
b: fullerene(C60)
b @ h/(4,0,A):0 [2+2]
"""
    )
    assert len(net.attach) == 2
    sp3 = [a for a in net.atoms if a.hyb == "sp3"]
    assert len(sp3) == 4
    codes = _codes(
        """hexfold 0.1
h: tube(5,5, len=8)
b: fullerene(C60)
b @ h/(4,0,A):0 [2+2]
"""
    )
    comp = [f for f in codes["euler.chi"] if f.where]
    # two surface components reported before the authored bonds
    assert {dict(f.data)["chi"] for f in comp} == {0, 2}


def test_87_seam() -> None:
    codes = _codes(
        """hexfold 0.1
h: tube(10,10, len=14)
b: fullerene(C60)
b @ h/(7,0,A):0 [8-7]
"""
    )
    assert dict(codes["seam.rings"][0].data)["rings"] == {7: 3, 8: 3}


@pytest.mark.parametrize("menu,rings", [("9-6", {6: 3, 9: 3}), ("8-7", {7: 3, 8: 3})])
@pytest.mark.parametrize("nm,site", [((10, 10), "(7,0,A)"), ((17, 0), "(8,0,A)")])
def test_bud_menu_seams(menu: str, rings: dict, nm: tuple, site: str) -> None:
    # Wang & Li 2009: bond-mode C54 attachment onto an intact host; the
    # two C3-symmetric registrations give exactly these seam multisets
    n, m = nm
    codes = _codes(
        f"""hexfold 0.1
h: tube({n},{m}, len=14)
b: fullerene(C60)
b @ h/{site}:0 [{menu}]
"""
    )
    assert "fit.unsolvable" not in codes
    assert dict(codes["seam.rings"][0].data)["rings"] == rings


def test_bud_menu_intact_host() -> None:
    # six authored bonds, host atoms go sp3, bud hole port is consumed
    net = _net(
        """hexfold 0.1
h: tube(10,10, len=14)
b: fullerene(C60)
b @ h/(7,0,A):0 [9-6]
"""
    )
    assert len(net.attach) == 6
    assert "b.hole" not in dict(net.ports)
    sp3 = sum(1 for a in net.atoms if a.hyb == "sp3")
    assert sp3 == 6


def test_db_neck() -> None:
    net = _net(
        """hexfold 0.1
h: tube(10,10, len=14)
b: fullerene(C60)
b @ h/(7,0,A):0 [DB-neck(3)]
"""
    )
    pn: dict[int, int] = {}
    for r in net.rings:
        pn[len(r)] = pn.get(len(r), 0) + 1
    assert pn[5] == 9  # one C60 hexagon excision kills 3 pentagons
    # Baowan's three heptagons are the bud-side seam's; the host join
    # carries its native six — P7 = 9 balances P5 = 9 (no collar)
    assert pn[7] == 9
    assert pn.get(8, 0) == 0
    rep = check(
        """hexfold 0.1
h: tube(10,10, len=14)
b: fullerene(C60)
b @ h/(7,0,A):0 [DB-neck(3)]
"""
    )
    res = [f for f in rep.findings if f.code == "euler.residual"]
    assert res and res[0].severity.name == "INFO"
    assert res[0].data == (("residual", 0), ("sheet", "sheet0"))
    # single connected component
    adj: dict[int, set[int]] = {}
    for i, j, _ in net.bonds:
        adj.setdefault(i, set()).add(j)
        adj.setdefault(j, set()).add(i)
    seen: set[int] = set()
    stack = [net.atoms[0].ord]
    while stack:
        x = stack.pop()
        if x not in seen:
            seen.add(x)
            stack.extend(adj.get(x, ()))
    assert len(seen) == len(net.atoms)
    # chi of the whole net: V - E + F_int = 0
    assert len(net.atoms) - len(net.bonds) + len(net.rings) == 0


def test_da_neck() -> None:
    # path3 host opening (3-atom path -> 5 dangling); the (5,0) neck seats
    # with the k-fit registration; single component, chi=0, residual 0
    net = _net(
        """hexfold 0.1
h: tube(10,10, len=14)
b: fullerene(C60)
b @ h/(7,0,A):0 [DA-neck(3)]
"""
    )
    pn: dict[int, int] = {}
    for r in net.rings:
        pn[len(r)] = pn.get(len(r), 0) + 1
    assert pn[5] == 11
    assert len(net.atoms) - len(net.bonds) + len(net.rings) == 0
    adj: dict[int, set[int]] = {}
    for i, j, _ in net.bonds:
        adj.setdefault(i, set()).add(j)
        adj.setdefault(j, set()).add(i)
    seen: set[int] = set()
    stack = [net.atoms[0].ord]
    while stack:
        x = stack.pop()
        if x not in seen:
            seen.add(x)
            stack.extend(adj.get(x, ()))
    assert len(seen) == len(net.atoms)
    codes = _codes(
        """hexfold 0.1
h: tube(10,10, len=14)
b: fullerene(C60)
b @ h/(7,0,A):0 [DA-neck(3)]
"""
    )
    seams = {tuple(sorted(dict(f.data)["rings"].items())) for f in codes["seam.rings"]}
    assert ((7, 5),) in seams
    assert ((6, 1), (7, 2), (8, 2)) in seams
    res = [f for f in codes["euler.residual"]]
    assert res and all(f.severity.name == "INFO" for f in res)


def test_thin_host_cut_overlap() -> None:
    # hexagon hole on a (6,0) tube: the rim wraps the circumference
    codes = _codes(
        """hexfold 0.1
h: tube(6,0, len=14) - hexagon@(4,0,A)
"""
    )
    errs = [f for f in codes.get("cut.overlap", [])]
    assert errs and errs[0].severity.name == "ERROR"
    assert "circumference" in errs[0].message


def test_frag_opaque() -> None:
    codes = _codes(
        """hexfold 0.1
frag: glucose = smiles(OC[C@H]1OC(O)[C@H](O)[C@@H](O)[C@@H]1O)
h: sheet(10,10)
glucose.1 --bond--> h/(3,3,A)
"""
    )
    assert "frag.unrealized" in codes
    j = json.loads(
        canonical_json(
            parse(
                "hexfold 0.1\n"
                "frag: glucose = smiles(OCC)\n"
                "h: sheet(10,10)\n"
                "glucose.1 --bond--> h/(3,3,A)\n"
            )
        )
    )
    assert j["frags"]["glucose"]["attachments"] == 1


@pytest.mark.parametrize(
    "path",
    [
        "tube_fuse.hx",
        "nanobud_22.hx",
        "nanobud_87.hx",
        "nanobud_96.hx",
        "nanobud_db_neck.hx",
        "nanobud_da_neck.hx",
    ],
)
def test_example_canon_stable(path: str) -> None:
    text = (_ROOT / "examples" / path).read_text(encoding="utf-8")
    a = canonical_json(text)
    b = canonical_json(text)
    assert a == b
    # text -> JSON -> text -> JSON round trip
    sp = parse(text)
    assert canonical_json(sp) == canonical_json(parse(to_text(sp)))


def test_stick_twice_identical() -> None:
    net = _net(FUSE2)
    a = stick(net)
    b = stick(net)
    assert (a == b).all()


def test_tube_fuse_stick_rms_regression() -> None:
    from hexfold.stick import stick_info

    net = _net((_ROOT / "examples" / "tube_fuse.hx").read_text(encoding="utf-8"))
    coords, _ = stick_info(net)
    import numpy as np

    devs = [
        float(np.linalg.norm(coords[i] - coords[j]) - net.lattice.sigma_A)
        for i, j, _ in net.bonds
    ]
    rms = float(np.sqrt(np.mean(np.square(devs))))
    assert rms < 0.15


def test_bud_menu_host_sublattice() -> None:
    # bond-mode attach: annot carries the host sublattice, not parity
    codes = _codes(
        """hexfold 0.1
h: tube(10,10, len=14)
b: fullerene(C60)
b @ h/(7,0,A):0 [9-6]
"""
    )
    subs = [dict(f.data)["host_sublattice"] for f in codes["annot.sublattice"]]
    assert len(subs) == 6 and all(x in ("A", "B") for x in subs)
    summary = dict(codes["annot.host_sublattices"][0].data)
    assert summary["A"] + summary["B"] == 6


def test_bond_joined_net_component_euler() -> None:
    # whole-net euler.chi is suppressed for bond-joined nets; the
    # consumed-rim component reports chi but no residual
    codes = _codes(
        """hexfold 0.1
h: tube(10,10, len=14)
b: fullerene(C60)
b @ h/(7,0,A):0 [9-6]
"""
    )
    chis = [f for f in codes["euler.chi"]]
    assert all("component" in f.message for f in chis)
    res = [f for f in codes["euler.residual"]]
    assert res and all(f.severity.name == "INFO" for f in res)
