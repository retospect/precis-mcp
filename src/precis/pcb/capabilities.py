"""JLCPCB fab-capability rules, as **versioned data**, not Python constants.

Per ``docs/backlog/pcb-guided-place-route.md`` "Export + order": a rule
table holding JLC's *published minimum* and our *house default* at a
deliberate margin above it, per (process/stackup) row. Two tiers make the
margin legible — a DRC digest can say "JLC min 3.5 mil, house default 6,
this trace spends 2.5 mil of headroom" instead of hiding a constant in code.
Data (not code) because aluminum and non-4-layer processes have genuinely
different capability rows, the same reason the stackup itself is data
(:data:`precis.pcb.DEFAULT_STACKUP`).

The numbers live in ``src/precis/data/pcb_capabilities.json``, each row
carrying ``source``/``retrieved`` (third-party figures that go stale) and a
``field_confidence`` map. Figures were checked directly against live JLCPCB
capability pages on 2026-08-27 (see the JSON file's ``_note`` for the exact
URLs); a field is "low"/"n/a" confidence only where JLC does not publish a
figure for that process at all, in which case the value is ``None`` rather
than an invented number — never carry a figure across from another process's
row (e.g. FR-4 numbers do not apply to the aluminum row).

``board_edge_clearance`` is two fields, not one:
``board_edge_clearance_routed_mm`` (mechanically-routed panel edges) and
``board_edge_clearance_vcut_mm`` (V-cut edges) — the two minimums differ and
neither is a safe stand-in for the other. When the panelization method isn't
known yet, use the V-cut figure as the conservative default.
``npth_annular_ring_mm`` is JLC's non-plated-hole copper-clearance
requirement, a separate figure from ``via_diameter_mm`` (a plated-via size).

``house_default`` is set by an auditable rule (also recorded in the JSON
``_note``): 1.5x ``jlc_min`` rounded up to the nearest 0.05mm, or JLC's own
published "recommended" figure where one exists, whichever is higher —
except ``soldermask_dam_mm``, whose default clears the 2oz-copper minimum
rather than the 1oz ``jlc_min``, so a copper-weight change on the order
doesn't silently violate it.

``soldermask_expansion_mm`` and ``silk_to_mask_clearance_mm`` are the two
terms of the silk-clearance chain (``pad copper edge + expansion = mask
opening; + silk-to-mask = silk line edge; + drawn stroke/2 = centreline``)
that :func:`precis.pcb.silk.silk_clearance_mm` walks to size a courtyard.
Only the second is a *minimum to exceed*: ``soldermask_expansion_mm`` is a
**design** value — the swell we draw the mask opening with, which
:func:`precis.pcb.gerber.soldermask_gerber` writes — so, like
``via_diameter_mm``, the flat 1.5x ``house_default`` rule must NOT be
applied to it; both tiers carry JLC's own applied default. Margining it up
would enlarge every mask opening on every board while pretending to be a
safety margin.

**``via_diameter_mm`` is DERIVED, not independently margined** (2026-08-28
fix): a via's diameter, drill and annular ring are geometrically coupled
(``diameter >= drill + 2 x annular_ring``) — applying the flat 1.5x-margin
rule to each of the three SEPARATELY, as if they were independent, silently
produced a ``house_default`` via whose ring (0.075mm) was below even
``jlc_min``'s own ring (0.18mm). :func:`load_capabilities` now computes each
tier's effective ``via_diameter_mm`` as ``max(published minimum finished-via
diameter, drill_mm + 2 x annular_ring_mm)`` at THAT tier — the JSON's stored
``via_diameter_mm`` is JLC's published floor (an independent, real
constraint), never the final number. :func:`_derive_via_diameter_mm` is the
one place this happens; no other consumer re-derives it.

**Process selection is authored, not only derived.** ``meta.process`` on
the board names a row directly (``'aluminum'``, ``'flex-2layer-35um'`` …)
and overrides the layer-count derivation in
:func:`precis.pcb.drc.process_for_stackup` — the only way a board says it
is not FR-4 (gr520549: the rigid 4-layer gate had no way to hear "this is
a flex board"). The ``flex-*`` rows carry JLC's flex process per outer
copper weight (12um≈1/3oz, 18um≈0.5oz, 35um≈1oz) — only the figures the
cited page publishes; unpublishable fields stay ``None`` per the same
never-invent rule as the aluminum row. ``layers`` pins each row's layer
count so a declared process contradicting the stackup refuses rather
than checks against the wrong process.
:func:`process_layer_counts` is that check's accessor.

**``ipc2221b_conductor_spacing_mm``** (docs/backlog/pcb-ewod-multitile.md
"Rulings 2026-09-19" item 10) is a SEPARATE top-level key, not a
per-process row — IPC-2221B Table 6-1's minimum electrical conductor
spacing is a function of PEAK working voltage and coating/layer alone, not
of any one fab's process capability. :func:`conductor_spacing_mm` is the
typed accessor: ``layer='internal'`` reads the B1 column (no separate
coated variant — IPC does not publish one), ``layer='external'`` reads B4
when ``coated`` else B2, and any voltage above the table's own 500V top
band is refused rather than extrapolated. Valid only below 3050m altitude
(the table's own scope, recorded in the JSON's ``note``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from importlib import resources
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from precis.pcb.profiles import ProfileRules

_PACKAGED_DATA = "precis.data"
_FILE = "pcb_capabilities.json"

#: The capability fields every row must define (a value may be ``None`` when
#: genuinely not applicable to that process — e.g. no vias on a single-layer
#: aluminum board — never as a silent placeholder for "unknown").
FIELDS = (
    "trace_width_mm",
    "trace_spacing_mm",
    "annular_ring_mm",
    "drill_mm",
    "via_diameter_mm",
    "npth_annular_ring_mm",
    "board_edge_clearance_routed_mm",
    "board_edge_clearance_vcut_mm",
    "soldermask_dam_mm",
    "silk_width_mm",
    "soldermask_expansion_mm",
    "silk_to_mask_clearance_mm",
)


@dataclass(frozen=True)
class CapabilityRow:
    """One process/stackup's capability rules, both tiers."""

    process: str
    label: str
    source: str
    retrieved: str
    jlc_min: dict[str, float | None]
    house_default: dict[str, float | None]
    field_confidence: dict[str, str]
    #: Process-level caveats that don't map onto one of ``FIELDS`` (e.g.
    #: aluminum's minimum CNC milling-cutter width, surface finish, mask
    #: color). Empty string when a row has nothing to add.
    process_notes: str = ""


@dataclass(frozen=True)
class ProfileCapabilityRow(CapabilityRow):
    """A fab row with a board profile laid over it (:func:`apply_profile`).
    A subclass, not a field on :class:`CapabilityRow`, so a plain fab row
    serialises exactly as before (route params and snapshots embed it)."""

    #: Fields whose ``house_default`` shortfall is an error, not a warn.
    strict_fields: frozenset[str] = frozenset()


def strict_fields_of(row: CapabilityRow) -> frozenset[str]:
    """The fields whose house-tier shortfall is an error for ``row``; empty
    for a plain fab row."""
    return row.strict_fields if isinstance(row, ProfileCapabilityRow) else frozenset()


def _load_raw() -> dict[str, Any]:
    raw = resources.files(_PACKAGED_DATA).joinpath(_FILE).read_text(encoding="utf-8")
    data: dict[str, Any] = json.loads(raw)
    return data


def _derive_via_diameter_mm(tier: dict[str, float | None]) -> float | None:
    """One tier's effective ``via_diameter_mm`` — ``max(published floor,
    drill + 2 x annular_ring)`` (module docstring). The JSON's stored
    ``via_diameter_mm`` is treated as the published floor input, not the
    final tunable: a process with no vias at all (the floor is ``None``,
    e.g. single-sided aluminum) stays ``None``, and a tier missing either
    coupled field (shouldn't happen for a real row, but a data gap must
    never crash) falls back to the floor unchanged."""
    floor = tier.get("via_diameter_mm")
    if floor is None:
        return None
    drill = tier.get("drill_mm")
    ring = tier.get("annular_ring_mm")
    if drill is None or ring is None:
        return floor
    return max(floor, drill + 2.0 * ring)


def load_capabilities() -> list[CapabilityRow]:
    """Every capability row in the table, in file order. ``via_diameter_mm``
    is derived per tier (:func:`_derive_via_diameter_mm`), not read verbatim
    off the JSON — every caller of this function sees the corrected number."""
    rows = []
    for r in _load_raw()["rows"]:
        jlc_min = dict(r["jlc_min"])
        house_default = dict(r["house_default"])
        jlc_min["via_diameter_mm"] = _derive_via_diameter_mm(jlc_min)
        house_default["via_diameter_mm"] = _derive_via_diameter_mm(house_default)
        rows.append(
            CapabilityRow(
                process=r["process"],
                label=r["label"],
                source=r["source"],
                retrieved=r["retrieved"],
                jlc_min=jlc_min,
                house_default=house_default,
                field_confidence=dict(r.get("field_confidence", {})),
                process_notes=r.get("process_notes", ""),
            )
        )
    return rows


#: Profile rule -> the capability field it raises (same name).
_PROFILE_FIELDS = (
    "trace_width_mm",
    "trace_spacing_mm",
    "annular_ring_mm",
    "drill_mm",
)


def apply_profile(row: CapabilityRow, rules: ProfileRules) -> CapabilityRow:
    """``row`` with ``rules`` laid over it: each profiled field's
    ``house_default`` becomes ``max(house_default, profile value)``, the
    via diameter follows (``max(current, drill + 2 x ring)``, the same
    coupling :func:`_derive_via_diameter_mm` holds), ``jlc_min`` is never
    touched, and ``strict_fields`` carries the profile's strict set. The
    all-off ``prod`` rules return ``row`` itself, so prod is byte-identical.
    A field the row has no ``jlc_min`` for (not applicable to the process)
    stays untouched."""
    house = dict(row.house_default)
    changed = False
    for field in _PROFILE_FIELDS:
        value = getattr(rules, field)
        if value is None or row.jlc_min.get(field) is None:
            continue
        current = house.get(field)
        raised = max(float(value), float(current or row.jlc_min[field] or 0.0))
        if current is None or raised > current:
            house[field] = raised
            changed = True
    drill, ring = house.get("drill_mm"), house.get("annular_ring_mm")
    via = house.get("via_diameter_mm")
    if changed and via is not None and drill is not None and ring is not None:
        house["via_diameter_mm"] = max(via, drill + 2.0 * ring)
    strict = frozenset(f for f in rules.strict_fields if row.jlc_min.get(f) is not None)
    if not changed and not strict:
        return row
    base = {f.name: getattr(row, f.name) for f in fields(CapabilityRow)}
    return ProfileCapabilityRow(
        **{**base, "house_default": house}, strict_fields=strict
    )


def capability_for(process: str) -> CapabilityRow:
    """The row for ``process`` (e.g. ``'2layer'``, ``'4layer'``,
    ``'aluminum'``, ``'flex-2layer-35um'``). Raises rather than silently
    defaulting — a wrong-process design would otherwise get checked
    against the wrong fab's rules."""
    rows = {r.process: r for r in load_capabilities()}
    row = rows.get(process)
    if row is None:
        raise KeyError(
            f"unknown pcb capability process {process!r}; known: {sorted(rows)}"
        )
    return row


def process_layer_counts() -> dict[str, int]:
    """Each row's published layer count (the JSON's ``layers`` key), by
    process name — the consistency check an authored ``meta.process``
    selection is validated against (a ``flex-2layer-*`` board on a
    four-layer stackup is contradictory authoring, not a flex board).
    Rows without the key are absent: no constraint."""
    return {
        str(r["process"]): int(r["layers"])
        for r in _load_raw()["rows"]
        if r.get("layers") is not None
    }


def design_value(row: CapabilityRow | None, field: str, *, fallback: float) -> float:
    """The figure a DESIGN should be built to for ``field``: this row's
    ``house_default``, falling back to its ``jlc_min``, falling back to
    ``fallback``.

    **The tier is pinned here, once, rather than at each consumer.**
    ``house_default`` is the tier by construction — it is what this table
    means by "the number we design to", and :func:`headroom` exists to
    say how much of the margin above ``jlc_min`` a design spends. A
    consumer picking its own tier is how two call sites end up quoting
    different clearances for the same board.

    ``fallback`` is not a convenience default: a row may carry ``None``
    for a field JLC publishes no figure for on that process (aluminum
    carries several), and the module docstring's rule is that such a
    value is deliberately absent, never a number borrowed from the FR-4
    rows. A geometric chain still needs *some* length, so the caller
    supplies its own documented constant and this function makes the
    substitution explicit rather than letting a ``None`` propagate into
    arithmetic. ``row=None`` (a stackup with no capability row at all)
    takes the same path.
    """
    for tier in (row.house_default, row.jlc_min) if row is not None else ():
        value = tier.get(field)
        if value is not None:
            return float(value)
    return fallback


def headroom(row: CapabilityRow, field: str, value_mm: float) -> float:
    """How much margin ``value_mm`` spends above JLC's published minimum for
    ``field`` — the quantity a DRC digest quotes ("2.5 mil of headroom")."""
    jlc_min = row.jlc_min[field]
    if jlc_min is None:
        raise ValueError(f"{row.process}: {field!r} has no jlc_min (not applicable)")
    return value_mm - jlc_min


def conductor_spacing_mm(
    voltage_v: float, *, layer: Literal["internal", "external"], coated: bool
) -> float:
    """IPC-2221B Table 6-1 (columns B1/B2/B4) minimum conductor spacing,
    in mm, for a PEAK working voltage of ``voltage_v`` — the module
    docstring's own paragraph on why this is a separate top-level key,
    not a per-process capability row.

    ``layer='internal'`` always reads the B1 column (IPC-2221B does not
    publish a separate coated-internal figure — inner-layer copper is
    never exposed to conformal coating in the first place); ``coated`` is
    ignored in that case. ``layer='external'`` reads B4 (``coated=True``)
    or B2 (``coated=False``). ``voltage_v`` above the table's own top band
    (500V) is refused outright — per-volt extrapolation past a table this
    coarse is out of scope (docs/backlog/pcb-ewod-multitile.md "Rulings
    2026-09-19" item 10); a design that needs it must supply its own
    ``hv_separation`` override rather than have one invented here. Valid
    only below 3050m altitude (this table's own scope — see the JSON
    file's ``ipc2221b_conductor_spacing_mm.note``)."""
    if layer not in ("internal", "external"):
        raise ValueError(
            f"conductor_spacing_mm: layer must be 'internal' or 'external', "
            f"got {layer!r}"
        )
    if voltage_v < 0:
        raise ValueError(
            f"conductor_spacing_mm: voltage_v must be >= 0, got {voltage_v}"
        )
    table = _load_raw()["ipc2221b_conductor_spacing_mm"]
    bands = table["bands"]
    if voltage_v > bands[-1]["max_voltage_v"] + 1e-9:
        raise ValueError(
            f"conductor_spacing_mm: {voltage_v}V exceeds IPC-2221B Table "
            f"6-1's {bands[-1]['max_voltage_v']}V top band -- per-volt "
            "extrapolation above it is out of scope; supply an explicit "
            "hv_separation override instead"
        )
    for band in bands:
        if voltage_v <= band["max_voltage_v"] + 1e-9:
            if layer == "internal":
                return float(band["b1_internal_mm"])
            return float(
                band["b4_external_coated_mm"]
                if coated
                else band["b2_external_uncoated_mm"]
            )
    raise AssertionError("unreachable: bands cover [0, top band] by construction")


def coating_extent_mm() -> tuple[float, float] | None:
    """The largest finished board the conformal-coating step accepts, as
    ``(max_width_mm, max_height_mm)`` — or ``None`` when no limit has been
    configured (gr414481).

    ``None`` means UNKNOWN, never "no limit". A caller that treats an
    absent figure as a pass turns "we did not check" into "it is fine",
    which is this package's own recurring failure (see
    :meth:`precis.store._pcb_ops.PcbMixin.pcb_drc_findings_latest`'s
    docstring: "no run yet means 'not yet', not 'clean'"). Every caller
    must therefore branch on ``None`` explicitly and SAY that the extent
    went unchecked.

    Why this is not a capability row: the coater's chamber is a property
    of the *process step*, not of the fab that etched the board — a board
    JLC will happily make can still be too large to parylene. It is also a
    MAXIMUM, and every figure in ``rows`` is a MINIMUM, so putting it there
    would invert :func:`headroom` and the ``house_default >= jlc_min``
    margin invariant. Same reasoning as
    ``ipc2221b_conductor_spacing_mm``; see the JSON key's own ``note``.
    """
    table = _load_raw().get("coating_extent_mm") or {}
    w, h = table.get("max_width_mm"), table.get("max_height_mm")
    if w is None or h is None:
        return None
    return float(w), float(h)


__all__ = [
    "FIELDS",
    "CapabilityRow",
    "capability_for",
    "coating_extent_mm",
    "conductor_spacing_mm",
    "design_value",
    "headroom",
    "load_capabilities",
    "process_layer_counts",
]
