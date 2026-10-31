"""precis_se.motor — se-flagellar-motor.md slice 1: the linear
torque-speed motor computer (``MOTOR_COMPUTERS['output-torque']``) on a
stator/rotor revolute pair."""

from __future__ import annotations

import pytest

from precis_se.measures import MeasureSpec
from precis_se.motor import MOTOR_COMPUTERS, torque_at_speed
from precis_se.ops import ConnectSpec, SeBlock, SeTree


def _motor_tree(stall: float = 10.0, zero_load: float = 100.0) -> SeTree:
    tree = SeTree(
        blocks={"stator": SeBlock(name="stator"), "rotor": SeBlock(name="rotor")}
    )
    tree.connects.append(
        ConnectSpec(
            a_block="stator",
            a_port="axle",
            b_block="rotor",
            b_port="axle",
            joint={"class": "revolute", "axis": [0.0, 0.0, 1.0]},
        )
    )
    tree.measures = [
        MeasureSpec(
            block="stator",
            name="stall",
            value=stall,
            measurand="stall-torque",
            unit="N*m",
        ),
        MeasureSpec(
            block="stator",
            name="w0",
            value=zero_load,
            measurand="zero-load-speed",
            unit="rad/s",
        ),
    ]
    return tree


def _output_measure() -> MeasureSpec:
    return MeasureSpec(
        block="stator", name="out", measurand="output-torque", unit="N*m"
    )


def test_torque_at_speed_linear_curve() -> None:
    assert torque_at_speed(10.0, 100.0, 50.0) == pytest.approx(5.0)
    assert torque_at_speed(10.0, 100.0, 0.0) == pytest.approx(10.0)
    assert torque_at_speed(10.0, 100.0, 100.0) == pytest.approx(0.0)


def test_output_torque_computer_reads_declared_measures() -> None:
    tree = _motor_tree(stall=10.0, zero_load=100.0)
    tree.measures.append(
        MeasureSpec(
            block="stator",
            name="w",
            value=50.0,
            measurand="operating-speed",
            unit="rad/s",
        )
    )
    comp = MOTOR_COMPUTERS["output-torque"]
    assert comp(tree, _output_measure()) == pytest.approx(5.0)

    tree.measures[-1].value = 0.0
    assert comp(tree, _output_measure()) == pytest.approx(10.0)

    tree.measures[-1].value = 100.0
    assert comp(tree, _output_measure()) == pytest.approx(0.0)


def test_output_torque_missing_operating_speed_reports_reason() -> None:
    tree = _motor_tree()
    comp = MOTOR_COMPUTERS["output-torque"]
    assert comp(tree, _output_measure()) is None
    assert "operating-speed" in (comp.reason or "")
