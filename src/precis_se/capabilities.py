"""Process capability rows — what a (process, material) can actually do
(se-kind.md "Manufacturing modes"; the pcb capability architecture
transferred).

**Seeded narrowly, on purpose.** The full field set — layer height,
overhang angle, bridge span, bed contact, the anisotropic strength vector,
the CNC/SLA/laser families — is se-kind.md slice 5. What exists today is
the three fields rung 3c could not proceed without: a printed hole comes
out *undersize*, so a core hole taken straight from
:mod:`precis.thread_forming` and modelled at that diameter prints smaller
than the screw's minor Ø and splits its boss. Rather than hardcode a
compensation in the fastening pass, the number lives in
``precis/data/se_capabilities.json`` with the two tiers, source and
confidence every capability row carries, and a slice-5 implementer grows
the file instead of reshaping it.

**Two tiers.** ``physical`` is the floor the process cannot beat whatever
anyone declares; ``house`` is what we build to, at a margin. Consumers
read the house figure. The per-block/per-design *override* layer the pcb
``resolve_net_rules`` pattern has — and the clamp that would go with it —
arrives with the rest of slice 5; there is nowhere to store an override
today, and a resolver with nothing to resolve would be scaffolding
pretending to be architecture.

An unknown mode returns ``None``, not a default. A capability figure
invented for a process nobody characterized is exactly the number that
would print.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any

_PACKAGED_DATA = "precis.data"
_FILE = "se_capabilities.json"


@dataclass(frozen=True)
class Capability:
    """One field of one capability row. ``house``/``physical`` are
    millimetres (the file's one unit); ``mm`` is named in the accessors
    below for the same reason it is everywhere else here."""

    mode: str
    field: str
    house_mm: float
    physical_mm: float
    confidence: str
    source: str

    @property
    def house_m(self) -> float:
        return self.house_mm / 1000.0


@lru_cache(maxsize=1)
def _rows() -> dict[str, dict[str, Any]]:
    text = resources.files(_PACKAGED_DATA).joinpath(_FILE).read_text(encoding="utf-8")
    data: dict[str, Any] = json.loads(text)
    return {str(r["mode"]): r for r in data.get("rows") or []}


def capability(mode: str | None, field: str) -> Capability | None:
    """One field of one row, or ``None`` when the mode or the field is
    uncharacterized — which a caller reports by name rather than
    substituting a neighbour's figure (the file's own rule)."""
    if not mode:
        return None
    row = _rows().get(str(mode).strip())
    if row is None:
        return None
    raw = (row.get("fields") or {}).get(field)
    if raw is None:
        return None
    return Capability(
        mode=str(row["mode"]),
        field=field,
        house_mm=float(raw["house"]),
        physical_mm=float(raw["physical"]),
        confidence=str(raw.get("confidence") or "unknown"),
        source=str(raw.get("source") or ""),
    )


def hole_compensation_m(mode: str | None) -> tuple[float, Capability | None]:
    """How much to **add** to a modelled hole diameter so the printed hole
    comes out at the size the design asked for.

    Returns ``(metres, the row it came from)``; ``(0.0, None)`` for a mode
    with no characterization — a zero that means "nobody has measured
    this", which the caller says out loud rather than passing off as a
    calibrated machine."""
    cap = capability(mode, "hole_diameter_compensation")
    return (0.0 if cap is None else cap.house_m), cap
