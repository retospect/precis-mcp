"""pcb-intent-api slice 2: lazy geometry refs resolve against poses."""

from __future__ import annotations

import pytest

from precis_geom import pcb

pytest.importorskip("shapely")


def _board() -> tuple[pcb.Board, pcb.Part, pcb.Part]:
    b = pcb.Board("t")
    b.outline(pcb.rect(40, 30))
    fp = b.footprint(
        pcb.Footprint(
            "F",
            [
                pcb.pad("1", "rect", -1, 0, w=1, h=1),
                pcb.pad("2", "rect", 1, 0, w=1, h=1),
            ],
            courtyard={"bbox": [-2, -1, 2, 1]},
        )
    )
    u = b.part("U1", footprint=fp).at(10, 10)
    r = b.part("R1", footprint=fp).at(20, 10, rot=90)
    return b, u, r


def test_refs_resolve_and_follow_poses() -> None:
    b, u, r = _board()
    snap = b.region_snapshot()
    g = u.courtyard.resolve(snap)
    assert g.bounds == (8.0, 9.0, 12.0, 11.0)
    # rotated 90°: courtyard 4x2 becomes 2x4
    assert r.courtyard.resolve(snap).bounds == pytest.approx((19.0, 8.0, 21.0, 12.0))
    # a solver iterate overrides the authored pose
    moved = u.courtyard.resolve(snap, {"U1": (0.0, 0.0, 0.0, "top")})
    assert moved.bounds == (-2.0, -1.0, 2.0, 1.0)
    pin = u["2"].center.resolve(snap)
    assert (pin.x, pin.y) == (11.0, 10.0)


def test_algebra_and_ast() -> None:
    b, u, r = _board()
    corridor = pcb.between(u.center, r.center).buffer(1.0)
    e = (corridor | pcb.box(0, 0, 5, 5)) - u.courtyard
    assert e.refs() == {"U1", "R1"}
    assert e.ast()["op"] == "diff"
    assert e.resolve(b.region_snapshot()).area > 0
    assert pcb.box(1, 2, 3).get("w") == 1  # 3-D box unchanged


def test_check_flags_unresolvable_regions() -> None:
    b, u, r = _board()
    b.region(u.courtyard.buffer(1), name="ok")
    assert not [f for f in b.check().findings if f.code == "region.unresolved"]
    b.region(u.courtyard & pcb.box(30, 20, 35, 25), name="empty")
    b.region(
        pcb.part_ref("NOPE", "center")
        if hasattr(pcb, "part_ref")
        else pcb.Region("part", "NOPE", "center"),
        name="ghost",
    )
    codes = [f.message for f in b.check().findings if f.code == "region.unresolved"]
    assert len(codes) == 2
    assert any("empty" in m for m in codes) and any("NOPE" in m for m in codes)
