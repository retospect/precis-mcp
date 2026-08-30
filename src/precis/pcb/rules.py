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
from dataclasses import dataclass
from typing import Any

from precis.pcb.capabilities import CapabilityRow
from precis.pcb.ir import UNSET_LAYER, PcbIR

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

    clearance_mm = overrides.get("clearance_mm")
    if clearance_mm is None:
        clearance_mm = _fab_floor(fab_caps, "trace_spacing_mm", _FALLBACK_CLEARANCE_MM)
    clearance_min = _fab_min(fab_caps, "trace_spacing_mm")
    if clearance_min is not None:
        clearance_mm = max(float(clearance_mm), clearance_min)

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
        clearance_mm=float(clearance_mm),
        via_dia_mm=None if via_dia_mm is None else float(via_dia_mm),
        via_drill_mm=None if via_drill_mm is None else float(via_drill_mm),
    )


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


#: Every instance's pad(s) are assumed to sit on this stackup layer — the
#: IR carries no per-instance mount-side field yet, so index 0 (the first
#: outer layer, F.Cu in :data:`precis.pcb.DEFAULT_STACKUP`) is the one
#: fixed reference every via transition (both :mod:`precis.pcb.realize`'s
#: geometry and :func:`implied_via_count`'s count below) is computed
#: against, not a per-instance lookup that doesn't exist yet. Lives here
#: (not :mod:`precis.pcb.realize`, its original home) so :mod:`precis.pcb.
#: cost` can share the exact same "did this segment change layer" test
#: without importing the realizer (see :func:`implied_via_count`'s
#: docstring for why that import would cycle); :mod:`precis.pcb.realize`
#: re-exports this same object rather than keeping a second definition.
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
    current_a = net_current_a_or_none(float(ir.net_current_a[net_id]))
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
    "PAD_LAYER",
    "VIA_REFERENCE_CAPACITY_A",
    "VIA_REFERENCE_DIA_MM",
    "NetRules",
    "implied_via_count",
    "ipc2221_capacity_a",
    "ipc2221_track_width_mm",
    "layer_is_outer",
    "net_current_a_or_none",
    "resolve_net_rules",
    "via_capacity_a",
    "via_count_for_current",
]
