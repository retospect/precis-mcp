"""The single per-net rules resolver — closes gr-shaped defects A and B in
docs/backlog/pcb-usb-c-pd-nano-testboard.md's §Blockers in ONE place rather
than two: :mod:`precis.pcb.realize` emitted a flat ``track_width_mm=0.25``
default regardless of current, and :mod:`precis.pcb.drc` read only the fab
capability table, never a ``pcb_net_classes.rules`` override. Both gaps are
the same defect — per-net electrical intent (current, an authored class
rule) never reaching the geometry — so this module is the one place that
turns that intent into a track width / clearance, and every consumer
(:mod:`precis.pcb.realize`, :mod:`precis.pcb.drc`, :mod:`precis.pcb.cost`)
reads through it instead of re-deriving its own answer.

**Resolution order, most specific first:**

1. an explicit ``pcb_net_classes.rules`` override for the net's class;
2. else, when the net carries a current annotation (``pcb_nets.est_
   current_a``), derive the width from IPC-2221 (see
   :func:`ipc2221_track_width_mm`) — clearance has no current-derived form,
   so a net with no override simply falls through to (3) for clearance;
3. else the fab capability floor (:mod:`precis.pcb.capabilities`) —
   existing behaviour, never emit below what the fab can manufacture.

**The result is ALWAYS clamped to the fab capability minimum**, regardless
of which tier produced it — an authored class rule or a current-derived
width may ask for MORE copper/clearance than the fab needs, never less
than the fab can make (:func:`resolve_net_rules`'s own clamp, applied
unconditionally as the last step).

**IPC-2221, and why external/internal is a real split, not a knob.**
``A_mils^2 = (I / (k * dT^0.44))^(1/0.725)``, with ``k=0.048`` external /
``k=0.024`` internal — the two k's differ by exactly 2x, and since area
enters the final width linearly while area itself is a *power* of the
current-to-k ratio, an inner layer needs ``2^(1/0.725) ~= 2.6x`` the
EXTERNAL width for the same current and temperature rise (external copper
sheds heat to open air on both sides; internal copper is sandwiched in
dielectric, a much worse conductor of heat, so more cross-section is the
only way to hold the same temperature rise). ``layer_is_outer`` is a
resolver INPUT, never inferred here, because layer identity is IR state
this module doesn't own (module docstring precedent set by ``ir.py``'s own
"layers are integer indexes" discipline).

**Via sizing is a field on :class:`NetRules`** — ``via_dia_mm``/
``via_drill_mm`` are populated from the fab capability floor only (no
override tier yet; a ``pcb_net_classes.rules`` via-size override is future
work, not needed for the realizer to stop being blind to vias entirely).
:mod:`precis.pcb.realize` is the first production caller (2026-08-28,
closing the master backlog's "Known-inert" via-geometry gap) — it extends
this SAME resolution order rather than inventing a second resolver.

**Via AMPACITY is a separate question from via SIZE**, answered by
:func:`via_capacity_a`/:func:`via_count_for_current` below: a via's
plated-drill diameter says how big a hole gets drilled, not how much
current its barrel can carry, and a single via cannot carry a real power
rail (backlog, verbatim: "a 0.3 mm via carries ~1-2 A, so a 10 A rail needs
an array"). This is a deliberately conservative HEURISTIC, not an IPC-2221
derivation — the plating thickness a real capacity formula needs isn't a
store column anywhere in this build, unlike trace width's copper-weight
figure — scaled linearly off the backlog's own reference point, at the LOW
end of its quoted 1-2 A range (never the high end: a via array that can't
carry its rail is the exact silent-failure class this module exists to
close, not one to risk re-introducing via an optimistic constant).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from precis.pcb.capabilities import CapabilityRow, conductor_spacing_mm
from precis.pcb.ir import NO_NET, UNSET_LAYER, PcbIR

#: IPC-2221 external/internal constants (module docstring, verbatim).
IPC2221_K_EXTERNAL = 0.048
IPC2221_K_INTERNAL = 0.024
#: mils^2 per (oz copper-weight * mil width) -- i.e. cross-sectional area
#: per unit width for 1oz copper is 1.378 mil of thickness.
_COPPER_MIL_PER_OZ = 1.378
_MM_PER_MIL = 0.0254

#: Sane fallbacks when neither an override, a current annotation, NOR a fab
#: capability figure is available (a capability field is legitimately
#: `None` for some process/field combinations, see capabilities.py) --
#: mirrors realize.py's pre-existing generic-class defaults so a design
#: with a truly unknown process never emits copper narrower than today's
#: behaviour did.
_FALLBACK_TRACK_WIDTH_MM = 0.25
_FALLBACK_CLEARANCE_MM = 0.15


@dataclass(frozen=True, slots=True)
class NetRules:
    """One net's resolved geometry rules -- the shape every consumer reads
    instead of inventing its own default. ``via_dia_mm``/``via_drill_mm``
    are reserved for the via-geometry task (module docstring); they are
    always fab-floor values today, never an override tier."""

    track_width_mm: float
    clearance_mm: float
    via_dia_mm: float | None = None
    via_drill_mm: float | None = None
    #: The net's HARD clearance floor (ewod-controller-and-hv-supply.md
    #: Slice 4b/c) — ``None`` means "the fab minimum is the whole answer"
    #: (today's behaviour). When set, :func:`precis.pcb.drc.check_clearance`
    #: reports a pair under it as an ERROR, not a WARN — a creepage/
    #: working-voltage requirement is a hard floor, not a preferred
    #: margin. Fed by a class's own ``min_clearance_mm`` key or derived
    #: from its ``working_voltage_v`` through IPC-2221B Table 6-1.
    min_clearance_mm: float | None = None
    #: The class this was resolved for — what a :class:`PairRule`'s
    #: ``with_class`` matches against.
    net_class: str = ""
    #: ``(layer name, tiers)`` for each layer the class's
    #: ``clearance_by_layer`` names; every other layer reads
    #: ``clearance_mm``/``min_clearance_mm`` (:meth:`tiers_on`).
    by_layer: tuple[tuple[str, LayerTiers], ...] = ()
    #: The class's ``pair_rules`` (:func:`required` applies them).
    pair_rules: tuple[PairRule, ...] = ()
    #: ``(layer name or None, IPC-2221B column, tiers)`` — set only for a
    #: class with ``working_voltage_v``, whose floor depends on the layer
    #: (Reto 2026-10-18, "Yes, IPC column per layer"): ``"B1"`` on inner
    #: layers, ``"B4"`` on outer layers of a coated board, ``"B2"`` on
    #: outer layers of an uncoated one. ``None`` is the class-wide row; a
    #: named layer is that ``clearance_by_layer`` entry.
    columns: tuple[tuple[str | None, str, LayerTiers], ...] = ()

    def tiers_on(
        self, layer: str, *, outer: bool = True, coated: bool = True
    ) -> LayerTiers:
        """This net's own clearance tiers on ``layer`` — an ``outer``
        copper layer or an inner one, on a ``coated`` board or not."""
        if self.columns:
            column = "B1" if not outer else ("B4" if coated else "B2")
            fallback: LayerTiers | None = None
            for name, col, tiers in self.columns:
                if col != column:
                    continue
                if name == layer:
                    return tiers
                if name is None:
                    fallback = tiers
            if fallback is not None:
                return fallback
        for name, tiers in self.by_layer:
            if name == layer:
                return tiers
        return LayerTiers(self.clearance_mm, self.min_clearance_mm)

    def max_clearance_mm(self) -> float:
        """The largest clearance any layer or pair rule of this net can
        ask for — the radius a candidate-pair prune must reach."""
        values = [self.clearance_mm]
        values += [t.clearance_mm for _, t in self.by_layer]
        values += [t.clearance_mm for _, _, t in self.columns]
        for rule in self.pair_rules:
            values += [
                v for v in (rule.clearance_mm, rule.min_clearance_mm) if v is not None
            ]
        return max(values)


@dataclass(frozen=True, slots=True)
class LayerTiers:
    """One net's two clearance tiers on one layer: ``clearance_mm`` the
    preferred (warn) tier, ``min_clearance_mm`` the hard (error) floor, or
    ``None`` when the fab minimum is the whole floor."""

    clearance_mm: float
    min_clearance_mm: float | None


@dataclass(frozen=True, slots=True)
class PairRule:
    """One ``pair_rules`` entry of a class: the floor between this class
    and ``with_class`` on ``layers`` (``None`` = every copper layer). A
    tier left ``None`` keeps the per-net value (:func:`required`)."""

    with_class: str
    layers: frozenset[str] | None
    clearance_mm: float | None
    min_clearance_mm: float | None

    def applies(self, other_class: str, layer: str) -> bool:
        return other_class == self.with_class and (
            self.layers is None or layer in self.layers
        )


def ipc2221_track_width_mm(
    current_a: float,
    *,
    layer_is_outer: bool,
    temp_rise_c: float = 10.0,
    copper_oz: float = 1.0,
) -> float:
    """The IPC-2221 external/internal trace-width formula (module
    docstring, verbatim) -- the width that holds ``current_a`` to
    ``temp_rise_c`` of temperature rise on ``copper_oz``-weight copper.
    Raises for a non-positive current (nothing to size against) rather
    than returning a nonsense value."""
    if current_a <= 0.0:
        raise ValueError(
            f"ipc2221_track_width_mm: current_a must be > 0, got {current_a!r}"
        )
    k = IPC2221_K_EXTERNAL if layer_is_outer else IPC2221_K_INTERNAL
    area_mil2 = (current_a / (k * temp_rise_c**0.44)) ** (1.0 / 0.725)
    width_mil = area_mil2 / (copper_oz * _COPPER_MIL_PER_OZ)
    return width_mil * _MM_PER_MIL


def ipc2221_capacity_a(
    width_mm: float,
    *,
    layer_is_outer: bool,
    temp_rise_c: float = 10.0,
    copper_oz: float = 1.0,
) -> float:
    """The exact algebraic inverse of :func:`ipc2221_track_width_mm`: the
    current a copper trace of ``width_mm`` can carry at ``temp_rise_c`` of
    rise -- what :mod:`precis.pcb.cost`'s ``thermal_rise`` term scores the
    net's ACTUAL current draw against, so the term reasons about the same
    width the geometry will actually carry (the exact bug this module
    closes, restated: an optimizer scoring against a width the realizer
    never emits)."""
    if width_mm <= 0.0:
        return 0.0
    k = IPC2221_K_EXTERNAL if layer_is_outer else IPC2221_K_INTERNAL
    width_mil = width_mm / _MM_PER_MIL
    area_mil2 = width_mil * copper_oz * _COPPER_MIL_PER_OZ
    return k * temp_rise_c**0.44 * area_mil2**0.725


#: The backlog's own reference point (module docstring, verbatim: "a
#: 0.3 mm via carries ~1-2 A") — :data:`VIA_REFERENCE_CAPACITY_A` is
#: deliberately the LOW end of that range, never the midpoint or high end.
VIA_REFERENCE_DIA_MM = 0.3
VIA_REFERENCE_CAPACITY_A = 1.0


def via_capacity_a(via_dia_mm: float) -> float:
    """A single plated via's CONSERVATIVE current-carrying capacity (module
    docstring's ampacity-vs-size distinction) — linear in diameter from the
    backlog's own reference point (a via barrel's copper cross-section, at
    fixed plating thickness, is proportional to its circumference, i.e. its
    diameter). Not a real IPC-2221-grade derivation; a documented,
    intentionally-conservative heuristic so an under-vias power rail is
    caught rather than silently accepted. ``via_dia_mm <= 0`` returns
    ``0.0`` (nothing to size against) rather than a negative/undefined
    capacity."""
    if via_dia_mm <= 0.0:
        return 0.0
    return via_dia_mm / VIA_REFERENCE_DIA_MM * VIA_REFERENCE_CAPACITY_A


def via_count_for_current(current_a: float | None, via_dia_mm: float) -> int:
    """How many vias, stitched in parallel, ``current_a`` needs against a
    single via of ``via_dia_mm``'s conservative capacity
    (:func:`via_capacity_a`) — always >= 1, never 0 (a net that needs a via
    at all needs at least one). ``current_a=None`` (or non-positive — no
    usable current annotation) returns 1, the same "keep today's minimal
    behaviour" default :func:`resolve_net_rules` uses for an absent current
    annotation elsewhere in this module — never a silently-invented
    current."""
    if current_a is None or current_a <= 0.0:
        return 1
    capacity = via_capacity_a(via_dia_mm)
    if capacity <= 0.0:
        return 1
    return max(1, math.ceil(current_a / capacity))


def _fab_floor(capability: CapabilityRow, field: str, fallback: float) -> float:
    value = capability.house_default.get(field)
    if value is None:
        value = capability.jlc_min.get(field)
    return value if value is not None else fallback


def _fab_min(capability: CapabilityRow, field: str) -> float | None:
    return capability.jlc_min.get(field)


def resolve_net_rules(
    net_class: str,
    *,
    layer_is_outer: bool,
    fab_caps: CapabilityRow,
    overrides: dict[str, Any] | None = None,
    current_a: float | None = None,
    temp_rise_c: float = 10.0,
    copper_oz: float = 1.0,
) -> NetRules:
    """Resolve ONE net's geometry rules -- the single function every
    consumer (realize.py/drc.py/cost.py) calls instead of re-deriving its
    own default (module docstring's resolution order).

    ``overrides`` is the net's class row from ``pcb_net_classes.rules``
    (``None`` when the design has no row for this class -- "missing row
    means built-in defaults", per that table's own comment). ``current_a``
    is the net's ``est_current_a`` annotation, or ``None``/NaN when the
    design carries no current estimate for it -- "keep today's behaviour"
    (fall through to the fab floor) is exactly what a ``None`` here does.
    """
    overrides = overrides or {}

    width_mm = overrides.get("track_width_mm")
    if width_mm is None and current_a is not None and current_a > 0:
        width_mm = ipc2221_track_width_mm(
            current_a,
            layer_is_outer=layer_is_outer,
            temp_rise_c=temp_rise_c,
            copper_oz=copper_oz,
        )
    if width_mm is None:
        width_mm = _fab_floor(fab_caps, "trace_width_mm", _FALLBACK_TRACK_WIDTH_MM)
    width_min = _fab_min(fab_caps, "trace_width_mm")
    if width_min is not None:
        width_mm = max(float(width_mm), width_min)

    tiers = _clearance_tiers(overrides, fab_caps, layer_is_outer=layer_is_outer)
    # Stored rules were refused at put time if malformed
    # (:func:`clearance_rule_problems`); an entry of the wrong shape that
    # reached the table anyway is skipped here rather than raising out of a
    # DRC run.
    raw_layers = overrides.get("clearance_by_layer")
    by_layer = tuple(
        (
            str(layer),
            _clearance_tiers(
                overrides, fab_caps, layer_is_outer=layer_is_outer, entry=entry
            ),
        )
        for layer, entry in by_layer_entries(raw_layers)
    )
    columns: tuple[tuple[str | None, str, LayerTiers], ...] = ()
    if overrides.get("working_voltage_v") is not None:
        entries: list[tuple[str | None, dict[str, Any] | None]] = [(None, None)]
        entries += [(name, entry) for name, entry in by_layer_entries(raw_layers)]
        columns = tuple(
            (
                name,
                column,
                _clearance_tiers(
                    overrides,
                    fab_caps,
                    layer_is_outer=column != "B1",
                    coated=column == "B4",
                    entry=entry,
                ),
            )
            for name, entry in entries
            for column in ("B1", "B2", "B4")
        )
    raw_pairs = overrides.get("pair_rules")
    pair_rules = tuple(
        PairRule(
            with_class=raw["with"],
            layers=(
                frozenset(str(n) for n in raw["layers"])
                if isinstance(raw.get("layers"), list | tuple)
                else None
            ),
            clearance_mm=_clamped(raw.get("clearance_mm"), fab_caps),
            min_clearance_mm=_clamped(raw.get("min_clearance_mm"), fab_caps),
        )
        for raw in (raw_pairs if isinstance(raw_pairs, list | tuple) else ())
        if isinstance(raw, dict) and isinstance(raw.get("with"), str)
    )

    via_dia_mm = overrides.get("via_dia_mm")
    if via_dia_mm is None:
        via_dia_mm = fab_caps.house_default.get(
            "via_diameter_mm"
        ) or fab_caps.jlc_min.get("via_diameter_mm")
    via_min = _fab_min(fab_caps, "via_diameter_mm")
    if via_dia_mm is not None and via_min is not None:
        via_dia_mm = max(float(via_dia_mm), via_min)

    via_drill_mm = overrides.get("via_drill_mm")
    if via_drill_mm is None:
        via_drill_mm = fab_caps.house_default.get("drill_mm") or fab_caps.jlc_min.get(
            "drill_mm"
        )
    drill_min = _fab_min(fab_caps, "drill_mm")
    if via_drill_mm is not None and drill_min is not None:
        via_drill_mm = max(float(via_drill_mm), drill_min)

    return NetRules(
        track_width_mm=float(width_mm),
        clearance_mm=tiers.clearance_mm,
        via_dia_mm=None if via_dia_mm is None else float(via_dia_mm),
        via_drill_mm=None if via_drill_mm is None else float(via_drill_mm),
        min_clearance_mm=tiers.min_clearance_mm,
        net_class=net_class,
        by_layer=by_layer,
        pair_rules=pair_rules,
        columns=columns,
    )


def by_layer_entries(raw: Any) -> list[tuple[str, dict[str, Any]]]:
    """A stored ``clearance_by_layer`` as sorted ``(layer, entry)`` pairs,
    skipping anything not shaped ``{layer: {...}}``."""
    if not isinstance(raw, dict):
        return []
    return sorted((str(k), v) for k, v in raw.items() if isinstance(v, dict))


def _clamped(value: Any, fab_caps: CapabilityRow) -> float | None:
    """An authored clearance clamped up to the fab minimum, or ``None``."""
    if value is None:
        return None
    floor = _fab_min(fab_caps, "trace_spacing_mm")
    return float(value) if floor is None else max(float(value), floor)


def _clearance_tiers(
    overrides: dict[str, Any],
    fab_caps: CapabilityRow,
    *,
    layer_is_outer: bool,
    coated: bool = True,
    entry: dict[str, Any] | None = None,
) -> LayerTiers:
    """A class's two clearance tiers — on every layer, or on the one
    layer ``entry`` (its ``clearance_by_layer[layer]``) describes. A tier
    the entry names replaces the class value AND the ``working_voltage_v``
    floor for that tier (vector-router slice 1, P1); the fab minimum
    clamps both tiers either way."""
    entry = entry or {}
    clearance_mm = entry.get("clearance_mm", overrides.get("clearance_mm"))

    # Slice 4c (ewod-controller-and-hv-supply.md): a class's
    # ``working_voltage_v`` — the SAME key ``drc.py::_net_voltage`` reads
    # for the ERC checks — derives a spacing floor through IPC-2221B Table
    # 6-1, so the number is derived, not typed. Internal layers read B1;
    # external layers read B4 when ``coated`` (the EWOD boards this exists
    # for are parylene-coated, ruling 3/10) and B2 when not.
    # :func:`resolve_net_rules`'s own fields keep B4 (the router's base
    # figure); per layer, :attr:`NetRules.columns` carries all three
    # (Reto 2026-10-18, "Yes, IPC column per layer"). Above the table's
    # 500 V top band this RAISES — a voltage claim the table cannot answer
    # is refused, never extrapolated.
    derived: float | None = None
    working_voltage_v = overrides.get("working_voltage_v")
    if working_voltage_v is not None:
        derived = conductor_spacing_mm(
            float(working_voltage_v),
            layer="internal" if not layer_is_outer else "external",
            coated=coated,
        )
        if clearance_mm is None and "clearance_mm" not in entry:
            clearance_mm = derived
    if clearance_mm is None:
        clearance_mm = _fab_floor(fab_caps, "trace_spacing_mm", _FALLBACK_CLEARANCE_MM)
    clearance_min = _fab_min(fab_caps, "trace_spacing_mm")
    if clearance_min is not None:
        clearance_mm = max(float(clearance_mm), clearance_min)

    # Slice 4b: ``min_clearance_mm`` is the class's HARD floor — the
    # error-tier threshold ``check_clearance`` applies where ``jlc_min``
    # alone used to set it. An authored explicit key and the derived
    # voltage floor both feed it; the resolved value never dips below the
    # fab floor (below it the fab can't build anything anyway).
    if "min_clearance_mm" in entry:
        explicit_floor = entry["min_clearance_mm"]
        derived = None
    else:
        explicit_floor = overrides.get("min_clearance_mm")
    min_clearance_mm: float | None = None
    if explicit_floor is not None or derived is not None:
        min_clearance_mm = max(
            float(explicit_floor) if explicit_floor is not None else 0.0,
            derived or 0.0,
        )
        if clearance_min is not None:
            min_clearance_mm = max(min_clearance_mm, clearance_min)
        # The derived floor also raises the preferred tier — the router
        # (which reads ``clearance_mm``) and the WARN band must both keep
        # the creepage distance, not merely error below it.
        clearance_mm = max(clearance_mm, min_clearance_mm)
    return LayerTiers(float(clearance_mm), min_clearance_mm)


#: JLCPCB "Via Hole-to-Hole Spacing" (gr489914, Reto ruling (a)
#: 2026-10-16): the minimum drilled-wall to drilled-wall gap between two
#: via holes. Source: https://jlcpcb.com/capabilities/pcb-capabilities ,
#: Drilling section, read 2026-10-16 10:00Z as "Via Hole-to-Hole Spacing
#: 0.2mm" (the same page lists "Pad Hole-to-Hole Spacing 0.45mm" for
#: plated pad holes). A drill figure, so it is not per copper process.
JLC_VIA_HOLE_TO_HOLE_MM = 0.2


@dataclass(frozen=True, slots=True)
class Requirement:
    """What one pair of objects owes each other on one layer:
    ``error_mm`` the hard floor, ``warn_mm`` the preferred tier (``None``
    when no tier above the floor exists), ``metric`` what is measured —
    ``"copper"`` edge to edge, ``"none"`` for copper that merges."""

    error_mm: float
    warn_mm: float | None
    metric: str


@dataclass(frozen=True, slots=True)
class Owner:
    """The rule-relevant identity of one object (vector-router step 1).
    Slice 1 resolves copper against copper by net; the clearance kernel
    (slice 2) adds the kind rules."""

    net: str | None
    kind: str = "copper"


@dataclass(frozen=True)
class RuleContext:
    """Board-wide inputs :func:`required` resolves against: the fab row,
    each net's resolved rules by NAME, the outer copper layer names (the
    IPC-2221B external/internal split; empty = unknown, read as outer, the
    larger figure), the annotated net voltages, and whether the board takes
    the conformal coat (:func:`precis.pcb.gerber.declares_parylene_coat`),
    which picks B4 over B2 on outer layers."""

    fab_caps: CapabilityRow
    net_rules: Mapping[str, NetRules] = field(default_factory=dict)
    outer_layers: frozenset[str] = frozenset()
    net_voltages: Mapping[str, float] | None = None
    coated: bool = True

    def is_outer(self, layer: str) -> bool:
        return not self.outer_layers or layer in self.outer_layers


def _pair_tiers(
    a: NetRules, b: NetRules, layer: str
) -> tuple[float | None, float | None, bool]:
    """``(clearance_mm, min_clearance_mm, matched)`` over every pair rule
    of either class that covers the other on ``layer``; the larger value
    per tier when several match."""
    clearance: float | None = None
    floor: float | None = None
    matched = False
    for mine, other in ((a, b), (b, a)):
        for rule in mine.pair_rules:
            if not rule.applies(other.net_class, layer):
                continue
            matched = True
            if rule.clearance_mm is not None:
                clearance = max(clearance or 0.0, rule.clearance_mm)
            if rule.min_clearance_mm is not None:
                floor = max(floor or 0.0, rule.min_clearance_mm)
    return clearance, floor, matched


def required(a: Owner, b: Owner, layer: str, ctx: RuleContext) -> Requirement:
    """The clearance ``a`` and ``b`` owe each other on ``layer`` — the one
    resolution routing and DRC share (``docs/backlog/pcb-vector-router.md``
    step 1, slice 1 decisions P1–P3, P6):

    1. same net: copper merges (``metric='none'``), except two vias,
       which owe :data:`JLC_VIA_HOLE_TO_HOLE_MM` of drill wall
       (``metric='hole_wall'``);
    2. per-net tiers on ``layer`` (:meth:`NetRules.tiers_on`): the larger
       of the two nets' preferred tier, the larger of their hard floors,
       never below the fab minimum;
    3. a class pair rule covering the pair on ``layer`` replaces each tier
       it names; the warn tier is then kept at or above the error tier;
    4. annotated voltages on both nets raise the warn tier to the
       IPC-2221B spacing for ``|V_a − V_b|`` (uncoated column, external on
       outer layers). A missing annotation is not 0 V.

    A net with no resolved rules reads the fab's house tier."""
    caps = ctx.fab_caps
    field_name = "trace_spacing_mm"
    jlc_min = caps.jlc_min.get(field_name) or 0.0
    house = caps.house_default.get(field_name)
    if a.net is not None and a.net != "" and a.net == b.net:
        if a.kind == "via" and b.kind == "via":
            # gr489914 ruling (a): two barrels of one net may touch; the fab
            # needs wall between the drills.
            return Requirement(JLC_VIA_HOLE_TO_HOLE_MM, None, "hole_wall")
        return Requirement(0.0, None, "none")
    ra = ctx.net_rules.get(str(a.net)) if ctx.net_rules else None
    rb = ctx.net_rules.get(str(b.net)) if ctx.net_rules else None
    outer = ctx.is_outer(layer)
    tiers = [
        r.tiers_on(layer, outer=outer, coated=ctx.coated)
        for r in (ra, rb)
        if r is not None
    ]
    warn: float | None = max(t.clearance_mm for t in tiers) if tiers else house
    error = max(
        [jlc_min]
        + [t.min_clearance_mm for t in tiers if t.min_clearance_mm is not None]
    )
    if ra is not None and rb is not None:
        pair_warn, pair_floor, matched = _pair_tiers(ra, rb, layer)
        if matched:
            if pair_floor is not None:
                error = max(jlc_min, pair_floor)
            if pair_warn is not None:
                warn = pair_warn
            if warn is not None:
                warn = max(warn, error)
    if ctx.net_voltages:
        v_a = ctx.net_voltages.get(str(a.net))
        v_b = ctx.net_voltages.get(str(b.net))
        if v_a is not None and v_b is not None:
            spacing = voltage_spacing_mm(v_a - v_b, outer=layer in ctx.outer_layers)
            if spacing is not None:
                warn = spacing if warn is None else max(warn, spacing)
    return Requirement(error, warn, "copper")


def voltage_spacing_mm(delta_v: float, *, outer: bool) -> float | None:
    """IPC-2221B Table 6-1 spacing for a potential difference (uncoated
    column), or ``None`` when the table cannot answer.

    :func:`precis.pcb.capabilities.conductor_spacing_mm` RAISES above its
    500 V top band rather than extrapolating (its own documented rule).
    That is right for a generator computing one electrode gap and wrong
    for a check: a single mis-typed annotation would take the whole
    ``view='drc'`` down with a ValueError instead of reporting a board. So
    the refusal becomes ``None`` and the caller reports it."""
    try:
        return conductor_spacing_mm(
            abs(delta_v), layer="external" if outer else "internal", coated=False
        )
    except ValueError:
        return None


#: The keys a ``clearance_by_layer`` entry and a ``pair_rules`` entry may
#: carry (slice 1, P7).
_LAYER_ENTRY_KEYS = frozenset({"clearance_mm", "min_clearance_mm"})
_PAIR_ENTRY_KEYS = frozenset({"with", "layers", "clearance_mm", "min_clearance_mm"})


def clearance_rule_problems(
    class_name: str,
    rules: dict[str, Any],
    *,
    layers: Iterable[str],
    classes: Iterable[str],
) -> list[str]:
    """Why ``rules``' ``clearance_by_layer``/``pair_rules`` cannot be
    honoured on a board with copper ``layers`` and net ``classes`` — one
    line per problem, empty when they can (slice 1, P7). Layers are any
    copper layer of the stackup, routable or not."""
    layer_set = {str(n) for n in layers}
    class_set = {str(n) for n in classes}
    problems: list[str] = []

    def _number(where: str, key: str, value: Any) -> None:
        if (
            not isinstance(value, int | float)
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value < 0
        ):
            problems.append(f"{where}.{key} must be a number of mm >= 0, got {value!r}")

    by_layer = rules.get("clearance_by_layer")
    if by_layer is not None:
        if not isinstance(by_layer, dict):
            problems.append(
                f"class {class_name!r}: clearance_by_layer must be "
                "{layer: {clearance_mm?, min_clearance_mm?}}"
            )
        else:
            for layer, entry in by_layer.items():
                where = f"class {class_name!r}: clearance_by_layer[{layer!r}]"
                if str(layer) not in layer_set:
                    problems.append(f"{where}: no such copper layer")
                if not isinstance(entry, dict) or not entry:
                    problems.append(f"{where} must be a non-empty dict")
                    continue
                for key, value in entry.items():
                    if key not in _LAYER_ENTRY_KEYS:
                        problems.append(f"{where}: unknown key {key!r}")
                    else:
                        _number(where, key, value)
    pairs = rules.get("pair_rules")
    if pairs is not None:
        if not isinstance(pairs, list | tuple):
            problems.append(f"class {class_name!r}: pair_rules must be a list")
            pairs = ()
        for i, entry in enumerate(pairs):
            where = f"class {class_name!r}: pair_rules[{i}]"
            if not isinstance(entry, dict):
                problems.append(f"{where} must be a dict")
                continue
            for key in entry:
                if key not in _PAIR_ENTRY_KEYS:
                    problems.append(f"{where}: unknown key {key!r}")
            other = entry.get("with")
            if not isinstance(other, str) or other not in class_set:
                problems.append(f"{where}.with: no such net class {other!r}")
            named = entry.get("layers")
            if named is not None:
                if not isinstance(named, list | tuple) or not named:
                    problems.append(f"{where}.layers must be a non-empty list")
                else:
                    unknown = [str(n) for n in named if str(n) not in layer_set]
                    if unknown:
                        problems.append(
                            f"{where}.layers: no such copper layer {unknown}"
                        )
            tiers = [k for k in ("clearance_mm", "min_clearance_mm") if k in entry]
            if not tiers:
                problems.append(
                    f"{where} names neither clearance_mm nor min_clearance_mm"
                )
            for key in tiers:
                _number(where, key, entry[key])
    return problems


def net_current_a_or_none(value: float | None) -> float | None:
    """Normalize a possibly-NaN/None current annotation to ``None`` -- the
    one place "no annotation" gets decided, so every caller (IR float64
    NaN sentinel, a plain ``None`` from a dict) agrees on what "absent"
    means before it reaches :func:`resolve_net_rules`."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return float(value)


def seg_current_a_or_none(ir: PcbIR, seg_id: int) -> float | None:
    """The current segment ``seg_id`` carries — its own resolved figure
    (:attr:`precis.pcb.ir.PcbIR.seg_current_a`, gr497904: a trunk carries
    the sum of its downstream loads, a branch its own pin's), falling
    back to the net's ``est_current_a`` annotation when the IR has no
    per-pin model for it. Same ``None`` = "no annotation" contract as
    :func:`net_current_a_or_none`, whose normalizer this reuses."""
    seg = net_current_a_or_none(float(ir.seg_current_a[seg_id]))
    if seg is not None:
        return seg
    return net_current_a_or_none(float(ir.net_current_a[int(ir.seg_net[seg_id])]))


def pin_current_a_or_none(ir: PcbIR, pin_id: int) -> float | None:
    """The current pin ``pin_id`` exchanges with its net — the resolved
    per-pin figure (:attr:`precis.pcb.ir.PcbIR.pin_current_a`), falling
    back to the net's ``est_current_a``. What a pad's drop vias or stub
    must carry (gr497904); ``None`` = "no annotation", same contract as
    :func:`net_current_a_or_none`."""
    pin = net_current_a_or_none(float(ir.pin_current_a[pin_id]))
    if pin is not None:
        return pin
    net_id = int(ir.pin_net[pin_id])
    if net_id == NO_NET:
        return None
    return net_current_a_or_none(float(ir.net_current_a[net_id]))


#: The ROUTER's own fixed reference layer for via-transition bookkeeping —
#: NOT "which copper layer a pad's real land sits on" (gr341516 split that
#: question off, once the IR gained a real per-instance mount-side field:
#: see :attr:`precis.pcb.ir.PcbIR.inst_bottom`, populated by
#: :func:`~precis.pcb.ir.from_graph` from ``pcb_instances.layer``, and
#: :func:`precis.pcb.realize.pads_for_ir`, which now emits each pad's own
#: ``layer`` key off that field instead of this constant). This constant
#: remains index 0 (the first outer layer, F.Cu in
#: :data:`precis.pcb.DEFAULT_STACKUP`) as the one fixed frame every via
#: transition the ROUTER draws (both :mod:`precis.pcb.realize`'s vector
#: plane-fanout/drop-via geometry and :func:`implied_via_count`'s count
#: below) is computed against — the router still routes every net as if
#: every pad started from the SAME outer layer regardless of its
#: instance's real mount side (a real, pre-existing, separate gap: a
#: bottom-side part's stub/via geometry is anchored to the wrong physical
#: layer today), which is a routing-engine change, not a DRC/IR-shape one,
#: and out of this constant's own scope to fix by renaming it away. Lives
#: here (not :mod:`precis.pcb.realize`, its original home) so
#: :mod:`precis.pcb.cost` can share the exact same "did this segment
#: change layer" test without importing the realizer (see
#: :func:`implied_via_count`'s docstring for why that import would
#: cycle); :mod:`precis.pcb.realize` re-exports this same object rather
#: than keeping a second definition.
PAD_LAYER = 0


def layer_is_outer(ir: PcbIR, layer: int) -> bool:
    """Whether ``layer`` is an OUTER stackup layer (index 0 or the last
    index — F.Cu/B.Cu in :data:`precis.pcb.DEFAULT_STACKUP`; "layers are
    integer indexes" per ``ir.py``'s own discipline, never a name compare).
    ``UNSET_LAYER`` (no L1 assignment yet) or an empty stackup default to
    ``True``: the common case, and IPC-2221's more generous (lower-
    required-width-for-the-same-current) assumption. Shared by
    :func:`resolve_net_rules`'s callers in both :mod:`precis.pcb.realize`
    (this module's original home for this predicate) and
    :func:`implied_via_count` below, so a track's realized width tier and
    its implied via sizing always agree on which side of the stackup a
    layer sits."""
    n = len(ir.stackup)
    if n == 0 or layer == UNSET_LAYER:
        return True
    return layer <= 0 or layer >= n - 1


def implied_via_count(
    ir: PcbIR,
    seg_id: int,
    *,
    fab_caps: CapabilityRow,
    class_rules: dict[str, dict[str, Any]] | None = None,
    temp_rise_c: float = 10.0,
    copper_oz: float = 1.0,
) -> int:
    """The number of vias segment ``seg_id``'s realized track will actually
    need — the ONE rule :mod:`precis.pcb.realize`'s ``_vias_for_track``
    applies to real geometry, hoisted here so :mod:`precis.pcb.cost``'s
    ``via_count`` MONEY term and the realizer's actual via output can never
    drift apart again. This closes a real, live defect (found 2026-08-28):
    ``via_count`` was reading ``PcbIR.n_vias``, a field ``PcbIR.add_via``
    alone grows -- and ``add_via`` had ZERO production callers anywhere in
    this package. The optimizer paid nothing for a layer change while
    ``realize.py`` independently emitted real vias whenever a track's
    layer differed from :data:`PAD_LAYER`, so every SA move that swapped a
    detour for a via looked free. This function is the one place that
    predicate now lives; both consumers call it, neither re-derives it.

    **Reads only ``seg_id``'s own net/layer fields** — never another
    segment's or instance's state — so recomputing this for ONE touched
    segment after a ``LAYER_ASSIGN``/``PLANE_PROMOTE``/``PLANE_DEMOTE``
    move is an O(1) bounded delta, never a board rescan (the SA locality
    contract :mod:`precis.pcb.optimize`'s other cost terms already obey —
    see that module's ``_refresh_via_count_for_segment``). Deliberately
    NEVER materializes a :class:`PcbIR` via row (``add_via`` uses
    ``np.append``, O(n) per call, and SA rejects most proposed moves — a
    materialized via would survive a rejected move's undo and leave state
    unrestorable): this is a pure, derived count, recomputed from
    ``seg_layer``/``net_plane_layers`` on every call, never stored.

    Zero when: the net is plane-promoted on ANY layer (``net_plane_layers
    != 0`` — a net may be poured on several layers now, but even one is
    enough: the dog-bone stub fans to its own via elsewhere, matching
    ``realize.py``'s ``RealizedTrack.is_dogbone`` exactly, restated here
    directly off the IR since no ``RealizedTrack`` exists at cost-eval
    time); the segment has no layer assigned yet or already sits on
    :data:`PAD_LAYER` (nothing to transition); or the fab publishes no via
    figures (``via_dia_mm``/``via_drill_mm`` both ``None`` -- genuinely
    nothing to size against). Otherwise: ``2 *
    via_count_for_current(...)`` -- one stitched group at EACH of the
    segment's two pads (start and end), exactly the loop ``_vias_for_track``
    runs, sized off the SAME :func:`resolve_net_rules` call every other
    consumer of net geometry uses -- never a second sizing path.
    """
    net_id = int(ir.seg_net[seg_id])
    if int(ir.net_plane_layers[net_id]) != 0:
        return 0
    layer = int(ir.seg_layer[seg_id])
    if layer in (UNSET_LAYER, PAD_LAYER):
        return 0
    net_class = str(ir.net_class[net_id])
    overrides = (class_rules or {}).get(net_class)
    current_a = seg_current_a_or_none(ir, seg_id)
    resolved = resolve_net_rules(
        net_class,
        layer_is_outer=layer_is_outer(ir, layer),
        fab_caps=fab_caps,
        overrides=overrides,
        current_a=current_a,
        temp_rise_c=temp_rise_c,
        copper_oz=copper_oz,
    )
    if resolved.via_dia_mm is None or resolved.via_drill_mm is None:
        return 0
    return 2 * via_count_for_current(current_a, resolved.via_dia_mm)


__all__ = [
    "IPC2221_K_EXTERNAL",
    "IPC2221_K_INTERNAL",
    "JLC_VIA_HOLE_TO_HOLE_MM",
    "PAD_LAYER",
    "VIA_REFERENCE_CAPACITY_A",
    "VIA_REFERENCE_DIA_MM",
    "LayerTiers",
    "NetRules",
    "Owner",
    "PairRule",
    "Requirement",
    "RuleContext",
    "clearance_rule_problems",
    "implied_via_count",
    "ipc2221_capacity_a",
    "ipc2221_track_width_mm",
    "layer_is_outer",
    "net_current_a_or_none",
    "pin_current_a_or_none",
    "required",
    "resolve_net_rules",
    "seg_current_a_or_none",
    "via_capacity_a",
    "via_count_for_current",
    "voltage_spacing_mm",
]
