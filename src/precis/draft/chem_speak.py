"""Deterministic chemistry-to-spoken-text transforms for the narration
(TTS) path — the same "code owns pronunciation" contract as
:mod:`precis.draft.verbalize` (numbers), applied to chemical notation that
survives verbatim in a paper title/abstract: ``Zn(NO3)2·6H2O``, ``MOF-74``,
bare formulas like ``CO2``/``H2O``. Left alone, Kokoro/espeak either spells
these letter-by-letter or mangles the digits.

**CONSERVATIVE by design**: :func:`speak_chemistry` only rewrites a span it
recognizes with high confidence via a curated lookup (ions, common exact
formulas) or a narrow structural grammar (the hydrate salt shape); anything
that doesn't fully match is left byte-identical rather than guessed at. No
network calls (a PubChem-backed lookup is explicitly out of scope, filed
separately), no new dependencies.

Three transforms, in match-priority order (most specific first, so a looser
pattern later can't re-carve a span the specific one already claimed):

1. :data:`_HYDRATE` — ``Cation(Anion)n·mH2O`` salt formulas (curated cation/
   anion ion tables + Greek hydrate-count prefixes), e.g.
   ``Zn(NO3)2·6H2O`` → ``zinc nitrate hexahydrate``. Only the parenthesized-
   anion grammar is covered (the literal shape this transform targets); a
   bare-anion salt like ``CuSO4·5H2O`` doesn't match and is left alone
   rather than guessing the cation/anion split.
2. :data:`_FORMULA_RE` — a curated table of ~2 dozen exact, unambiguous
   formulas (``H2O`` → water, ``CO2`` → carbon dioxide, …), word-boundary
   matched so ``CO2`` in prose converts but ``CO2R`` (a substituent label,
   not the molecule) doesn't — ``\\b`` never breaks between two ``\\w``
   characters, so a trailing letter glued onto the formula (digit→letter,
   both word chars) blocks the match for free.
3. :data:`_DESIGNATOR` — the generic acronym-number designator shape
   (``MOF-74``, ``ZIF-8``, ``UiO-66``, …): drops the hyphen (``MOF-74`` →
   ``MOF 74``) rather than spelling the number out itself, composing with
   :func:`precis.draft.verbalize.verbalize_numbers`, which is applied
   *downstream* of this module in the narration pipeline (see
   :mod:`precis.draft.narrate`) and turns the now-bare ``74`` into "seventy-
   four" — the same guard logic that already protects ``GPT-4``/``bge-m3``
   from being digit-mangled would otherwise leave ``MOF-74`` untouched too,
   since the ``-`` glues the number to a letter. A small denylist
   (:data:`_DESIGNATOR_DENYLIST`) keeps the generic pattern (deliberately
   not a per-chemistry-name table, per the gripe) off well-known non-
   chemistry acronym-number pairs that already have their own established
   spoken form (``GPT-4``, ``COVID-19``, common file/protocol acronyms) —
   not exhaustive, a safety valve, not a guarantee.

Pure function, no I/O, no store/TTS import — unit-testable like the rest of
:mod:`precis.draft.narrate`.
"""

from __future__ import annotations

import re

# ── ion tables (curated, not exhaustive) ────────────────────────────────

#: Common cation symbol -> spoken name, for the hydrate-salt grammar.
_CATIONS: dict[str, str] = {
    "Li": "lithium",
    "Na": "sodium",
    "K": "potassium",
    "Rb": "rubidium",
    "Cs": "cesium",
    "Mg": "magnesium",
    "Ca": "calcium",
    "Sr": "strontium",
    "Ba": "barium",
    "Zn": "zinc",
    "Cu": "copper",
    "Fe": "iron",
    "Ni": "nickel",
    "Co": "cobalt",
    "Mn": "manganese",
    "Al": "aluminum",
    "Ag": "silver",
    "Cd": "cadmium",
    "Pb": "lead",
    "Sn": "tin",
    "Cr": "chromium",
    "Ti": "titanium",
}

#: Common anion symbol (as written inside the parens of a salt formula) ->
#: spoken name.
_ANIONS: dict[str, str] = {
    "NO3": "nitrate",
    "NO2": "nitrite",
    "SO4": "sulfate",
    "SO3": "sulfite",
    "CO3": "carbonate",
    "PO4": "phosphate",
    "OH": "hydroxide",
    "CN": "cyanide",
    "ClO4": "perchlorate",
    "ClO3": "chlorate",
    "CrO4": "chromate",
    "C2O4": "oxalate",
    "C2H3O2": "acetate",
    "CH3COO": "acetate",
}

#: Greek hydrate-count prefix, 1-10 waters of crystallization.
_HYDRATE_PREFIXES: dict[int, str] = {
    1: "mono",
    2: "di",
    3: "tri",
    4: "tetra",
    5: "penta",
    6: "hexa",
    7: "hepta",
    8: "octa",
    9: "nona",
    10: "deca",
}

# ``Zn(NO3)2·6H2O``: cation, ``(``anion``)``, optional anion multiplier
# (unused for naming — stoichiometry, not pronunciation), the middle-dot
# hydrate separator, hydrate count, ``H2O``. Only the literal ASCII middle
# dot (U+00B7) is accepted — a plain ``.`` is a sentence period elsewhere in
# prose and isn't safe to claim generically.
_HYDRATE = re.compile(r"\b([A-Z][a-z]?)\(([A-Za-z0-9]+)\)(\d*)·(\d+)H2O\b")


def _hydrate_repl(m: re.Match[str]) -> str:
    cation_name = _CATIONS.get(m.group(1))
    anion_name = _ANIONS.get(m.group(2))
    n = int(m.group(4))
    prefix = _HYDRATE_PREFIXES.get(n)
    if cation_name is None or anion_name is None or prefix is None:
        return m.group(0)  # unrecognized ion or hydrate count — don't guess
    return f"{cation_name} {anion_name} {prefix}hydrate"


# ── curated exact-formula table ─────────────────────────────────────────

#: A couple dozen high-frequency, unambiguous formulas -> spoken name.
#: Deliberately excludes formulas with more than one common name (e.g.
#: Fe3O4 is both "iron oxide" and "magnetite") — ambiguity means "don't
#: guess", per the module contract.
_FORMULAS: dict[str, str] = {
    "H2O": "water",
    "H2O2": "hydrogen peroxide",
    "CO2": "carbon dioxide",
    "CO": "carbon monoxide",
    "NH3": "ammonia",
    "H2SO4": "sulfuric acid",
    "HNO3": "nitric acid",
    "HCl": "hydrochloric acid",
    "NaCl": "sodium chloride",
    "NaOH": "sodium hydroxide",
    "KOH": "potassium hydroxide",
    "TiO2": "titanium dioxide",
    "SiO2": "silicon dioxide",
    "Al2O3": "aluminum oxide",
    "Fe2O3": "iron oxide",
    "CaCO3": "calcium carbonate",
    "CaO": "calcium oxide",
    "MgO": "magnesium oxide",
    "ZnO": "zinc oxide",
    "CuO": "copper oxide",
    "Na2CO3": "sodium carbonate",
    "NaHCO3": "sodium bicarbonate",
    "K2CO3": "potassium carbonate",
    "CH4": "methane",
    "C2H4": "ethylene",
    "C2H6": "ethane",
    "C6H6": "benzene",
    "O2": "oxygen",
    "O3": "ozone",
    "N2": "nitrogen",
    "SO2": "sulfur dioxide",
    "SO3": "sulfur trioxide",
    "NO2": "nitrogen dioxide",
}
# "NO" (nitric oxide) is deliberately absent: \bNO\b also matches the
# English word "NO" in all-caps emphasis, and a wrong "nitric oxide" there
# is worse than a spelled-out N-O.
# Deliberately excludes salts that commonly appear hydrated in the
# bare-anion form (CuSO4, Na2SO4, CaCl2, ...): the exact-formula pass would
# convert the anhydrous prefix and leave a dangling "·nH2O" behind — a
# partial, inconsistent conversion rather than the "fully match or leave
# untouched" contract. That bare-anion hydrate grammar is out of scope for
# this transform (see :data:`_HYDRATE`'s docstring point).

# Longest-first so a formula that is a substring of another (by characters,
# not by the \b-guarded position it would need) never shadows it — belt and
# braces alongside the \b anchors, which already block e.g. "H2" matching
# inside "H2O" (digit->letter is a \w->\w transition, no boundary there).
_FORMULA_RE = re.compile(
    r"\b("
    + "|".join(re.escape(k) for k in sorted(_FORMULAS, key=len, reverse=True))
    + r")\b"
)


def _formula_repl(m: re.Match[str]) -> str:
    return _FORMULAS[m.group(1)]


# ── generic acronym-number designator ───────────────────────────────────

# MOF-74, ZIF-8, UiO-66, ... — an uppercase-led acronym (1-6 chars) glued by
# a hyphen to its designator number. Generic (not a per-name table) per the
# gripe, guarded by a small denylist below for known non-chemistry
# collisions that already have an established spoken form.
_DESIGNATOR = re.compile(r"\b([A-Z][A-Za-z]{1,5})-(\d+)\b")

#: Safety valve, not exhaustive: well-known acronym-number pairs from
#: outside chemistry that this generic pattern would otherwise also catch.
_DESIGNATOR_DENYLIST: frozenset[str] = frozenset(
    {
        "GPT",
        "COVID",
        "SARS",
        "ISO",
        "IEEE",
        "USB",
        "HTTP",
        "HTTPS",
        "RFC",
        "PDF",
        "JPEG",
        "PNG",
        "GIF",
        "SHA",
        "MD5",
        "AES",
        "RSA",
        "GPS",
        "LED",
        "LCD",
        "OLED",
        "MP3",
        "MP4",
        "DVD",
    }
)


def _designator_repl(m: re.Match[str]) -> str:
    acronym = m.group(1)
    if acronym in _DESIGNATOR_DENYLIST:
        return m.group(0)
    # Drop the hyphen; downstream verbalize_numbers spells the bare number
    # out once it's no longer letter-glued (see module docstring point 3).
    return f"{acronym} {m.group(2)}"


def speak_chemistry(text: str) -> str:
    """Rewrite recognized chemistry notation in ``text`` into spoken words.

    Conservative: a span that doesn't fully match a curated lookup or the
    narrow hydrate-salt grammar is left byte-identical. Runs in match-
    priority order — hydrate salts (most specific) before the exact-formula
    table before the generic designator pattern — so a looser rule later
    never re-carves a span an earlier, more specific one already claimed.
    """
    t = _HYDRATE.sub(_hydrate_repl, text)
    t = _FORMULA_RE.sub(_formula_repl, t)
    t = _DESIGNATOR.sub(_designator_repl, t)
    return t


__all__ = ["speak_chemistry"]
