"""Term coverage — does anything the reviewer signs actually name what the
claim names? Pure text, no DB: the nanopub layer
(:mod:`precis.nanopub.term_coverage`) feeds it passages and a per-hub
acronym map; a draft-side lint (G3) can import it the same way.

The failure it exists for (fi189535): the claim says TEM and STS
measurements; the signed grounding was an abstract plus a definition
sentence, so neither method appears in any grounding passage — and a human
re-check that searched one literal phrase ("tunnelling spectroscopy")
missed that the paper writes "scanning tunnelling microscopy (STM) and
spectroscopy (STS)". So a term counts as covered when the passage names it
*or its expansion*, and the expansion comes from the evidence papers' own
text (:func:`acronym_map`), never a global list.

Three kinds of claim term (:func:`claim_terms`):

* **mode** — an epistemic mode word (:func:`~precis.taproot.sentence_lint.
  find_epistemic_modes`), covered by the stem rules and the generic-head
  semantics of :func:`~precis.taproot.reword._ungrounded_modes`
  ("calculations" is satisfied by any specific method; "molecular
  dynamics" is not satisfied by "simulations").
* **acronym** — an all-caps/digit token (``^[A-Z][A-Z0-9]{1,9}$``, >= 2
  capitals): TEM, STS, DFT, NEGF, B3LYP, HITP. Dropped: roman numerals
  (I/V/X only), a small unit/boilerplate stoplist, and chemical formulas
  (every letter a single-letter element symbol AND a digit present or only
  two letters: CO2, H2O, CH4, NO, HF; mixed-case formulas — TiO2, NaCl —
  never match the all-caps shape). Three-plus-letter all-element tokens
  without a digit (PVC, KOH) stay: an acronym is covered by itself, so the
  cost of keeping a formula is a warning only when the grounding omits the
  formula too.
* **number** — a number-bearing token from
  :func:`~precis.taproot.migrate._number_bearing_tokens`, reduced to its
  numeric core (``5nm`` and ``5 nm`` both -> ``5``; ``10^-6`` kept), with
  ``2D``/``3D`` and ordinals dropped.

Acronym expansion (:func:`acronym_map`) is Schwartz–Hearst style over
``long form (SF)`` and ``SF (long form)``. A strict pass first: each SF
letter is the initial of a distinct word, matched right to left, the last
letter on the word beside the parenthesis, up to
:data:`_MAX_SKIPPED` words skipped between matches. That is what makes
"scanning tunnelling microscopy (STM) and spectroscopy (STS)" yield
STS = *scanning tunnelling … spectroscopy*: the parenthetical (STM) is
blanked, the words "microscopy and" are skipped, and the long form's *key
words* are the ones that supplied a letter (scanning, tunnelling,
spectroscopy). If the strict pass fails, classic SH letter matching runs
and the key words are every non-stopword in the span (a single-word
expansion such as HITP = hexaiminotriphenylene lands here).

A long form matches text when its key words appear in order, stemmed
(:func:`_stem`: ``tunnelling`` = ``tunneling``, ``microscopy`` =
``microscopic``), with at most :data:`_MAX_GAP` other tokens between
neighbours once parentheticals are blanked — stop words and
parentheticals between them are free.

:func:`uncovered_terms` ties it together. An acronym is covered by itself
(case-sensitive, word boundary) or by a long form (case-insensitive,
key words in order). A long-form phrase *in the claim* found through the
map is an acronym term too (covered by the long form or the acronym), and
the mode words inside it are subsumed rather than reported twice.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from precis.taproot.migrate import _STOPWORDS, _number_bearing_tokens
from precis.taproot.reword import _mode_probes, _ungrounded_modes
from precis.taproot.sentence_lint import GENERIC_EPISTEMIC_HEADS, find_epistemic_modes

__all__ = [
    "AcronymMap",
    "LongForm",
    "Prepared",
    "Term",
    "acronym_map",
    "claim_terms",
    "count_term",
    "find_term",
    "prepare",
    "uncovered_terms",
]

#: Term kinds, in report order.
KIND_ACRONYM = "acronym"
KIND_MODE = "mode"

#: Mode tokens that pass the claim-sentence lint (a mathematical claim's way
#: of knowing, sentence_lint 2026-10-03) but name no method a passage must
#: carry: a "theorem" or "proof" claim is not a measurement, so these never
#: become coverage terms, method-gap searches or a method-claim prefill order.
NON_METHOD_MODES = frozenset({"proof", "proofs", "theorem"})
KIND_NUMBER = "number"
KINDS = (KIND_ACRONYM, KIND_MODE, KIND_NUMBER)

#: Other tokens allowed between two consecutive key words of a long form.
#: 3 admits "scanning tunnelling microscopy and spectroscopy" for STS.
_MAX_GAP = 3

#: Words a strict SH match may skip between matched initials.
_MAX_SKIPPED = 3

_TAG_RE = re.compile(r"<[^>]+>")
_POSSESSIVE_RE = re.compile(r"['’]s\b")
_PAREN_RE = re.compile(r"\(([^()]{2,80})\)")
_BLANK_PAREN_RE = re.compile(r"\([^()]*\)")
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
_ACRONYM_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z][A-Z0-9]{1,9})(?![A-Za-z0-9])")
_SF_RE = re.compile(r"^[A-Z][A-Z0-9]{1,9}s?$")
_SENT_END_RE = re.compile(r"[.;:!?]\s")

_ROMAN_RE = re.compile(r"^[IVX]+$")

#: Units and boilerplate that are all-caps in running text but name no
#: method. Deliberately short: a method acronym wrongly listed here is
#: silently never checked.
_NOT_ACRONYMS = frozenset(
    {
        "EV", "KV", "MV", "NM", "PM", "MM", "CM", "KG", "MG", "HZ", "KHZ",
        "MHZ", "GHZ", "THZ", "GPA", "MPA", "KPA", "PPM", "PPB", "RPM",
        "MOL", "UM", "AU", "SI", "US", "UK", "USA", "EU", "DOI", "PDF",
        "FIG", "TABLE", "AND", "OR", "NOT", "THE",
    }
)  # fmt: skip

#: Single-letter element symbols — an all-caps token spelled only from
#: these (with a digit, or two letters long) is a formula, not an acronym.
_ONE_LETTER_ELEMENTS = frozenset("BCFHIKNOPSUVWY")


@dataclass(frozen=True, slots=True)
class Term:
    """One thing a claim names. ``expansion`` is the long form for an
    acronym the evidence papers define (display only)."""

    kind: str
    text: str
    expansion: str | None = None


@dataclass(frozen=True, slots=True)
class LongForm:
    """One expansion of an acronym: ``words`` as written (display),
    ``keys`` the stemmed words that supplied an SF letter (matching)."""

    words: str
    keys: tuple[str, ...]


#: acronym -> its long forms in the evidence papers' own text.
AcronymMap = dict[str, tuple[LongForm, ...]]


# --------------------------------------------------------------------- text


def _clean(text: str) -> str:
    """Drop markup tags (``sp<sup>3</sup>`` -> ``sp3``) and possessives."""
    # "$" is LaTeX math fencing ("$6\\mu_B$"): a space lets the number inside it
    # tokenise like any other.
    return _POSSESSIVE_RE.sub("", _TAG_RE.sub("", (text or "").replace("$", " ")))


_STEM_SUFFIXES = ("ically", "ical", "ies", "ing", "ic", "ed", "es", "ly", "y", "s")


def _stem(word: str) -> str:
    """Lowercase + a light suffix strip so spelling variants meet:
    tunnelling/tunneling/tunnels -> ``tunnel``; microscopy/microscopic ->
    ``microscop``. Never strips below 5 letters, so short words stay whole."""
    w = word.lower().replace("ll", "l")
    for suffix in _STEM_SUFFIXES:
        if w.endswith(suffix) and len(w) - len(suffix) >= 5:
            return w[: -len(suffix)]
    return w


@dataclass(frozen=True, slots=True)
class _Tok:
    stem: str
    start: int


class Prepared:
    """One passage, cleaned once: the text, its tokens (parentheticals
    blanked so they never count as a gap) and its number set."""

    __slots__ = ("numbers", "text", "toks")

    def __init__(self, raw: str) -> None:
        self.text = _clean(raw)
        blanked = _BLANK_PAREN_RE.sub(lambda m: " " * len(m.group(0)), self.text)
        self.toks = [
            _Tok(_stem(m.group(0)), m.start()) for m in _TOKEN_RE.finditer(blanked)
        ]
        self.numbers = _numbers(self.text)


#: Chirality / index pairs "(10,0)" are one measurement-like unit, not two
#: numbers; "[2+2]" is a cycloaddition label, not a quantity (2026-10-04
#: prod pass: both were pure noise as bare numbers).
_PAIR_RE = re.compile(r"\(\s*(\d+)\s*,\s*(\d+)\s*\)")
_SUM_LABEL_RE = re.compile(r"\[\s*\d+\s*\+\s*\d+\s*\]")


def _number_terms(text: str) -> list[str]:
    """Number terms of ``text``, in order, deduped: the numeric core of each
    migrate number token, plus each ``(n,m)`` pair as ``"(n,m)"``."""
    pairs = [f"({a},{b})" for a, b in _PAIR_RE.findall(text)]
    rest = _SUM_LABEL_RE.sub(" ", _PAIR_RE.sub(" ", text))
    cores = [n for n in map(_number_core, _number_bearing_tokens(rest)) if n]
    return list(dict.fromkeys(cores + pairs))


def _numbers(text: str) -> frozenset[str]:
    return frozenset(_number_terms(text))


_NUMBER_CORE_RE = re.compile(r"^[~±]?(\d+(?:\.\d+)?(?:\^-?\d+)?)")
_NUMBER_SKIP_RE = re.compile(r"^\d+(?:d|st|nd|rd|th)$", re.IGNORECASE)


def _number_core(token: str) -> str | None:
    """The numeric core of one migrate number token: units glued on
    (``5nm``, ``50%``, ``1960s``) fall away so ``5nm`` meets ``5 nm``;
    ``2D``/``3D`` and ordinals are not measurements."""
    if _NUMBER_SKIP_RE.match(token):
        return None
    m = _NUMBER_CORE_RE.match(token)
    if m is None:
        return None
    # 6.0 and 6 are the same reading.
    return re.sub(r"^(\d+)\.0+$", r"\1", m.group(1))


def _find_phrase(
    keys: tuple[str, ...], toks: Sequence[_Tok], *, lenient: bool = False
) -> int | None:
    """Char offset of the first place ``keys`` occur in order with at most
    :data:`_MAX_GAP` tokens between neighbours; ``None`` if nowhere.
    ``lenient`` also accepts the phrase without its first key when the long
    form has three or more ("tunnelling spectroscopy" for *scanning
    tunnelling spectroscopy*) — a passage-side rule only, never for finding
    a long form in the claim, where it would read "electron microscopy" as
    both TEM and SEM."""
    if lenient and len(keys) >= 3:
        pos = _find_phrase(keys, toks)
        return pos if pos is not None else _find_phrase(keys[1:], toks)
    first = keys[0]
    for i, tok in enumerate(toks):
        if tok.stem != first:
            continue
        j = i
        for key in keys[1:]:
            window = toks[j + 1 : j + 2 + _MAX_GAP]
            hit = next((k for k, t in enumerate(window) if t.stem == key), None)
            if hit is None:
                break
            j += 1 + hit
        else:
            return tok.start
    return None


def _acronym_pos(acronym: str, text: str) -> int | None:
    m = re.search(r"(?<![A-Za-z0-9])" + re.escape(acronym) + r"s?(?![A-Za-z0-9])", text)
    return m.start() if m else None


# --------------------------------------------------------------- claim terms


def _is_acronym(token: str) -> bool:
    if sum(c.isupper() for c in token) < 2 or token in _NOT_ACRONYMS:
        return False
    if _ROMAN_RE.match(token):
        return False
    letters = [c for c in token if c.isalpha()]
    if all(c in _ONE_LETTER_ELEMENTS for c in letters) and (
        any(c.isdigit() for c in token) or len(token) == 2
    ):
        return False
    return True


def claim_terms(sentence: str) -> list[Term]:
    """Every mode word, acronym and number the sentence names, each kind
    in first-occurrence order, deduped. Long-form phrases need the
    evidence's acronym map and are added by :func:`uncovered_terms`."""
    text = _clean(sentence)
    terms: list[Term] = [
        Term(KIND_ACRONYM, tok)
        for tok in dict.fromkeys(m.group(1) for m in _ACRONYM_RE.finditer(text))
        if _is_acronym(tok)
    ]
    terms += [
        Term(KIND_MODE, m)
        for m in find_epistemic_modes(text)
        if m.lower() not in NON_METHOD_MODES
    ]
    terms += [Term(KIND_NUMBER, n) for n in _number_terms(text)]
    return terms


# ------------------------------------------------------------- acronym map


def _words(text: str) -> list[str]:
    return _TOKEN_RE.findall(_POSSESSIVE_RE.sub("", text))


def _strict_match(sf: str, words: list[str]) -> list[int] | None:
    """Right-to-left initials match: the indices of the words that supplied
    a letter, in order, or ``None``. The last SF letter must sit on the
    last word (the one beside the parenthesis); at most
    :data:`_MAX_SKIPPED` words are skipped between matches."""
    letters = [c.lower() for c in sf if c.isalnum()]
    if len(letters) < 2 or len(words) < 1:
        return None
    used: list[int] = []
    idx = len(words) - 1
    for n, ch in enumerate(reversed(letters)):
        start = idx
        while idx >= 0 and words[idx][0].lower() != ch:
            idx -= 1
        if idx < 0 or (n == 0 and idx != len(words) - 1) or start - idx > _MAX_SKIPPED:
            return None
        used.append(idx)
        idx -= 1
    used.reverse()
    return used


def _sh_span(sf: str, candidate: str) -> int | None:
    """Classic Schwartz–Hearst: start offset in ``candidate`` of the long
    form whose letters contain ``sf`` in order (first letter at a word
    start), or ``None``."""
    s, ell = len(sf) - 1, len(candidate) - 1
    while s >= 0:
        c = sf[s].lower()
        if not c.isalnum():
            s -= 1
            continue
        while ell >= 0 and (
            candidate[ell].lower() != c
            or (s == 0 and ell > 0 and candidate[ell - 1].isalnum())
        ):
            ell -= 1
        if ell < 0:
            return None
        ell -= 1
        s -= 1
    return candidate.rfind(" ", 0, ell + 1) + 1


def _long_form(sf: str, words: list[str]) -> LongForm | None:
    """The expansion of ``sf`` ending at the last of ``words`` (strict
    initials first, classic SH as the fallback), or ``None``."""
    strict = _strict_match(sf, words)
    if strict is not None:
        used = strict
        return LongForm(
            words=" ".join(words[used[0] :]),
            keys=tuple(_stem(words[i]) for i in used),
        )
    candidate = " ".join(words)
    start = _sh_span(sf, candidate)
    if start is None:
        return None
    span = candidate[start:].split()
    if not span or len(span) > len(sf) + 2:
        return None
    keys = tuple(_stem(w) for w in span if w.lower() not in _STOPWORDS)
    if not keys:
        return None
    return LongForm(words=" ".join(span), keys=keys)


def _window(before: str, sf: str) -> list[str]:
    """The words a long form for ``sf`` can come from: the stretch before
    the parenthesis, back to the last sentence end, other parentheticals
    blanked, capped at ``min(len(sf) + 5, 2 * len(sf))`` words."""
    before = _BLANK_PAREN_RE.sub(" ", before)
    cuts = list(_SENT_END_RE.finditer(before))
    if cuts:
        before = before[cuts[-1].end() :]
    words = _words(before)
    return words[-min(len(sf) + 5, 2 * len(sf)) :]


def acronym_map(texts: Sequence[str]) -> AcronymMap:
    """Per-hub acronym -> long forms, read off ``long form (SF)`` and
    ``SF (long form)`` in the evidence papers' own text. No global list:
    a paper that never defines an acronym contributes nothing for it."""
    found: dict[str, dict[str, LongForm]] = {}
    for raw in texts:
        text = _clean(raw)
        for m in _PAREN_RE.finditer(text):
            inner = m.group(1).strip()
            if _SF_RE.match(inner) and sum(c.isupper() for c in inner) >= 2:
                sf = inner.rstrip("s") if inner[-1] == "s" else inner
                lf = _long_form(sf, _window(text[: m.start()], sf))
            else:
                prior = _words(text[: m.start()][-40:])
                sf = prior[-1] if prior else ""
                if not (_SF_RE.match(sf) and sum(c.isupper() for c in sf) >= 2):
                    continue
                sf = sf.rstrip("s") if sf[-1] == "s" else sf
                inner_words = _words(inner)
                if len(inner_words) < 2:
                    continue
                lf = _long_form(sf, inner_words)
            if lf is not None:
                found.setdefault(sf, {}).setdefault(lf.words.lower(), lf)
    return {sf: tuple(forms.values()) for sf, forms in found.items()}


# ----------------------------------------------------------------- coverage


def _long_form_usable(lf: LongForm) -> bool:
    """One-key long forms match only when the key is distinctive (a long
    word): "hexaiminotriphenylene" yes, "theory" no."""
    return len(lf.keys) >= 2 or len(lf.keys[0]) >= 8


def _claim_long_forms(
    claim: Prepared, amap: AcronymMap, skip: set[str]
) -> list[tuple[str, LongForm]]:
    out: list[tuple[str, LongForm]] = []
    for acronym, forms in amap.items():
        if acronym in skip:
            continue
        for lf in forms:
            if _long_form_usable(lf) and _find_phrase(lf.keys, claim.toks) is not None:
                out.append((acronym, lf))
                break
    return out


def _acronym_pos_in(acronym: str, passage: Prepared, amap: AcronymMap) -> int | None:
    pos = _acronym_pos(acronym, passage.text)
    if pos is not None:
        return pos
    for lf in amap.get(acronym, ()):
        if _long_form_usable(lf):
            pos = _find_phrase(lf.keys, passage.toks, lenient=True)
            if pos is not None:
                return pos
    return None


def _mode_pos(token: str, passage: Prepared) -> int | None:
    haystack = passage.text.lower()
    for probe in _mode_probes(token):
        m = re.search(r"\b" + re.escape(probe), haystack)
        if m:
            return m.start()
    return None


def prepare(text: str) -> Prepared:
    """Tokenise ``text`` once for repeated :func:`find_term` calls."""
    return Prepared(text)


def find_term(term: Term, text: str | Prepared, amap: AcronymMap) -> int | None:
    """Offset (into the markup-stripped ``text``) of the first literal
    carrier of ``term`` — the acronym or one of its long forms; the mode
    stem; the number — or ``None``. Unlike :func:`uncovered_terms`, a
    generic mode is *not* satisfied by an unrelated specific method: this
    answers "which chunk says it", for suggestions."""
    passage = text if isinstance(text, Prepared) else Prepared(text)
    if term.kind == KIND_ACRONYM:
        return _acronym_pos_in(term.text, passage, amap)
    if term.kind == KIND_MODE:
        return _mode_pos(term.text, passage)
    if term.text in passage.numbers:
        m = re.search(r"(?<![\d.])" + re.escape(term.text) + r"(?![\d])", passage.text)
        return m.start() if m else 0
    return None


def count_term(term: Term, text: str | Prepared, amap: AcronymMap) -> int:
    """How often ``text`` carries ``term`` literally (a long-form-only hit
    counts once) — the density tie-break for suggestions."""
    passage = text if isinstance(text, Prepared) else Prepared(text)
    if find_term(term, passage, amap) is None:
        return 0
    if term.kind == KIND_ACRONYM:
        pattern = r"(?<![A-Za-z0-9])" + re.escape(term.text) + r"s?(?![A-Za-z0-9])"
        return max(1, len(re.findall(pattern, passage.text)))
    if term.kind == KIND_MODE:
        probe = _mode_probes(term.text)[0]
        return max(1, len(re.findall(r"\b" + re.escape(probe), passage.text.lower())))
    return max(1, len(re.findall(re.escape(term.text), passage.text)))


def uncovered_terms(
    sentence: str, passages: Sequence[str], amap: AcronymMap
) -> list[Term]:
    """Terms ``sentence`` names that no passage carries. ``[]`` when there
    are no passages (nothing to compare against — a hanging claim has its
    own flag)."""
    texts = [p for p in passages if p and p.strip()]
    if not texts:
        return []
    prepared = [Prepared(t) for t in texts]
    claim = Prepared(sentence)
    base = claim_terms(sentence)
    literal_terms = [t for t in base if t.kind == KIND_ACRONYM]
    literal = {t.text for t in literal_terms}

    # Acronyms the claim spells out: found through the map's long forms.
    spelled = _claim_long_forms(claim, amap, literal)
    subsumed = {key for _, lf in spelled for key in lf.keys}
    terms = list(literal_terms)
    for acronym, lf in spelled:
        terms.append(Term(KIND_ACRONYM, acronym, lf.words))
    terms = [
        Term(t.kind, t.text, t.expansion or _expansion(t.text, amap))
        if t.kind == KIND_ACRONYM
        else t
        for t in terms
    ]
    acronym_texts = {t.text for t in terms}
    in_passages = {
        a for a in amap if any(_acronym_pos(a, p.text) is not None for p in prepared)
    }
    uncovered_modes = _ungrounded_modes(claim.text, [p.text for p in prepared])
    rest = [t for t in base if t.kind != KIND_ACRONYM]

    out: list[Term] = []
    for t in terms:
        if not any(_acronym_pos_in(t.text, p, amap) is not None for p in prepared):
            out.append(t)
    for t in rest:
        if t.kind == KIND_MODE:
            # TEM/DFT are mode tokens AND acronyms: reported once, as the acronym.
            if t.text not in uncovered_modes or t.text in acronym_texts:
                continue
            words = [w for w in re.split(r"[\s-]+", t.text) if w]
            stem = _stem(words[-1])
            if stem in subsumed or any(
                stem in lf.keys for a in in_passages for lf in amap[a]
            ):
                continue
            # "transmission electron microscopy" is covered by a passage that
            # writes its initialism (TEM) even where no paper defines it.
            initialism = "".join(w[0].upper() for w in words)
            if len(words) >= 2 and any(
                _acronym_pos(initialism, p.text) is not None for p in prepared
            ):
                continue
            out.append(t)
        elif not any(t.text in p.numbers for p in prepared):
            out.append(t)
    if any(t.kind == KIND_ACRONYM for t in out):
        # A named method is missing: the generic head beside it ("simulations"
        # next to MD) is the same gap said twice — report the specific one.
        out = [
            t
            for t in out
            if not (t.kind == KIND_MODE and t.text.lower() in GENERIC_EPISTEMIC_HEADS)
        ]
    return out


def _expansion(acronym: str, amap: AcronymMap) -> str | None:
    forms = amap.get(acronym)
    return forms[0].words if forms else None
