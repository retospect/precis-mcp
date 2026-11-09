"""The ALCHEM graph as the external comparison set for numeric extraction.

gr606461, Reto ruling 2026-11-09 ("yes for the macatamo extraction"): the
ALCHEM Lab (Hanyang ERICA) knowledge graph — CC BY 4.0 ``data.trig`` at
:data:`ALCHEM_TRIG_URL`, ~11.3k ``alchem:PropertyValue`` nodes over 76
papers, each keyed by DOI with a material, an ``experiment`` /
``computation`` origin, a unit and a section locator — is the comparison
set for precis measure extraction. It is one gpt-6.1-sol pass with no
human review, so this module reports **agreement**, never
precision/recall: per paper, every live output measure and every ALCHEM
value lands in one of ``match`` / ``value≠`` / ``unit≠`` / ``ours-only``
/ ``theirs-only``. The diff is the fix queue for
``docs/backlog/table-numeric-extraction.md`` and
``docs/backlog/measures-substrate.md`` — the unmapped-measurand list and
the unparsed-unit disagreements are enumerable and meant to shrink, so
the script is built to be re-run as extraction improves.

The DB/network glue lives in ``scripts/_alchem_diff.py`` (``scripts/
alchem-diff``). This module is pure: rdflib for the TriG, the
measure-unit machinery for conversion, no store and no LLM.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import rdflib
from rdflib import RDF, RDFS, Namespace

from precis.errors import BadInput
from precis.taxonomy.measure_units import (
    NeedsMolarMass,
    format_value,
    make_converter,
)
from precis.utils.text import slugify

#: The published CC BY 4.0 dump the diff runs against (gr606461).
ALCHEM_TRIG_URL: Final = "https://alchemlab.hanyang.ac.kr/ontology/v1/data.trig"

NS: Final = Namespace("https://w3id.org/alchem/")

# ── parse ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AlchemValue:
    """One ``alchem:PropertyValue`` node (a ``paper/N/Vk`` subject)."""

    vid: str  # 'V12'
    paper: str  # '61'
    #: The ``alchem:term`` slug ('overpotential'), '' when untyped.
    term: str
    #: ``alchem:property`` — the printed measurand label, which can differ
    #: from the term (``plating_lifespan`` printed as "stability_duration").
    prop: str
    value: float | None
    value_min: float | None
    value_max: float | None
    unit: str | None
    value_text: str | None
    #: ``rdfs:label`` of the ``alchem:material`` node ('O-ND/C_10-Li').
    material: str | None
    #: ``alchem:origin`` fragment — 'experiment' or 'computation'.
    origin: str | None
    #: ``alchem:evidence_locator`` ('Section 2.1', 'Fig. S23').
    locator: str | None


@dataclass(slots=True)
class AlchemPaper:
    number: int
    iri: str
    doi: str | None
    title: str | None
    values: list[AlchemValue] = field(default_factory=list)


def _num(node: Any) -> float | None:
    """An RDF literal as a float, or None for non-numeric content."""
    if node is None:
        return None
    try:
        val = node.toPython()
    except (ValueError, AttributeError):
        val = None
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return float(val)
    try:
        return float(str(node))
    except (TypeError, ValueError):
        return None


def _text(node: Any) -> str | None:
    if node is None:
        return None
    s = str(node).strip()
    return s or None


def _frag(node: Any) -> str:
    """An IRI's fragment name ('…/term/overpotential' -> 'overpotential',
    'alchem:experiment' -> 'experiment')."""
    return str(node).rsplit("/", 1)[-1].rsplit("#", 1)[-1]


_PV_RE: Final = re.compile(r"/paper/(\d+)/(V\d+)$")


def parse_alchem_trig(text: str) -> dict[str, AlchemPaper]:
    """Parse the ``data.trig`` dump into ``{doi: AlchemPaper}``.

    ``alchem:doi``/``alchem:title`` live in the metadata graph while the
    ``PropertyValue`` nodes live in ``graph/paper/N``; both are read
    dataset-wide. Papers are keyed by lowercase DOI; a paper without one
    keys on ``'paper/N'``. Only papers that carry values or a DOI are
    returned.
    """
    # default_union: queries must span the named graphs (each paper lives
    # in its own ``graph/paper/N``; DOIs live in the metadata graph).
    ds = rdflib.Dataset(default_union=True)
    ds.parse(data=text, format="trig")
    papers: dict[str, AlchemPaper] = {}

    def paper_for(iri: Any) -> AlchemPaper:
        num = int(str(iri).rsplit("/", 1)[-1])
        if str(num) not in papers:
            papers[str(num)] = AlchemPaper(
                number=num, iri=str(iri), doi=None, title=None
            )
        return papers[str(num)]

    for subj in set(ds.subjects(RDF.type, NS.Paper)):
        p = paper_for(subj)
        doi = _text(ds.value(subj, NS.doi))
        p.doi = p.doi or (doi.lower() if doi else None)
        p.title = p.title or _text(ds.value(subj, NS.title))

    for subj in set(ds.subjects(RDF.type, NS.PropertyValue)):
        m = _PV_RE.search(str(subj))
        if m is None:
            continue
        p = paper_for(rdflib.URIRef(str(subj).rsplit("/", 1)[0]))
        material = ds.value(subj, NS.material)
        origin = ds.value(subj, NS.origin)
        # NS["term"], not NS.term: ``term`` is a method on Namespace and
        # attribute access would return the bound method, never the IRI.
        term = ds.value(subj, NS["term"])
        p.values.append(
            AlchemValue(
                vid=m.group(2),
                paper=m.group(1),
                term=_frag(term) if term is not None else "",
                prop=_text(ds.value(subj, NS.property)) or "",
                value=_num(ds.value(subj, NS.value)),
                value_min=_num(ds.value(subj, NS.value_min)),
                value_max=_num(ds.value(subj, NS.value_max)),
                unit=_text(ds.value(subj, NS.unit)),
                value_text=_text(ds.value(subj, NS.value_text)),
                material=(
                    _text(ds.value(material, RDFS.label))
                    if material is not None
                    else None
                ),
                origin=_frag(origin) if origin is not None else None,
                locator=_text(ds.value(subj, NS.evidence_locator)),
            )
        )
    return {
        (p.doi or f"paper/{p.number}"): p for p in papers.values() if p.values or p.doi
    }


# ── unit normalisation ────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class NormUnit:
    """An ``alchem:unit`` spelling re-said in the reported-unit dialect
    ``make_converter`` reads.

    ``scale`` folds a printed magnitude into the numbers (``10^-3 Å2`` ->
    Å² at 1e-3, ``ppb`` -> 1 at 1e-9); ``reference`` carries an electrode
    reference the spelling smuggles in (``V_RHE`` -> V + 'RHE') — ours
    live on ``measures.reference``, never in the unit; ``basis`` carries
    a subscript normalization label (``mA/cm2_ECSA`` -> 'ECSA') extracted
    here because our own basis regex needs a letter before the ``_``.
    ``text`` is None only when the value carried no unit at all; an
    explicit dimensionless keeps ``'1'`` so the converter still runs.
    """

    text: str | None = None
    scale: float = 1.0
    reference: str | None = None
    basis: str | None = None


#: Spellings decided before any rewriting: dimensionless labels, counts
#: and pint's false friends (``ML`` megaliter, ``µB`` microbyte, ``e``
#: elementary charge, ``Ohm`` unparseable, ``decade`` a time).
_UNIT_EXACT: Final[dict[str, NormUnit]] = {
    "1": NormUnit("1"),
    "a.u.": NormUnit("1"),
    "[in_i]": NormUnit("1"),
    "[PPI]": NormUnit("1"),
    "mol/mol": NormUnit("1"),
    "ML": NormUnit("1"),  # monolayer, not megaliter
    "e": NormUnit("count"),  # electrons transferred, not a charge
    "cycle": NormUnit("count"),
    "cycles": NormUnit("count"),
    "layer": NormUnit("count"),
    "atomic_rows": NormUnit("count"),
    "sites/unit_cell": NormUnit("count"),
    "GPU": NormUnit("count"),
    "ppb": NormUnit("1", 1e-9),  # pint has ppm, no ppb
    "wt%": NormUnit("%"),
    "at%": NormUnit("%"),
    "vol%": NormUnit("%"),
    "mol%": NormUnit("%"),
    "µB": NormUnit("bohr_magneton"),  # pint reads microbyte
    "wk": NormUnit("week"),
    "Ohm": NormUnit("ohm"),
    "Ω": NormUnit("ohm"),
    "mOhm": NormUnit("mohm"),
    "MOhm": NormUnit("Mohm"),
    "Ohm/sq": NormUnit("ohm"),  # sheet resistance: the square is a formality
    "Ohm.cm2": NormUnit("ohm*cm^2"),
    "Ohm.s-1/2": NormUnit("ohm*s^-0.5"),  # Warburg tail, not s^-1 halves
    "Ohm.s-0.5": NormUnit("ohm*s^-0.5"),
    "mV/dec": NormUnit("mV"),  # Tafel slope: decade is a count, not a time
    "mV/pH": NormUnit("mV"),
    "ug metal-1 s-1": NormUnit("ug/s", basis="metal"),  # per metal mass
    "UDS/t.oz": NormUnit("USD/troy_ounce"),  # USD typo + one 't.oz' token
    "USD/GGE_H2": NormUnit("USD/gal", basis="H2"),  # gasoline-gallon equiv.
    "USD/kg_H2": NormUnit("USD/kg", basis="H2"),
    "m0": NormUnit("m0"),  # electron rest mass — no pint unit, stays unread
    "Barrer": NormUnit("Barrer"),  # gas permeability — no pint unit either
}

#: ``V_RHE`` / ``V_Li/Li+`` / ``V_Ag/AgCl`` / ``V_SCE`` / ``V_SHE`` /
#: ``V_Na/Na+`` — the reference electrode folded into the unit.
_V_REF_RE: Final = re.compile(r"^V_(?P<ref>.+)$")

#: ``10^-3 Å2``, ``×10^25 cm3``, ``10^-4 s-1``: a printed magnitude the
#: value is read through.
_SCALE_RE: Final = re.compile(
    r"^[×x]?\s*10\^\s*(?P<exp>-?\d+(?:\.\d+)?)\s*[.·]?\s*(?P<rest>.+)$"
)

#: ``mol.rad2`` / ``cm4.F-2.V-1`` / ``W/(m.K)``: the ALCHEM separator dot.
#: A digit before it is allowed (``cm4.F``); a digit after is not, so
#: decimals (``s-0.5``) and the scale prefix never fold.
_DOT_SEP_RE: Final = re.compile(r"(?<=[A-Za-z0-9)µμΩÅ\]])\.(?=[A-Za-z(µμΩÅ])")

#: ``cc(STP)``: the STP qualifier on a volume is provenance, not a unit.
_STP_RE: Final = re.compile(r"\(STP\)")

#: A subscript basis label (``_MOF``, ``_ECSA``, ``_Ir``) — extracted
#: before the exponent rewrite because upstream's label regex needs a
#: letter before the ``_`` and ``cm2_ECSA`` has a digit there.
_BASIS_LABEL_RE: Final = re.compile(r"_([A-Za-z0-9]+-?[A-Za-z][A-Za-z0-9]*)")

#: Unit stems an exponent suffix may attach to (``cm-1`` -> ``cm^-1``,
#: ``rad2`` -> ``rad^2``). Whitelisted so ``4-NP``, ``H2`` and ``CO2``
#: never read as units with an exponent.
_STEMS: Final[frozenset[str]] = frozenset(
    [
        "cm",
        "mm",
        "m",
        "km",
        "nm",
        "pm",
        "Å",
        "s",
        "ms",
        "us",
        "µs",
        "ns",
        "ps",
        "fs",
        "min",
        "h",
        "wk",
        "bar",
        "mbar",
        "Mbar",
        "kPa",
        "Pa",
        "mmHg",
        "cmHg",
        "rad",
        "deg",
        "F",
        "mF",
        "uF",
        "V",
        "mV",
        "A",
        "mA",
        "uA",
        "L",
        "mL",
        "g",
        "mg",
        "ug",
        "µg",
        "kg",
        "mol",
        "mmol",
        "umol",
        "µmol",
        "nmol",
        "ohm",
        "mohm",
        "Mohm",
        "esu",
        "cc",
        "C",
        "mC",
        "J",
        "mJ",
        "kJ",
        "W",
        "mW",
        "eV",
        "meV",
        "keV",
        "GPa",
        "P",
        "Da",
        "kDa",
        "S",
        "mS",
        "Hz",
        "K",
        "N",
        "Wh",
        "mWh",
        "kWh",
        "Ah",
        "mAh",
        "cd",
        "in",
        "B",
        "kcal",
        "t",
        "oz",
    ]
)

_EXP_SUFFIX_RE: Final = re.compile(r"([A-Za-zµμΩÅ]+?)(-?\d+(?:\.\d+)?)\b")

#: ``mAh/gcarbon`` / ``A/mgPt`` / ``molCO/(gFe.s)`` / ``mgC/(gcat.h)`` /
#: ``mgcatalyst/cm2`` / ``gHC`` — a basis the spelling leaves unmarked;
#: add the ``_`` the basis extraction reads.
_BASIS_FIX_RE: Final = re.compile(
    r"(?<![A-Za-z_])(g|mg|ug|µg|kg|mmol|mol|A|mA|mAh)"
    r"(cat|catalyst|carbon|metal|MOF|RDE|HC|C|Ir|Pt|Pd|Fe|Mn|Zn|Cu|Co|Ni|CO)\b"
)

#: Whole-token rewrites applied after the exact map (``cc`` -> ``cm^3``,
#: a pH denominator -> ``1``, ALCHEM's ``UDS`` typo -> ``USD``).
_TOKEN_MAP: Final[dict[str, str]] = {
    "cc": "cm^3",
    "dec": "count",
    "pH": "1",
    "molecules": "count",
    "molecule": "count",
    "atom": "count",
    "sites": "count",
    "pores": "count",
    "Ohm": "ohm",
    "MOhm": "Mohm",
    "mOhm": "mohm",
    "UDS": "USD",
}
_TOKEN_RE: Final = re.compile(r"[A-Za-zµμΩÅ]+(?![A-Za-z0-9_^])")


def _exp_suffix(unit: str) -> str:
    """Attach trailing digits as exponents on whitelisted stems:
    ``cm-1`` -> ``cm^-1``, ``cm4.F-2.V-1`` -> ``cm^4*F^-2*V^-1``."""

    def repl(m: re.Match[str]) -> str:
        stem, exp = m.group(1), m.group(2)
        if stem not in _STEMS or float(exp) == 0.0:
            return m.group(0)
        return f"{stem}^{exp}"

    return _EXP_SUFFIX_RE.sub(repl, unit)


def alchem_unit(raw: str | None) -> NormUnit:
    """Normalise an ``alchem:unit`` spelling for ``make_converter``.

    Returns ``NormUnit(None)`` for a missing/empty unit. Unparseable
    leftovers (``Barrer``, ``UDS/t.oz`` shapes) are returned as-is: the
    converter refuses them and the row lands in the unit≠ bucket, which
    is where the fix queue wants it.
    """
    text = (raw or "").strip()
    if not text:
        return NormUnit(None)
    if text in _UNIT_EXACT:
        return _UNIT_EXACT[text]
    if (m := _V_REF_RE.match(text)) is not None:
        return NormUnit("V", reference=m.group("ref").upper())
    scale = 1.0
    if (m := _SCALE_RE.match(text)) is not None:
        scale = 10.0 ** float(m.group("exp"))
        text = m.group("rest").strip()
    text = _STP_RE.sub("", text)
    text = text.replace("×", "*")  # 'Å×Å' — a left-over magnitude cross
    text = _DOT_SEP_RE.sub("*", text)
    text = _BASIS_FIX_RE.sub(lambda m: f"{m.group(1)}_{m.group(2)}", text)
    labels = _BASIS_LABEL_RE.findall(text)
    text = _BASIS_LABEL_RE.sub("", text)
    text = _exp_suffix(text)
    text = _TOKEN_RE.sub(lambda m: _TOKEN_MAP.get(m.group(0), m.group(0)), text)
    return NormUnit(text or None, scale, basis=labels[0] if labels else None)


# ── measurand keys ────────────────────────────────────────────────────

#: ALCHEM term/property slugs that must not be read as-is because the
#: precis measurand names them differently. Key: slugified ALCHEM term or
#: property string; value: the slugified precis measurand key. Extend as
#: reports surface unmapped terms — each entry is a one-line fix.
ALCHEM_MEASURAND_MAP: Final[dict[str, str]] = {
    # ALCHEM 'plating_lifespan' is the same quantity precis calls
    # stability_duration (its own property label says so).
    "plating-lifespan": "stability-duration",
}


def _key(text: str | None) -> str:
    return slugify(text or "")


def their_key(value: AlchemValue) -> str:
    """The comparison key for one ALCHEM value: the mapped term slug,
    else the mapped property label, else the raw slug."""
    return (
        ALCHEM_MEASURAND_MAP.get(_key(value.term))
        or ALCHEM_MEASURAND_MAP.get(_key(value.prop))
        or _key(value.term)
        or _key(value.prop)
    )


# ── our side ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class OursValue:
    """One live output measure as the diff sees it (the adapter input is
    a ``measures_for_paper`` row)."""

    id: Any
    measurand_key: str  # slugify'd measurand slug — the grouping key
    measurand: str  # display name
    keys: frozenset[str]  # slug + name + aliases, slugified
    subject_keys: frozenset[str]  # normalised subject labels
    subject: str  # display label
    interval: tuple[float, float] | None  # canonical-unit extent
    text: str | None  # categorical/boolean compare text
    canonical_unit: str | None
    literal: str
    reported_unit: str | None
    normalization: str | None
    reference: str | None
    attribution: str | None
    tier: str | None


_SUB_DIGITS: Final = {ord(c): str(i) for i, c in enumerate("₀₁₂₃₄₅₆₇₈₉")}


def _subject_key(label: str | None) -> str:
    """A material/subject label folded for comparison: subscripts to
    digits, underscores out ('O-ND/C_10-Li' -> 'o-nd-c10-li')."""
    return slugify((label or "").translate(_SUB_DIGITS).replace("_", ""))


def _our_interval(row: Mapping[str, Any]) -> tuple[float, float] | None:
    num = row.get("value_num")
    low = row.get("value_low")
    high = row.get("value_high")
    if num is None and low is None and high is None:
        return None
    form = row.get("value_form") or "point"
    lo = -math.inf if form == "upper_bound" else (low if low is not None else num)
    hi = math.inf if form == "lower_bound" else (high if high is not None else num)
    if lo is None or hi is None:
        return None
    return (float(lo), float(hi))


def ours_from_measure_row(row: Mapping[str, Any]) -> OursValue:
    """Adapt one ``measures_for_paper`` row for :func:`diff_paper`."""
    slug = row.get("measurand_slug") or row.get("measurand") or ""
    keys = {_key(str(slug)), _key(str(row.get("measurand") or ""))}
    keys |= {_key(str(a)) for a in (row.get("measurand_aliases") or [])}
    subject_keys = {
        _subject_key(str(row["subject"]) if row.get("subject") else None),
        _subject_key(str(row["subject_title"]) if row.get("subject_title") else None),
    }
    text = row.get("value_text") or row.get("literal")
    if text is None and row.get("value_bool") is not None:
        text = "true" if row["value_bool"] else "false"
    return OursValue(
        id=row.get("id"),
        measurand_key=_key(str(slug)),
        measurand=str(row.get("measurand") or slug),
        keys=frozenset(k for k in keys if k),
        subject_keys=frozenset(k for k in subject_keys if k),
        subject=str(row.get("subject") or row.get("subject_title") or ""),
        interval=_our_interval(row),
        text=str(text) if text is not None else None,
        canonical_unit=(
            str(row["canonical_unit"]) if row.get("canonical_unit") else None
        ),
        literal=str(row.get("literal") or ""),
        reported_unit=(str(row["reported_unit"]) if row.get("reported_unit") else None),
        normalization=(str(row["normalization"]) if row.get("normalization") else None),
        reference=str(row["reference"]) if row.get("reference") else None,
        attribution=(
            str(row["source_attribution"]) if row.get("source_attribution") else None
        ),
        tier=str(row["tier"]) if row.get("tier") else None,
    )


# ── the diff ──────────────────────────────────────────────────────────

#: Pair kinds, in report order.
PAIR_KINDS: Final[tuple[str, ...]] = (
    "match",
    "value",
    "unit",
    "ours_only",
    "theirs_only",
)


@dataclass(frozen=True, slots=True)
class Pair:
    """One verdict. ``ours``/``theirs`` are None on the *_only kinds."""

    kind: str  # 'match' | 'value' | 'unit' | 'ours_only' | 'theirs_only'
    ours: OursValue | None
    theirs: AlchemValue | None
    note: str = ""


@dataclass(slots=True)
class PaperDiff:
    """The agreement verdict for one paper."""

    label: str  # our paper handle or the DOI
    doi: str | None
    pairs: list[Pair]
    #: ALCHEM values with no precis measurand key at all — the vocabulary
    #: gap list, distinct from theirs_only on a mapped key (a recall gap).
    unmapped: list[AlchemValue] = field(default_factory=list)

    def counts(self) -> Counter[str]:
        return Counter(p.kind for p in self.pairs)


def _subjects_compatible(ours: OursValue, material: str | None) -> bool:
    """Compatible when either side is silent or a normalised label
    matches — a paper-level measure has no subject label and may pair
    with any material."""
    if not material or not ours.subject_keys:
        return True
    return _subject_key(material) in ours.subject_keys


def _their_interval(value: AlchemValue, scale: float) -> tuple[float, float] | None:
    """The ALCHEM row's extent in its printed unit, the unit's scale
    folded in. ``value`` alone is a point; a lone ``value_max`` is an
    upper bound (``value_min`` a lower one)."""
    v, lo, hi = value.value, value.value_min, value.value_max
    if v is None and lo is None and hi is None:
        return None
    if lo is None and hi is None:
        lo, hi = v, v
    elif lo is None:
        lo = -math.inf
    elif hi is None:
        hi = math.inf
    if lo is None or hi is None:
        return None
    return (float(lo) * scale, float(hi) * scale)


def _mid(iv: tuple[float, float]) -> float:
    lo, hi = iv
    if math.isinf(lo):
        return 0.0 if math.isinf(hi) else hi
    return lo if math.isinf(hi) else (lo + hi) / 2.0


def _rel_gap(a: tuple[float, float], b: tuple[float, float]) -> float:
    """0 when the intervals touch, else the gap over the larger
    midpoint magnitude."""
    gap = max(a[0] - b[1], b[0] - a[1], 0.0)
    if gap == 0.0:
        return 0.0
    return gap / max(abs(_mid(a)), abs(_mid(b)), 1e-300)


def _ref_conflict(their_ref: str | None, our_ref: str | None) -> str | None:
    """An electrode-reference disagreement worth flagging: they assert a
    reference ours lacks or contradicts. An ours-only assertion is kept
    as detail, not classed — ALCHEM's bare 'V' is silence, not a claim."""
    if their_ref is None:
        return None
    if our_ref is None:
        return f"reference {their_ref} unrecorded on ours"
    if _key(their_ref) != _key(our_ref):
        return f"reference {their_ref} vs {our_ref}"
    return None


def _basis_conflict(their_basis: str | None, our_norm: str | None) -> str | None:
    """A normalization-basis disagreement (``mA/cm2_ECSA`` vs a geometric
    area). Either side asserting while the other is silent counts: an
    unrecorded basis is exactly what the diff exists to surface."""
    if their_basis is None and our_norm is None:
        return None
    if their_basis is None:
        return f"basis unrecorded on theirs (ours: {our_norm})"
    if our_norm is None:
        return f"basis {their_basis} unrecorded on ours"
    if _key(their_basis) not in _key(our_norm):
        return f"basis {their_basis} vs normalization {our_norm}"
    return None


@dataclass(frozen=True, slots=True)
class _Cand:
    """One possible pairing, ranked for the greedy pass."""

    rank: int  # 0 numeric, 1 categorical, 2 form clash, 3 unit
    rel: float  # interval gap for rank 0, 0/inf for cat, inf otherwise
    ours: OursValue
    theirs: AlchemValue
    kind: str  # 'num' | 'cat' | 'form' | 'unit'
    note: str = ""


def _conv_interval(conv: Any, iv: tuple[float, float]) -> tuple[float, float]:
    """Convert an interval's finite endpoints; infinities pass through —
    an affine offset on ``-inf`` is not a number pint should see."""
    lo, hi = iv
    return (
        conv.value(lo) if math.isfinite(lo) else lo,
        conv.value(hi) if math.isfinite(hi) else hi,
    )


def _candidate(ours: OursValue, theirs: AlchemValue) -> _Cand:
    """Classify one subject-compatible pair."""
    t_unit = alchem_unit(theirs.unit)
    t_iv = _their_interval(theirs, t_unit.scale)
    o_iv = ours.interval
    if o_iv is not None and t_iv is not None:
        canon = (ours.canonical_unit or "").strip() or None
        if canon is not None and t_unit.text is not None:
            try:
                conv = make_converter(t_unit.text, canon)
            except (BadInput, NeedsMolarMass) as exc:
                return _Cand(
                    3,
                    math.inf,
                    ours,
                    theirs,
                    "unit",
                    note=f"{t_unit.text!r} vs canonical {canon!r}: "
                    f"{str(exc).splitlines()[0]}",
                )
            note = _ref_conflict(t_unit.reference, ours.reference) or (
                _basis_conflict(t_unit.basis or conv.label, ours.normalization)
            )
            if note:
                return _Cand(3, math.inf, ours, theirs, "unit", note=note)
            return _Cand(
                0, _rel_gap(o_iv, _conv_interval(conv, t_iv)), ours, theirs, "num"
            )
        if canon in (None, "1", "count") and t_unit.text is None:
            return _Cand(0, _rel_gap(o_iv, t_iv), ours, theirs, "num")
        note = (
            f"ours has no canonical unit (theirs: {t_unit.text!r})"
            if canon is None
            else f"theirs carries no unit (canonical {canon!r})"
        )
        return _Cand(3, math.inf, ours, theirs, "unit", note=note)
    if (o_iv is None) != (t_iv is None):
        which = "ours" if o_iv is None else "theirs"
        return _Cand(
            2,
            math.inf,
            ours,
            theirs,
            "form",
            note=f"numeric vs non-numeric ({which} non-numeric)",
        )
    ours_text = _key(ours.text)
    theirs_text = _key(theirs.value_text)
    return _Cand(
        1,
        0.0 if ours_text and ours_text == theirs_text else math.inf,
        ours,
        theirs,
        "cat",
    )


def _tier_note(ours: OursValue, theirs: AlchemValue) -> str:
    """An origin/tier mismatch rides along as detail, never a class."""
    expected = {
        "experiment": {"measured", "asserted"},
        "computation": {"computed", "derived"},
    }
    if (
        theirs.origin in expected
        and ours.tier
        and ours.tier not in expected[theirs.origin]
    ):
        return f"origin {theirs.origin} vs tier {ours.tier}"
    return ""


def diff_paper(
    ours: Sequence[OursValue],
    theirs: Sequence[AlchemValue],
    *,
    label: str = "",
    doi: str | None = None,
    rel_tol: float = 0.01,
) -> PaperDiff:
    """Bucket every row of both sides for one paper.

    Pairing needs the same measurand key and a compatible subject. Within
    a group, numeric pairs go first by relative gap, then categorical,
    then form clashes, then unit failures — so a convertible pair is
    never eaten by an unconvertible one. ``rel_tol`` is the match
    ceiling on the relative interval gap (default 1%).
    """
    our_keys: dict[str, str] = {}
    for o in ours:
        for k in o.keys:
            our_keys.setdefault(k, o.measurand_key)

    groups: dict[str, dict[str, list[Any]]] = defaultdict(
        lambda: {"ours": [], "theirs": []}
    )
    for o in ours:
        groups[o.measurand_key]["ours"].append(o)
    unmapped: list[AlchemValue] = []
    for t in theirs:
        keys = [
            ALCHEM_MEASURAND_MAP.get(_key(t.term)) or _key(t.term),
            ALCHEM_MEASURAND_MAP.get(_key(t.prop)) or _key(t.prop),
        ]
        hit = next((our_keys[k] for k in keys if k in our_keys), None)
        if hit is not None:
            groups[hit]["theirs"].append(t)
        else:
            groups[their_key(t)]["theirs"].append(t)
            unmapped.append(t)

    pairs: list[Pair] = []
    for key in sorted(groups):
        g_ours: list[OursValue] = groups[key]["ours"]
        g_theirs: list[AlchemValue] = groups[key]["theirs"]
        ranked = sorted(
            (
                _candidate(o, t)
                for o in g_ours
                for t in g_theirs
                if _subjects_compatible(o, t.material)
            ),
            key=lambda c: (c.rank, c.rel, str(c.ours.id), c.theirs.vid),
        )
        used_o: set[int] = set()
        used_t: set[int] = set()
        for c in ranked:
            if id(c.ours) in used_o or id(c.theirs) in used_t:
                continue
            used_o.add(id(c.ours))
            used_t.add(id(c.theirs))
            note = c.note or _tier_note(c.ours, c.theirs)
            if c.kind == "num":
                kind = "match" if c.rel <= rel_tol else "value"
                if kind == "value":
                    note = note or f"rel gap {c.rel:.3g} > {rel_tol:g}"
            elif c.kind == "cat":
                kind = "match" if c.rel == 0.0 else "value"
                if kind == "value":
                    note = note or "categorical text differs"
            else:
                kind = "value" if c.kind == "form" else "unit"
            pairs.append(Pair(kind, c.ours, c.theirs, note))
        for o in g_ours:
            if id(o) not in used_o:
                pairs.append(Pair("ours_only", o, None))
        for t in g_theirs:
            if id(t) not in used_t:
                pairs.append(Pair("theirs_only", None, t))
    return PaperDiff(label=label, doi=doi, pairs=pairs, unmapped=unmapped)


# ── render ────────────────────────────────────────────────────────────


def _fmt_theirs(t: AlchemValue | None) -> str:
    assert t is not None  # paired rows only
    text = t.value_text or ""
    if not text and t.value is not None:
        text = f"{t.value:g} {t.unit or ''}".strip()
    bits = [t.vid, t.term or t.prop or "?", f"'{text}'"]
    if t.material:
        bits.append(f"on {t.material}")
    if t.locator:
        bits.append(f"@{t.locator}")
    return " ".join(bits)


def _fmt_ours(o: OursValue | None) -> str:
    assert o is not None  # paired rows only
    bits = [f"me{o.id}", o.measurand_key, f"'{o.literal}'"]
    if (
        o.interval is not None
        and math.isfinite(o.interval[0])
        and o.interval[0] == o.interval[1]
    ):
        canon = format_value(
            o.interval[0], canonical_unit=o.canonical_unit, display_unit=None
        )
        bits.append(f"= {canon}")
    if o.subject:
        bits.append(f"on {o.subject}")
    return " ".join(bits)


def render_paper(diff: PaperDiff, *, details: int = 40) -> str:
    """The per-paper block: counts, disagreements, the unmatched tails."""
    c = diff.counts()
    n_pairs = c["match"] + c["value"] + c["unit"]
    n_theirs = n_pairs + c["theirs_only"]
    lines = [f"## {diff.label}" + (f" · {diff.doi}" if diff.doi else "")]
    lines.append(
        f"match {c['match']} · value≠ {c['value']} · unit≠ {c['unit']} · "
        f"ours-only {c['ours_only']} · theirs-only {c['theirs_only']} "
        f"(alchem {n_theirs}, precis {n_pairs + c['ours_only']})"
    )
    shown = 0
    for p in diff.pairs:
        if p.kind in ("value", "unit") and shown < details:
            note = f" — {p.note}" if p.note else ""
            lines.append(
                f"- {p.kind}≠ {_fmt_theirs(p.theirs)} vs {_fmt_ours(p.ours)}{note}"
            )
            shown += 1
    for p in diff.pairs:
        if p.kind == "theirs_only" and shown < details:
            lines.append(f"- theirs-only {_fmt_theirs(p.theirs)}")
            shown += 1
    for p in diff.pairs:
        if p.kind == "ours_only" and shown < details:
            lines.append(f"- ours-only {_fmt_ours(p.ours)}")
            shown += 1
    if diff.unmapped:
        terms = sorted({t.term or t.prop for t in diff.unmapped})
        lines.append("unmapped alchem terms: " + ", ".join(terms))
    return "\n".join(lines)


def render_report(diffs: Sequence[PaperDiff], *, details: int = 40) -> str:
    """The full report: a per-paper summary table, then per-paper
    sections. Agreement, not precision/recall — the ALCHEM set is itself
    one unreviewed model pass."""
    total: Counter[str] = Counter()
    for d in diffs:
        total += d.counts()
    lines = [
        "# ALCHEM agreement diff",
        "",
        "Agreement with the ALCHEM graph (one unreviewed gpt-6.1-sol pass),",
        "not precision/recall — gr606461.",
        "",
        "| paper | doi | match | value≠ | unit≠ | ours-only | theirs-only |",
        "|---|---|---|---|---|---|---|",
    ]
    for d in diffs:
        c = d.counts()
        lines.append(
            f"| {d.label} | {d.doi or ''} | {c['match']} | {c['value']} | "
            f"{c['unit']} | {c['ours_only']} | {c['theirs_only']} |"
        )
    lines.append(
        f"| **total** | | {total['match']} | {total['value']} | "
        f"{total['unit']} | {total['ours_only']} | {total['theirs_only']} |"
    )
    lines.append("")
    for d in diffs:
        lines.append(render_paper(d, details=details))
        lines.append("")
    return "\n".join(lines)


__all__ = [
    "ALCHEM_MEASURAND_MAP",
    "ALCHEM_TRIG_URL",
    "PAIR_KINDS",
    "AlchemPaper",
    "AlchemValue",
    "NormUnit",
    "OursValue",
    "Pair",
    "PaperDiff",
    "alchem_unit",
    "diff_paper",
    "ours_from_measure_row",
    "parse_alchem_trig",
    "render_paper",
    "render_report",
    "their_key",
]
