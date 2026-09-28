"""gr454650: a one-period tube (`len=1`) reported its `in`/`out` rims as
every atom on both ends instead of the disjoint bottom/top rows -- the
generic face-orbit walk collapses to a single whole-patch cycle when the
patch has no interior ring row of its own to anchor the two ends apart
(``build._split_degenerate_tube_rims``). Regression coverage: the two rims
stay disjoint size-N halves at ``len=1`` for an armchair and a zigzag tube,
the fuse onto a matching cap now succeeds, and ``len=2`` is unaffected."""

from __future__ import annotations

from hexfold.build import build
from hexfold.check import check
from hexfold.report import Severity


def _spec(body: str) -> str:
    return "hexfold 0.2\nlattice: element=C sigma=1.42\n\n" + body + "\n"


def test_armchair_len1_rims_are_disjoint_tens() -> None:
    net = build(_spec("t: tube(5,5,len=1)"))
    ports = dict(net.ports)
    assert set(ports) == {"in", "out"}
    assert ports["in"].size == 10
    assert ports["out"].size == 10
    assert set(ports["in"].dangling) & set(ports["out"].dangling) == set()
    # every atom is on the surface at len=1 -- the two rims partition it
    assert set(ports["in"].dangling) | set(ports["out"].dangling) == {
        a.ord for a in net.atoms
    }


def test_zigzag_len1_rims_are_disjoint_sixes() -> None:
    net = build(_spec("t: tube(6,0,len=1)"))
    ports = dict(net.ports)
    assert ports["in"].size == 6
    assert ports["out"].size == 6
    assert set(ports["in"].dangling) & set(ports["out"].dangling) == set()


def test_len1_tube_fuses_onto_a_matching_cap() -> None:
    text = _spec("t: tube(5,5,len=1)\nc: cap(5,5)\nt.out --fuse k=0--> c.in\n")
    report = check(text)
    errors = [f for f in report.findings if f.severity == Severity.ERROR]
    assert errors == []
    codes = {f.code for f in report.findings}
    assert "port.mismatch" not in codes

    (seam,) = [f for f in report.findings if f.code == "seam.rings"]
    rings = dict(dict(seam.data)["rings"])
    # every seam face is a plausible sp2 ring (SPEC's own unusual-size
    # threshold is outside [4, 8]) and the face count matches the 10 fused
    # bonds (one face per pair of consecutive fused atoms)
    assert rings
    assert all(4 <= size <= 8 for size in rings)
    assert sum(rings.values()) == 10
    assert "ring.size.unusual" not in codes


def test_armchair_len2_rims_are_still_disjoint_tens() -> None:
    net = build(_spec("t: tube(5,5,len=2)"))
    ports = dict(net.ports)
    assert ports["in"].size == 10
    assert ports["out"].size == 10
    assert set(ports["in"].dangling) & set(ports["out"].dangling) == set()
