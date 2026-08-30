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
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from typing import Any

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


def capability_for(process: str) -> CapabilityRow:
    """The row for ``process`` (e.g. ``'2layer'``, ``'4layer'``,
    ``'aluminum'``). Raises rather than silently defaulting — a wrong-process
    design would otherwise get checked against the wrong fab's rules."""
    rows = {r.process: r for r in load_capabilities()}
    row = rows.get(process)
    if row is None:
        raise KeyError(
            f"unknown pcb capability process {process!r}; known: {sorted(rows)}"
        )
    return row


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


__all__ = [
    "FIELDS",
    "CapabilityRow",
    "capability_for",
    "design_value",
    "headroom",
    "load_capabilities",
]
