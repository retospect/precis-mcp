"""Per-connection OBJECTIVE VECTORS — the mechanism that replaces every
bespoke "decap near its IC" / "affinity edge" special case. See
docs/backlog/pcb-guided-place-route.md §"Connections carry OBJECTIVE
VECTORS".

**Why this beats special-casing.** "Decap near its IC" is not a rule here;
it's a *consequence* of the decap's two terminals each carrying a `low
impedance` objective (one to PWR, one to GND). No group table, no
pattern-matcher keyed on component label. The same six-term vector covers
the switcher hot loop, the crystal, sense resistors, and terminations —
they differ only in *which* objectives their connections carry, never in
mechanism.

**It absorbs the trace-width policy too.** `fixed`/`minimum`/`free` isn't
a separate enum: low capacitance implies narrow, low resistance implies
wide, and the tiling pass (a later module) reads the objective vector
directly. Do not reintroduce a width enum here — that would be the same
special-casing this file exists to remove, one layer down.

**Impedance is a LOOP property, so it is loop-scoped.** "Low impedance to
PWR" is trivially satisfiable by a via anywhere (a plane is low-impedance
everywhere); the quantity that actually matters is the *loop* `pin ->
plane -> partner -> plane -> pin`, whose spreading inductance grows with
separation. Nets are already domain/class-typed, so the loop's *return*
net is implied rather than stated per-instance: a PWR connection's return
is its paired GND. :func:`objectives_for_connection` resolves this from
``net_class``/``net_domain`` plus an explicit ``return_net`` when the
caller has one (e.g. a design's own PWR/GND class pairing table).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# Reference magnitudes for the coupling normalization below — order-of-
# magnitude constants, not calibrated (backlog: "structure is sound,
# numbers are not validated"). A fast digital edge is ~1 V/ns; a "high"
# impedance is board-scale (a crystal/sense node, effectively open at DC).
_REF_EDGE_RATE_V_PER_NS = 1.0
_REF_HIGH_Z_OHM = 1.0e5


class SignalLevel(Enum):
    """Coarse per-net signal-level classification, used only to pick a
    fallback :class:`NetAnnotation` when no datasheet-derived one exists."""

    LOW = "low"  # sensitive analog / small-swing
    LOGIC = "logic"  # ordinary digital rail-to-rail
    POWER = "power"  # rail-level, effectively DC


@dataclass(frozen=True, slots=True)
class NetAnnotation:
    """The three per-connection facts the coupling term derives from —
    LLM-derived at part ingestion from datasheet timing tables and input
    specs, falling back to a function-keyed library default (a crystal
    node is high-Z by construction, a switcher SW node a violent
    aggressor, an ADC input a sensitive victim) when no datasheet detail
    exists yet.
    """

    impedance_ohm: float | None  # None => unknown; treated as high-Z (worst-case victim)
    edge_rate_v_per_ns: float | None  # None => quiescent/DC; not an aggressor
    signal_level: SignalLevel


#: Function-keyed fallback library (backlog: "a fallback library keyed by
#: function"). Deliberately small — real annotations come from datasheet
#: ingestion; this only keeps the coupling term from silently going to
#: zero (undefined != zero, the same rule cost.py enforces) when no
#: annotation has been authored yet.
_FUNCTION_DEFAULTS: dict[str, NetAnnotation] = {
    "crystal": NetAnnotation(impedance_ohm=_REF_HIGH_Z_OHM, edge_rate_v_per_ns=0.0, signal_level=SignalLevel.LOW),
    "switcher_sw": NetAnnotation(impedance_ohm=1.0, edge_rate_v_per_ns=5.0, signal_level=SignalLevel.POWER),
    "adc_input": NetAnnotation(impedance_ohm=_REF_HIGH_Z_OHM, edge_rate_v_per_ns=0.0, signal_level=SignalLevel.LOW),
    "digital_logic": NetAnnotation(impedance_ohm=1.0e3, edge_rate_v_per_ns=1.0, signal_level=SignalLevel.LOGIC),
    "power_rail": NetAnnotation(impedance_ohm=0.1, edge_rate_v_per_ns=0.0, signal_level=SignalLevel.POWER),
}

#: A conservative default when neither a datasheet annotation nor a
#: function hint is available: high impedance (worst-case victim), no
#: assumed edge rate (not asserted an aggressor without evidence) — never
#: zero-out the annotation just because nothing was authored.
_UNKNOWN_DEFAULT = NetAnnotation(
    impedance_ohm=_REF_HIGH_Z_OHM, edge_rate_v_per_ns=None, signal_level=SignalLevel.LOGIC
)


def annotation_for(function_hint: str | None, override: NetAnnotation | None = None) -> NetAnnotation:
    """The per-net annotation to use: an explicit ``override`` (datasheet-
    derived) wins; else the function-keyed library default; else the
    conservative unknown default. Never returns "no annotation" — the
    coupling term always has something to normalize against."""
    if override is not None:
        return override
    if function_hint and function_hint in _FUNCTION_DEFAULTS:
        return _FUNCTION_DEFAULTS[function_hint]
    return _UNKNOWN_DEFAULT


@dataclass(frozen=True, slots=True)
class ObjectiveVector:
    """The six physical objectives one connection minimizes jointly. Each
    weight is a dimensionless emphasis in ``[0, 1]``; ``0`` means "cost.py
    should treat this term as absent for this connection," not "minimize
    it to exactly zero" — a connection with ``low_capacitance=0`` is simply
    not a capacitance-sensitive connection, e.g. a plain logic net.

    ``return_net`` names the implied return path for ``low_impedance``
    (loop-scoped, see the module docstring) — ``None`` when this
    connection has no loop-inductance concern (most logic nets don't).
    """

    low_impedance: float
    low_resistance: float
    low_capacitance: float
    small_loop_area: float
    low_coupling: float
    matched_length: float
    return_net: int | None = None

    def __post_init__(self) -> None:
        for name in (
            "low_impedance",
            "low_resistance",
            "low_capacitance",
            "small_loop_area",
            "low_coupling",
            "matched_length",
        ):
            v = getattr(self, name)
            if not (0.0 <= v <= 1.0):
                raise ValueError(f"objective weight {name}={v!r} must be in [0, 1]")


#: net_class (lower-cased) -> (ObjectiveVector template, one-line reason).
#: This is the "handful of meaningful dials" the backlog asks for — class-
#: driven presets an author can override per connection, not a weight
#: table to hand-tune per net. Absorbs the width-policy enum: decap/power
#: connections favour low_impedance+low_resistance (=> wide), RF/high-Z
#: nodes favour low_capacitance (=> narrow).
_CLASS_PRESETS: dict[str, tuple[ObjectiveVector, str]] = {
    "power": (
        ObjectiveVector(
            low_impedance=1.0,
            low_resistance=1.0,
            low_capacitance=0.0,
            small_loop_area=0.8,
            low_coupling=0.2,
            matched_length=0.0,
        ),
        "a rail must source current with low IR drop and a tight decoupling loop",
    ),
    "ground": (
        ObjectiveVector(
            low_impedance=1.0,
            low_resistance=1.0,
            low_capacitance=0.0,
            small_loop_area=0.8,
            low_coupling=0.0,
            matched_length=0.0,
        ),
        "the return path shares the power rail's low-impedance requirement",
    ),
    "rf": (
        ObjectiveVector(
            low_impedance=0.3,
            low_resistance=0.2,
            low_capacitance=0.9,
            small_loop_area=0.3,
            low_coupling=0.9,
            matched_length=0.3,
        ),
        "controlled-impedance RF traces are capacitance- and coupling-sensitive, not current-sensitive",
    ),
    "clock": (
        ObjectiveVector(
            low_impedance=0.2,
            low_resistance=0.2,
            low_capacitance=0.6,
            small_loop_area=0.4,
            low_coupling=0.7,
            matched_length=0.8,
        ),
        "a clock edge is a strong aggressor and a timing-sensitive victim of its own reflections",
    ),
    "diffpair": (
        ObjectiveVector(
            low_impedance=0.2,
            low_resistance=0.2,
            low_capacitance=0.5,
            small_loop_area=0.3,
            low_coupling=0.6,
            matched_length=1.0,
        ),
        "differential timing skew converts directly to common-mode EMI",
    ),
    "signal": (
        ObjectiveVector(
            low_impedance=0.0,
            low_resistance=0.1,
            low_capacitance=0.2,
            small_loop_area=0.1,
            low_coupling=0.2,
            matched_length=0.0,
        ),
        "an ordinary logic net has no loop or width requirement beyond DRC minimums",
    ),
}

_GROUND_CLASSES = frozenset({"ground", "gnd"})
_POWER_CLASSES = frozenset({"power", "pwr"})


def objectives_for_connection(
    net_class: str, net_domain: str = "electrical", *, return_net: int | None = None
) -> tuple[ObjectiveVector, str]:
    """The objective vector + one-line reason for a connection on
    ``net_class``. Falls back to the ``signal`` preset (no special
    requirement) for an unrecognized class — the safe, common case,
    rather than raising, since most nets on any real board are plain
    signals. ``net_domain != 'electrical'`` is out of v1 scope (backlog:
    fluidic/thermal rejected at the handler) and raises here too, so a
    caller can't silently cost a fluidic net with electrical physics.
    """
    if net_domain != "electrical":
        raise ValueError(f"objectives are electrical-only in v1, got domain={net_domain!r}")
    key = net_class.strip().lower()
    template, reason = _CLASS_PRESETS.get(key, _CLASS_PRESETS["signal"])
    wants_loop = key in _POWER_CLASSES or key in _GROUND_CLASSES
    rn = return_net if wants_loop else None
    if template.return_net == rn:
        return template, reason
    return (
        ObjectiveVector(
            low_impedance=template.low_impedance,
            low_resistance=template.low_resistance,
            low_capacitance=template.low_capacitance,
            small_loop_area=template.small_loop_area,
            low_coupling=template.low_coupling,
            matched_length=template.matched_length,
            return_net=rn,
        ),
        reason,
    )


def aggressor_strength(a: NetAnnotation) -> float:
    """``[0, 1]``: how strongly ``a`` disturbs a neighbour. High dV/dt is
    the whole signal — a quiescent (edge_rate None or 0) net can't be an
    aggressor regardless of anything else, which is why a decap's DC PWR
    terminal correctly drops out of every coupling sum."""
    edge = a.edge_rate_v_per_ns or 0.0
    return max(0.0, min(1.0, edge / _REF_EDGE_RATE_V_PER_NS))


def victim_susceptibility(v: NetAnnotation) -> float:
    """``[0, 1]``: how susceptible ``v`` is to a nearby aggressor's
    E-field. High-Z nets (crystal, ADC input, anything effectively open at
    DC) couple capacitively far more readily than a low-Z rail, which is
    why "unknown" defaults to high-Z — the conservative direction for a
    victim susceptibility term."""
    z = v.impedance_ohm if v.impedance_ohm is not None else _REF_HIGH_Z_OHM
    return max(0.0, min(1.0, z / _REF_HIGH_Z_OHM))


def coupling(aggressor: NetAnnotation, victim: NetAnnotation, k_geometry: float) -> float:
    """``coupling(a -> v) = aggressor_strength(a) x victim_susceptibility(v)
    x k(geometry)`` (backlog formula, verbatim). ``k_geometry in [0, 1]``
    is the caller's spatial-decay factor — cost.py supplies a
    level-appropriate one (an admissible worst-case constant at coarse
    levels, a real proximity-derived value once L4 exists). Order-of-
    magnitude accuracy is the design target here, not precision: the
    geometry term varies over a far wider range than the annotation
    error, so refining these constants further would just be over-fitting
    (backlog, verbatim rationale)."""
    return aggressor_strength(aggressor) * victim_susceptibility(victim) * max(0.0, min(1.0, k_geometry))
