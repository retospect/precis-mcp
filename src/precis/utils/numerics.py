"""Numeric-token extraction for the lexical search index.

Walks a chunk's text and pulls out every recognizable
``<number><unit>`` token, returning normalized strings the agent
can grep over. Stored alongside chunks in a GIN-indexed TEXT[]
column so search can do exact-value lookups (``"1.523 eV"``) and
the agent can iterate values manually for range-ish queries.

Deliberately **not** structured — no parsed `value: 1.523`, no unit
normalization (eV ↔ meV ↔ J), no uncertainty handling. That's path 3
(:mod:`paper_facts`, deferred as task #63). This module is the cheap
precursor: lexical-only, ingest-time, no LLM, no schema commitment
beyond a single TEXT[] column.

Returned tokens are *normalized for matching*, not for display:

* Numbers stay as written (``"1.523"`` not ``"1523e-3"``)
* Unit casing stays as written (``"eV"`` vs ``"ev"`` differ; we
  preserve original) so the agent's query matches what the paper
  actually says.
* Whitespace between number and unit collapses to a single space
  (``"1.523eV"`` → ``"1.523 eV"`` for index uniformity).
"""

from __future__ import annotations

import re

# Units we recognize as a closed set, ordered longest-first so the
# regex consumes the longest match (avoids partial matches like
# ``"keV"`` matching as ``"k"`` + value).
#
# Sources: typical scientific-paper unit vocabulary. Add new units
# when corpus evidence demands them — keeping this closed prevents
# matching arbitrary trailing words as units.
_UNITS: tuple[str, ...] = (
    # vibrational / spectroscopy
    "cm-1", "cm−1",
    # energy
    "keV", "meV", "eV", "kJ", "J",
    # voltage
    "kV", "mV", "V",
    # current
    "mA", "µA", "uA", "nA", "A",
    # frequency
    "GHz", "MHz", "kHz", "Hz",
    # percent
    "%",
    # temperature
    "°C", "°F", "K",
    # pressure
    "GPa", "MPa", "kPa", "mbar", "atm", "bar", "Pa", "torr",
    # concentration
    "mM", "µM", "uM", "nM", "ppm", "ppb", "M",
    # length
    "nm", "µm", "um", "mm", "cm", "Å", "Angstrom",
    # mass
    "kg", "mg", "µg", "ug", "ng",
    # time / count
    "ms", "µs", "us", "ns", "ps", "min", "hr", "h", "days", "cycles", "s",
    # current-density-ish
    "mAh/g", "Ah/g", "mAh", "Ah",
)


# Compile once at module load. Longest-first ordering is preserved
# because Python's ``re`` engine tries alternatives left-to-right and
# the union is sorted by length descending.
_NUM_RE_PART = r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"
_UNITS_SORTED = sorted(_UNITS, key=len, reverse=True)
_UNITS_RE_PART = "|".join(re.escape(u) for u in _UNITS_SORTED)
_NUMERIC_RE = re.compile(
    rf"(?<![\w.])({_NUM_RE_PART})\s*({_UNITS_RE_PART})(?!\w)",
    re.UNICODE,
)


def extract_numerics(text: str) -> list[str]:
    """Return ``["<number> <unit>", …]`` for every recognized token.

    Deduplicated case-sensitively (preserving paper casing) and
    ordered by first occurrence. Empty input → ``[]``.

    The output is suitable for direct inclusion in a ``TEXT[]``
    column with a GIN index — query side does ``WHERE numerics @>
    ARRAY['1.523 eV']``. For approximate matching the agent issues
    multiple parallel queries (``"1.5 eV"``, ``"1.52 eV"``, …);
    that's the path-2 ergonomics tradeoff we accepted for v1.
    """
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _NUMERIC_RE.finditer(text):
        number = m.group(1)
        unit = m.group(2)
        # Special-case ``%`` — print without the space between number
        # and unit ("12%" is more idiomatic than "12 %" in queries).
        if unit == "%":
            token = f"{number}%"
        else:
            token = f"{number} {unit}"
        if token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


__all__ = ["extract_numerics"]
