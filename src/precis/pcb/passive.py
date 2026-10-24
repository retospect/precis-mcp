"""Catalog-description passive parsing — the pure-text half of
:mod:`precis.pcb.catalog`, split out so the geometric kernel
(:mod:`precis.pcb.drc`, ``check_passive_rating``) can ship in
``precis-geom[pcb-kernel]`` without dragging the catalog's store/HTTP
half along (pcb-workspace-code slice 4).

(docs/backlog/pcb-datasheet-review.md slice 3, in-scope 4: "a
passive's rating and dielectric parsed from the part catalog
description — C1525 → 100nF 16V X7R"). The catalog ``description`` is
free text in jlcparts' house order — ``"100nF ±10% 16V X7R 0402
Multilayer Ceramic Capacitors MLCC - SMD/SMT RoHS"`` — so the parse is
deliberately conservative: a field that does not pattern-match stays
``None`` and the consumer (``drc.check_passive_rating``) reports that
pin of the spec as cannot-check, never a guessed value.
"""

from __future__ import annotations

import re
from typing import Any

#: ``100nF`` / ``0.1uF`` / ``22pF`` — the value token in a capacitor
#: description. Longer unit spellings first so ``mF`` beats ``F``.
_CAP_VALUE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(pF|nF|µF|μF|uF|mF|F)\b")

#: ``16V`` / ``6.3V`` / ``25VDC`` — the rated-voltage token. The
#: lookbehind refuses a digit-V run inside a longer token (the ``5V`` of
#: the ``Y5V`` dielectric must never read as a 5 V rating); the lookahead
#: refuses a ``V`` that starts a longer word (``16Volt``, ``16VAC``) but
#: allows the ``VDC`` spelling. The first match in a description is the
#: rating, later ones would be test conditions.
_CAP_VOLT_RE = re.compile(r"(?<![A-Za-z0-9])(\d+(?:\.\d+)?)\s*V(?:DC)?(?![\w.])")

#: The dielectric classes ``CAP_DIELECTRIC_RANK`` knows, as a word in a
#: description. Case-insensitive — vendor text writes ``x7r`` often
#: enough; the match is lowercased into the rank table's keyspace.
_DIELECTRIC_RE = re.compile(
    r"\b(c0g|np0|x8r|x8l|x7r|x7s|x6s|x5r|x5s|y5v|z5u)\b", re.IGNORECASE
)

#: ``10kΩ`` / ``4.7K`` / ``0.1R`` — the resistance token in a resistor
#: description. Longer suffixes first; bare ``K`` (``10K ±1%``) only
#: matches here, never in the capacitor path, because ``K`` collides
#: with unrelated text too easily.
_RES_VALUE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(mΩ|kΩ|KΩ|MΩ|GΩ|Ω|mR|kR|KR|MR|R|K)\b")

#: Rated power: ``100mW``, ``0.25W``, or the fraction spelling ``1/8W``.
_POWER_MW_RE = re.compile(r"(\d+(?:\.\d+)?)\s*mW\b")
_POWER_W_RE = re.compile(r"(\d+(?:\.\d+)?)\s*W\b")
_POWER_FRAC_RE = re.compile(r"(\d+)\s*/\s*(\d+)\s*W\b")

_CAP_FARADS = {
    "pf": 1e-12,
    "nf": 1e-9,
    "uf": 1e-6,
    "µf": 1e-6,
    "μf": 1e-6,
    "mf": 1e-3,
    "f": 1.0,
}


def _res_unit(unit: str) -> float:
    """Ohms multiplier for a resistance suffix. Case-SENSITIVE — ``MΩ``
    (mega) and ``mΩ`` (milli) differ only by case, so the unit is never
    lowercased here."""
    if unit in ("mΩ", "mR"):
        return 1e-3
    if unit in ("Ω", "R"):
        return 1.0
    if unit in ("kΩ", "KΩ", "kR", "KR", "K"):
        return 1e3
    if unit in ("MΩ", "MR"):
        return 1e6
    if unit == "GΩ":
        return 1e9
    return 1.0


def parse_passive_description(description: str | None) -> dict[str, Any] | None:
    """One catalog ``parts.description`` → the passive's parsed ratings.

    Returns ``{"kind": "capacitor", "value_f", "rated_v", "dielectric"}``
    or ``{"kind": "resistor", "ohm", "rated_w"}`` (any field ``None``
    when the text does not carry it), else ``None`` when the description
    is not recognisably a capacitor or resistor — kind detection needs
    the word ("capacitor"/"mlcc", "resistor"), never a unit pattern
    alone, so a ferrite bead's ``120Ω`` or a diode's ``16V`` does not
    misparse into a passive.
    """
    if not description:
        return None
    low = description.lower()
    if "capacitor" in low or "mlcc" in low:
        value = _CAP_VALUE_RE.search(description)
        volt = _CAP_VOLT_RE.search(description)
        diel = _DIELECTRIC_RE.search(description)
        if value is None and volt is None and diel is None:
            return None
        unit = value.group(2).lower() if value else None
        return {
            "kind": "capacitor",
            "value_f": (
                float(value.group(1)) * _CAP_FARADS.get(unit or "", 1.0)
                if value
                else None
            ),
            "rated_v": float(volt.group(1)) if volt else None,
            "dielectric": diel.group(1).lower() if diel else None,
        }
    if "resistor" in low:
        value = _RES_VALUE_RE.search(description)
        if value is None:
            return None
        ohm = float(value.group(1)) * _res_unit(value.group(2))
        watts = _POWER_MW_RE.search(description)
        rated_w = float(watts.group(1)) / 1000.0 if watts else None
        if rated_w is None:
            frac = _POWER_FRAC_RE.search(description)
            if frac and float(frac.group(2)):
                rated_w = float(frac.group(1)) / float(frac.group(2))
        if rated_w is None:
            whole = _POWER_W_RE.search(description)
            rated_w = float(whole.group(1)) if whole else None
        return {"kind": "resistor", "ohm": ohm, "rated_w": rated_w}
    return None
