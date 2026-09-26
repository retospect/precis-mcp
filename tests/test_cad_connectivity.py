"""Assembly connectivity graph.

Who touches whom, the connected bodies parts weld into, a contact path
between two parts, and — the headline — the *post-cut* correctness that
makes "is the hub connected to the rim?" a physical answer, not a pre-cut
one: two discs that overlapped massively before their cutouts must read as
disconnected once each is carved down.
"""

from __future__ import annotations

import pytest

from precis.cad.dsl import build_config
from precis.cad.graph import Design
from precis.cad.relate import connectivity
from precis.cad.vec import translation
from precis.dispatch import Hub
from precis.handlers.cad import CadHandler


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


# ── handler view='connectivity' — the `welded` flag ──────────────────────
# (docs/backlog/cad-intended-overlap-weld.md item 4)


@pytest.fixture
def cad(store):
    return CadHandler(hub=Hub(store=store))


@pytest.mark.slow  # 31s in the 2026-09-26 gate profile
def test_connectivity_marks_only_the_welded_contact(cad):
    # a (@0) and b (@8mm) overlap 2mm; c (@14mm) overlaps b by 4mm but is
    # clear of a — a-b declared welded, b-c is an undeclared penetration.
    cad.put(
        id="chainw",
        text=(
            "component a\nabox add box:w10mmd10mmh10mm\n"
            "component b\nbbox add box:w10mmd10mmh10mm @8mm,0mm,0mm\n"
            "component c\ncbox add box:w10mmd10mmh10mm @14mm,0mm,0mm\n"
            "weld a b\n"
        ),
    )
    rep = cad.get(id="chainw", view="connectivity")
    lines = [ln.strip() for ln in rep.body.splitlines()]
    ab_row = next(ln for ln in lines if ln.startswith("a") and "\tb\t" in ln)
    bc_row = next(ln for ln in lines if ln.startswith("b") and "\tc\t" in ln)
    assert ab_row.endswith("true")
    assert bc_row.endswith("false")
