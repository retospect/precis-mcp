"""Advisory admissibility/grammar lint for taproot claim sentences and scope.

The 2026-08-19 corpus audit (`docs/backlog/nanopub-corpus-remediation.md`)
found the 1,527-hub claim corpus broadly unpublishable in ways
`taproot/notation.py::lint_notation` does not touch: bibliography stubs
minted as claims, "the same group"-style dangling references, sentences
joining two assertions, 506-character titles. That doc's Phase 0 defines
the standard ("claim admissibility": falsifiable, self-contained,
method-attributed, single-assertion); this module is Phase 1 -- the
machine-checkable form of it.

:func:`lint_claim_sentence` and :func:`lint_scope` share
:func:`lint_notation`'s contract: advisory only, never raise, never
rewrite, return human-readable warnings naming the offending text. They
flag for judgment; nothing here blocks a write. (Per the remediation
doc's "enforcement asymmetry": lint *advises* at mint, a caller may
choose to *block* at approve -- that gate lives outside this module.)

`lint_scope` encodes the doc's Phase 3/"Why dedup never fired" finding:
`pub_id = hash(sentence, scope)`, and free-text `scope` values
manufacture spurious non-convergence (372 distinct values across 1,527
hubs, 74% `{}`, two byte-identical title pairs differing only by prose
drift inside `scope`). The fix taken there -- keep `scope` in the
identity hash, add a controlled vocabulary -- is what
:data:`SCOPE_KEYS` encodes.

**`not-falsifiable` includes a `verbless` detector, folded into the same
code rather than a fifth, separate one.** A prod-corpus run (2026-08-19,
1,524 live hubs) found `not-falsifiable`'s four label/copula/bibliography/
study-happened shapes under-catch: a colon-label whose right side is bare
measurements, a topic label with no structural marker at all, and a
label-style em-dash respelled as an ASCII hyphen all slipped through. The
one signal unifying all three misses: no finite verb -- a claim asserts
something, an assertion needs a verb. :func:`_has_finite_verb` is a
POS-tagger-free approximation (closed aux/copula set, the existing verb
whitelists, then a `word ending in s/ed/es, not a known plural noun or
attributive participle` fallback) -- false-negative-biased on purpose,
since wrongly marking a real claim non-falsifiable feeds an untag pass.
Folded into `not-falsifiable` rather than a new code so an existing
caller acting on that code (e.g. an untag pass) needs no change to pick
up the wider recall.

**Tense (`past-passive` blocking; `past-tense` / `present-perfect`
advisory), decided 2026-08-19.** A claim hub is a standing assertion, so
tense is a claim about how the statement relates to time, not a style
preference. Simple present is the default, for both the evidence verb and
the asserted content, and is never flagged. Simple past is correct only
when the claim's subject is itself a historical event ("Haber's 1927
gold-from-seawater program failed because...") -- machine-undecidable
(the test is "does rewriting to present make the sentence false or
absurd", which needs judgment), so `past-tense` is advisory. Present
perfect is allowed only for existence/achievement claims ("room-
temperature coherence has been demonstrated in...") -- elsewhere it hides
the agent and the conditions -- also machine-undecidable, so
`present-perfect` is advisory too. Past passive with no result stated is
banned outright ("...was proposed by Kirkpatrick et al.", "Surface
interactions...were investigated"): these are history-of-science or
activity reports, not claims, so `past-passive` is blocking (see
`gates._BLOCKING_LINT_CODES`). `past-passive` is the most specific of the
three: a sentence it matches never also emits `past-tense` (checked in
that order below). `present-perfect`'s `has/have (been) VERBed` shape is
structurally disjoint from `past-passive`'s `was/were VERBed` by
construction (different literal auxiliary), never by exclusion logic, so
the two never collide on the same match. Both the passive and
perfect/past detectors reuse `_VERB_SHAPE_EXCEPTIONS` (an attributive
participle -- "the laminated card" -- must never read as a passive main
verb) and skip any participle that itself matches `_EVIDENCE_VERB_RE`
(shows/measures/observes/demonstrates/... -- a passive clause naming a
controlled evidence verb already carries a finding, so it reads as
evidence stated passively, not a bare "a study happened" report; corpus
check, 2026-08-19: this is what separates "were investigated" (bans) from
"were observed with a discrete set of opening angles of ~19°..." (does
not) among the was/were+participle candidates in the live corpus).

**`em-dash` is a separate code from `not-falsifiable`, and the two are
allowed -- expected -- to fire together on the same sentence.**
`_LABEL_STYLE_RE` (above) already treats a *leading* `Label — topic`
shape as evidence of "this is a citation stub, not a claim"; it matches
the em dash (and its ASCII stand-ins) only incidentally, as one of three
interchangeable spellings of that leading separator, and only when the
label is short and at the very start of the sentence. `em-dash` is a
different, notation-level rule (canon table, "Em-dash is never a claim
separator"): the punctuation itself is banned as a clause/label
separator anywhere in the sentence, independent of position or of
whether the sentence also happens to read as a bibliography stub. A
sentence can fail one without the other -- a mid-sentence em-dash past
the 60-char label window trips only `em-dash`; a leading `Surname 19xx:
topic` colon-label trips only `not-falsifiable`. When a sentence does
match both, that is not disagreement between the rules, it is two true,
independently actionable facts about the same string (one grammatical:
rewrite as an assertion; one mechanical: swap the dash for a comma or an
en dash) -- suppressing either would hide a real, corpus-evidenced
failure the skill doc calls "the single most reliable syntactic marker"
of the bibliography-stub failure mode. The en dash `–` (U+2013, a
different code point from em dash `—` U+2014) is never matched by
`em-dash` -- it has 102 legitimate uses in this corpus (ranges, compound
method/element names) that must never trip it.

**`mixed-point-range` (2026-08-20 numeric-value policy) is the content
counterpart to `notation.py`'s form checks.** The policy: prefer a range
over a bare point wherever the source supports a spread, and if the
source designates a typical value, use typical-plus-range
(`≈9 GPa across a reported 9-12 GPa`) -- never a bare midpoint, since a
midpoint is arithmetic, not a measurement. This module has no access to
the source passage, so it can only ever flag the *shape* (a bare point
and a range sharing a unit, with no typical-value marker nearby) --
never confirm whether the point is source-designated or invented.
Deliberately advisory-only and deliberately conservative for that reason
-- a false positive here is worse than a miss, so a marker word anywhere
in a short leading window suppresses the warning, and every range's own
span is excluded from the point search so two side-by-side ranges (two
different regimes) never cross-fire.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["EPISTEMIC_MODE_TOKENS", "SCOPE_KEYS", "lint_claim_sentence", "lint_scope"]

#: Method/instrument/technique tokens that count as an epistemic mode.
#: Seeded from the remediation doc's Phase 1 list -- deliberately a plain
#: module-level set, not a taxonomy, so adding a technique is a one-line
#: edit.
EPISTEMIC_MODE_TOKENS: frozenset[str] = frozenset(
    {
        "DFT",
        "spin-polarized DFT",
        "spin-polarised DFT",
        "molecular dynamics",
        "MD",
        "NEGF",
        "TEM",
        "SEM",
        "STM",
        "AFM",
        "c-AFM",
        "Raman",
        "XRD",
        "XPS",
        "NMR",
        "nanoindentation",
        "first-principles",
        "first principles",
        "Monte Carlo",
        # 2026-08-23 growth, corpus-dry-run vetted (strict cohort, n=1,267;
        # every flipped sentence eyeballed per
        # docs/conventions/corpus-normalization.md). First: spelled-out
        # forms of the seeded acronyms -- `\bDFT\b` missed "Density
        # functional theory predicts ..." on 21 exemplary house-grammar
        # hubs, the single most embarrassing gap.
        "density functional theory",
        "density-functional theory",
        "X-ray diffraction",
        "X-ray photoelectron spectroscopy",
        "transmission electron microscopy",
        "scanning electron microscopy",
        "scanning tunneling microscopy",
        "scanning tunnelling microscopy",
        "atomic force microscopy",
        "nuclear magnetic resonance",
        # Techniques the corpus names that the seed list lacked. Vetted
        # candidates deliberately NOT added: `FRET` and bare `fluorescence`
        # (the corpus uses both as the phenomenon *claimed*, not the way of
        # knowing -- only the phrase form "fluorescence measurements" names
        # a mode); `SQUID`/`BET`/`SIMS`/`EDS` (case-insensitive matches on
        # ordinary words or bibliography "Eds."); zero-yield speculative
        # entries (DMFT, EPR, ellipsometry, ...) stay out until the corpus
        # shows them.
        "DFTB",
        "ab initio",
        "device simulation",
        "device simulations",
        "SAXS",
        "cryo-EM",
        "FTIR",
        "UV-vis",
        "photoluminescence",
        "voltammetry",
        "impedance spectroscopy",
        "X-ray absorption spectroscopy",
        "mass spectrometry",
        "force spectroscopy",
        "electrical measurements",
        "transport measurements",
        "fluorescence measurements",
        "Rosetta",
        "UV/vis",
        # Generic way-of-knowing head nouns (2026-08-23 rewrite pilot).
        # A 50-hub Opus rewrite pilot produced honestly method-attributed
        # house-grammar sentences -- "Thermal-conductance measurements on
        # single-molecule junctions find ...", "A randomized double-blind
        # trial ... finds ..." -- that this closed set still rejected: no
        # enumeration of *specific* technique spellings can cover the
        # space of legitimate mode phrases. The heads are themselves a
        # small closed set; the qualifier ("current-voltage",
        # "thermal-conductance") rides free. Corpus dry run: content-use
        # false passes (e.g. "bridging tile theory to ...") all stay
        # blocked by other codes, so none reaches lint-clean wrongly.
        "measurement",
        "measurements",
        "simulation",
        "simulations",
        "calculation",
        "calculations",
        "spectroscopy",
        "microscopy",
        "experiments",
        "analysis",
        "theory",
        "trial",
        "trials",
        "imaging",
        "assay",
        "assays",
        "modelling",
        "modeling",
    }
)

_EPISTEMIC_MODE_RE = re.compile(
    r"\b(?:"
    + "|".join(
        re.escape(t) for t in sorted(EPISTEMIC_MODE_TOKENS, key=len, reverse=True)
    )
    + r")\b",
    re.IGNORECASE,
)

#: Controlled evidence verbs (canon: "claim admissibility", test
#: `no-evidence-verb`) -- inflections included, matched case-insensitively.
_EVIDENCE_VERB_RE = re.compile(
    r"\b(?:predicts?|predicted|predicting|"
    r"finds?|found|"
    r"shows?|showed|shown|"
    r"measures?|measured|measuring|"
    r"observes?|observed|observing|"
    r"demonstrates?|demonstrated|demonstrating|"
    # 2026-08-23 growth, corpus-dry-run vetted alongside
    # EPISTEMIC_MODE_TOKENS (same eyeball pass). `computed` is participle-
    # only on purpose: `computes`/`computing` match domain nouns and
    # content verbs ("reservoir computing", "logic gates compute...") --
    # 15 of 19 corpus flips were wrong. `estimates?` also matching the
    # noun ("a first-order estimate") is accepted: the noun still names
    # the epistemic act. Rejected: `detects` (zero corpus yield),
    # `characterizes` (active-voice study-happened -- "A characterizes B"
    # asserts no finding), `reports`/`reported` (would exempt "was
    # reported by ..." from past-passive, the history-of-science shape
    # that code exists to block).
    r"calculates?|calculated|calculating|"
    r"computed|"
    r"estimates?|estimated|estimating|"
    r"reveals?|revealed|revealing|"
    r"confirms?|confirmed|confirming|"
    r"identif(?:y|ies|ied|ying)|"
    r"indicates?|indicated|indicating)\b",
    re.IGNORECASE,
)

# ── not-falsifiable: five independent shapes, one code ───────────────────

#: A short title-case label followed by a colon, or a dash used as a
#: label separator, at the start of the sentence -- `Meir & Wingreen 1992
#: — Landauer formula...`. Corpus text uses `-`, `--`, and `—`/`–`
#: interchangeably for this (`Landauer 1957/1970 - conductance as
#: transmission`), so the dash form matches all three: `\s(?:-{1,2}|[—–])\s`
#: (spaces required, so it never fires on an in-word hyphen like
#: `chloride-medium`).
_LABEL_STYLE_RE = re.compile(r"^[A-Z][\w.&' -]{2,60}(?::\s|\s(?:-{1,2}|[—–])\s)")

# ── em-dash: dash used as a clause/label separator, anywhere ─────────────

#: Em dash `—` (U+2014), and its ASCII stand-ins used the same way, as a
#: clause/label separator -- `Landauer 1957/1970 — conductance as
#: transmission`, `Yoon & Guo 2007 -- NEGF upper bounds ...`. Corpus
#: evidence (2026-08-19, 1,524 live hubs): 90 hubs contain an em dash and
#: **all 90** use the spaced ` — ` form in this separator role -- no
#: legitimate parenthetical use exists in this corpus, so the em dash
#: fires whether spaced or not (there is nothing correct to protect). The
#: ASCII stand-ins ` -- ` / ` - ` only fire spaced, mirroring
#: `_LABEL_STYLE_RE`'s existing guard, so an in-word hyphen
#: (`chloride-medium`, `sub-10-nm`, `UiO-66`) never trips it. The en dash
#: `–` (U+2013 -- a distinct code point from em dash `—` U+2014) is never
#: matched: it has 102 legitimate uses in this corpus (42 numeric ranges,
#: the rest compound method/element names like `DFT–NEGF`, `Cu–Zn`) that
#: must never trip this code.
_EM_DASH_SEPARATOR_RE = re.compile(r"—|\s-{1,2}\s")

#: Leading `Surname & Surname 19xx`/`20xx` -- a bibliography entry, not a
#: claim.
_BIBLIOGRAPHY_LEAD_RE = re.compile(r"^[A-Z][a-z]+ (?:&|and) [A-Z][a-z]+ (?:19|20)\d\d")

#: Copula definition -- `X is a software suite for...`, `X is a
#: technique...`. Curated noun list, not a general "is a" match (that
#: would false-positive on ordinary claims like "graphene is a
#: two-dimensional material with...").
_COPULA_DEFINITION_RE = re.compile(
    r"\bis an? (?:software suite|technique|method|methodology|tool|"
    r"framework|algorithm|protocol|program|package|library|approach|"
    r"model|platform|database)\b",
    re.IGNORECASE,
)

#: "X was/were investigated" -- states a study happened, asserts no
#: finding.
_STUDY_HAPPENED_RE = re.compile(
    r"\b(?:was|were)\s+(?:investigated|studied|examined|"
    r"characteri[sz]ed|explored)\b",
    re.IGNORECASE,
)

# ── verbless (folded into not-falsifiable) ────────────────────────────────

#: Closed set of copulas/auxiliaries/modals -- any hit is unambiguous verb
#: evidence regardless of shape.
_AUX_COPULA_TOKENS: frozenset[str] = frozenset(
    {
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "has",
        "have",
        "had",
        "does",
        "do",
        "did",
        "can",
        "could",
        "will",
        "would",
        "may",
        "might",
        "shall",
        "should",
        "must",
    }
)

#: An acronym plural (`MOSFETs`, `DFTs`, `TEMs`) -- `\w+s\b`-shaped but not
#: a verb; excluded before the generic shape fallback runs.
_ACRONYM_PLURAL_RE = re.compile(r"^[A-Z]{2,}(?:s|ed|es)$")

#: Generic verb-inflection shape -- the fallback signal, only consulted
#: once the closed set and the two curated verb whitelists (below) have
#: already missed. `\w+(s|ed|es)` also matches most English plural nouns
#: and past-participle-as-adjective forms, so it is gated by
#: `_VERB_SHAPE_EXCEPTIONS`.
_VERB_SHAPE_RE = re.compile(r"^[A-Za-z]+(?:s|ed|es)$")

#: Words that end in `s`/`ed`/`es` but are not, in this corpus, ever the
#: sentence's main verb -- plural nouns (`applications`, `results`, ...)
#: and attributive past-participles used as adjectives (`laminated card`,
#: not "X laminated Y"). Corpus-specific and deliberately extend-as-needed
#: (per `docs/backlog/nanopub-corpus-remediation.md`'s prod lint run);
#: false-negative-biased -- an omission here just means a truly verbless
#: sentence slips through, never a real claim wrongly flagged.
_VERB_SHAPE_EXCEPTIONS: frozenset[str] = frozenset(
    {
        # plural nouns
        "applications",
        "properties",
        "materials",
        "results",
        "rules",
        "studies",
        "devices",
        "films",
        "cells",
        "electrodes",
        "series",
        "analysis",
        "measurements",
        "concentrations",
        "conditions",
        "electrons",
        "ions",
        "nanotubes",
        "cycles",
        "volts",
        "atoms",
        "molecules",
        "particles",
        "layers",
        "sensors",
        "surfaces",
        "processes",
        "structures",
        "systems",
        "networks",
        "compounds",
        "reactions",
        "solutions",
        "samples",
        "values",
        "parameters",
        "mechanisms",
        "defects",
        "states",
        "modes",
        "bands",
        "gaps",
        "peaks",
        "spectra",
        "images",
        "regimes",
        "substrates",
        "composites",
        "coatings",
        "membranes",
        "capacitors",
        "resistors",
        "circuits",
        "components",
        "units",
        "dimensions",
        "sizes",
        "shapes",
        "forces",
        "currents",
        "voltages",
        "energies",
        "frequencies",
        "wavelengths",
        "temperatures",
        "pressures",
        "densities",
        "ratios",
        "rates",
        "yields",
        "capacities",
        "efficiencies",
        "domains",
        "boundaries",
        "interfaces",
        "junctions",
        "sites",
        "regions",
        "arrays",
        "grains",
        "crystals",
        "phases",
        "alloys",
        "polymers",
        "nanoparticles",
        "nanowires",
        "nanosheets",
        "catalysts",
        "batteries",
        "supercapacitors",
        "nanobuds",
        "fullerenes",
        "hybrids",
        "cartridges",
        "cards",
        "regen",
        # attributive past-participles used adjectivally, not as the verb
        "laminated",
        "printed",
        "molded",
        "machined",
        "assembled",
        "mounted",
        "sealed",
        "threaded",
        "welded",
        "milled",
        "integrated",
        "embedded",
        "layered",
        "structured",
        "textured",
        "curved",
        "rounded",
        "polished",
        "stacked",
        "folded",
        "aligned",
        "oriented",
        # corpus check, 2026-08-19 (past-tense/past-passive addition):
        # non-hyphenated attributive past participles found on otherwise-
        # clean, correctly present-tense hub titles ("nanobuds built on
        # zigzag...", "one set of experimental parameters", "the
        # conductance suppressed at...", "a pored graphene sheet",
        # "thermodynamically preferred coordination geometries", "ab
        # initio parametrized force fields", "sinelike buckled Stone-Wales
        # defect") -- the hyphen-premodifier case (`DFT-computed`,
        # `single-walled`) is instead handled structurally in
        # `_simple_past_match`, since that shape generalizes and an
        # enumerated list of every possible compound modifier would not.
        "built",
        "set",
        "suppressed",
        "pored",
        "preferred",
        "parametrized",
        "buckled",
        "calculated",
        "expected",
    }
)

_WORD_RE = re.compile(r"[A-Za-z]+")


def _has_finite_verb(sentence: str) -> bool:
    """Heuristic, POS-tagger-free "does this sentence assert something".

    False-negative-biased: an ambiguous word is treated as non-evidence
    (see `_VERB_SHAPE_EXCEPTIONS`), so this can under-report a verb and
    trigger `verbless` on a real claim only in the rare case none of its
    words hit the closed aux/copula set, `_EVIDENCE_VERB_RE`, or
    `_FINITE_VERB_RE` either -- i.e. it names no controlled evidence verb
    at all, which independently means `no-evidence-verb` would already
    have fired.
    """
    for word in _WORD_RE.findall(sentence):
        low = word.lower()
        if low in _AUX_COPULA_TOKENS:
            return True
        if _EVIDENCE_VERB_RE.fullmatch(word) or _FINITE_VERB_RE.fullmatch(word):
            return True
        if _ACRONYM_PLURAL_RE.match(word):
            continue
        if (
            _VERB_SHAPE_RE.match(word)
            and low not in _VERB_SHAPE_EXCEPTIONS
            and len(word) > 2
        ):
            return True
    return False


# ── tense: past-passive (blocking), past-tense / present-perfect (advisory) ─

#: Irregular past-tense/participle forms observed in corpus text (`built`,
#: `shown`, `found`, ...) or otherwise common enough to be worth the extra
#: recall. Shared by the past-passive, present-perfect, and simple-past
#: detectors below -- each already has its own exclusion (evidence-verb,
#: attributive-stoplist) layered on top, so one shared shape list is
#: sufficient rather than three near-duplicate ones.
_IRREGULAR_PAST_FORMS: frozenset[str] = frozenset(
    {
        "built",
        "shown",
        "given",
        "taken",
        "made",
        "done",
        "written",
        "known",
        "seen",
        "chosen",
        "held",
        "sent",
        "spent",
        "kept",
        "left",
        "brought",
        "thought",
        "taught",
        "bought",
        "caught",
        "understood",
        "grown",
        "born",
        "driven",
        "sung",
        "begun",
        "found",
        "put",
        "set",
        "run",
        "went",
        "grew",
        "rose",
        "fell",
        "wrote",
        "spoke",
        "broke",
        "sang",
        "ran",
        "sold",
        "sought",
        "bore",
        "said",
        "did",
        "came",
        "became",
    }
)

_IRREGULAR_PAST_FORMS_ALT = "|".join(
    sorted(_IRREGULAR_PAST_FORMS, key=len, reverse=True)
)

#: `was/were + past participle` as the sentence's main verb -- a bare
#: activity/history report ("was proposed by...", "were investigated"). One
#: optional adverb is allowed between the aux and the participle (`was
#: first demonstrated`), corpus-observed. Deliberately does NOT match
#: `has/have (been) VERBed` (`_PRESENT_PERFECT_RE` below) -- the aux token
#: is a different literal (`was`/`were`, never `has`/`have`), so the two
#: codes are structurally disjoint by construction, never by exclusion
#: logic.
_PAST_PASSIVE_RE = re.compile(
    r"\b(?:was|were)\s+(?:not\s+|also\s+|then\s+|first\s+|later\s+|"
    r"previously\s+|originally\s+|initially\s+)?"
    r"([A-Za-z]+ed|" + _IRREGULAR_PAST_FORMS_ALT + r")\b",
    re.IGNORECASE,
)

#: `has/have (not) (been) + past participle` -- present perfect. Matches
#: whether or not `been` is present (`has demonstrated` and `has been
#: demonstrated` are both present perfect).
_PRESENT_PERFECT_RE = re.compile(
    r"\b(?:has|have)\s+(?:not\s+)?(?:been\s+)?"
    r"([A-Za-z]+ed|" + _IRREGULAR_PAST_FORMS_ALT + r")\b",
    re.IGNORECASE,
)

#: Aux/copula tokens that, immediately preceding a past-tense-shaped word,
#: mean that word is part of a passive or perfect construction rather than
#: a bare simple-past main verb -- `_simple_past_match` skips those so it
#: does not double-report the same word `_PAST_PASSIVE_RE`/
#: `_PRESENT_PERFECT_RE` already accounts for.
_PAST_TENSE_AUX_TOKENS: frozenset[str] = frozenset(
    {"was", "were", "has", "have", "had", "is", "are", "be", "been", "being"}
)


def _passive_or_perfect_hit(
    pattern: re.Pattern[str], sentence: str, *, exclude_evidence_verbs: bool
) -> re.Match[str] | None:
    """First match of ``pattern`` whose participle is not an attributive
    adjective (`_VERB_SHAPE_EXCEPTIONS`).

    ``exclude_evidence_verbs`` additionally skips a controlled evidence
    verb (`_EVIDENCE_VERB_RE`) -- for `_PAST_PASSIVE_RE`, a passive clause
    naming an evidence verb already carries a finding, so it reads as
    evidence stated passively, not a bare "a study happened" activity
    report. That reasoning does not apply to present perfect -- the
    canonical achievement-claim shape ("has been demonstrated...") *is*
    an evidence verb -- so `_PRESENT_PERFECT_RE` callers pass ``False``.
    """
    for m in pattern.finditer(sentence):
        word = m.group(1)
        if word.lower() in _VERB_SHAPE_EXCEPTIONS:
            continue
        if exclude_evidence_verbs and _EVIDENCE_VERB_RE.fullmatch(word):
            continue
        return m
    return None


def _simple_past_match(sentence: str) -> re.Match[str] | None:
    """First bare simple-past main verb -- an `-ed`/irregular past-tense
    word not immediately preceded by an aux/copula (which would make it
    part of a passive or perfect construction instead) and not an
    attributive participle.

    A word immediately preceded by a hyphen (`DFT-computed`, `CVD-grown`,
    `single-walled`) is a compound premodifier, not a finite verb, and is
    always skipped -- corpus check (2026-08-19): this single rule accounts
    for 8 of 16 false positives found on otherwise-clean, correctly
    present-tense hub titles in a bare-shape-based first pass; the
    remaining false positives (non-hyphenated attributive participles:
    `built`, `set`, `preferred`, ...) are folded into
    `_VERB_SHAPE_EXCEPTIONS`, the same stoplist `past-passive` reuses."""
    words = list(_WORD_RE.finditer(sentence))
    for idx, wm in enumerate(words):
        word = wm.group(0)
        low = word.lower()
        if len(word) <= 2 or low in _VERB_SHAPE_EXCEPTIONS:
            continue
        is_past_shape = (low.endswith("ed") and _VERB_SHAPE_RE.match(word)) or (
            low in _IRREGULAR_PAST_FORMS
        )
        if not is_past_shape:
            continue
        if wm.start() > 0 and sentence[wm.start() - 1] == "-":
            continue
        prev_word = words[idx - 1].group(0).lower() if idx > 0 else ""
        if prev_word in _PAST_TENSE_AUX_TOKENS:
            continue
        return wm
    return None


# ── dangling-reference ───────────────────────────────────────────────────

_DANGLING_PHRASES: tuple[str, ...] = (
    "the same group",
    "this work",
    "as above",
    "the authors",
    "these results",
    "the former",
    "the latter",
    "among those examined",
    "the best performing",
)

# ── multi-assertion ───────────────────────────────────────────────────────

#: The two coordinating-join shapes. `_clause_fragments` trusts `; ` but
#: has to interrogate `, and` (see gr245400).
_SEMICOLON_JOIN_RE = re.compile(r";\s+")
_COMMA_AND_JOIN_RE = re.compile(r",\s+and\s+")

#: Longest a comma-delimited span may be and still read as a list item
#: rather than a clause. Measured, not guessed: across the 1,547-hub claim
#: corpus every true enumeration item closed by a serial comma is <= 4
#: words ("ABTS", "[2]pseudorotaxane", "CD-MOF-2 (Rb+)"), and every true
#: coordinated clause is longer.
_ENUM_ITEM_MAX_WORDS = 4

#: `10,000` -- a comma bracketed by digits, which separates nothing.
_DIGIT_GROUP_COMMA_RE = re.compile(r"\d,\d")

#: A broad, heuristic finite-verb set for the multi-assertion check and
#: `lint_scope`'s free-text detector. False-negative-biased on purpose --
#: this is a "does this look like a clause/sentence fragment" smell test,
#: not a parser.
_FINITE_VERB_RE = re.compile(
    r"\b(?:is|are|was|were|has|have|had|"
    r"shows?|reveals?|exhibits?|reaches?|increases?|decreases?|enables?|"
    r"allows?|leads?|results?|causes?|produces?|yields?|indicates?|"
    r"confirms?|reports?|suggests?|finds?|found|proves?|demonstrates?|"
    r"predicts?|measures?|observes?|exceeds?|remains?|drops?|rises?|"
    r"falls?|induces?|converts?|forms?|binds?|catalyzes?|catalyses?|"
    r"engineered|printed|grown|deposited|synthesi[sz]ed|fabricated|"
    r"doped|annealed|coated|etched|patterned|functionali[sz]ed)\b",
    re.IGNORECASE,
)


def _clause_fragments(sentence: str) -> list[str]:
    """Split `sentence` at genuine clause joins (`, and` / `; `).

    A serial ("Oxford") comma closing a noun enumeration -- `A, B, and C` --
    is not a clause join, and splitting there strands the sentence's subject
    on one side of the cut and its verb on the other. The finite-verb count
    in `lint_claim_sentence` then reads one assertion as two (gr245400: 3 of
    96 rewrites in the 2026-08-23 graduation tranche were blocked this way,
    every one a single assertion).

    The enumeration tell is local: an earlier comma opened the list, and the
    item it closes -- the span between that comma and `, and` -- is a SHORT
    bare noun phrase carrying no finite verb. Both halves of that test earn
    their keep. Verbless alone lets `..., including mechanistically
    controlled NDC/PDC transformation, and remain thermally stable` through,
    because the heuristic verb set does not know every predicate it might
    meet; the length cap catches those, since a real coordinated clause is
    never four words long. Erring toward firing is the safe direction here
    -- `multi-assertion` blocks at approve, so a missed enumeration costs a
    reword while a missed coordination ships two claims as one.
    """
    cuts: list[tuple[int, int]] = [
        (m.start(), m.end()) for m in _SEMICOLON_JOIN_RE.finditer(sentence)
    ]
    for m in _COMMA_AND_JOIN_RE.finditer(sentence):
        prev_comma = sentence.rfind(",", 0, m.start())
        # A digit-grouping comma (`10,000`) never opens a list -- skip past
        # it, or the "item" it appears to close is the tail of a number.
        while prev_comma > 0 and _DIGIT_GROUP_COMMA_RE.match(sentence, prev_comma - 1):
            prev_comma = sentence.rfind(",", 0, prev_comma)
        if prev_comma >= 0:
            item = sentence[prev_comma + 1 : m.start()]
            if len(item.split()) <= _ENUM_ITEM_MAX_WORDS and not _FINITE_VERB_RE.search(
                item
            ):
                continue  # serial comma inside an enumeration, not a clause join
        cuts.append((m.start(), m.end()))

    if not cuts:
        return [sentence]
    cuts.sort()
    fragments: list[str] = []
    pos = 0
    for start, end in cuts:
        fragments.append(sentence[pos:start])
        pos = end
    fragments.append(sentence[pos:])
    return fragments


# ── mixed-point-range (2026-08-20 numeric-value policy) ──────────────────
#
# Not a notation check -- `notation.py::_RANGE_UNIT_REPEATED_RE` already
# owns the deterministic "unit stated twice in a range" form defect. This
# is the content-shaped one the policy calls "the useful one": a sentence
# giving both a range and a bare point value for what reads as the same
# quantity (same unit) is the false-precision shape UNLESS the point
# value carries a source-designated-typical marker ("approximately 9 GPa
# across a reported 9-12 GPa" -- fine; "3.2 GPa" beside "3-3.4 GPa" with
# no marker -- the shape the policy bans, a midpoint asserted as if
# measured). This module has no access to the source passage, so it can
# never tell a genuine source-designated typical from an invented one --
# deliberately advisory-only, and deliberately conservative: a marker
# word anywhere in a short window before the point value suppresses the
# warning, and any point-looking number that falls inside a *second*
# range in the same sentence is skipped rather than misread as a stray
# point (two side-by-side ranges for two different regimes is common and
# must never fire this).

#: Numeric range with a trailing unit -- same shape as `notation.py`'s
#: `hyphen-numeric-range`/`range-unit-repeated` detectors (deliberately
#: not imported from there: this module stays a pure string check with no
#: cross-module coupling, matching every other pattern here).
_RANGE_WITH_UNIT_RE = re.compile(
    r"(?<![-A-Za-z\d])(\d+(?:\.\d+)?)\s?[–-]\s?(\d+(?:\.\d+)?)"
    r"\s?([A-Za-zµ°Ω%][\w°Ω%⁻¹²³⁴⁵⁶⁷⁸⁹⁰/]*)"
)

#: Wording that marks a nearby number as a source-designated typical, not
#: an invented midpoint -- `≈9 GPa`, `~9 GPa`, `approximately`,
#: `typically`, `nominally`, `on average`, `about`, `around`. Checked in
#: a short window immediately before the point value; false-negative-
#: biased on purpose -- an unrecognised marker phrase just means this
#: code stays silent on a sentence that may in fact be fine, never that
#: it wrongly blames one (module contract above).
_TYPICAL_MARKER_RE = re.compile(
    r"≈|~|\bapproximately\b|\btypically\b|\bnominally\b|\bon average\b|"
    r"\babout\b|\baround\b|\bapprox\.?\b",
    re.IGNORECASE,
)

#: Chars of leading context inspected for a typical-value marker --
#: generous enough for "approximately " (14 chars) plus a short lead-in
#: clause, tight enough that a marker from an unrelated earlier clause in
#: a long sentence doesn't leak across and suppress a real miss.
_TYPICAL_MARKER_WINDOW_CHARS = 24


def _mixed_point_range_hit(sentence: str) -> tuple[str, str] | None:
    """First bare point value sharing a range's unit with no nearby
    typical-value marker, or ``None``. Returns ``(point_text, unit)``.

    Checks each range's unit independently (a sentence can name two
    ranges for two different regimes, each with its own unit) and always
    excludes every range's own span from the point-value search -- one
    range's right endpoint must never read as a stray point next to a
    *different* range sharing its unit.
    """
    range_matches = list(_RANGE_WITH_UNIT_RE.finditer(sentence))
    if not range_matches:
        return None
    range_spans = [m.span() for m in range_matches]
    units = dict.fromkeys(m.group(3) for m in range_matches)  # dedup, keep order
    for unit in units:
        point_re = re.compile(
            r"(?<![-\d.])(\d+(?:\.\d+)?)\s?" + re.escape(unit) + r"\b"
        )
        for m in point_re.finditer(sentence):
            if any(start <= m.start() < end for start, end in range_spans):
                continue  # this hit is a range endpoint, not a stray point
            window = sentence[
                max(0, m.start() - _TYPICAL_MARKER_WINDOW_CHARS) : m.start()
            ]
            if _TYPICAL_MARKER_RE.search(window):
                continue
            return m.group(1), unit
    return None


# ── author-name ───────────────────────────────────────────────────────────

_AUTHOR_NAME_RE = re.compile(
    r"\bet al\.?\b|\b[A-Z][a-zA-Z]+ (?:et al\.?\s*)?(?:19|20)\d\d\b"
)

#: Terseness budget. Corpus median 147 chars, p90 249, max 506
#: (`docs/backlog/nanopub-corpus-remediation.md` audit table). 250 sits
#: just above p90: terse is a rule, not a preference -- a sentence past
#: this length is disproportionately likely to be the multi-clause-collapse
#: shape the remediation doc names (a joined "and"-claim, a bibliography
#: paragraph). A shorter atom makes that failure structurally impossible;
#: the threshold exists to make "shorter" checkable rather than aspirational.
_OVER_LONG_CHARS = 250


def lint_claim_sentence(sentence: str) -> list[str]:
    """Return human-readable admissibility/grammar warnings about
    ``sentence``.

    Advisory only: never raises (any input, including ``""``, returns a
    list -- possibly empty), never rewrites ``sentence``. Heuristic by
    construction -- it flags for judgment, never blocks a write.
    """
    if not sentence:
        return []

    warnings: list[str] = []

    for pattern, hint in (
        (_LABEL_STYLE_RE, "colon/em-dash label style"),
        (_BIBLIOGRAPHY_LEAD_RE, "leading 'Surname & Surname YYYY' bibliography shape"),
        (_COPULA_DEFINITION_RE, "copula definition ('X is a ...')"),
        (_STUDY_HAPPENED_RE, "study-happened phrasing ('was/were investigated')"),
    ):
        m = pattern.search(sentence)
        if m:
            warnings.append(
                f"not-falsifiable: {m.group(0)!r} found -- {hint}; a claim "
                "must assert a finding a future measurement could "
                "contradict, not narrate that a study happened."
            )

    m = _EM_DASH_SEPARATOR_RE.search(sentence)
    if m:
        warnings.append(
            f"em-dash: {m.group(0)!r} found -- an em dash or ASCII "
            "stand-in used as a clause/label separator is banned outright "
            "(canon: en dash only for ranges/compound names, never a "
            "separator); replace with a comma, or rewrite the sentence "
            "as an assertion if the two sides are a citation and a topic."
        )

    # Tense (2026-08-19 standard): past-passive is blocking (checked at
    # approve, see gates._BLOCKING_LINT_CODES); past-tense/present-perfect
    # are machine-undecidable and stay advisory. past-passive is the most
    # specific of the three -- when it matches, past-tense is not also
    # checked on this sentence.
    passive_hit = _passive_or_perfect_hit(
        _PAST_PASSIVE_RE, sentence, exclude_evidence_verbs=True
    )
    if passive_hit:
        warnings.append(
            f"past-passive: {passive_hit.group(0)!r} found -- past passive "
            "with no result stated reads as a history-of-science or "
            "activity report ('was proposed by...', 'were investigated'), "
            "not a claim; rewrite as an active, present-tense assertion of "
            "the finding."
        )
    else:
        past_hit = _simple_past_match(sentence)
        if past_hit:
            warnings.append(
                f"past-tense: {past_hit.group(0)!r} found -- simple past is "
                "only correct when the claim's subject is itself a "
                "historical event (rewriting to present would make the "
                "sentence false or absurd); otherwise prefer simple "
                "present."
            )

    perfect_hit = _passive_or_perfect_hit(
        _PRESENT_PERFECT_RE, sentence, exclude_evidence_verbs=False
    )
    if perfect_hit:
        warnings.append(
            f"present-perfect: {perfect_hit.group(0)!r} found -- present "
            "perfect is allowed only for existence/achievement claims "
            "('X has been demonstrated in...'); elsewhere it hides the "
            "agent and the conditions -- prefer simple present."
        )

    if not _has_finite_verb(sentence):
        warnings.append(
            "not-falsifiable: verbless -- no finite verb found; a claim "
            "asserts something, an assertion needs a verb (a topic label "
            "or a colon-prefixed measurement dump is not a claim)."
        )

    lowered = sentence.lower()
    for phrase in _DANGLING_PHRASES:
        if phrase in lowered:
            warnings.append(
                f"dangling-reference: {phrase!r} found -- claim sentences "
                "are hashed and read standalone; a referent that only "
                "resolves in surrounding context (or an unnamed "
                "comparison set) breaks self-containment."
            )

    if not _EVIDENCE_VERB_RE.search(sentence):
        warnings.append(
            "no-evidence-verb: none of predicts/finds/shows/measures/"
            "observes/demonstrates/calculates/computed/estimates/reveals/"
            "confirms/identifies/indicates (or an inflection) found -- a "
            "claim sentence should name how the finding was established."
        )

    if not _EPISTEMIC_MODE_RE.search(sentence):
        warnings.append(
            "no-epistemic-mode: no method token found (e.g. DFT, TEM, "
            f"Raman -- see {sorted(EPISTEMIC_MODE_TOKENS)[0]!r} and "
            "friends in EPISTEMIC_MODE_TOKENS) -- the epistemic mode "
            "should be readable from the sentence, not only from linked "
            "evidence."
        )

    clauses = [c for c in _clause_fragments(sentence) if len(c.split()) >= 3]
    if len(clauses) >= 2 and sum(1 for c in clauses if _FINITE_VERB_RE.search(c)) >= 2:
        warnings.append(
            "multi-assertion: sentence joins two clauses each carrying a "
            "finite verb (', and'/'; ') -- split into two atomic claims; "
            "terse is the rule, not the preference."
        )

    if not sentence.rstrip().endswith("."):
        warnings.append(
            "no-terminal-period: sentence does not end in '.' -- claim "
            "sentences are complete sentences."
        )

    if len(sentence) > _OVER_LONG_CHARS:
        warnings.append(
            f"over-long: {len(sentence)} chars found (budget "
            f"{_OVER_LONG_CHARS}, corpus median 147/p90 249) -- prefer the "
            "shortest sentence that stays falsifiable and self-contained; "
            "this also makes the multi-clause-collapse failure mode "
            "structurally impossible."
        )

    m = _AUTHOR_NAME_RE.search(sentence)
    if m:
        warnings.append(
            f"author-name: {m.group(0)!r} found -- provenance belongs in "
            "evidence edges (`link(rel='corroborates', ...)`), not the "
            "claim sentence itself."
        )

    point_hit = _mixed_point_range_hit(sentence)
    if point_hit:
        point_text, unit = point_hit
        warnings.append(
            f"mixed-point-range: {point_text!r} {unit} found beside a "
            f"{unit} range with no typical-value marker nearby -- if the "
            "source designates a typical value, write it as "
            f"'≈{point_text} {unit} across a reported <range> {unit}' "
            "(precis-taproot-mint-help's numeric-value policy); "
            "otherwise a bare point beside a range reads as an invented "
            "midpoint. Advisory only -- this check has no access to the "
            "source and cannot confirm which case applies."
        )

    return warnings


#: Controlled scope-key vocabulary (`docs/backlog/nanopub-corpus-
#: remediation.md`, "Why dedup never fired" #2 -- 372 free-text values
#: across 1,527 hubs). Deliberately small; adding a key is a deliberate
#: act, not a drive-by.
SCOPE_KEYS: frozenset[str] = frozenset(
    {"material", "method", "regime", "system", "quantity", "substrate", "temperature"}
)

_TRAILING_PREPOSITIONS: frozenset[str] = frozenset(
    {
        "of",
        "to",
        "in",
        "on",
        "for",
        "with",
        "by",
        "from",
        "into",
        "onto",
        "as",
        "at",
        "the",
        "a",
        "an",
    }
)


def _looks_like_prose(value: str) -> bool:
    words = value.split()
    if len(words) > 4:
        return True
    if words and words[-1].lower().strip(".,;:") in _TRAILING_PREPOSITIONS:
        return True
    if _FINITE_VERB_RE.search(value):
        return True
    return False


def lint_scope(scope: dict[str, Any]) -> list[str]:
    """Return human-readable warnings about a claim hub's ``scope`` dict.

    Advisory only: never raises (any input, including ``None``-ish falsy
    values, returns a list -- possibly empty), never rewrites ``scope``.
    """
    if not scope:
        return ["scope-empty: scope is empty -- advisory only, not an error."]

    warnings: list[str] = []

    for key, value in scope.items():
        if key not in SCOPE_KEYS:
            warnings.append(
                f"scope-unknown-key: {key!r} found -- the enumerated set "
                f"is {sorted(SCOPE_KEYS)!r}; adding a key is a deliberate "
                "act, not a drive-by."
            )
        if isinstance(value, str) and value and _looks_like_prose(value):
            warnings.append(
                f"scope-free-text: {key}={value!r} found -- a scope value "
                "should be a short enumerated token, not a prose fragment "
                "(>4 words, a trailing preposition, or a finite verb all "
                "read as prose)."
            )

    return warnings
