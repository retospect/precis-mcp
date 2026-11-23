"""Deterministic chemistry-to-spoken-text transforms for the narration
(TTS) path — the same "code owns pronunciation" contract as
:mod:`precis.draft.verbalize` (numbers), applied to chemical notation that
survives verbatim in a paper title/abstract: ``Zn(NO3)2·6H2O``, ``MOF-74``,
bare formulas like ``CO2``/``H2O``. Left alone, Kokoro/espeak either spells
these letter-by-letter or mangles the digits.

**CONSERVATIVE by design**: :func:`speak_chemistry` only rewrites a span it
recognizes with high confidence via a curated lookup (ions, common exact
formulas) or a narrow structural grammar (the hydrate salt shape); anything
that doesn't fully match is left byte-identical. Curated registry IDs and
IUPAC names follow the formula table. Matching saved chem queries can
supply spoken synonyms from PubChem. All substitutions use local data.

Notation transforms, in match-priority order (most specific first, so a looser
pattern later can't re-carve a span the specific one already claimed):

1. :data:`_HYDRATE` — ``Cation(Anion)n·mH2O`` salt formulas (curated cation/
   anion ion tables + Greek hydrate-count prefixes), e.g.
   ``Zn(NO3)2·6H2O`` → ``zinc nitrate hexahydrate``. Only the parenthesized-
   anion grammar is covered (the literal shape this transform targets); a
   bare-anion salt like ``CuSO4·5H2O`` doesn't match and is left alone
   rather than guessing the cation/anion split.
2. :data:`_formula_re` — a curated table of exact, unambiguous formulas
   (``H2O`` → water, ``CO2`` → carbon dioxide, …), loaded from the
   ``chemistry`` section of the shared plain-data lexicon
   ``data/tts_lexicon.json`` (the file the phonetics DB shares with the
   abbreviation pass, gr690184) rather than a hardcoded dict. Word-boundary
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

import json
import re
from collections.abc import Mapping, Sequence
from functools import cache, lru_cache
from importlib import resources

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


# ── curated exact-formula table (data: precis/data/tts_lexicon.json) ─────

#: The exact-formula table lives in the ``chemistry`` section of the shared
#: TTS lexicon data file, NOT here — the phonetics DB and the abbreviation
#: pass read one plain-data source (gr690184), and a formula's spoken name
#: can be overridden per-draft through the usual lexicon mechanism (applied
#: before this transform in narrate.py). Curated rules the table obeys:
#: every entry has exactly one common name — ambiguity means "don't guess"
#: (e.g. Fe3O4 is both "iron oxide" and "magnetite", so it stays out) — and
#: no formula whose surface collides with an English word ("NO" reads as
#: all-caps emphasis too, so a wrong "nitric oxide" there is worse than a
#: spelled-out N-O). Salts that commonly appear hydrated in the bare-anion
#: form (CuSO4, Na2SO4, CaCl2, …) are also excluded: the exact-formula pass
#: would convert the anhydrous prefix and leave a dangling "·nH2O" behind —
#: a partial, inconsistent conversion rather than the "fully match or leave
#: untouched" contract. That bare-anion hydrate grammar is out of scope for
#: this transform (see :data:`_HYDRATE`'s docstring point).


@lru_cache(maxsize=1)
def _formula_table() -> dict[str, str]:
    """The curated formula → spoken-name table from the shared lexicon data
    file (``chemistry`` section of ``precis/data/tts_lexicon.json``)."""
    raw = json.loads(
        resources.files("precis.data")
        .joinpath("tts_lexicon.json")
        .read_text(encoding="utf-8")
    )
    return {str(k): str(v) for k, v in raw["chemistry"].items()}


@lru_cache(maxsize=1)
def _formula_re() -> re.Pattern[str]:
    """The exact-formula pattern, built from :func:`_formula_table`.
    Longest-first so a formula that is a substring of another (by characters,
    not by the \\b-guarded position it would need) never shadows it — belt
    and braces alongside the \\b anchors, which already block e.g. "H2"
    matching inside "H2O" (digit→letter is a \\w→\\w transition, no boundary
    there)."""
    table = _formula_table()
    return re.compile(
        r"\b("
        + "|".join(re.escape(k) for k in sorted(table, key=len, reverse=True))
        + r")\b"
    )


def _formula_repl(m: re.Match[str]) -> str:
    return _formula_table()[m.group(1)]


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


def speak_chemistry(
    text: str, *, cached: Mapping[tuple[str, str], Sequence[str]] | None = None
) -> str:
    """Rewrite recognized chemistry notation in ``text`` into spoken words.

    Conservative: a span that doesn't fully match a curated lookup or the
    narrow hydrate-salt grammar is left byte-identical unless the
    caller supplied synonyms for an unambiguous saved query. Hydrates
    and exact formulas take priority over cached names and designators.
    """
    t = _HYDRATE.sub(_hydrate_repl, text)
    t = _formula_re().sub(_formula_repl, t)
    t = _spell_compounds(t, cached)
    t = _DESIGNATOR.sub(_designator_repl, t)
    return t


# ── reverse lookup: formula / registry ID / IUPAC name -> spoken name ────


@cache
def _lexicon_chem(section: str) -> dict[str, str]:
    raw = json.loads(
        resources.files("precis.data")
        .joinpath("tts_lexicon.json")
        .read_text(encoding="utf-8")
    )
    return {k.casefold(): v for k, v in raw.get(section, {}).items()}


_PLAIN_NAME = re.compile(r"[A-Za-z]+(?:[ -][A-Za-z]+)*")
_SPOKEN_MAX_LEN = 30


def pick_spoken_synonym(synonyms: Sequence[str]) -> str | None:
    """Pick a name worth saying aloud from a cached PubChem synonym list.

    PubChem's preferred title and first synonym are often an IUPAC name, a
    registry code or an all-caps trade name, so neither is trusted. A synonym
    survives only if it contains plain words, includes lowercase letters,
    and is at least four characters and at most :data:`_SPOKEN_MAX_LEN` characters. Of the
    survivors the shortest wins (ties: PubChem order). ``None`` = no safe
    spoken name; the caller leaves the text verbatim.
    """
    best: str | None = None
    for syn in synonyms:
        name = syn.strip()
        if not 4 <= len(name) <= _SPOKEN_MAX_LEN:
            continue
        if not _PLAIN_NAME.fullmatch(name):
            continue
        if name.isupper():
            continue
        if best is None or len(name) < len(best):
            best = name.lower()
    return best


def _spell_compounds(
    text: str, cached: Mapping[tuple[str, str], Sequence[str]] | None
) -> str:
    # Fold systematic names only. Formula case distinguishes elements.
    names = {
        key: spoken
        for key, spoken in _lexicon_chem("chemistry_iupac").items()
        if key != spoken
    }
    exact = dict(_lexicon_chem("chemistry_registry"))
    clashes: set[str] = set()
    for (query, kind), synonyms in (cached or {}).items():
        spoken = pick_spoken_synonym(synonyms)
        if spoken is None or query.casefold() == spoken:
            continue
        if kind == "name":
            key = query.casefold()
            if key in _lexicon_chem("chemistry_iupac"):
                continue
            if key in names and names[key] != spoken:
                clashes.add(key)
            names.setdefault(key, spoken)
        else:
            exact.setdefault(query, spoken)
    for key in clashes:
        names.pop(key, None)

    patterns = [(key, re.escape(key)) for key in exact] + [
        (key, f"(?i:{re.escape(key)})") for key in names
    ]
    alternatives = "|".join(
        pattern for _, pattern in sorted(patterns, key=lambda item: -len(item[0]))
    )
    if not alternatives:
        return text
    pattern = rf"(?<![\w·-])(?:{alternatives})(?![\w·-])"
    return re.sub(
        pattern,
        lambda m: exact.get(m[0], names.get(m[0].casefold(), m[0])),
        text,
    )


__all__ = ["pick_spoken_synonym", "speak_chemistry"]
