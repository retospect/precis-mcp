"""Unit tests for precis.pcb.rules — the single per-net rules resolver
closing pcb-usb-c-pd-nano-testboard.md's Gaps A/B. No DB.

Covers: IPC-2221 width at known reference currents (checked against a
hand-derived magnitude, not the implementation's own formula restated),
the external-vs-internal split, a class-rule override beating the
current-derived width, and the fab floor clamping a too-small request.
"""

from __future__ import annotations

import math

import pytest

from precis.pcb.capabilities import capability_for
from precis.pcb.rules import (
    IPC2221_K_EXTERNAL,
    IPC2221_K_INTERNAL,
    VIA_REFERENCE_CAPACITY_A,
    VIA_REFERENCE_DIA_MM,
    ipc2221_capacity_a,
    ipc2221_track_width_mm,
    net_current_a_or_none,
    resolve_net_rules,
    via_capacity_a,
    via_count_for_current,
)

_CAP4 = capability_for("4layer")
# 4-layer trace_width_mm: jlc_min=0.09, house_default=0.15 (capabilities.py)


# ── IPC-2221 formula: reference points ──────────────────────────────────


def _hand_ipc2221_width_mm(current_a: float, k: float, temp_rise_c: float) -> float:
    """The formula restated independently (mils, then converted) — a real
    cross-check, not the implementation's own math read back."""
    area_mil2 = (current_a / (k * temp_rise_c**0.44)) ** (1.0 / 0.725)
    width_mil = area_mil2 / 1.378  # 1oz copper
    return width_mil * 0.0254


@pytest.mark.parametrize("current_a", [1.0, 2.0, 3.0, 5.0, 10.0])
def test_ipc2221_width_matches_hand_derived_formula_external(current_a: float):
    got = ipc2221_track_width_mm(current_a, layer_is_outer=True, temp_rise_c=10.0)
    expected = _hand_ipc2221_width_mm(current_a, IPC2221_K_EXTERNAL, 10.0)
    assert got == pytest.approx(expected, rel=1e-9)


def test_ipc2221_five_amps_ten_c_rise_one_oz_external_lands_around_2mm_not_tenths():
    """The sanity check named in the task: 5A/1oz/external/10C rise must
    land in the few-mm range (a real conductor), never tenths of a mm (a
    fuse) — the magnitude regression this whole module exists to fix."""
    width = ipc2221_track_width_mm(5.0, layer_is_outer=True, temp_rise_c=10.0)
    assert 1.5 < width < 4.0
    assert width > 1.0  # an order of magnitude above the old flat 0.25mm default


def test_ipc2221_one_amp_external_matches_common_rule_of_thumb():
    """precis-net-class-help.md's own documented rule of thumb ("~0.5
    mm/A on external 1oz copper for a ~10C rise") — cross-checked against
    the already-authored product doc, not just the formula."""
    width = ipc2221_track_width_mm(1.0, layer_is_outer=True, temp_rise_c=10.0)
    assert 0.15 < width < 0.5


# ── outer vs. inner: the real (not cosmetic) split ───────────────────────


def test_ipc2221_internal_needs_roughly_2_6x_external_width_same_current():
    ext = ipc2221_track_width_mm(5.0, layer_is_outer=True, temp_rise_c=10.0)
    inner = ipc2221_track_width_mm(5.0, layer_is_outer=False, temp_rise_c=10.0)
    ratio = inner / ext
    # exact algebraic ratio: (k_ext/k_int)^(1/0.725) = 2^(1/0.725)
    expected_ratio = (IPC2221_K_EXTERNAL / IPC2221_K_INTERNAL) ** (1.0 / 0.725)
    assert ratio == pytest.approx(expected_ratio, rel=1e-9)
    assert 2.0 < ratio < 3.0  # "roughly double", not identical, not 10x


def test_ipc2221_capacity_a_is_the_exact_inverse_of_track_width():
    for current_a in (0.5, 1.0, 3.0, 5.0, 8.0):
        for outer in (True, False):
            width = ipc2221_track_width_mm(current_a, layer_is_outer=outer)
            back = ipc2221_capacity_a(width, layer_is_outer=outer)
            assert back == pytest.approx(current_a, rel=1e-6)


# ── resolve_net_rules: resolution order ──────────────────────────────────


def test_resolve_net_rules_no_override_no_current_falls_to_fab_floor():
    rules = resolve_net_rules(
        "signal", layer_is_outer=True, fab_caps=_CAP4, overrides=None, current_a=None
    )
    assert rules.track_width_mm == pytest.approx(_CAP4.house_default["trace_width_mm"])
    assert rules.clearance_mm == pytest.approx(_CAP4.house_default["trace_spacing_mm"])


def test_resolve_net_rules_derives_width_from_current_when_no_override():
    rules = resolve_net_rules(
        "power", layer_is_outer=True, fab_caps=_CAP4, overrides=None, current_a=5.0
    )
    expected = ipc2221_track_width_mm(5.0, layer_is_outer=True)
    assert rules.track_width_mm == pytest.approx(expected, rel=1e-9)
    assert rules.track_width_mm > 1.0  # nowhere near the old 0.25mm fuse


def test_resolve_net_rules_class_override_beats_current_derivation():
    """An explicit pcb_net_classes.rules override wins even when a current
    annotation is present -- resolution order step 1, before step 2."""
    rules = resolve_net_rules(
        "power",
        layer_is_outer=True,
        fab_caps=_CAP4,
        overrides={"track_width_mm": 1.23, "clearance_mm": 0.5},
        current_a=5.0,  # would otherwise derive ~2.77mm -- override wins
    )
    assert rules.track_width_mm == pytest.approx(1.23)
    assert rules.clearance_mm == pytest.approx(0.5)


def test_resolve_net_rules_clamps_a_too_narrow_override_up_to_fab_floor():
    rules = resolve_net_rules(
        "signal",
        layer_is_outer=True,
        fab_caps=_CAP4,
        overrides={"track_width_mm": 0.01, "clearance_mm": 0.01},
    )
    jlc_min_w = _CAP4.jlc_min["trace_width_mm"]
    jlc_min_c = _CAP4.jlc_min["trace_spacing_mm"]
    assert jlc_min_w is not None and jlc_min_c is not None
    assert rules.track_width_mm == pytest.approx(jlc_min_w)
    assert rules.clearance_mm == pytest.approx(jlc_min_c)


def test_resolve_net_rules_clamps_a_too_small_current_derived_width_up_to_fab_floor():
    """A tiny current derives a sub-mil width -- the fab floor clamp,
    never a manufacturing-impossible trace."""
    rules = resolve_net_rules(
        "power", layer_is_outer=True, fab_caps=_CAP4, overrides=None, current_a=0.01
    )
    derived = ipc2221_track_width_mm(0.01, layer_is_outer=True)
    jlc_min_w = _CAP4.jlc_min["trace_width_mm"]
    assert jlc_min_w is not None
    assert derived < jlc_min_w  # sanity: this trial genuinely needs clamping
    assert rules.track_width_mm == pytest.approx(jlc_min_w)


def test_resolve_net_rules_a_generous_current_derived_width_is_never_reduced():
    rules = resolve_net_rules(
        "power", layer_is_outer=True, fab_caps=_CAP4, overrides=None, current_a=5.0
    )
    jlc_min_w = _CAP4.jlc_min["trace_width_mm"]
    assert jlc_min_w is not None
    assert rules.track_width_mm > jlc_min_w  # the clamp never LOWERS a wide request


def test_resolve_net_rules_never_below_fab_minimum_property():
    """A class rule may ask for more copper/clearance, never less than
    the fab can make -- checked across a spread of override values."""
    jlc_min_w = _CAP4.jlc_min["trace_width_mm"]
    jlc_min_c = _CAP4.jlc_min["trace_spacing_mm"]
    assert jlc_min_w is not None and jlc_min_c is not None
    for w in (0.001, 0.05, 0.09, 0.2, 1.0):
        for c in (0.001, 0.05, 0.09, 0.2, 1.0):
            rules = resolve_net_rules(
                "signal",
                layer_is_outer=True,
                fab_caps=_CAP4,
                overrides={"track_width_mm": w, "clearance_mm": c},
            )
            assert rules.track_width_mm >= jlc_min_w - 1e-9
            assert rules.clearance_mm >= jlc_min_c - 1e-9


def test_resolve_net_rules_via_fields_are_populated_from_fab_floor():
    rules = resolve_net_rules("power", layer_is_outer=True, fab_caps=_CAP4)
    assert rules.via_dia_mm is not None and rules.via_dia_mm > 0
    assert rules.via_drill_mm is not None and rules.via_drill_mm > 0


# ── via ampacity: via_capacity_a / via_count_for_current ─────────────────


def test_via_capacity_a_matches_the_reference_point_exactly():
    assert via_capacity_a(VIA_REFERENCE_DIA_MM) == pytest.approx(
        VIA_REFERENCE_CAPACITY_A
    )


def test_via_capacity_a_is_linear_in_diameter():
    assert via_capacity_a(0.6) == pytest.approx(2 * via_capacity_a(0.3))


def test_via_capacity_a_non_positive_diameter_is_zero():
    assert via_capacity_a(0.0) == 0.0
    assert via_capacity_a(-0.1) == 0.0


def test_via_count_for_current_no_annotation_defaults_to_one():
    assert via_count_for_current(None, 0.4) == 1
    assert via_count_for_current(0.0, 0.4) == 1
    assert via_count_for_current(-1.0, 0.4) == 1


def test_via_count_for_current_a_single_via_suffices_for_a_small_current():
    # via_capacity_a(0.4) ~= 1.33A -- 0.5A comfortably fits one via.
    assert via_count_for_current(0.5, 0.4) == 1


def test_via_count_for_current_scales_up_for_a_real_power_rail():
    """The exact regression this task exists to prevent: a 5A rail must
    NOT be assigned a single via — a single via cannot carry it."""
    dia = 0.4
    capacity = via_capacity_a(dia)
    n = via_count_for_current(5.0, dia)
    assert n > 1
    assert n * capacity >= 5.0  # the stitched group's total capacity covers the draw
    assert n == math.ceil(5.0 / capacity)


def test_via_count_for_current_never_below_one():
    for current in (0.001, 0.1, 1.0, 100.0):
        assert via_count_for_current(current, 0.4) >= 1


# ── net_current_a_or_none ────────────────────────────────────────────────


def test_net_current_a_or_none_normalizes_nan_and_none():
    assert net_current_a_or_none(None) is None
    assert net_current_a_or_none(math.nan) is None
    assert net_current_a_or_none(5.0) == 5.0
