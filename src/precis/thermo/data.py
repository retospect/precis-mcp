"""Vetted neutral identities and NASA-7 fits; offline and loaded on first use.

H uses the source's fixed elemental enthalpy zero at 298.15 K. Formation
H/G subtract reference-element H/G at the requested T; reaction sums use H
directly because elemental references cancel in a balanced equation.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from precis.errors import BadInput

R = 8.31446261815324  # J/mol/K, current SI molar gas constant
NASA7 = "nasa7-fit"
_REFERENCES = {"H": ("H2", 2), "N": ("N2", 2), "O": ("O2", 2), "C": ("C(gr)", 1)}


@dataclass(frozen=True)
class SpeciesData:
    formula: str
    phase: str
    cas: str | None
    dHf: float | None  # formation enthalpy at T, J/mol
    S: float | None  # absolute standard entropy at T, J/mol/K
    dGf: float | None  # formation Gibbs energy at T, J/mol
    source: str = NASA7
    tables: str = ""
    missing: list[str] = field(default_factory=list)
    note: str = ""
    name: str | None = None
    H: float | None = None  # source-reference enthalpy at T, J/mol
    Cp: float | None = None  # J/mol/K
    charge: int | None = None


@lru_cache(maxsize=1)
def _records() -> dict[str, Any]:
    return json.loads(
        Path(__file__).with_name("nasa7.json").read_text(encoding="utf-8")
    )["species"]


def _evaluate(record: dict[str, Any], T: float) -> tuple[float, float, float]:
    """NASA-7 H, S, Cp; inclusive endpoints, lower interval at a join."""
    ranges = record["ranges"]
    if not math.isfinite(T) or T <= 0:
        raise BadInput("T must be finite and > 0 K", next="args={'T': 298.15}")
    if not ranges[0] <= T <= ranges[-1]:
        raise BadInput(
            f"{record['name']} NASA-7 fit supports {ranges[0]:g}..{ranges[-1]:g} K; T={T:g} is out of range",
            next="choose T within every species' published fit range",
        )
    i = next(i for i, upper in enumerate(ranges[1:]) if upper >= T)
    a, b, c, d, e, f, g = record["coefficients"][i]
    cp = R * (a + b * T + c * T**2 + d * T**3 + e * T**4)
    h = R * (a * T + b * T**2 / 2 + c * T**3 / 3 + d * T**4 / 4 + e * T**5 / 5 + f)
    s = R * (a * math.log(T) + b * T + c * T**2 / 2 + d * T**3 / 3 + e * T**4 / 4 + g)
    return h, s, cp


def lookup_species(
    formula: str, phase: str, atoms: dict[str, int], T: float
) -> SpeciesData:
    """Exact vetted source ID only; never select an isomer or ion by formula.

    The unavailable radical records carry identity hints but no coefficients:
    their third-party redistribution permission is unresolved.
    """
    key = "H2O(L)" if formula == "H2O" and phase == "l" else formula
    record = _records().get(key)
    if record is None:
        return SpeciesData(
            formula,
            phase,
            None,
            None,
            None,
            None,
            missing=["identity", "H(T)", "S(T)", "Cp(T)"],
            note="identity unavailable: use a vetted source species ID; molecular formulas can name multiple isomers",
        )
    if record["composition"] != atoms or record["charge"] != 0:
        raise BadInput(
            "source identity/composition mismatch",
            next="use a vetted neutral species ID",
        )
    if phase != record["phase"] or not record.get("coefficients"):
        why = record.get("unavailable_reason", f"no approved {phase}-phase fit")
        return SpeciesData(
            formula,
            phase,
            record["cas"],
            None,
            None,
            None,
            missing=["H(T)", "S(T)", "Cp(T)"],
            note=why,
            name=record["name"],
            charge=record["charge"],
        )
    h, s, cp = _evaluate(record, T)
    h_ref = s_ref = 0.0
    for element, count in atoms.items():
        ref_id, divisor = _REFERENCES[element]
        rh, rs, _ = _evaluate(_records()[ref_id], T)
        h_ref += count * rh / divisor
        s_ref += count * rs / divisor
    hf = h - h_ref
    ranges = record["ranges"]
    source = f"NASA-TM-4513 ID={key}, {record['source_note']}; {record['source_file']} sha256={record['source_sha256'][:12]} (full pin in nasa7.json)"
    return SpeciesData(
        formula,
        phase,
        record["cas"],
        hf,
        s,
        hf - T * (s - s_ref),
        tables=source,
        name=record["name"],
        H=h,
        Cp=cp,
        charge=0,
        note=f"polynomial fit, standard pressure 1 bar; range {ranges[0]:g}..{ranges[-1]:g} K",
    )
