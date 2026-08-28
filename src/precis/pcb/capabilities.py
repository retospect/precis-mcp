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


def load_capabilities() -> list[CapabilityRow]:
    """Every capability row in the table, in file order."""
    return [
        CapabilityRow(
            process=r["process"],
            label=r["label"],
            source=r["source"],
            retrieved=r["retrieved"],
            jlc_min=dict(r["jlc_min"]),
            house_default=dict(r["house_default"]),
            field_confidence=dict(r.get("field_confidence", {})),
            process_notes=r.get("process_notes", ""),
        )
        for r in _load_raw()["rows"]
    ]


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
    "headroom",
    "load_capabilities",
]
