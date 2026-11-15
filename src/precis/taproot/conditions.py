"""Advisory conditions-completeness check for taproot claim sentences.

Reto, 2026-11-15 (chat, on dr348633 "NO to NH3 and H2 generation"): "the
thermodynamics of electrochemical steps depend on the applied potential, and
the claims often do not say which. Claims, in the hub and in the final draft
text, must be complete enough for an expert in the field to replicate them.
No pedantry: conditions the field assumes by default (about 25 degC, 1 bar,
air, standard states) stay implicit. Conditions with no default must be
stated." (`docs/backlog/claim-conditions-completeness.md`.)

A read-only measurement over the 2771 live hubs cited by dr348633 found 57%
of electrochemical-measurement hubs, 77% of DFT-step hubs and 48% of
photocatalytic-rate hubs missing a must-state condition. `aida_uri` keys on
the sentence alone (`aida-uri-ignores-scope.md`, Reto 2026-10-21), so the
condition has to be in the sentence, not only in `scope`: this module reads
the sentence and nothing else.

Decisions:

- **One :class:`ConditionProfile` per claim family**, each a trigger, the
  must-state :class:`ConditionGroup` regexes, the defaults that need not be
  stated, and its exemptions. First applicable profile wins (families are
  mutually exclusive by construction), so a sentence yields at most one gap.
- **Deterministic and advisory.** Regexes only; never raises; a false
  positive only queues work. It is wired into
  :func:`~precis.taproot.sentence_lint.lint_claim_sentence` as
  ``conditions-incomplete`` and is deliberately absent from
  ``gates._BLOCKING_LINT_CODES``.
- **A sentence with no number is exempt** (the prototype's ``HASNUM``): a
  qualitative mechanism claim carries no value that depends on a state
  variable.
- **DFT adsorption descriptors are exempt** (``ΔG_H*``, ``ΔG*NO3``,
  "adsorption energy") unless the sentence also speaks of a step, PDS,
  limiting potential or uphill/downhill: U = 0 V is the convention there.
- **DFT barriers fire only when the sentence itself calls the step
  electrochemical / potential-determining** and states no potential; a
  chemical barrier needs none. False positives are kept low here.
- **A galvanostatic or device claim** (current density, cell voltage,
  MEA, electrolyzer, battery) satisfies the potential requirement. A bare
  "V" with no reference electrode is its own missing item, ``reference``.
- **Computed STH / band-edge claims are not photo-rate**: a computed
  quantity has no lamp.
- **Thermal catalysis** needs a temperature and never fires in an
  electrochemical (nitrate / NO3RR / ``*N``) or DFT context.

Not done here: the profile-as-finding versioning, mint-time extraction
changes and hub backfill (backlog doc, Design 1 and 3-5).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

_I = re.IGNORECASE

# --- shared vocabulary -----------------------------------------------------

#: A claim states a quantity (the spec's "HASNUM" rule); without one the
#: sentence is qualitative and exempt.
_HASNUM = re.compile(
    r"\d+(\.\d+)?\s*(%|eV|V\b|mA|A g|mmol|µmol|μmol|mg|µg|μg|kJ|kcal|nm|°C|"
    r"h⁻¹|times|-fold|fold)"
)

_DFT = re.compile(
    r"\bDFT\b|first[- ]principles|density functional|calculation|computed|"
    r"theoretical|simulat",
    _I,
)
_STEP_WORDS = re.compile(
    r"free[- ]energy|ΔG|\bdelta ?G\b|limiting potential|potential[- ](determining|limiting)|"
    r"\bPDS\b|theoretical overpotential|onset potential|uphill|downhill|Gibbs",
    _I,
)
#: Words that make an adsorption-descriptor sentence a step claim after all.
_STEP_CLAIM = re.compile(
    r"\bsteps?\b|\bPDS\b|limiting potential|potential[- ](determining|limiting)|"
    r"uphill|downhill|rate[- ]limiting|overpotential",
    _I,
)
#: ΔG_H*, ΔG*H, ΔG(NO3*), "adsorption (free) energy": U = 0 V by convention.
_ADSORPTION = re.compile(
    r"ΔG[\s_{(]*\*\s*[A-Za-z]|ΔG[\s_{(]*[A-Za-z][\w₀-₉]*\s*\*|ΔG[\s_{(]*H\b|"
    r"adsorption (free )?energ|binding (free )?energ",
    _I,
)
_BARRIER = re.compile(r"barrier|activation (free )?energ|transition state|\bEa\b", _I)
_ELECTRO_STEP = re.compile(
    r"electrochemical (step|barrier|reaction)|potential[- ](determining|limiting)|"
    r"proton[- ]electron|proton[- ]coupled|\bPCET\b|\bCPET\b",
    _I,
)
_POT = re.compile(
    r"\bU\s*=|−?-?\d+(\.\d+)?\s*V\b|0 V\b|vs\.? ?(RHE|SHE|NHE|Ag/AgCl|SCE|Hg/HgO)|"
    r"computational hydrogen electrode|\bCHE\b|constant[- ]potential|grand[- ]canonical|"
    r"equilibrium potential|applied potential|zero (applied )?potential",
    _I,
)

_PHOTO = re.compile(
    r"photocatal|photoelectro|\bAQY\b|apparent quantum|solar[- ]to[- ]hydrogen|\bSTH\b|"
    r"visible[- ]light|under (UV|light|illumination)",
    _I,
)
#: A light SOURCE: lamp, wavelength, power, AM 1.5 / solar simulator. "visible
#: light" or "UV" alone names a band, not a source, and does not count.
#: ``LED`` and the ``W`` power unit are case-sensitive ("enabled", "5 wt%").
_LIGHT = re.compile(
    r"\d+\s*nm\b|AM ?1\.5|\blamp\b|(?-i:\bLED\b)|\d+(?:\.\d+)?\s*(?-i:m?W)\b|"
    r"simulated sunlight|solar[- ](simulator|irradiation)|λ\s*[>≥]",
    _I,
)
_COMPUTED = re.compile(
    r"comput|calculat|theoretical|predict|\bDFT\b|simulat|first[- ]principles", _I
)
_STH_OR_BANDEDGE = re.compile(
    r"\bSTH\b|solar[- ]to[- ]hydrogen|band[- ]edge|band ?gap|\bCBM\b|\bVBM\b", _I
)

_EC = re.compile(
    r"Faradaic|(?-i:\bFE\b)|yield rate|partial current|current density|"
    r"mA ?cm|onset potential|overpotential|Tafel|electroly|electrochemical|"
    r"electroreduction|electrocatal|chronoamper|flow cell|\bMEA\b",
    _I,
)
_EC_QUANT = re.compile(
    r"Faradaic|(?-i:\bFE\b)|yield|partial current|current density|mA ?cm|"
    r"onset|overpotential|\d+(\.\d+)?\s*%|selectivity|mmol|µmol|μmol|mg ?h",
    _I,
)
_POT_REF = re.compile(r"vs\.? ?(RHE|SHE|NHE|Ag/AgCl|SCE|Hg/HgO)|\bRHE\b", _I)
_BARE_V = re.compile(r"−?-?\d+(\.\d+)?\s*V\b")
_GALV = re.compile(
    r"\d+\s*m?A ?cm|galvanostatic|constant current|\d+\s*mA\b|cell voltage|"
    r"electrolyzer|electrolyser|\bMEA\b|battery",
    _I,
)
_ELYTE = re.compile(
    r"\d+(\.\d+)?\s*(?-i:m?M)\b|\bpH\b|KOH|NaOH|H₂SO₄|H2SO4|Na₂SO₄|Na2SO4|K₂SO₄|PBS|"
    r"phosphate|bicarbonate|KHCO₃|seawater|electrolyte|acidic|alkaline|neutral",
    _I,
)
_ANY_POTENTIAL = re.compile(f"{_POT_REF.pattern}|{_BARE_V.pattern}|{_GALV.pattern}", _I)

#: A zero-gap membrane-electrode assembly has no bulk liquid electrolyte.
_MEA = re.compile(r"\bMEA\b|membrane[- ]electrode|zero[- ]gap", _I)

#: A gas-phase flow reactor: "under light" there is a variable, not a lamp claim.
_GAS_REACTOR = re.compile(r"fixed[- ]bed|GHSV|WHSV", _I)

_THERM = re.compile(
    r"\bSCR\b|fixed[- ]bed|GHSV|WHSV|light-off|\bT50\b|\bT90\b|conversion|"
    r"thermal catal|thermocatal|H₂-SCR|CO-SCR|NH₃-SCR|reactor",
    _I,
)
_TEMP = re.compile(
    r"\d+\s*°C|\d+\s*K\b|room temperature|\bRT\b|ambient|temperature window", _I
)
_ELEC_CTX = re.compile(r"nitrate|nitrite|NO3RR|NORR|NO₃|NO₂⁻|\*N|electro", _I)


# --- the explicit "not reported" marker ------------------------------------

#: Spec (Design 3): when the evidence paper does not state a must-state
#: condition, the claim says so instead of leaving it out silently. The one
#: phrasing, shared by the writer (:func:`not_reported_marker`) and the
#: check: ``(potential not reported)``, ``(potential and electrolyte not
#: reported)``. A condition named inside such a parenthetical satisfies its
#: group.
NOT_REPORTED_PHRASE = "not reported"
_NOT_REPORTED_RE = re.compile(
    r"\(([^()]{1,80}?)\s+" + re.escape(NOT_REPORTED_PHRASE) + r"\s*\)", _I
)


def not_reported_marker(names: Sequence[str]) -> str:
    """``"(potential and electrolyte not reported)"`` for group names
    ``names`` (the :attr:`ConditionGap.missing` vocabulary)."""
    uniq = list(dict.fromkeys(names))
    if len(uniq) > 1:
        joined = ", ".join(uniq[:-1]) + " and " + uniq[-1]
    else:
        joined = "".join(uniq)
    return f"({joined} {NOT_REPORTED_PHRASE})"


def reported_as_missing(sentence: str) -> frozenset[str]:
    """Condition names a sentence explicitly marks "not reported"."""
    names: set[str] = set()
    for m in _NOT_REPORTED_RE.finditer(sentence):
        names.update(w.lower() for w in re.findall(r"[A-Za-z]+", m.group(1)))
    names.discard("and")
    return frozenset(names)


# --- contract ----------------------------------------------------------------


@dataclass(frozen=True)
class ConditionGroup:
    """One must-state condition: ``pattern`` satisfies it.

    ``when`` (if set) makes the group apply only to sentences it matches;
    ``unless`` (if set) lifts the requirement when it matches. ``hint`` is
    the verb-less phrase naming what to add.
    """

    name: str
    pattern: re.Pattern[str]
    hint: str
    when: re.Pattern[str] | None = None
    unless: re.Pattern[str] | None = None


@dataclass(frozen=True)
class ConditionProfile:
    """A claim family: when it applies, what it must state, what it may omit.

    Applies when ``trigger`` matches, every ``requires`` pattern matches and
    no ``excludes`` pattern does. Exempt (never a gap) when every
    ``exempt_all`` pattern matches and ``exempt_unless`` does not.
    ``defaults`` is the human text of what the field assumes and the
    sentence need not state.
    """

    family: str
    trigger: re.Pattern[str]
    groups: tuple[ConditionGroup, ...]
    defaults: tuple[str, ...]
    requires: tuple[re.Pattern[str], ...] = ()
    excludes: tuple[re.Pattern[str], ...] = ()
    exempt_all: tuple[re.Pattern[str], ...] = ()
    exempt_unless: re.Pattern[str] | None = None

    def applies(self, sentence: str) -> bool:
        if not self.trigger.search(sentence):
            return False
        if not all(p.search(sentence) for p in self.requires):
            return False
        return not any(p.search(sentence) for p in self.excludes)

    def exempt(self, sentence: str) -> bool:
        if not self.exempt_all or not all(p.search(sentence) for p in self.exempt_all):
            return False
        return not (self.exempt_unless and self.exempt_unless.search(sentence))

    def missing(self, sentence: str) -> list[ConditionGroup]:
        out: list[ConditionGroup] = []
        marked = reported_as_missing(sentence)
        for g in self.groups:
            if g.name in marked:
                continue
            if g.when is not None and not g.when.search(sentence):
                continue
            if g.unless is not None and g.unless.search(sentence):
                continue
            if not g.pattern.search(sentence):
                out.append(g)
        return out


@dataclass(frozen=True)
class ConditionGap:
    """A claim sentence missing must-state conditions of one family."""

    family: str
    missing: tuple[str, ...]
    hint: str


_POTENTIAL_DFT = ConditionGroup(
    "potential",
    _POT,
    "the applied potential and its reference (e.g. 'at U = 0 V vs RHE, CHE model')",
)

PROFILES: tuple[ConditionProfile, ...] = (
    ConditionProfile(
        family="dft-barrier",
        trigger=_BARRIER,
        requires=(_DFT, _ELECTRO_STEP),
        groups=(_POTENTIAL_DFT,),
        defaults=("a chemical (non-electrochemical) step needs no potential",),
    ),
    ConditionProfile(
        family="dft-step-thermo",
        trigger=_STEP_WORDS,
        requires=(_DFT,),
        excludes=(_BARRIER,),
        groups=(_POTENTIAL_DFT,),
        defaults=("adsorption descriptors (ΔG_H*) at U = 0 V",),
        exempt_all=(_ADSORPTION,),
        exempt_unless=_STEP_CLAIM,
    ),
    ConditionProfile(
        family="photo-rate",
        trigger=_PHOTO,
        requires=(_EC_QUANT,),
        excludes=(_GAS_REACTOR,),
        groups=(
            ConditionGroup(
                "light",
                _LIGHT,
                "the light source (lamp or filter, wavelength, power)",
            ),
        ),
        defaults=("room temperature",),
        exempt_all=(_COMPUTED, _STH_OR_BANDEDGE),
    ),
    ConditionProfile(
        family="ec-measurement",
        trigger=_EC,
        requires=(_EC_QUANT,),
        excludes=(_DFT,),
        groups=(
            ConditionGroup(
                "potential",
                _ANY_POTENTIAL,
                "the potential vs a named reference electrode, or the current "
                "density / cell voltage",
            ),
            ConditionGroup(
                "reference",
                _POT_REF,
                "the reference electrode of the quoted potential (e.g. 'vs RHE')",
                when=_BARE_V,
                unless=_GALV,
            ),
            ConditionGroup(
                "electrolyte",
                _ELYTE,
                "the electrolyte and its concentration or pH",
                unless=_MEA,
            ),
        ),
        defaults=("room temperature", "ambient pressure"),
    ),
    ConditionProfile(
        family="thermal",
        trigger=_THERM,
        excludes=(_EC, _PHOTO, _ELEC_CTX, _DFT),
        groups=(ConditionGroup("temperature", _TEMP, "the reaction temperature"),),
        defaults=("ambient pressure",),
    ),
)


def _evaluate(sentence: str) -> tuple[str | None, list[ConditionGap]]:
    """``(family, gaps)``: the family a quantitative sentence falls in (None
    when no profile claims it or it is exempt) and its gaps."""
    if not sentence or not _HASNUM.search(sentence):
        return None, []
    for profile in PROFILES:
        if not profile.applies(sentence):
            continue
        if profile.exempt(sentence):
            return None, []
        missing = profile.missing(sentence)
        if not missing:
            return profile.family, []
        hint = "state " + " and ".join(g.hint for g in missing) + "."
        gap = ConditionGap(
            family=profile.family,
            missing=tuple(g.name for g in missing),
            hint=hint,
        )
        return profile.family, [gap]
    return None, []


def condition_family(sentence: str) -> str | None:
    """The claim family whose conditions ``sentence`` is subject to, gap or
    not (``None``: qualitative, exempt, or no profile applies). Never raises."""
    try:
        return _evaluate(sentence)[0]
    except Exception:
        return None


def check_conditions(sentence: str) -> list[ConditionGap]:
    """Return the must-state conditions ``sentence`` leaves out.

    Advisory and deterministic: never raises, returns ``[]`` for ``""``, for
    a sentence with no number (qualitative claims are exempt) and for one no
    profile claims. At most one gap (first applicable profile wins).
    """
    try:
        return _evaluate(sentence)[1]
    except Exception:  # advisory: a lint bug must never block a write
        return []


__all__ = [
    "NOT_REPORTED_PHRASE",
    "PROFILES",
    "ConditionGap",
    "ConditionGroup",
    "ConditionProfile",
    "check_conditions",
    "condition_family",
    "not_reported_marker",
    "reported_as_missing",
]
