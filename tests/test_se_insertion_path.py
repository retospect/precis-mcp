"""``fastener_insertion_path`` — can the screw REACH its seat, and a tool
(and for a screwdriver, a hand) operate it there, in the fully assembled
state (docs/backlog/se-mechanical-drc.md rulings 1-7).

The geometric cases are hand-checkable: a screw at the origin drives +z
into a plate, its head standing at z in [-4 mm, 0]; the tool and the screw
body come in from -z. A blocker is a box placed on that side.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from precis_se import catalog, fasten, toolaccess
from precis_se.ops import ConnectSpec, SeBlock, SeTree, apply_ops
from precis_se.validate import ValidationIssue

M4 = {
    "thread_size": "M4",
    "thread_pitch": 0.0007,
    "outer_diameter": 0.004,
    "head_diameter": 0.007,
    "head_height": 0.004,
    "length": 0.020,
    "drive_size": 0.003,
    "drive_type": "socket",
    "head_form": "cap",
    "point_type": "machine",
}


def _tree(*, drive: str = "socket") -> SeTree:
    tree = SeTree()
    screw = SeBlock(name="screw", pose=[0, 0, 0])
    screw.bound_kind = "component"
    screw.bound = "iso-4762-m4x20"
    screw.derived = catalog.derive("fastener", {**M4, "drive_type": drive})
    tree.blocks["screw"] = screw
    plate = SeBlock(name="plate", pose=[0, 0, 0.006], envelope="box:w0.05d0.05h0.010")
    plate.mode = "cnc-2.5ax/aluminium"
    tree.blocks["plate"] = plate
    tree.connects.append(
        ConnectSpec(
            a_block="screw",
            a_port="thread",
            b_block="plate",
            b_port="hole",
            joint={"class": "rigid", "mechanism": "screw"},
        )
    )
    return tree


def _rules(tree: SeTree) -> dict[str, ValidationIssue]:
    (res,) = fasten.fasten(tree)
    return {f.rule: f for f in res.findings}


class TestBody:
    def test_a_sibling_over_the_head_blocks_the_screw_and_mutes_no_tool_access(
        self,
    ) -> None:
        tree = _tree()
        # 10 mm above the head: the screw cannot be lifted back to start
        # insertion, and no tool fits in the gap either.
        tree.blocks["cover"] = SeBlock(
            name="cover", pose=[0, 0, -0.014], envelope="box:w0.03d0.03h0.004"
        )
        rules = _rules(tree)
        issue = rules["fastener_insertion_path"]
        assert issue.severity == "error"
        assert "cannot reach its seat" in issue.detail
        assert "screw body" in issue.detail
        assert "'cover'" in issue.detail
        assert "no_tool_access" not in rules
        assert "material_parent_not_walked" not in rules
        assert issue.geometry is not None
        assert [g["role"] for g in issue.geometry] == ["body"]
        body = issue.geometry[0]
        assert body["blocker"] == "cover"
        assert body["envelope"].startswith("cyl:")
        assert len(body["pose"]) == 3 and len(body["rot"]) == 3
        # the body starts one screw length plus one head height back from
        # the seat plane (z=0), towards the tool side (-z): with the tip at
        # the seat plane the head sits at z in [-24 mm, -20 mm]
        assert abs(body["pose"][2] - (-0.024)) < 1e-9

    def test_view_fasten_marks_the_error_apart_from_warnings(self) -> None:
        from precis_se.handler import _render_fasten

        tree = _tree()
        tree.blocks["cover"] = SeBlock(
            name="cover", pose=[0, 0, -0.014], envelope="box:w0.03d0.03h0.004"
        )
        assert "✗ fastener_insertion_path:" in _render_fasten(tree)

    def test_the_head_start_position_is_swept(self) -> None:
        tree = _tree()
        # z in [-23.5 mm, -20.5 mm]: past the shank's start (-20 mm) but
        # inside the head's (-24..-20 mm), clear of every seated part
        tree.blocks["shelf"] = SeBlock(
            name="shelf", pose=[0, 0, -0.0235], envelope="box:w0.03d0.03h0.003"
        )
        issue = _rules(tree)["fastener_insertion_path"]
        assert "'shelf'" in issue.detail
        assert "24.0 mm straight back" in issue.detail

    def test_a_clear_path_adds_no_findings(self) -> None:
        rules = _rules(_tree())
        assert "fastener_insertion_path" not in rules
        assert "material_parent_not_walked" not in rules
        assert "no_tool_access" not in rules


def _under(parent_mode: str | None) -> SeTree:
    tree = _tree()
    box = SeBlock(name="housing", pose=[0, 0, -0.030], envelope="box:w0.05d0.05h0.05")
    box.mode = parent_mode
    tree.blocks["housing"] = box
    tree.blocks["screw"].parent = "housing"
    return tree


class TestAncestors:
    def test_a_material_parent_gives_only_the_warning(self) -> None:
        rules = _rules(_under("fdm/asa"))
        issue = rules["material_parent_not_walked"]
        assert issue.severity == "warn"
        assert "'housing'" in issue.detail
        assert "UNVERIFIED" in issue.detail
        assert "fastener_insertion_path" not in rules
        assert "no_tool_access" not in rules
        assert issue.geometry and issue.geometry[0]["blocker"] == "housing"

    def test_a_bound_parent_is_material_too(self) -> None:
        tree = _under(None)
        tree.blocks["housing"].bound_kind = "cad"
        rules = _rules(tree)
        assert "material_parent_not_walked" in rules
        assert "fastener_insertion_path" not in rules

    def test_a_pure_grouping_parent_is_not_an_obstacle(self) -> None:
        rules = _rules(_under(None))
        assert "material_parent_not_walked" not in rules
        assert "fastener_insertion_path" not in rules
        assert "no_tool_access" not in rules


class TestHand:
    def test_the_hand_data_is_on_the_bit_screwdriver_only(self) -> None:
        tools = {t.tool_id: t for t in toolaccess.tools_for("torx", 3.95)}
        assert tools["bit-driver"].hand_radius_m == 0.045
        assert tools["bit-driver"].hand_length_m == 0.110
        assert "PROVISIONAL" in tools["bit-driver"].hand_source
        for tool_id in ("bit-ratchet", "t-handle"):
            assert tools[tool_id].hand_radius_m == 0.0
        for tool in toolaccess.tools_for("socket", 3.0):
            if tool.tool_id.startswith("hex-key"):
                assert tool.hand_radius_m == 0.0

    def test_a_screwdriver_blocked_by_the_hand_alone_names_the_hand(self) -> None:
        tree = _tree(drive="torx")
        # Two walls 38 mm either side of the axis: nearer than the
        # ratchet's (75 mm) and the T-handle's (45 mm) swing, wider than
        # the bit driver's 13 mm shaft, but inside its 45 mm hand.
        for i, x in enumerate((-0.040, 0.040)):
            tree.blocks[f"wall_{i}"] = SeBlock(
                name=f"wall_{i}",
                pose=[x, 0, -0.250],
                envelope="box:w0.004d0.05h0.22",
            )
        rules = _rules(tree)
        issue = rules["fastener_insertion_path"]
        assert issue.severity == "error"
        assert "bit-driver: the hand hits 'wall_" in issue.detail
        assert "closest is bit screwdriver — its hand hits" in issue.detail
        assert issue.geometry is not None
        roles = [g["role"] for g in issue.geometry]
        assert roles == ["tool", "tool", "hand"]
        assert issue.geometry[-1]["blocker"].startswith("wall_")
        assert issue.geometry[0]["blocker"] is None  # the tool itself clears

    def test_without_the_hand_the_same_walls_would_pass_the_bit_driver(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ruling 7: nothing but the hand blocks it, so removing the hand
        record from the tool turns the error into a pass."""
        tree = _tree(drive="torx")
        for i, x in enumerate((-0.040, 0.040)):
            tree.blocks[f"wall_{i}"] = SeBlock(
                name=f"wall_{i}",
                pose=[x, 0, -0.250],
                envelope="box:w0.004d0.05h0.22",
            )
        original = toolaccess.tools_for

        def no_hands(
            drive_type: str | None, size: float | None
        ) -> list[toolaccess.Tool]:
            return [
                replace(t, hand_radius_m=0.0, hand_length_m=0.0)
                for t in original(drive_type, size)
            ]

        monkeypatch.setattr(toolaccess, "tools_for", no_hands)
        rules = _rules(tree)
        assert "fastener_insertion_path" not in rules


class TestUnicycle:
    """The motivating design (``tests/fixtures/viewer_check``), built in
    memory: bought-fastener specs are attached by hand, as the catalog
    would derive them, because the ops carry only the binding slug."""

    _SPECS = {
        "iso-4762-m5x16": {
            **M4,
            "thread_size": "M5",
            "thread_pitch": 0.0008,
            "outer_diameter": 0.005,
            "head_diameter": 0.008,
            "head_height": 0.005,
            "length": 0.016,
            "drive_size": 0.004,
        },
        "iso-4762-m6x30": {
            **M4,
            "thread_size": "M6",
            "thread_pitch": 0.001,
            "outer_diameter": 0.006,
            "head_diameter": 0.010,
            "head_height": 0.006,
            "length": 0.030,
            "drive_size": 0.005,
        },
        "iso-4762-m4x20": M4,
    }

    def _load(self) -> SeTree:
        path = Path(__file__).parent / "fixtures/viewer_check/unicycle-c1.ops.json"
        ops = json.loads(path.read_text(encoding="utf-8"))["ops"]
        keep = [
            o
            for o in ops
            if not (o["op"] == "set_binding" and o.get("design") not in self._SPECS)
        ]
        tree = apply_ops(SeTree(), keep)
        for node in tree.blocks.values():
            if node.bound in self._SPECS:
                node.derived = catalog.derive("fastener", self._SPECS[node.bound])
        return tree

    def test_flange_bolts_and_clamp_bolt(self) -> None:
        by_fastener = {r.fastener: r for r in fasten.fasten(self._load())}
        right = {f.rule: f for f in by_fastener["flange_bolt_right"].findings}
        assert right["material_parent_not_walked"].severity == "warn"
        assert "'crown'" in right["material_parent_not_walked"].detail
        assert "fastener_insertion_path" not in right
        # MEASURED, not forced: the left flange bolt's head sits directly
        # under the horizontal seatpost pinch bolt, so a real (non-ancestor)
        # block is in its way and the sibling error wins over the crown
        # warning. The right bolt has no such neighbour.
        left = {f.rule: f for f in by_fastener["flange_bolt_left"].findings}
        assert left["fastener_insertion_path"].severity == "error"
        assert "'seatpost_clamp_bolt'" in left["fastener_insertion_path"].detail
        assert "no_tool_access" not in left
        # The clamp bolt's own path leaves the crown sideways: no finding.
        clamp = {f.rule for f in by_fastener["seatpost_clamp_bolt"].findings}
        assert not clamp & {
            "fastener_insertion_path",
            "material_parent_not_walked",
            "no_tool_access",
        }
