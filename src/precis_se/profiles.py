"""T-slot extrusion profiles — a part names a profile, not its section
(``se-off-the-shelf-fabrication.md`` "Profiles capability", gr415062).

**Data, not code.** ``precis/data/se_profiles.json`` holds one row per
*manufacturer part* (currently MISUMI HFS5/6/8), because T-slot geometry
is vendor-specific: no DIN/ISO fixes it, and a 40x40 exists with 8 mm and
10 mm slots. The file's ``_note`` states the source, retrieval date and
``verified`` status; this module never adds a figure of its own.

A reference is a part number (``HFS8-4040``, case-insensitive) or a
nominal-size alias (``40x40``). An alias that more than one seeded part
claims is **ambiguous and refused**, naming the part numbers — picking one
would silently choose a slot width. A field the source did not publish is
``None``, never borrowed from a neighbour.

Pure: no store, no network. ``*_m*`` accessors give SI (m, m², m⁴), matching
the rest of se; mass per metre is kg/m either way.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any

from precis.errors import BadInput

_PACKAGED_DATA = "precis.data"
_FILE = "se_profiles.json"


@dataclass(frozen=True)
class Profile:
    """One manufacturer profile. Lengths in mm, area mm², inertia mm⁴ as
    published; the ``*_m*`` properties give SI."""

    part_number: str
    manufacturer: str
    aliases: tuple[str, ...]
    width_mm: float
    height_mm: float
    slot_type: str | None
    slot_width_mm: float | None
    bore_diameter_mm: float | None
    end_tap: str | None
    area_mm2: float
    ix_mm4: float
    iy_mm4: float
    mass_kg_per_m: float
    product_url: str
    verified: bool

    @property
    def area_m2(self) -> float:
        return self.area_mm2 * 1e-6

    @property
    def ix_m4(self) -> float:
        return self.ix_mm4 * 1e-12

    @property
    def iy_m4(self) -> float:
        return self.iy_mm4 * 1e-12


def _from_row(r: Mapping[str, Any]) -> Profile:
    return Profile(
        part_number=str(r["part_number"]),
        manufacturer=str(r["manufacturer"]),
        aliases=tuple(str(a) for a in r.get("aliases") or ()),
        width_mm=float(r["width_mm"]),
        height_mm=float(r["height_mm"]),
        slot_type=r.get("slot_type"),
        slot_width_mm=r.get("slot_width_mm"),
        bore_diameter_mm=r.get("bore_diameter_mm"),
        end_tap=r.get("end_tap"),
        area_mm2=float(r["area_mm2"]),
        ix_mm4=float(r["ix_mm4"]),
        iy_mm4=float(r["iy_mm4"]),
        mass_kg_per_m=float(r["mass_kg_per_m"]),
        product_url=str(r["product_url"]),
        verified=bool(r.get("verified", False)),
    )


@lru_cache(maxsize=1)
def profiles() -> tuple[Profile, ...]:
    """Every seeded profile, file order."""
    text = resources.files(_PACKAGED_DATA).joinpath(_FILE).read_text(encoding="utf-8")
    return tuple(_from_row(r) for r in json.loads(text).get("rows") or [])


def resolve_in(ref: str, table: tuple[Profile, ...]) -> Profile:
    """Resolve ``ref`` against ``table``; :class:`BadInput` if unknown or
    ambiguous (the message lists what would have worked)."""
    key = ref.strip().lower().replace("×", "x")
    if not key:
        raise BadInput("profile: empty reference")
    exact = [p for p in table if p.part_number.lower() == key]
    if exact:
        return exact[0]
    hits = [p for p in table if key in (a.lower() for a in p.aliases)]
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise BadInput(
            f"profile {ref!r} is ambiguous (T-slot geometry is "
            f"vendor-specific): name one of "
            f"{', '.join(p.part_number for p in hits)}"
        )
    have = ", ".join(f"{p.part_number} ({'/'.join(p.aliases)})" for p in table)
    raise BadInput(f"unknown profile {ref!r}; known: {have}")


def resolve(ref: str) -> Profile:
    """Resolve a part number or nominal-size alias against the seed."""
    return resolve_in(ref, profiles())


def part_profile(part: Mapping[str, Any]) -> Profile | None:
    """The profile a part's ``profile`` key names, or ``None`` if it has
    none. An unknown/ambiguous name raises :class:`BadInput`."""
    ref = part.get("profile")
    if ref is None:
        return None
    return resolve(str(ref))
