"""The ``tensioner`` mechanism's thread-pitch param and the nipple-turns
assembly-instruction slot (gr334786): a spoke's preload, tied to the
tensioning part as turns = (preload / rate) / thread pitch."""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.store import Store
from precis_se import joints as se_joints
from precis_se.handler import SeHandler
from precis_se.ops import SeTree, apply_ops

_TIE = {"tension_capacity": 100.0, "compression_capacity": 0.0}


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    return SeHandler(hub=hub)


def _params(**extra: Any) -> dict[str, Any]:
    return se_joints.validate_joint(
        {"class": "axial", "mechanism": "tensioner", "params": dict(_TIE, **extra)}
    )["params"]


def test_thread_pitch_defaults_to_standard_spoke_thread() -> None:
    plan = se_joints.nipple_turns(_params(preload=600.0, rate=1.2e6))
    assert plan is not None
    assert plan["pitch_m"] == pytest.approx(0.0254 / 56)  # 56 TPI = 0.4536 mm
    # 600 N / 1.2e6 N/m = 0.5 mm stretch; 0.5 / 0.4536 = 1.10 turns
    assert plan["extension_m"] == pytest.approx(5e-4)
    assert plan["turns"] == pytest.approx(5e-4 / (0.0254 / 56))


def test_thread_pitch_override_changes_turns() -> None:
    params = _params(preload=600.0, rate=1.2e6, thread_pitch=0.0005)
    assert params["thread_pitch"] == 0.0005
    plan = se_joints.nipple_turns(params)
    assert plan is not None and plan["turns"] == pytest.approx(1.0)


@pytest.mark.parametrize("bad", [0, -0.001, "fine", float("nan"), float("inf")])
def test_thread_pitch_validated(bad: object) -> None:
    with pytest.raises(se_joints.JointError, match="thread_pitch"):
        _params(thread_pitch=bad)


def test_nipple_turns_needs_preload_and_rate() -> None:
    assert se_joints.nipple_turns(_params(preload=600.0)) is None
    assert se_joints.nipple_turns(_params(rate=1.0e6)) is None


def _tree(params: dict[str, Any]) -> SeTree:
    ops: list[dict[str, Any]] = []
    for name, pose in (("a", [0.0, 0.0, 0.0]), ("b", [1.0, 0.0, 0.0])):
        ops.append({"op": "add_block", "name": name, "pose": pose})
        ops.append({"op": "add_port", "block": name, "name": "pin"})
    ops.append(
        {
            "op": "connect",
            "a": "a.pin",
            "b": "b.pin",
            "joint": {"class": "axial", "mechanism": "tensioner", "params": params},
        }
    )
    return apply_ops(SeTree(), ops)


def test_bom_view_gives_tensioning_instruction(handler: SeHandler) -> None:
    tree = _tree(dict(_TIE, preload=600.0, rate=1.2e6))
    body = "\n".join(handler._tensioner_instructions(tree))
    assert "tension to 600 N = 1.10 nipple turns" in body
    assert "0.4536 mm thread pitch" in body


def test_no_instruction_without_rate(handler: SeHandler) -> None:
    assert handler._tensioner_instructions(_tree(dict(_TIE, preload=600.0))) == []
