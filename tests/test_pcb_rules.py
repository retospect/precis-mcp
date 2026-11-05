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
    LayerTiers,
    NetRules,
    Owner,
    RouteVia,
    RuleContext,
    clearance_rule_problems,
    ipc2221_capacity_a,
    ipc2221_track_width_mm,
    net_current_a_or_none,
    required,
    resolve_net_rules,
    route_via,
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


def test_net_row_stores_current_as_amps_number_and_keeps_explicit_zero():
    """gr520542: ``current``/``est_current_a`` is amps, a plain number —
    no unit scaling, and an explicit ``0`` (a real "carries nothing"
    annotation) is stored, never collapsed to NULL by the alias fold."""
    from precis.pcb.rows import net_row

    assert net_row({"name": "X", "current": 0.5})["est_current_a"] == 0.5
    assert net_row({"name": "X", "est_current_a": 0.001})["est_current_a"] == 0.001
    assert net_row({"name": "X", "current": 0})["est_current_a"] == 0
    assert net_row({"name": "X"})["est_current_a"] is None


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


def test_resolve_net_rules_via_drill_follows_an_overridden_dia():
    """gr524334: dia and drill are ONE coupled barrel — a class
    ``via_dia_mm`` override under ``drill + 2 x ring`` must shrink the
    drill, else the resolved pair emits an annular ring under the fab
    floor (0.45 dia over the house 0.25 drill leaves 0.10 mm, under
    JLC's 0.15). The 4layer fallback pair is exactly dia 0.45 / drill
    0.15 / ring 0.15."""
    rules = resolve_net_rules(
        "signal",
        layer_is_outer=True,
        fab_caps=_CAP4,
        overrides={"via_dia_mm": _CAP4.jlc_min["via_diameter_mm"]},
    )
    ring_min = _CAP4.jlc_min["annular_ring_mm"]
    assert ring_min is not None
    assert rules.via_dia_mm == pytest.approx(_CAP4.jlc_min["via_diameter_mm"])
    assert rules.via_drill_mm == pytest.approx(_CAP4.jlc_min["drill_mm"])
    assert rules.via_dia_mm is not None and rules.via_drill_mm is not None
    assert (rules.via_dia_mm - rules.via_drill_mm) / 2.0 == pytest.approx(ring_min)


def test_resolve_net_rules_via_drill_unmoved_when_the_ring_already_clears():
    """The coupling never shrinks a drill whose ring already clears the
    floor: the plain house pair (0.75 dia / 0.25 drill / 0.25 ring)
    resolves unchanged."""
    rules = resolve_net_rules("signal", layer_is_outer=True, fab_caps=_CAP4)
    assert rules.via_dia_mm == pytest.approx(_CAP4.house_default["via_diameter_mm"])
    assert rules.via_drill_mm == pytest.approx(_CAP4.house_default["drill_mm"])


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


# ── route-stage via choice: route_via ────────────────────────────────────
#
# The ladder the rule ranks (4layer, derived): the fab floor 0.45/0.15
# (1.5 A conservative capacity) and the house via 0.75/0.25 (2.5 A). An
# n-barrel row packs at same-net pitch dia + 0.2 (the fab's hole-to-hole
# spacing) behind one clearance ring c:
#   footprint(d, n) = ((n - 1)(d + 0.2) + d + 2c)(d + 2c).

_FAB_VIA = (
    float(_CAP4.jlc_min["via_diameter_mm"] or 0.0),  # 0.45
    float(_CAP4.jlc_min["drill_mm"] or 0.0),  # 0.15
)
_HOUSE = NetRules(
    track_width_mm=0.15,
    clearance_mm=0.15,
    via_dia_mm=_CAP4.house_default["via_diameter_mm"],  # 0.75
    via_drill_mm=_CAP4.house_default["drill_mm"],  # 0.25
)


def test_route_via_no_current_takes_one_fab_minimum_via() -> None:
    """A signal via carries no current: the smallest legal barrel takes
    the least room — via room is the router's dominant failure."""
    for cur in (None, math.nan, 0.0):
        v = route_via(cur, _FAB_VIA, _HOUSE)
        assert v is not None and v.count == 1
        assert v.dia_mm == pytest.approx(0.45)
        assert v.drill_mm == pytest.approx(0.15)


def test_route_via_at_fab_capacity_is_still_one_fab_via() -> None:
    v = route_via(via_capacity_a(_FAB_VIA[0]), _FAB_VIA, _HOUSE)
    assert v is not None and v.count == 1 and v.dia_mm == pytest.approx(0.45)


def test_route_via_three_amps_is_an_array_of_small_vias() -> None:
    """The footprint formula's worked example: 3 A needs two barrels at
    either rung (fab 1.5 A, house 2.5 A per via), and the 0.45 pair's
    keep-out (1.1 + 2c)(0.45 + 2c) = 1.26 mm^2 undercuts the 0.75
    pair's (1.7 + 2c)(0.75 + 2c) = 2.16 mm^2 at c = 0.15 — the rail is
    carried by an array of small vias, not one large."""
    v = route_via(3.0, _FAB_VIA, _HOUSE)
    assert v is not None
    assert v.dia_mm == pytest.approx(0.45)
    assert v.drill_mm == pytest.approx(0.15)
    assert v.count == 2


def test_route_via_one_big_via_wins_when_the_ring_dominates() -> None:
    """The other end of the tradeoff: on a net whose clearance is the
    room (an HV 1.0 mm ring), a 2 A draw's two fab vias footprint
    (1.1 + 2)(0.45 + 2) = 7.595 mm^2 against one house barrel's
    (0.75 + 2)^2 = 7.5625 mm^2 — one big via."""
    hv = NetRules(
        track_width_mm=0.15,
        clearance_mm=1.0,
        via_dia_mm=0.75,
        via_drill_mm=0.25,
    )
    v = route_via(2.0, _FAB_VIA, hv)
    assert v == RouteVia(0.75, 0.25, 1)


def test_route_via_equal_footprints_go_to_fewer_vias() -> None:
    """The tie rule: at c = 0.2 a fab 0.2 needing 3 and a house 0.3
    needing 2 both footprint 0.84 mm^2 (1.4 x 0.6 against 1.2 x 0.7);
    fewer vias wins."""
    house = NetRules(
        track_width_mm=0.15,
        clearance_mm=0.2,
        via_dia_mm=0.3,
        via_drill_mm=0.2,
    )
    assert route_via(1.5, (0.2, 0.15), house) == RouteVia(0.3, 0.2, 2)


def test_route_via_never_below_the_fab_floor() -> None:
    """A house via under the fab floor is not a rung — the floor is,
    however small the net rule asks."""
    under = NetRules(
        track_width_mm=0.15,
        clearance_mm=0.15,
        via_dia_mm=0.3,
        via_drill_mm=0.15,
    )
    v0 = route_via(None, _FAB_VIA, under)
    assert v0 is not None and v0.dia_mm == pytest.approx(0.45)
    v = route_via(3.0, _FAB_VIA, under)
    assert v is not None and v.dia_mm == pytest.approx(0.45) and v.count == 2


def test_route_via_no_fab_floor_uses_the_house_via() -> None:
    assert route_via(None, (None, None), _HOUSE) == RouteVia(0.75, 0.25, 1)
    v = route_via(5.0, (None, None), _HOUSE)
    assert v is not None and v.dia_mm == pytest.approx(0.75) and v.count == 2


def test_route_via_inherited_floor_drill_keeps_the_annular_ring() -> None:
    """A house via under the fab floor collapses into the floor rung;
    when the fab carries no drill figure the rung inherits the house
    drill — clamped to ``floor_dia - 2 x ring_min`` (the same coupling
    resolve_net_rules holds) so the emitted barrel's annular ring never
    sits under the fab floor."""
    under = NetRules(
        track_width_mm=0.15,
        clearance_mm=0.15,
        via_dia_mm=0.4,
        via_drill_mm=0.3,
    )
    v = route_via(None, (0.45, None), under, ring_min_mm=0.13)
    assert v is not None
    assert v.dia_mm == pytest.approx(0.45)
    assert v.drill_mm == pytest.approx(0.45 - 2 * 0.13)
    # no ring figure passed: the rung inherits the house drill as-is
    v = route_via(None, (0.45, None), under)
    assert v is not None and v.drill_mm == pytest.approx(0.3)


def test_route_via_no_via_figures_at_all_returns_none() -> None:
    bare = NetRules(track_width_mm=0.15, clearance_mm=0.15)
    assert route_via(None, (None, None), bare) is None
    assert route_via(9.0, (None, None), bare) is None


# ── net_current_a_or_none ────────────────────────────────────────────────


def test_net_current_a_or_none_normalizes_nan_and_none():
    assert net_current_a_or_none(None) is None
    assert net_current_a_or_none(math.nan) is None
    assert net_current_a_or_none(5.0) == 5.0


# ── per-(class pair, layer) clearance (pcb-vector-router slice 1) ────────────


def _resolve(overrides: dict, net_class: str = "c") -> NetRules:
    return resolve_net_rules(
        net_class, layer_is_outer=True, fab_caps=_CAP4, overrides=overrides
    )


def test_no_new_keys_resolve_exactly_as_before() -> None:
    hv = _resolve({"working_voltage_v": 250, "clearance_mm": 0.099})
    assert hv.by_layer == () and hv.pair_rules == ()
    assert hv.clearance_mm == pytest.approx(0.4)
    assert hv.min_clearance_mm == pytest.approx(0.4)
    assert hv.tiers_on("In2.Cu") == LayerTiers(hv.clearance_mm, hv.min_clearance_mm)


def test_layer_entry_replaces_the_voltage_floor_on_that_layer_only() -> None:
    """P1: a named tier replaces the class value and the working_voltage_v
    floor on its layer; other layers keep the class's B4 floor."""
    rules = _resolve(
        {
            "working_voltage_v": 250,
            "clearance_mm": 0.099,
            "clearance_by_layer": {
                "F.Cu": {"min_clearance_mm": 0.152},
                "B.Cu": {"min_clearance_mm": 0.4},
            },
        }
    )
    front, back = rules.tiers_on("F.Cu"), rules.tiers_on("B.Cu")
    assert front.min_clearance_mm == pytest.approx(0.152)
    assert front.clearance_mm == pytest.approx(0.152)
    assert back.min_clearance_mm == pytest.approx(0.4)
    assert rules.tiers_on("In1.Cu").min_clearance_mm == pytest.approx(0.4)


def test_layer_entry_is_clamped_to_the_fab_minimum() -> None:
    jlc_min = _CAP4.jlc_min["trace_spacing_mm"]
    rules = _resolve({"clearance_by_layer": {"F.Cu": {"min_clearance_mm": 0.0}}})
    assert rules.tiers_on("F.Cu").min_clearance_mm == pytest.approx(jlc_min)


def _ctx(**net_rules: NetRules) -> RuleContext:
    return RuleContext(
        fab_caps=_CAP4,
        net_rules=net_rules,
        outer_layers=frozenset({"F.Cu", "B.Cu"}),
    )


def test_required_takes_the_stricter_net_per_layer() -> None:
    hv = _resolve(
        {"clearance_by_layer": {"B.Cu": {"min_clearance_mm": 0.4}}}, net_class="hv"
    )
    plain = _resolve({}, net_class="plain")
    ctx = _ctx(A=hv, B=plain)
    back = required(Owner("A"), Owner("B"), "B.Cu", ctx)
    front = required(Owner("A"), Owner("B"), "F.Cu", ctx)
    assert back.error_mm == pytest.approx(0.4)
    assert back.warn_mm == pytest.approx(0.4)
    assert front.error_mm == pytest.approx(_CAP4.jlc_min["trace_spacing_mm"])
    assert front.metric == "copper"
    assert required(Owner("A"), Owner("A"), "F.Cu", ctx).metric == "none"


def test_pair_rule_wins_over_the_per_net_floor_and_is_symmetric() -> None:
    """P2: the pair rule replaces the per-net floor for the pair on its
    layers, from either side; elsewhere the class floor holds."""
    escape = _resolve(
        {
            "working_voltage_v": 250,
            "pair_rules": [
                {"with": "pixel", "layers": ["F.Cu"], "min_clearance_mm": 0.152}
            ],
        },
        net_class="escape",
    )
    pixel = _resolve({"clearance_mm": 0.099}, net_class="pixel")
    other = _resolve({}, net_class="other")
    ctx = _ctx(E=escape, P=pixel, O=other)
    for a, b in (("E", "P"), ("P", "E")):
        req = required(Owner(a), Owner(b), "F.Cu", ctx)
        assert req.error_mm == pytest.approx(0.152)
        # The rule names no warn tier: the per-net 0.4 stays the warn tier.
        assert req.warn_mm == pytest.approx(0.4)
    assert required(Owner("E"), Owner("P"), "B.Cu", ctx).error_mm == pytest.approx(0.4)
    assert required(Owner("E"), Owner("O"), "F.Cu", ctx).error_mm == pytest.approx(0.4)


def test_pair_rules_from_both_classes_take_the_larger() -> None:
    a = _resolve(
        {"pair_rules": [{"with": "b", "min_clearance_mm": 0.2}]}, net_class="a"
    )
    b = _resolve(
        {"pair_rules": [{"with": "a", "min_clearance_mm": 0.3, "clearance_mm": 0.35}]},
        net_class="b",
    )
    req = required(Owner("A"), Owner("B"), "In1.Cu", _ctx(A=a, B=b))
    assert req.error_mm == pytest.approx(0.3)
    assert req.warn_mm == pytest.approx(0.35)


def test_required_folds_the_pairwise_voltage_into_the_warn_tier() -> None:
    plain = _resolve({})
    ctx = RuleContext(
        fab_caps=_CAP4,
        net_rules={"A": plain, "B": plain},
        outer_layers=frozenset({"F.Cu", "B.Cu"}),
        net_voltages={"A": 250.0, "B": 0.0},
    )
    req = required(Owner("A"), Owner("B"), "F.Cu", ctx)
    assert req.warn_mm == pytest.approx(1.25)  # B2, external
    inner = required(Owner("A"), Owner("B"), "In1.Cu", ctx)
    assert inner.warn_mm == pytest.approx(0.2)  # B1
    assert req.error_mm == pytest.approx(_CAP4.jlc_min["trace_spacing_mm"])


def test_clearance_rule_problems_names_layers_classes_and_keys() -> None:
    layers = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    good = {
        "clearance_by_layer": {"B.Cu": {"min_clearance_mm": 0.4}},
        "pair_rules": [{"with": "px", "layers": ["F.Cu"], "min_clearance_mm": 0.152}],
    }
    assert (
        clearance_rule_problems("hv", good, layers=layers, classes={"hv", "px"}) == []
    )
    bad = {
        "clearance_by_layer": {
            "In3.Cu": {"min_clearance_mm": 0.4},
            "F.Cu": {"gap": 1, "clearance_mm": -1},
        },
        "pair_rules": [
            {"with": "nope", "layers": ["X.Cu"], "min_clearance_mm": 0.1},
            {"with": "px"},
        ],
    }
    text = "\n".join(
        clearance_rule_problems("hv", bad, layers=layers, classes={"hv", "px"})
    )
    assert "clearance_by_layer['In3.Cu']: no such copper layer" in text
    assert "unknown key 'gap'" in text
    assert "clearance_mm must be a number of mm >= 0" in text
    assert "no such net class 'nope'" in text
    assert "no such copper layer ['X.Cu']" in text
    assert "names neither clearance_mm nor min_clearance_mm" in text
