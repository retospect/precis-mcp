"""SPEC 10 rim type: ``Port.rim_type`` read off the dangling pattern, the
INFO-level ``rim.nonstandard`` code, and the zigzag-onto-armchair fuse
being the 30-degree grain-boundary adapter rather than a mismatch."""

from __future__ import annotations

from hexfold.build import build
from hexfold.check import check
from hexfold.report import Severity


def _spec(body: str) -> str:
    return "hexfold 0.2\n" + body + "\n"


def _codes(text: str) -> dict[str, list]:
    out: dict[str, list] = {}
    for f in check(text).findings:
        out.setdefault(f.code, []).append(f)
    return out


def test_zigzag_tube_ends_are_z_n() -> None:
    net = build(_spec("a: tube(12,0, len=3)"))
    assert {p.rim_type for _n, p in net.ports} == {("z", 12)}


def test_armchair_tube_ends_are_a_2n() -> None:
    net = build(_spec("a: tube(5,5, len=3)"))
    assert {p.rim_type for _n, p in net.ports} == {("a", 10)}


def test_chiral_rims_are_mixed_and_c60_cap_rim_is_armchair() -> None:
    chiral = build(_spec("a: tube(6,3, len=3)"))
    assert {p.rim_type for _n, p in chiral.ports} == {None}
    # the C60 hemisphere cut across its C5 axis ends in five bonded pairs
    # of dangling atoms: a pure armchair rim, which is why it fuses onto
    # tube(5,5) with an all-hexagon seam
    cap = build(_spec("a: cap(5,5)"))
    assert {p.rim_type for _n, p in cap.ports} == {("a", 10)}


def test_flat_lid_rim_is_zigzag_6k() -> None:
    lid = build(_spec("a: cap(12,0)"))
    (port,) = [p for _n, p in lid.ports]
    # the lid's rim word is all-zigzag but its 6 flake corners put two
    # dangling atoms side by side, so the dangling pattern is mixed --
    # it still fuses onto tube(12,0) (equal N), the seam rings being the
    # six pentagons.
    assert port.size == 12
    assert port.rim_type is None


def test_rim_type_is_not_in_the_authored_json() -> None:
    net = build(_spec("a: tube(12,0, len=3)"))
    for pdata in net.authored_dict()["ports"].values():
        assert "type" not in pdata and "rim_type" not in pdata


def test_rim_nonstandard_is_info_and_names_the_reason() -> None:
    standard = _codes(_spec("a: tube(12,0, len=3)"))
    assert "rim.nonstandard" not in standard

    chiral = _codes(_spec("a: tube(6,3, len=3)"))
    fs = chiral["rim.nonstandard"]
    assert {f.severity for f in fs} == {Severity.INFO}
    assert {f.where for f in fs} == {"in", "out"}
    assert all(dict(f.data)["type"] is None for f in fs)
    assert all("mixed" in f.message for f in fs)

    off_series = _codes(_spec("a: tube(7,0, len=3)"))
    fs = off_series["rim.nonstandard"]
    assert {dict(f.data)["type"] for f in fs} == {"z7"}
    assert all("multiple of 6" in f.message for f in fs)

    # armchair is standard at any N; no series rule applies to it
    assert "rim.nonstandard" not in _codes(_spec("a: tube(5,5, len=3)"))


def test_zigzag_onto_armchair_fuse_is_the_grain_boundary_adapter() -> None:
    """Equal N, different pure types: legal (no ``port.mismatch``), and the
    seam census is the alternating 5-7 line of a 30-degree graphene grain
    boundary -- the one adapter the two-type rim standard needs."""
    text = _spec("a: tube(12,0, len=3)\nb: tube(6,6, len=3)\na.out --fuse k=0--> b.in")
    net = build(text, strict=False)
    codes = _codes(text)
    assert "port.mismatch" not in codes
    assert dict(net.ports)["a.in"].rim_type == ("z", 12)
    assert dict(net.ports)["b.out"].rim_type == ("a", 12)
    (seam,) = codes["seam.rings"]
    rings = dict(seam.data)["rings"]
    assert set(rings) == {5, 7}
    assert rings[5] == rings[7] == 6
