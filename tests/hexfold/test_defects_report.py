"""Cut disks, counting law, and check() diagnostics."""

from __future__ import annotations

from collections import Counter

from hexfold.build import build
from hexfold.check import check
from hexfold.defects import Defect, counting_residual, cut_disk, disks_disjoint
from hexfold.lattice import Lattice, Site

LAT = Lattice()


def test_cut_disk_disjoint_positive() -> None:
    a = Defect(5, Site(0, 0, 0), 0)
    b = Defect(5, Site(8, 8, 0), 0)
    assert disks_disjoint([a, b], LAT)


def test_cut_disk_overlap_negative() -> None:
    a = Defect(5, Site(0, 0, 0), 0)
    b = Defect(5, Site(0, 0, 0), 1)
    assert not disks_disjoint([a, b], LAT)
    assert cut_disk(a, LAT) & cut_disk(b, LAT)


def test_cut_overlap_finding() -> None:
    r = check(
        "hexfold 0.1\norigin s\n"
        "s: sheet(12, 12) + pentagon@(4,4,A):0 + pentagon@(4,4,A):1\n"
    )
    assert any(f.code == "cut.overlap" for f in r.errors())


def test_counting_law_values() -> None:
    assert counting_residual({5: 12}, 0, 2) == 0  # fullerene
    assert counting_residual({5: 3}, 3, 1) == 0  # P=3 cone
    assert counting_residual({6: 10}, 6, 1) == 0  # plain sheet
    assert counting_residual({5: 11}, -5, 1) == 0  # C60 minus pentagon


def test_check_valence_and_ring_codes() -> None:
    r = check("hexfold 0.1\norigin s\ns: sheet(6, 4)\n")
    assert r.ok


def test_check_geometry_codes_fire_on_stick() -> None:
    # geometry tier adds geom.* findings; on the fullerene (seed3-exact)
    # they should be benign infos at most
    r = check(_C60_SPEC, geometry=True)
    codes = {f.code for f in r.findings}
    assert "geom.summary" in codes


_C60_SPEC = "hexfold 0.1\norigin c\nc: fullerene(C60)\n"


# gr464341 (wontfix, Reto's option-c ruling 2026-10-12): vertex ids order by
# str(Site), so Patch.faces() starts the rim walk at a different edge when a
# sheet hole straddles the 9 -> 10 digit boundary, and the k=0 / k=1 fuse
# phases swap there. Numeric ordering (18bd85545) was reverted by 03c36e89c
# because it changed 32 golden tests; changing this order needs regenerated
# goldens through the full gate. Do not "fix" this test to be position
# independent.
_STR_ORDER_CLEAN = {(12, 12), (14, 14), (5, 9)}
_STR_ORDER_FLIPPED = {(5, 10), (9, 12), (19, 10), (9, 9)}
_SEAM_CLEAN_777 = {6: 696, 7: 3}
_SEAM_FLIPPED_557 = {5: 3, 6: 690, 7: 6}


def _fused_foot_ring_sizes(i: int, j: int, k: int) -> dict[int, int]:
    net = build(
        "hexfold 0.2\norigin s\n"
        f"s: sheet(30,24) - hex(1)@({i},{j},A):0\n"
        "c: cap(24,0) + 3@(-1,0,A):2 - hex(1)@(-1,0,A):0\n"
        "t: tube(6,0,len=3)\n"
        f"s.hole --fuse k={k}--> c.in\n",
        strict=False,
    )
    assert not net.report.errors()
    return dict(sorted(Counter(len(r) for r in net.rings).items()))


def test_vertex_id_order_is_str_not_numeric_gr464341() -> None:
    for k in (0, 1):
        clean, flipped = (
            (_SEAM_CLEAN_777, _SEAM_FLIPPED_557)
            if k == 0
            else (_SEAM_FLIPPED_557, _SEAM_CLEAN_777)
        )
        for cell in sorted(_STR_ORDER_CLEAN):
            assert _fused_foot_ring_sizes(*cell, k) == clean, (k, cell)
        for cell in sorted(_STR_ORDER_FLIPPED):
            assert _fused_foot_ring_sizes(*cell, k) == flipped, (k, cell)
