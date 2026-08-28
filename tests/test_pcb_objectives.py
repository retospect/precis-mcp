"""Unit tests for precis.pcb.objectives — objective vectors, the loop-
scoped impedance objective, and the coupling formula. No DB.
"""

from __future__ import annotations

import pytest

from precis.pcb.objectives import (
    NetAnnotation,
    ObjectiveVector,
    SignalLevel,
    aggressor_strength,
    annotation_for,
    coupling,
    objectives_for_connection,
    victim_susceptibility,
)


def test_objective_vector_rejects_out_of_range_weight():
    with pytest.raises(ValueError):
        ObjectiveVector(
            low_impedance=1.5,
            low_resistance=0,
            low_capacitance=0,
            small_loop_area=0,
            low_coupling=0,
            matched_length=0,
        )


def test_power_and_ground_get_a_loop_return_net_signal_does_not():
    power_vec, power_why = objectives_for_connection("power", return_net=7)
    ground_vec, _ = objectives_for_connection("ground", return_net=7)
    signal_vec, signal_why = objectives_for_connection("signal", return_net=7)
    assert power_vec.return_net == 7
    assert ground_vec.return_net == 7
    assert signal_vec.return_net is None
    assert power_why and signal_why  # every preset carries a reason


def test_power_absorbs_the_width_policy_wide_not_narrow():
    # low_resistance high (=> wide), low_capacitance low (=> not narrow) —
    # this IS the width-policy absorption the backlog calls for; no
    # separate width enum should exist anywhere in this module.
    vec, _ = objectives_for_connection("power")
    assert vec.low_resistance > vec.low_capacitance


def test_rf_favours_capacitance_over_resistance():
    vec, _ = objectives_for_connection("rf")
    assert vec.low_capacitance > vec.low_resistance


def test_unknown_class_falls_back_to_plain_signal():
    vec, reason = objectives_for_connection("some-unrecognized-class")
    signal_vec, _ = objectives_for_connection("signal")
    assert vec == signal_vec
    assert reason


def test_non_electrical_domain_rejected():
    with pytest.raises(ValueError):
        objectives_for_connection("signal", "fluidic")


def test_annotation_for_override_wins_over_function_hint():
    override = NetAnnotation(
        impedance_ohm=1.0, edge_rate_v_per_ns=9.0, signal_level=SignalLevel.POWER
    )
    assert annotation_for("crystal", override) is override


def test_annotation_for_unknown_hint_is_conservative_not_zero():
    ann = annotation_for(None)
    assert ann.impedance_ohm is not None and ann.impedance_ohm > 0
    # unknown -> high-Z (worst-case victim), no asserted edge rate (not an
    # aggressor without evidence)
    assert ann.edge_rate_v_per_ns is None


def test_aggressor_strength_bounds():
    quiet = NetAnnotation(
        impedance_ohm=50.0, edge_rate_v_per_ns=None, signal_level=SignalLevel.LOGIC
    )
    fast = NetAnnotation(
        impedance_ohm=1.0, edge_rate_v_per_ns=100.0, signal_level=SignalLevel.POWER
    )
    assert aggressor_strength(quiet) == 0.0
    assert 0.0 <= aggressor_strength(fast) <= 1.0
    assert aggressor_strength(fast) > aggressor_strength(quiet)


def test_victim_susceptibility_bounds_and_direction():
    low_z = NetAnnotation(
        impedance_ohm=0.1, edge_rate_v_per_ns=0.0, signal_level=SignalLevel.POWER
    )
    high_z = NetAnnotation(
        impedance_ohm=1.0e6, edge_rate_v_per_ns=0.0, signal_level=SignalLevel.LOW
    )
    assert 0.0 <= victim_susceptibility(low_z) <= 1.0
    assert 0.0 <= victim_susceptibility(high_z) <= 1.0
    assert victim_susceptibility(high_z) >= victim_susceptibility(low_z)


def test_coupling_is_zero_when_aggressor_is_quiescent():
    # the decap-DC-terminal case: a PWR net at DC can't be an aggressor
    # regardless of anything else, which is exactly why it correctly
    # drops out of a coupling sum without any special-casing.
    quiet_pwr = NetAnnotation(
        impedance_ohm=0.1, edge_rate_v_per_ns=None, signal_level=SignalLevel.POWER
    )
    sensitive = NetAnnotation(
        impedance_ohm=1.0e6, edge_rate_v_per_ns=None, signal_level=SignalLevel.LOW
    )
    assert coupling(quiet_pwr, sensitive, k_geometry=1.0) == 0.0


def test_coupling_monotonic_in_geometry_factor():
    aggressor = NetAnnotation(
        impedance_ohm=1.0, edge_rate_v_per_ns=10.0, signal_level=SignalLevel.POWER
    )
    victim = NetAnnotation(
        impedance_ohm=1.0e6, edge_rate_v_per_ns=None, signal_level=SignalLevel.LOW
    )
    near = coupling(aggressor, victim, k_geometry=0.9)
    far = coupling(aggressor, victim, k_geometry=0.1)
    assert near > far >= 0.0


def test_coupling_clamps_k_geometry_outside_unit_interval():
    aggressor = NetAnnotation(
        impedance_ohm=1.0, edge_rate_v_per_ns=10.0, signal_level=SignalLevel.POWER
    )
    victim = NetAnnotation(
        impedance_ohm=1.0e6, edge_rate_v_per_ns=None, signal_level=SignalLevel.LOW
    )
    assert coupling(aggressor, victim, k_geometry=5.0) == coupling(
        aggressor, victim, k_geometry=1.0
    )
    assert coupling(aggressor, victim, k_geometry=-5.0) == 0.0
