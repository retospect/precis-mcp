"""The BOM consolidation nudge (bom-consolidation-nudge.md): the
``## Consolidation`` footer on ``get(kind='component', view='bom')`` and
``get(kind='pcb', view='bom')`` — attribute-role grouping, the LLM-verdict
line, tool lines, ``kept-distinct`` suppression, house-set proposals and
the cross-design usage count. The verdict seam is stubbed throughout;
``precis.bom_nudge.ask_merge_verdict`` is resolved at call time so the
patch lands.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from precis import bom_nudge
from precis.bom_nudge import MergeVerdict
from precis.dispatch import Hub
from precis.handlers.component import ComponentHandler
from precis.handlers.pcb import PcbHandler
from precis.store.types import Tag


@pytest.fixture
def comp(store: Any) -> ComponentHandler:
    return ComponentHandler(hub=Hub(store=store))


@pytest.fixture
def pcb(store: Any) -> PcbHandler:
    return PcbHandler(hub=Hub(store=store))


@pytest.fixture(autouse=True)
def _stub_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test stubs the verdict — a real LLM call must never fire."""
    monkeypatch.setattr(
        bom_nudge,
        "ask_merge_verdict",
        lambda ctx: MergeVerdict(
            text="stub verdict: looks safe",
            inputs=list(ctx["inputs"]),
        ),
    )


def _resistor(comp: ComponentHandler, slug: str, ohm: float, **kw: Any) -> None:
    comp.put(id=slug, title=slug, category="resistor", **kw)
    comp.put(id=slug, spec="resistance", value=ohm, unit="ohm")
    comp.put(id=slug, spec="package", value="0603")
    comp.put(id=slug, spec="power_rating", value=0.1, unit="W")
    comp.put(id=slug, spec="tolerance", value=1.0, unit="%")


def _resistor_bom(comp: ComponentHandler) -> None:
    """4x 10k, 3x 12k, 1x 8k, all 0603 — the doc's fixture."""
    comp.put(id="lamp", title="Lamp driver", category="electronic")
    for slug, ohm in (("r-10k", 10000), ("r-12k", 12000), ("r-8k", 8000)):
        _resistor(comp, slug, ohm)
    comp.put(id="lamp", contains="component:r-10k", qty=4)
    comp.put(id="lamp", contains="component:r-12k", qty=3)
    comp.put(id="lamp", contains="component:r-8k", qty=1)


# ── grouping + the resistor line ─────────────────────────────────────


class TestResistorGroup:
    def test_one_consolidation_line_naming_10k(
        self, store: Any, comp: ComponentHandler
    ) -> None:
        _resistor_bom(comp)
        body = comp.get(id="lamp", view="bom").body
        assert "## Consolidation" in body
        lines = [ln for ln in body.splitlines() if "resistors" in ln]
        assert len(lines) == 1
        assert "10k x4" in lines[0] and "12k x3" in lines[0]
        assert "8k x1" in lines[0] and "0603" in lines[0]
        # 10k is both plurality and the house value — proposed by name
        assert "propose 10k" in lines[0]
        assert "house value" in lines[0]

    def test_pct_deltas_against_the_proposal(
        self, store: Any, comp: ComponentHandler
    ) -> None:
        _resistor_bom(comp)
        body = comp.get(id="lamp", view="bom").body
        assert "-17%" in body  # 12k → 10k
        assert "+25%" in body  # 8k → 10k

    def test_verdict_and_cited_inputs_render(
        self, store: Any, comp: ComponentHandler
    ) -> None:
        _resistor_bom(comp)
        body = comp.get(id="lamp", view="bom").body
        assert "verdict: stub verdict: looks safe" in body
        assert "no stated intent" in body

    def test_no_intent_states_so_rather_than_asserting(
        self, store: Any, comp: ComponentHandler
    ) -> None:
        _resistor_bom(comp)
        body = comp.get(id="lamp", view="bom").body
        assert "no design intent stated" in body

    def test_declared_intent_is_cited(self, store: Any, comp: ComponentHandler) -> None:
        _resistor_bom(comp)
        comp.put(id="lamp", meta={"intent": "LED string driver"})
        body = comp.get(id="lamp", view="bom").body
        assert "intent: LED string driver" in body
        assert "no design intent stated" not in body


# ── screws: free-attribute verdict + the tool line ───────────────────


class TestFasteners:
    def _screws(self, comp: ComponentHandler, *, drives: bool = True) -> None:
        comp.put(id="frame", title="Frame", category="fastener")
        for slug, length, drive in (
            ("m4x10", 10, "hex"),
            ("m4x12", 12, "torx"),
            ("m4x16", 16, "phillips"),
        ):
            comp.put(id=slug, title=slug, category="fastener")
            comp.put(id=slug, spec="thread_size", value="M4")
            comp.put(id=slug, spec="length", value=length, unit="mm")
            if drives:
                comp.put(id=slug, spec="drive_type", value=drive)
        comp.put(id="frame", contains="component:m4x10", qty=6)
        comp.put(id="frame", contains="component:m4x12", qty=2)
        comp.put(id="frame", contains="component:m4x16", qty=1)

    def test_length_group_line_carries_the_stubbed_verdict(
        self, store: Any, comp: ComponentHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            bom_nudge,
            "ask_merge_verdict",
            lambda ctx: MergeVerdict(
                text="the 16 mm position has only 14 mm of stack",
                inputs=list(ctx["inputs"]),
            ),
        )
        self._screws(comp, drives=False)
        body = comp.get(id="frame", view="bom").body
        line = next(ln for ln in body.splitlines() if "fasteners M4" in ln)
        assert "10 mm x6" in line and "12 mm x2" in line and "16 mm x1" in line
        assert "the 16 mm position has only 14 mm of stack" in line

    def test_three_drive_types_one_tool_line(
        self, store: Any, comp: ComponentHandler
    ) -> None:
        self._screws(comp)
        body = comp.get(id="frame", view="bom").body
        tool = [ln for ln in body.splitlines() if "one driver" in ln]
        assert len(tool) == 1
        assert "hex x6" in tool[0] and "torx x2" in tool[0]
        assert "phillips x1" in tool[0]


# ── kept-distinct suppression ────────────────────────────────────────


class TestKeptDistinct:
    def test_link_suppresses_group_and_prints_reason(
        self, store: Any, comp: ComponentHandler
    ) -> None:
        _resistor_bom(comp)
        a = store.get_ref(kind="component", id="r-10k")
        b = store.get_ref(kind="component", id="r-8k")
        store.add_link(
            src_ref_id=a.id,
            dst_ref_id=b.id,
            relation="kept-distinct",
            meta={"reason": "8k sets the LED current"},
        )
        body = comp.get(id="lamp", view="bom").body
        assert "kept distinct" in body
        assert "8k sets the LED current" in body
        assert "one resistance" not in body

    def test_root_tag_suppresses_group(
        self, store: Any, comp: ComponentHandler
    ) -> None:
        _resistor_bom(comp)
        root = store.get_ref(kind="component", id="lamp")
        store.add_tag(
            root.id,
            Tag.open("kept-distinct:r-10k:r-12k:tracking divider"),
        )
        body = comp.get(id="lamp", view="bom").body
        assert "kept distinct" in body
        assert "tracking divider" in body
        assert "one resistance" not in body


# ── house set + cross-design usage (slice 3) ─────────────────────────


class TestStandardSet:
    def test_nonstandard_members_fall_back_to_plurality(
        self, store: Any, comp: ComponentHandler
    ) -> None:
        comp.put(id="rig", title="Rig", category="electronic")
        for slug, ohm in (("r-11k-a", 11000), ("r-11k-b", 11000), ("r-13k", 13000)):
            _resistor(comp, slug, ohm)
        comp.put(id="rig", contains="component:r-11k-a", qty=3)
        comp.put(id="rig", contains="component:r-11k-b", qty=3)
        comp.put(id="rig", contains="component:r-13k", qty=1)
        body = comp.get(id="rig", view="bom").body
        line = next(ln for ln in body.splitlines() if "resistors" in ln)
        # neither value is E6 → plurality wins, flagged as not the house set
        assert "propose 11k" in line and "house value" not in line

    def test_used_in_other_designs_count(
        self, store: Any, comp: ComponentHandler
    ) -> None:
        _resistor_bom(comp)
        # the same leaf component in a second, independent assembly
        comp.put(id="panel", title="Panel", category="electronic")
        comp.put(id="panel", contains="component:r-10k", qty=2)
        body = comp.get(id="lamp", view="bom").body
        assert "used in 1 other design" in body


# ── the pcb bom footer ───────────────────────────────────────────────


def _seed_parts(store: Any) -> None:
    rows = [
        ("C101", "10kΩ"),
        ("C102", "12kΩ"),
        ("C103", "8.2kΩ"),
    ]
    with store.pool.connection() as conn:
        for lcsc, res in rows:
            conn.execute(
                "INSERT INTO parts (lcsc, package, params, description) "
                "VALUES (%s, '0603', %s::jsonb, %s)",
                (
                    lcsc,
                    json.dumps(
                        {
                            "_category": "Resistors / Chip Resistor - Surface Mount",
                            "Resistance": res,
                            "Power(Watts)": "0.1W",
                            "Tolerance": "±1%",
                        }
                    ),
                    f"chip resistor {res} 0603",
                ),
            )


def _pcb_resistor_board(pcb: PcbHandler, slug: str = "sensor") -> None:
    comps = []
    for i in range(4):
        comps.append(
            {
                "refdes": f"R{i + 1}",
                "label": "10k 0603",
                "part": "C101",
                "footprint": "0603",
                "pins": [{"name": "1"}, {"name": "2"}],
            }
        )
    for i, lcsc in enumerate(("C102", "C102", "C103")):
        comps.append(
            {
                "refdes": f"R{i + 5}",
                "label": ("12k 0603" if lcsc == "C102" else "8.2k 0603"),
                "part": lcsc,
                "footprint": "0603",
                "pins": [{"name": "1"}, {"name": "2"}],
            }
        )
    pcb.put(id=slug, args={"components": comps})


class TestPcbBom:
    def test_pcb_bom_groups_by_jlcparts_class(
        self, store: Any, pcb: PcbHandler, tmp_path: Any
    ) -> None:
        _seed_parts(store)
        _pcb_resistor_board(pcb)
        resp = pcb.get(id="sensor", view="bom", args={"dir": str(tmp_path)})
        assert "## Consolidation" in resp.body
        line = next(ln for ln in resp.body.splitlines() if "resistors" in ln)
        assert "10k x4" in line and "12k x2" in line
        assert "propose 10k" in line
        # the exported CSV is untouched — the nudge is response-only
        csv = (tmp_path / "sensor-bom.csv").read_text(encoding="utf-8")
        assert "Consolidation" not in csv
        assert "10k" in csv

    def test_pcb_cross_design_usage(
        self, store: Any, pcb: PcbHandler, tmp_path: Any
    ) -> None:
        _seed_parts(store)
        _pcb_resistor_board(pcb, "sensor-a")
        _pcb_resistor_board(pcb, "sensor-b")
        resp = pcb.get(id="sensor-a", view="bom", args={"dir": str(tmp_path)})
        assert "used in 1 other design" in resp.body


# ── invariants ───────────────────────────────────────────────────────


class TestInvariants:
    def test_footer_is_last_and_render_is_pure(
        self, store: Any, comp: ComponentHandler
    ) -> None:
        _resistor_bom(comp)
        leaf = store.get_ref(kind="component", id="r-10k")
        n_values = len(store.component_values_for_ref(leaf.id))
        n_links = len(store.links_for(leaf.id))
        a = comp.get(id="lamp", view="bom").body
        b = comp.get(id="lamp", view="bom").body
        assert a == b
        assert a.rindex("## Consolidation") > a.rindex("mass total")
        assert len(store.component_values_for_ref(leaf.id)) == n_values
        assert len(store.links_for(leaf.id)) == n_links

    def test_no_roles_no_footer(self, store: Any, comp: ComponentHandler) -> None:
        comp.put(id="plain", title="Plain", category="widget")
        comp.put(id="sub", title="Sub", category="widget")
        comp.put(id="sub2", title="Sub2", category="widget")
        comp.put(id="plain", contains="component:sub", qty=2)
        comp.put(id="plain", contains="component:sub2", qty=1)
        body = comp.get(id="plain", view="bom").body
        assert "## Consolidation" not in body

    def test_spec_roles_are_registry_rows(self, store: Any) -> None:
        spec = store.component_spec_get("resistance")
        assert spec is not None and spec["spec_roles"] == {"free": True}
        spec = store.component_spec_get("thread_size")
        assert spec["spec_roles"]["identity"] is True
        assert spec["spec_roles"]["tool"]["kind"] == "tap"
        cat = store.component_category_get("resistor")
        assert "jlcparts" in cat["spec_roles"]
