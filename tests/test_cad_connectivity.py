"""Assembly connectivity graph (ADR 0041 §7).

Who touches whom, the connected bodies parts weld into, a contact path
between two parts, and — the headline — the *post-cut* correctness that
makes "is the hub connected to the rim?" a physical answer, not a pre-cut
one: two discs that overlapped massively before their cutouts must read as
disconnected once each is carved down.
"""

from __future__ import annotations

from precis.cad.dsl import build_config
from precis.cad.graph import Design
from precis.cad.relate import connectivity
from precis.cad.vec import translation


def _two_boxes(dx: float) -> Design:
    d = Design()
    d.add_component("a", d.prim("a", build_config("box:w10d10h10")))
    d.add_component(
        "b", d.prim("b", build_config("box:w10d10h10"), translation(dx, 0, 0))
    )
    return d


def test_single_component_is_trivially_connected() -> None:
    d = Design()
    d.add_component("solo", d.prim("solo", build_config("box:w10d10h10")))
    c = connectivity(d)
    assert c.connected
    assert c.groups == (("solo",),)
    assert c.isolated() == []


def test_touching_boxes_are_one_body() -> None:
    d = _two_boxes(9.0)  # 1 mm overlap → touching
    c = connectivity(d)
    assert c.connected
    assert len(c.groups) == 1
    assert c.path("a", "b") == ["a", "b"]
    assert c.neighbors("a") == ["b"]
    assert c.isolated() == []


def test_separated_boxes_are_two_bodies() -> None:
    d = _two_boxes(40.0)  # 30 mm gap → disconnected
    c = connectivity(d)
    assert not c.connected
    assert len(c.groups) == 2
    assert c.path("a", "b") is None
    assert sorted(c.isolated()) == ["a", "b"]


def _wheel(*, spoke: bool) -> Design:
    """A hub (small disc r5) and a rim (annulus r15..r20 = disc r20 − disc
    r15). The raw discs overlap massively; the realised annulus is 10 mm
    clear of the hub. With ``spoke`` a bar bridges the two."""
    d = Design()
    d.add_component("hub", d.prim("hub", build_config("cyl:r5h4")))
    rdisc = d.prim("rdisc", build_config("cyl:r20h4"))
    rhole = d.prim("rhole", build_config("cyl:r15h6"), translation(0, 0, -1))
    d.add_component("rim", d.subtract(rdisc, rhole))
    if spoke:
        d.add_component(
            "spoke", d.prim("spoke", build_config("box:w20d2h4"), translation(10, 0, 0))
        )
    return d


def test_rim_and_hub_disconnected_after_cut() -> None:
    # Raw discs (r20 vs r5) overlap, but post-cut the rim is an annulus 10 mm
    # clear of the hub — connectivity must reflect the carved material.
    d = _wheel(spoke=False)
    c = connectivity(d)
    assert not c.connected
    assert c.path("hub", "rim") is None
    assert sorted(c.isolated()) == ["hub", "rim"]


def test_spoke_bridges_hub_and_rim() -> None:
    d = _wheel(spoke=True)
    c = connectivity(d)
    assert c.connected
    assert c.path("hub", "rim") == ["hub", "spoke", "rim"]
    assert c.neighbors("spoke") == ["hub", "rim"]
