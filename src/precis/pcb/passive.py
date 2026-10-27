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
from collections.abc import Mapping
from typing import Any

#: ``100nF`` / ``0.1uF`` / ``22pF`` — the value token in a capacitor
#: description. Longer unit spellings first so ``mF`` beats ``F``.
#: Case-insensitive — ``100nf`` is the same capacitor ask as ``100nF``
#: (gr520325); ``mF`` still precedes ``F`` in the alternation.
CAP_VALUE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(pF|nF|µF|μF|uF|mF|F)\b", re.IGNORECASE)

#: Pre-public-name alias — kept for callers that imported the private
#: spelling (gr520325 r2).
_CAP_VALUE_RE = CAP_VALUE_RE

#: ``16V`` / ``6.3V`` / ``25VDC`` / ``1.25kV`` — the rated-voltage token.
#: The lookbehind refuses a digit-V run inside a longer token (the ``5V``
#: of the ``Y5V`` dielectric must never read as a 5 V rating); the
#: lookahead refuses a ``V`` that starts a longer word (``16Volt``,
#: ``16VAC``) but allows the ``VDC`` spelling. An optional ``k`` scales
#: the number by 1000 (``1.25kV`` → 1250 V). The first match in a
#: description is the rating, later ones would be test conditions.
_CAP_VOLT_RE = re.compile(r"(?<![A-Za-z0-9])(\d+(?:\.\d+)?)\s*([kK]?)V(?:DC)?(?![\w.])")

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


def _rated_watts(text: str) -> float | None:
    """The rated power in a description or attribute value: ``100mW``,
    ``0.25W``, or the fraction spelling ``1/8W``. Milliwatts read first
    (their ``mW`` token would otherwise split into a bare ``W``)."""
    watts = _POWER_MW_RE.search(text)
    if watts:
        return float(watts.group(1)) / 1000.0
    frac = _POWER_FRAC_RE.search(text)
    if frac and float(frac.group(2)):
        return float(frac.group(1)) / float(frac.group(2))
    whole = _POWER_W_RE.search(text)
    return float(whole.group(1)) if whole else None


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
        value = CAP_VALUE_RE.search(description)
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
            "rated_v": (
                float(volt.group(1)) * (1e3 if volt.group(2) else 1.0) if volt else None
            ),
            "dielectric": diel.group(1).lower() if diel else None,
        }
    if "resistor" in low:
        value = _RES_VALUE_RE.search(description)
        if value is None:
            return None
        ohm = float(value.group(1)) * _res_unit(value.group(2))
        return {"kind": "resistor", "ohm": ohm, "rated_w": _rated_watts(description)}
    return None


# ── the part's own rating data (``parts.params``) ─────────────────────

#: The ``parts.params`` attribute names a capacitor's rated voltage
#: rides under in the jlcparts/LCSC parametric blob — ``"Voltage
#: Rating"`` (C1578291's supercap), ``"Rated Voltage"`` and the
#: ``"Voltage - Rated DC"`` spelling other listings use.
_CAP_PARAM_VOLT_KEYS = (
    "Voltage Rating",
    "Rated Voltage",
    "Voltage - Rated DC",
    "Rated Voltage(DC)",
    "Rated Voltage DC",
    "Withstand Voltage",
)

#: …and the attribute names for a resistor's value and rated power
#: (``"Power(Watts)"`` is the LCSC column's own spelling).
_RES_PARAM_OHM_KEYS = ("Resistance", "Resistance (Ohms)", "Resistance(Ω)")
_RES_PARAM_WATT_KEYS = (
    "Power(Watts)",
    "Rated Power",
    "Rated Power(Watts)",
    "Power Rating",
)

#: The ``_category`` words that declare a part's passive kind — WORD
#: matches, so ``Supercapacitors`` (a battery charger's category text)
#: never misreads as the capacitor it charges.
_PARAMS_CAP_KIND_RE = re.compile(r"\bcapacitors?\b|\bmlcc\b", re.IGNORECASE)
_PARAMS_RES_KIND_RE = re.compile(r"\bresistors?\b", re.IGNORECASE)


def _param_text(params: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    """The first non-empty attribute under ``keys`` — values arrive as
    text (jlcparts ``attribute_value_name``); a bare number coerces."""
    for key in keys:
        val = params.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, (int, float)):
            return str(val)
    return None


def params_passive_kind(params: Mapping[str, Any] | None) -> str | None:
    """``'capacitor'``/``'resistor'`` when ``parts.params._category``
    names the class — the vendor's own declaration — else ``None``."""
    if not isinstance(params, Mapping):
        return None
    cat = str(params.get("_category") or "")
    if _PARAMS_CAP_KIND_RE.search(cat):
        return "capacitor"
    if _PARAMS_RES_KIND_RE.search(cat):
        return "resistor"
    return None


def params_passive_fields(
    params: Mapping[str, Any] | None, kind: str
) -> dict[str, Any]:
    """The rating fields ``parts.params`` carries for ``kind`` —
    ``{value_f, rated_v, dielectric}`` for a capacitor, ``{ohm,
    rated_w}`` for a resistor, each ``None`` when no attribute names it
    (the caller fills the gap from the description parse; never a guess).
    ``{}`` for an empty blob or an unknown kind."""
    if not isinstance(params, Mapping):
        return {}
    if kind == "capacitor":
        value = _param_text(params, ("Capacitance",))
        cap = CAP_VALUE_RE.search(value) if value else None
        volt = _param_text(params, _CAP_PARAM_VOLT_KEYS)
        v = _CAP_VOLT_RE.search(volt) if volt else None
        diel = _param_text(params, ("Temperature Coefficient",))
        d = _DIELECTRIC_RE.search(diel) if diel else None
        return {
            "value_f": (
                float(cap.group(1)) * _CAP_FARADS.get(cap.group(2).lower(), 1.0)
                if cap
                else None
            ),
            "rated_v": float(v.group(1)) * (1e3 if v.group(2) else 1.0) if v else None,
            "dielectric": d.group(1).lower() if d else None,
        }
    if kind == "resistor":
        value = _param_text(params, _RES_PARAM_OHM_KEYS)
        ohm = _RES_VALUE_RE.search(value) if value else None
        watts = _param_text(params, _RES_PARAM_WATT_KEYS)
        return {
            "ohm": float(ohm.group(1)) * _res_unit(ohm.group(2)) if ohm else None,
            "rated_w": _rated_watts(watts) if watts else None,
        }
    return {}
