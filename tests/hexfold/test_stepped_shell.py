"""Radius-changing shell (SPEC 28.3): the ``cap(6k,0) - hex(r)@...`` washer
between a neck and a wider bulge.  Fusing a neck into the washer's hole
mints six heptagons (collar shrinks); fusing the bulge onto the washer's
rim mints six pentagons (collar grows) -- both as seam rings, net charge
0.  ``valve_shell.hx`` / ``valve_shell_lidded.hx`` are the first built
instance (rotary-ratchet-valve, SPEC docs/backlog)."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from hexfold.build import Net, build
from hexfold.check import check
from hexfold.stick import stick

ROOT = Path(__file__).resolve().parents[2] / "hexfold"


def _net(text: str) -> Net:
    return build(text, strict=False)


# ---------- the washer primitive ----------


def test_washer_ports() -> None:
    net = _net("hexfold 0.1\norigin b\nb: cap(24,0) - hex(1)@(0,0,A):0\n")
    assert not any(f.severity.name == "ERROR" for f in net.report.findings)
    assert len(net.atoms) == 72
    ports = dict(net.ports)
    assert len(ports["hole"].dangling) == 12
    assert ports["hole"].b_expected == -6
    assert len(ports["in"].dangling) == 24
    assert ports["in"].b_expected == 6


def test_washer_seams_are_the_collar() -> None:
    text = """hexfold 0.2
origin neck
neck: tube(12, 0, len=3)
washer: cap(24, 0) - hex(1)@(0,0,A):0
bulge: tube(24, 0, len=3)
neck.out --fuse k=0--> washer.hole
washer.in --fuse k=0--> bulge.in
"""
    rep = check(text)
    assert not any(f.severity.name == "ERROR" for f in rep.findings)
    seams = [f for f in rep.findings if f.code == "seam.rings"]
    assert len(seams) == 2
    rings = sorted((dict(f.data)["rings"] for f in seams), key=str)
    assert rings == [{5: 6, 6: 18}, {6: 6, 7: 6}]
    res = [f for f in rep.findings if f.code == "euler.residual"]
    assert res and all(dict(f.data)["residual"] == 0 for f in res)


def test_thin_washer_refused() -> None:
    # washer rule k >= r+3: cap(18,0) is k=3 with a hex(1) hole (r=1), so
    # the hole is one ring short of clearing the cap's own rim
    net = _net("hexfold 0.1\norigin b\nb: cap(18,0) - hex(1)@(0,0,A):0\n")
    kinds = [f for f in net.report.findings if f.code == "cut.overlap"]
    assert kinds and all(f.severity.name == "ERROR" for f in kinds)


# ---------- valve_shell.hx ----------


def test_valve_shell_example_checks_clean() -> None:
    text = (ROOT / "examples" / "valve_shell.hx").read_text(encoding="utf-8")
    rep = check(text)
    assert not any(f.severity.name == "ERROR" for f in rep.findings), [
        f for f in rep.findings if f.severity.name == "ERROR"
    ]
    res = [f for f in rep.findings if f.code == "euler.residual"]
    assert res and all(dict(f.data)["residual"] == 0 for f in res)
    net = _net(text)
    assert {n for n, _ in net.ports} == {
        "bulge.hole",
        "bulge.hole1",
        "neck_a.in",
        "neck_b.out",
    }
    fit = [f for f in rep.findings if f.code == "fit.alternatives"]
    assert fit and dict(fit[0].data)["applied"] == 3


def test_valve_shell_seams_flat() -> None:
    text = (ROOT / "examples" / "valve_shell.hx").read_text(encoding="utf-8")
    net = _net(text)
    seams = [f for f in net.report.findings if f.code == "seam.rings"]
    assert seams
    for f in seams:
        for mult in dict(f.data)["rings"].values():
            assert mult % 6 == 0
    pn = Counter(len(r) for r in net.rings)
    assert pn == {6: 310, 5: 12, 7: 12}


def test_wall_holes_c2() -> None:
    from hexfold.lattice import Site, circumferential

    c1 = circumferential(Site(6, -3, 0), 24, 0)
    c2 = circumferential(Site(18, -3, 0), 24, 0)
    assert abs((c1 - c2) % 1.0 - 0.5) < 1e-9


def test_lidded_shell_closed_but_wall_holes() -> None:
    text = (ROOT / "examples" / "valve_shell_lidded.hx").read_text(encoding="utf-8")
    net = _net(text)
    assert not any(f.severity.name == "ERROR" for f in net.report.findings)
    chi = len(net.atoms) - len(net.bonds) + len(net.rings)
    assert chi == 0
    assert {n for n, _ in net.ports} == {"bulge.hole", "bulge.hole1"}
    pn = Counter(len(r) for r in net.rings)
    assert pn == {6: 336, 5: 24, 7: 12}
    res = [f for f in check(text).findings if f.code == "euler.residual"]
    assert res and all(dict(f.data)["residual"] == 0 for f in res)


def test_valve_shell_stick_no_error_geometry() -> None:
    text = (ROOT / "examples" / "valve_shell.hx").read_text(encoding="utf-8")
    rep = check(text, geometry=True)
    assert not any(f.severity.name == "ERROR" for f in rep.findings)
    net = _net(text)
    coords = stick(net)
    assert coords.shape == (708, 3)
