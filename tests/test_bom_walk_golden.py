"""Golden test for the bom-walker extraction (bom-tables slice 1):

``get(kind='component', view='bom')`` output is pinned byte-for-byte on
fixture component trees — nested subassemblies, path-multiplied
quantities, a shared leaf reached at two depths, a shared subassembly,
cost/mass rollups, the ``spec=`` consistency summary and the
consolidation-nudge footer. The extraction of the tree walk into a
store-level ``component_bom_walk`` (``_flatten_bom`` becomes a wrapper)
must leave every body below identical.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis import bom_nudge
from precis.bom_nudge import MergeVerdict
from precis.dispatch import Hub
from precis.handlers.component import ComponentHandler


def _handler(store: Any) -> ComponentHandler:
    return ComponentHandler(hub=Hub(store=store))


def _mint(h: ComponentHandler, slug: str, *, category: str = "fastener") -> None:
    h.put(id=slug, title=slug, category=category)


def _nested_shared(h: ComponentHandler) -> None:
    """3-level assembly. ``sub`` is a shared subassembly (under ``rig``
    directly and under ``plate``); ``bolt`` is a shared leaf reached at
    three depths. Leaves: bolt 2*3 + 1*1*3 + 1 = 10, washer
    2*1 + 1*1 + 1*4 = 7."""
    _mint(h, "rig", category="assembly")
    _mint(h, "sub", category="assembly")
    _mint(h, "plate", category="assembly")
    _mint(h, "bolt")
    _mint(h, "washer")
    h.put(id="rig", contains="component:sub", qty=2, ref_designator="A1,A2")
    h.put(id="rig", contains="component:plate", qty=1)
    h.put(id="rig", contains="component:bolt", qty=1)
    h.put(id="sub", contains="component:bolt", qty=3, ref_designator="J9")
    h.put(id="sub", contains="component:washer", qty=1)
    h.put(id="plate", contains="component:sub", qty=1)
    h.put(id="plate", contains="component:washer", qty=4)
    h.put(id="bolt", spec="unit_cost", value=0.05, unit="USD")
    h.put(id="bolt", spec="grade", value="A2")
    h.put(id="washer", spec="mass", value=0.001, unit="kg")
    h.put(id="washer", spec="grade", value="A2")


def _nudge(h: ComponentHandler) -> None:
    """The resistor fixture from test_bom_nudge: 4x 10k, 3x 12k, 1x 8k,
    all 0603 — the footer fires (verdict stubbed)."""
    h.put(id="lamp", title="Lamp driver", category="electronic")
    for slug, ohm in (("r-10k", 10000), ("r-12k", 12000), ("r-8k", 8000)):
        h.put(id=slug, title=slug, category="resistor")
        h.put(id=slug, spec="resistance", value=ohm, unit="ohm")
        h.put(id=slug, spec="package", value="0603")
        h.put(id=slug, spec="power_rating", value=0.1, unit="W")
        h.put(id=slug, spec="tolerance", value=1.0, unit="%")
    h.put(id="lamp", contains="component:r-10k", qty=4)
    h.put(id="lamp", contains="component:r-12k", qty=3)
    h.put(id="lamp", contains="component:r-8k", qty=1)


def _leaf(h: ComponentHandler) -> None:
    _mint(h, "washer")


def _bodies(store: Any) -> dict[str, str]:
    """Render every golden case against one store. Each case builds its
    own tree, so ids stay stable and independent."""
    h = _handler(store)
    _nested_shared(h)
    _nudge(h)
    _leaf(h)
    return {
        "nested_shared": h.get(id="rig", view="bom").body,
        "nested_shared_spec": h.get(id="rig", view="bom", spec="grade").body,
        "nested_shared_subtree": h.get(id="sub", view="bom").body,
        "nudge": h.get(id="lamp", view="bom").body,
        "leaf": h.get(id="washer", view="bom").body,
    }


_GOLDEN: dict[str, str] = {
    "leaf": "# BOM: washer (washer) — 1 leaf\n"
    "{component\tqty\tunit_cost\tmass}\n"
    "washer (washer)\t1\t—\t0.001\n"
    "\n"
    "unit_cost total: 0 — unit_cost: 0 of 1 leaves\n"
    "mass total: 0.001 — mass: 1 of 1 leaves",
    "nested_shared": "# BOM: rig (rig) — 2 leaves\n"
    "{component\tqty\tunit_cost\tmass}\n"
    "bolt (bolt)\t10\t0.05\t—\n"
    "washer (washer)\t7\t—\t0.001\n"
    "\n"
    "unit_cost total: 0.5 — unit_cost: 1 of 2 leaves\n"
    "mass total: 0.007 — mass: 1 of 2 leaves\n"
    "\n"
    "## Consolidation\n"
    "- fasteners A2: bolt x10, washer x7 — identical on declared specs; one part?",
    "nested_shared_spec": "# BOM: rig (rig) — 2 leaves\n"
    "{component\tqty\tunit_cost\tmass\tgrade}\n"
    "bolt (bolt)\t10\t0.05\t—\tA2\n"
    "washer (washer)\t7\t—\t0.001\tA2\n"
    "\n"
    "unit_cost total: 0.5 — unit_cost: 1 of 2 leaves\n"
    "mass total: 0.007 — mass: 1 of 2 leaves\n"
    "\n"
    "grade: all 'A2' (2/2 leaves)\n"
    "\n"
    "## Consolidation\n"
    "- fasteners A2: bolt x10, washer x7 — identical on declared specs; one "
    "part?",
    "nested_shared_subtree": "# BOM: sub (sub) — 2 leaves\n"
    "{component\tqty\tunit_cost\tmass}\n"
    "bolt (bolt)\t3\t0.05\t—\n"
    "washer (washer)\t1\t—\t0.001\n"
    "\n"
    "unit_cost total: 0.15 — unit_cost: 1 of 2 leaves\n"
    "mass total: 0.001 — mass: 1 of 2 leaves\n"
    "\n"
    "## Consolidation\n"
    "- fasteners A2: bolt x3, washer x1 — identical on declared specs; one "
    "part?",
    "nudge": "# BOM: Lamp driver (lamp) — 3 leaves\n"
    "{component\tqty\tunit_cost\tmass}\n"
    "r-10k (r-10k)\t4\t—\t—\n"
    "r-12k (r-12k)\t3\t—\t—\n"
    "r-8k (r-8k)\t1\t—\t—\n"
    "\n"
    "unit_cost total: 0 — unit_cost: 0 of 3 leaves\n"
    "mass total: 0 — mass: 0 of 3 leaves\n"
    "\n"
    "## Consolidation\n"
    "- resistors 0603: 10k x4, 12k x3, 8k x1 — one resistance? propose 10k (the house "
    "value); power rating, tolerance, package identical; 12k→10k is -17%, 8k→10k is +25%. "
    "verdict: stub verdict: looks safe (inputs: no stated intent) (no design intent stated)",
}


@pytest.fixture(autouse=True)
def _stub_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """The nudge's LLM verdict seam is stubbed — deterministic and never
    a real call (same pattern as test_bom_nudge)."""
    monkeypatch.setattr(
        bom_nudge,
        "ask_merge_verdict",
        lambda ctx: MergeVerdict(
            text="stub verdict: looks safe",
            inputs=list(ctx["inputs"]),
        ),
    )


@pytest.mark.parametrize("case", sorted(_GOLDEN))
def test_bom_render_is_byte_identical(store: Any, case: str) -> None:
    bodies = _bodies(store)
    assert bodies[case] == _GOLDEN[case]
