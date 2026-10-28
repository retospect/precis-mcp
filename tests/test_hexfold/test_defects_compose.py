"""Several authored ring defects on one part (gr459928).

Each wedge cut runs from its core to the patch edge, so two defects compose
only if neither cut reaches the other. ``_apply_authored`` tries orders
authored-first and keeps the first whose rings are exactly the authored
ones; when none composes, ``cut.crosses`` names the pair and a re-cut.
Wedge defects on tubes and closed caps are refused, not silently dropped.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest

from hexfold import build as hb
from hexfold.build import Net, build
from hexfold.check import check

_EX = Path(__file__).resolve().parents[2] / "hexfold" / "examples"


def _net(body: str) -> Net:
    return build(f"hexfold 0.2\n{body}\n", strict=False)


def _defect_rings(net: Net) -> Counter[int]:
    return Counter(len(r) for r in net.rings if len(r) != 6)


def _codes(net: Net) -> set[str]:
    return {f.code for f in net.report.errors()}


def test_two_pentagon_example_builds_both_rings_and_checks_clean() -> None:
    text = (_EX / "sheet_two_pentagons.hx").read_text(encoding="utf-8")
    net = build(text, strict=False)
    assert _defect_rings(net) == Counter({5: 2})
    assert not net.report.errors()
    report = check(text, geometry=True)
    assert not [f for f in report.findings if f.severity.name == "ERROR"]


def test_two_heptagons_compose_on_one_sheet() -> None:
    """This pair tore the net (internal.euler) before the order search."""
    net = _net("s: sheet(30,30) + heptagon@(15,15,A):1 + heptagon@(6,15,A):1")
    assert _defect_rings(net) == Counter({7: 2})
    assert not net.report.errors()


def test_order_search_composes_a_pair_the_authored_order_tears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orders: list[tuple[int, ...]] = []
    real = hb._surgery

    def spy(patch, authored, order):
        orders.append(order)
        real(patch, authored, order)

    monkeypatch.setattr(hb, "_surgery", spy)
    net = _net("s: sheet(20,20) + heptagon@(10,10,A):2 + pentagon@(6,10,A):2")
    assert orders == [(0, 1), (1, 0)]
    assert _defect_rings(net) == Counter({5: 1, 7: 1})
    assert not net.report.errors()


def test_crossing_cuts_are_an_error_with_a_recut_that_composes() -> None:
    """Before: one pentagon silently deleted by the other's wedge."""
    net = _net("s: sheet(30,30) + pentagon@(15,15,A):2 + pentagon@(6,15,A):0")
    [err] = [f for f in net.report.errors() if f.code == "cut.crosses"]
    assert "pentagon@(15,15,A):2 and pentagon@(6,15,A):0" in err.message
    hint = re.search(r"-> (pentagon@\(\d+,\d+,[AB]\):\d)", err.message)
    assert hint is not None
    fixed = _net(f"s: sheet(30,30) + {hint.group(1)} + pentagon@(6,15,A):0")
    assert _defect_rings(fixed) == Counter({5: 2})
    assert not fixed.report.errors()


@pytest.mark.parametrize("d1", range(6))
def test_no_pair_is_silently_wrong(d1: int) -> None:
    """Every pair either builds exactly its rings or carries an ERROR."""
    for kind, ring in (("pentagon", 5), ("heptagon", 7)):
        for d2 in range(6):
            net = _net(
                f"s: sheet(20,20) + heptagon@(10,10,A):{d1} + {kind}@(6,10,A):{d2}"
            )
            ok = _defect_rings(net) == Counter([7, ring])
            assert ok or net.report.errors(), (d1, kind, d2)


def test_lone_defect_that_cannot_cut_is_an_error_not_dropped() -> None:
    net = _net("c: cap(6,0) + pentagon@(0,0,A):0")
    assert "cut.crosses" in _codes(net)


def test_wedge_defect_on_a_tube_is_refused() -> None:
    net = _net("t: tube(12,0, len=14) + heptagon@(0,8,A):0")
    assert "build.defect" in _codes(net)
    assert _defect_rings(net) == Counter()


def test_defect_on_a_closed_cap_is_refused() -> None:
    net = _net("c: cap(5,5) + pentagon@(0,0,A):0")
    assert "build.defect" in _codes(net)
