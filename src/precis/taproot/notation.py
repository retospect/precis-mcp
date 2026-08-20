"""Advisory notation lint for taproot claim sentences.

A claim hub's sentence is hashed into its ``pub_id`` via
:func:`precis.identity.normalize_text_for_hash` (NFKD-fold, lowercase,
whitespace-collapse). NFKD forgives some spelling differences for free
(``cm²``≡``cm2``, ``C₆₀``≡``C60``) but does **not** forgive the rest — a
TeX fragment, an ASCII caret, digit grouping, an ASCII ``x`` for
multiplication, and so on each hash to a *different* ``pub_id`` than the
UTF-8-canonical spelling of the identical claim. Two agents writing the
same claim with different notation therefore mint two hubs instead of
converging on one — see `precis-notation-canon.md`, which is the spec
this module enforces (including its three carve-outs: quote-containment,
never-convert-the-paper's-unit, and nomenclature hyphens like ``5-8-5``
aren't minus signs).

:func:`lint_notation` is advisory only: it never raises, never rewrites
the sentence, and never blocks a write. It exists to surface the drift at
mint/reword time (`taproot/authoring.py::seed_claim_hub`,
`taproot/hub.py::refine_claim_sentence`) so a caller can fix notation
before it silently forks a hub.

**Canon v2** (`docs/backlog/nanopub-corpus-remediation.md` Phase 0/1) adds
a closed ASCII->UTF-8 fallback table -- ``+/-``, ``ug``/``micro``,
``degrees C``, ``Ohm``, ``Angstrom``, ``micrometer``/``micron``, ``1e3``
scientific notation -- each a *symbol respelling*, not a unit conversion,
so none of these trips the "never convert the paper's unit" carve-out.
v2 also tightens three rules that produced false positives/negatives in
the audit: the proportionality tilde (`E_g ~ 1/W` is not approximation),
the caret rule (`2^N` is a letter superscript, which canon keeps ASCII --
only *digit* and ``+``/``-`` sub/superscripts convert to Unicode), and
`ascii-minus-exponent` (a corpus dry run found it firing on chemical/
material nomenclature -- `Fe-ZSM-5`, `MOF-74`, `UiO-66`, `sub-10-nm` --
146 of 149 corpus rewrites were this false positive; it now fires only
when the token immediately left of the hyphen is a standalone member of
`_ACCEPTED_DENOMINATORS`, the same closed unit-symbol allowlist the
two-denominator-solidus rule uses).

**Canon v3** adds two more mechanically-safe rewrites and one lint-only
detector, all found by the same corpus dry-run process: `hyphen-numeric-
range` (an ASCII hyphen joining two bare numbers immediately followed by
a unit -- `300-800 mg/g` -> `300–800 mg/g`; canon's numeric-range row
wants an unspaced en dash, unenforced until now) and `ascii-x-multiplier`
(`1.61x` -> `1.61×`) are safe to auto-rewrite -- a corpus dry run found
zero false positives once each rule's neighbour-hyphen/letter guard was
added (see each regex's docstring below). `formula-ascii-subscript`
(`C60` -> `C₆₀`) is **not** safe to auto-rewrite: the same "element
symbol immediately followed by digits" signature that catches genuine
stoichiometry (`C60`, `N2`, `Fe3O4`, `Cu3(HHTP)2`) also catches, with no
regex-visible distinction, crown-ether/cryptand nomenclature that reuses
`C` for "crown" (`DB18C6`, `15C5`, `B12C4`), DFT-functional names
(`B3LYP`, `M06-2X`), a benchmark-set name (`S22`), vitamin names
(`B9`/`B12`), alkyl/chain-length labels (`C10-DNTT`, `C2-C10`, `C8-
grafted`), point-group symmetry labels (`C4`), a non-stoichiometric
formula (`Cu2-xS`), and ASCII ionic-charge digits that want a
*superscript*, not a subscript (`Mg2+`, `Gd3+`). A corpus count of the
naive rewrite found roughly a third of matches in this second group --
so it stays `lint_notation`-only, per this module's advisory contract;
`normalize_notation` never touches it.

**Canon v3.1** closes the same defect in the four remaining members of
the ASCII->UTF-8 family. `ascii-minus-exponent` had been read as a
one-off, but the 2026-08-20 corpus re-run showed its shape repeated
wherever a rule converts a unit *name* to a unit *symbol* without
checking that a numerical value precedes it -- SI writes a symbol with a
value and the name in words. `ascii-plusminus` was the damaging one: with
no right-hand numeral guard it consumed an oxidation state
(`Zn2+-sensing` -> `Zn2±sensing`), 2 of its 4 corpus fires. `ascii-
micrometre` was 5 right : 5 wrong (`micron-scale` -> `µm-scale`, `ten
micrometres` -> `ten µm`); `ascii-micro` rewrote a sentence-opening
`Microsecond` into `µs`. All four now share `_NUMERAL_LEFT`. The same
pass fixed a partial-application bug in `ascii-degrees`, whose
plural-only `deg(?:rees)?` left `1 degree C/min` ASCII while converting
`60 degrees C` in the same sentence -- a rule that rewrites some
occurrences of a unit must rewrite all of them. `_OHM_RE` and
`_ANGSTROM_RE` deliberately keep no numeral guard: an SI prefix
intervenes (`40 kOhm`) and `per angstrom` is a correct unqualified use.

:func:`normalize_notation` is the deterministic counterpart: it applies
only the classes of fix that are mechanically unambiguous (the ASCII->
UTF-8 respelling table, digit grouping, ASCII multiplication, E-notation,
digit-only caret/minus exponents, simple `$_{60}$`/`$^{2}$` TeX
fragments, a missing terminal period) and leaves judgment calls --
`two-denominator-solidus`, `over-long`, `multi-assertion`, anything in
`sentence_lint` -- untouched. It exists because the corpus's first
normalization pass was three agents hand-interpreting this same canon
three different ways.

**Sequencing constraint for a corpus-wide run:** normalizing a sentence
changes it, and the sentence is part of `pub_id`'s hash input -- so a
corpus-wide `normalize_notation` pass must be followed by recomputing
`pub_id` for every touched hub, and *then* a fresh duplicate scan.
Normalizing `10^4` -> `10⁴` can make two previously-distinct titles
identical without collapsing their *already-stored* `pub_id`s; only a
rescan after the pass catches the newly-created duplicate.
"""

from __future__ import annotations

import re
from collections.abc import Callable

__all__ = ["lint_notation", "normalize_notation"]

#: Single-symbol denominators the "solidus is fine" rule (canon table,
#: "single denominator" row) accepts unqualified -- `S/cm`, `mA/µm`,
#: `dI/dV`, `mV/dec` must never warn. Compared case-sensitively after
#: stripping a trailing UTF-8 superscript/`²`/`³`. Also reused (below) as
#: the standalone-unit allowlist for `ascii-minus-exponent`, so both rules
#: share one canon-defined set of "these are units" symbols.
_ACCEPTED_DENOMINATORS: frozenset[str] = frozenset(
    {
        "cm",
        "mm",
        "nm",
        "pm",
        "um",
        "µm",
        "μm",
        "m",
        "km",
        "s",
        "ms",
        "us",
        "µs",
        "μs",
        "ns",
        "ps",
        "fs",
        "g",
        "kg",
        "mg",
        "ug",
        "µg",
        "μg",
        "K",
        "V",
        "mV",
        "kV",
        "A",
        "mA",
        "µA",
        "μA",
        "uA",
        "nA",
        "W",
        "mW",
        "kW",
        "J",
        "mJ",
        "eV",
        "meV",
        "keV",
        "MeV",
        "Hz",
        "kHz",
        "MHz",
        "GHz",
        "THz",
        "mol",
        "mmol",
        "L",
        "mL",
        "µL",
        "μL",
        "uL",
        "dec",
        "decade",
        "Pa",
        "kPa",
        "MPa",
        "GPa",
        "N",
        "C",
        "F",
        "T",
        "bar",
        "min",
        "h",
        "hr",
        "day",
        "yr",
        "px",
        "bit",
        "B",
        "atom",
        "cycle",
        "sr",
        "rad",
        "Ω",
        "ohm",
        "S",
        # `M-1` (molar, e.g. rate constants in `M⁻¹ s⁻¹`) -- genuinely
        # missing from the unit table; added for `ascii-minus-exponent`.
        "M",
    }
)

#: UTF-8 superscript digits -- stripped off the *tail* of a denominator
#: before the accepted-set lookup (e.g. a trailing power like `cm³`).
_SUPERSCRIPT_DIGITS = "⁰¹²³⁴⁵⁶⁷⁸⁹"

#: Trailing UTF-8 superscript digits/exponent chars stripped from a
#: denominator before the accepted-set lookup (`cm²/Vs` -> strip `²` from
#: the *numerator* text separately -- here it's for a denominator that
#: itself carries a trailing power, e.g. `.../cm³`).
_SUPERSCRIPT_TAIL_RE = re.compile(f"[{_SUPERSCRIPT_DIGITS}]+$")

#: A letter-run following a `/` -- the candidate denominator to classify.
_DENOMINATOR_RUN_RE = re.compile(f"/([A-Za-zµμΩ]+[{_SUPERSCRIPT_DIGITS}]*)")

#: A differential-quotient denominator (`dI/dV`, `dR/dT`, ...) -- physics
#: shorthand where `d<Symbol>` is half of a derivative, not a compound
#: unit; the ambiguity the two-denominator rule guards against (two units
#: bundled on one solidus) doesn't apply here, so it's a carve-out on top
#: of the accepted-unit set rather than an entry in it.
_DIFFERENTIAL_DENOMINATOR_RE = re.compile(r"^d[A-Z]$")

_DIGIT_GROUPING_RE = re.compile(r"\d,\d{3}(?!\d)")
_ASCII_MULT_RE = re.compile(r"\d\s*x\s*10")

#: `~` before a numeral, digit run NOT immediately followed by `/` --
#: `~19` (approximation) fires, `~ 1/W` (proportionality -- an expression,
#: not a bare quantity, on the right) does not. Canon: "`~` is overloaded"
#: (`docs/backlog/nanopub-corpus-remediation.md` Phase 0).
_TILDE_APPROX_RE = re.compile(r"~\s*\d+(?!\s*/)")

#: Unit-symbol alternation for the negative-exponent rule below --
#: `_ACCEPTED_DENOMINATORS` sorted longest-first so e.g. `MeV` wins over a
#: shorter same-prefix alternative tried at the same start position.
_UNIT_TOKEN_ALT = "|".join(
    re.escape(u) for u in sorted(_ACCEPTED_DENOMINATORS, key=len, reverse=True)
)

#: `ascii-minus-exponent`: an accepted unit symbol, standalone (the
#: negative lookbehind means not itself the tail of a longer letter run --
#: so `M` in `ZSM-5` never matches even though `M` is itself an accepted
#: unit, because it's preceded by `S`), immediately left of `-<digits>`,
#: optionally through a caret (`V^-1`). Shared by `lint_notation`'s
#: detector and `normalize_notation`'s rewriter (below) so they can't
#: drift apart -- a match found by this regex IS, by construction, a real
#: negative exponent, never a compound/series-name hyphen (`Fe-ZSM-5`,
#: `MOF-74`, `UiO-66`, `HKUST-1`, `sub-10-nm`).
_ASCII_MINUS_EXP_RE = re.compile(rf"(?<![A-Za-zµμΩ])({_UNIT_TOKEN_ALT})\^?-(\d+)\b")

#: A caret followed by an uppercase letter is a letter superscript
#: (`2^N`) -- canon keeps these ASCII (no Unicode subscript `d`/`g`, no
#: consistent modifier-superscript rendering), so it is exempt from the
#: caret-exponent warning. `10^5`, `cm^2`, `V^-1` (digit or `+`/`-` after
#: the caret) still fire.
_CARET_EXPONENT_RE = re.compile(r"\^(?![A-Z])")

# ── canon v2: ASCII -> UTF-8 symbol-respelling table ────────────────────
# Each of these is a spelling fix, not a unit conversion -- explicitly
# exempt from the "never convert the paper's unit" carve-out (canon v2,
# `docs/backlog/nanopub-corpus-remediation.md` Phase 0).

#: A numeral immediately left of a spelled-out unit name, with an optional
#: single space or hyphen between. SI writes a unit *symbol* with a
#: numerical value and the unit *name* in words, so `50 micrometres` ->
#: `50 µm` is a respelling but `micron-scale particles` -> `µm-scale
#: particles` is not -- there is no value for the symbol to qualify. A
#: corpus dry run (2026-08-20) found every unqualified name->symbol rule
#: firing on adjectival prose: `micron-scale`, `micrometre-dimension`,
#: `nanometer to micrometer scale`, `ten micrometres`, and a sentence-
#: opening `Microsecond` rewritten to `µs`. That is the same defect shape
#: as the pre-`_ACCEPTED_DENOMINATORS` `ascii-minus-exponent` rule, one
#: family over: a rule that reads a *pattern* where it must read a *role*.
#: `_OHM_RE` and `_ANGSTROM_RE` deliberately do NOT take this guard -- an
#: SI prefix intervenes (`40 kOhm`) and `per angstrom` is a correct
#: unqualified use; both were inspected at 12/12 and 17/17 correct.
_NUMERAL_LEFT = r"(?P<num>\d)(?P<sep>\s?-?\s?)"

#: Tolerance `±` needs a numeral on BOTH sides. Without the right-hand
#: guard this fires on an oxidation state followed by a hyphenated
#: compound adjective and destroys it: `Zn2+-sensing` -> `Zn2±sensing`,
#: `Ni2+-binding` -> `Ni2±binding` (2 of its 4 corpus fires, 2026-08-20).
#: `Zn2±sensing` is not a notation variant of the input, it is a different
#: and meaningless string -- and it would have been signed into an artifact.
_PLUSMINUS_RE = re.compile(r"(?<=\d)(\s?)(?:\+/-|\+-)(\s?)(?=\d)")

#: `ug` reading as a mass unit -- preceded by a digit (with optional
#: space), or immediately followed by `/` (`ug/mL`). Word-bounded so it
#: never matches inside an ordinary word (`bug`, `plug`).
_UG_UNIT_RE = re.compile(r"\d\s?ug\b|\bug/")

#: `micro` spelled out as an SI prefix in front of a recognised unit word
#: -- deliberately narrow (a fixed unit-word list) so it does not fire on
#: `microscopy`/`microscope`/`microstructure`/`microorganism`, none of
#: which are a prefix+unit compound.
_MICRO_PREFIX_RE = re.compile(
    _NUMERAL_LEFT + r"micro(?P<unit>"
    r"molar|watts?|amperes?|amps?|volts?|grams?|liters?|litres?|"
    r"seconds?|farads?|henr(?:y|ies)|siemens|newtons?|pascals?|joules?"
    r")\b",
    re.IGNORECASE,
)

#: `rees?` (not `rees`) so the SINGULAR `1 degree C` matches. The plural-
#: only alternation silently half-converted `80 to 60 degrees C at 1
#: degree C/min` -- the first occurrence became `°C`, the second stayed
#: ASCII, leaving two spellings of one unit in one sentence, which is
#: worse than either input. A rule that rewrites some occurrences of a
#: unit must rewrite all of them.
_DEGREES_C_RE = re.compile(_NUMERAL_LEFT + r"deg(?:rees?)?\.?\s?C\b")

_OHM_RE = re.compile(r"\b([kM]?)([Oo]hm)s?\b")

_ANGSTROM_RE = re.compile(r"\bÅngströms?\b|\bAngstroms?\b", re.IGNORECASE)

_MICROMETRE_WORD_RE = re.compile(
    _NUMERAL_LEFT + r"(?:micromet(?:er|re)s?|microns?)\b", re.IGNORECASE
)
#: `um` reading as a length unit -- same preceded-by-digit / followed-by-
#: `/` guard as `_UG_UNIT_RE`, so `album`/`forum`/`vacuum` never match.
_UM_UNIT_RE = re.compile(r"\d\s?um\b|\bum/")

#: Scientific E-notation -- `1e3`, `4.6E-6`. Digit required immediately
#: before `e`/`E` so this never fires on an ordinary word.
_E_NOTATION_RE = re.compile(r"\b(\d+(?:\.\d+)?)[eE]([+-]?\d+)\b")

#: `≈`-spacing: a "symbol" is a single letter/Greek letter, or an
#: identifier with a subscript underscore (`K_d`, `E_g`, `ΔG_aq`, `R_Q`)
#: -- deliberately narrow so ordinary words ("reaches", "near") never
#: match (they have no underscore and are rarely a single letter).
_APPROX_SYMBOL_RE = re.compile(
    r"^[A-Za-zΑ-Ωα-ωΔδ]$|^[A-Za-zΑ-Ωα-ωΔδ][A-Za-z0-9]*_[A-Za-z0-9]+$"
)
_APPROX_TOKEN_RE = re.compile(r"[^\s]+$")

# ── canon v3: numeric-range dash, ASCII multiplier, formula subscript ───

#: `hyphen-numeric-range`: two bare numbers joined by an ASCII hyphen,
#: immediately (optionally through one space) followed by a unit-ish
#: token -- `300-800 mg/g`, `10-20 Mt`, `0.7-0.9`. Three guards keep
#: nomenclature hyphens out, found by a corpus dry run:
#: `(?<![-A-Za-z\d])` before the first number excludes a hyphen/letter/
#: digit immediately to its left, so a *named-method* hyphen (`M06-2X`,
#: a DFT functional) never fires (the `06` would otherwise read as the
#: left half of a range) and neither does a mid-chain member of a longer
#: hyphenated run; `(?!-\d)` after the second number excludes a
#: `-<digit>` immediately to its right, so a three-way nomenclature chain
#: (`5-8-5`, a Stone-Wales defect label) never fires on either of its two
#: hyphens. Compound/series names (`Fe-ZSM-5`, `UiO-66`, `MIL-101`,
#: `HKUST-1`) already fail to match because they never have a digit on
#: *both* sides of the relevant hyphen. `10-100x` (a ratio range) still
#: fires -- `x` is itself a unit-ish token here -- correctly, per canon.
_HYPHEN_NUMERIC_RANGE_RE = re.compile(
    r"(?<![-A-Za-z\d])(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)(?!-\d)(?=\s?[A-Za-zµ°%])"
)

#: `ascii-x-multiplier`: a bare ASCII `x` immediately after a digit,
#: word-bounded on the right -- `1.61x`, `4-8x`, `100x`. The lookbehind
#: means the `x` must directly abut the digit (no space), and the
#: trailing `\b` means a grid-style label like `2x2` never fires (`x` is
#: followed by another digit, so no word boundary sits between them).
_ASCII_X_MULT_RE = re.compile(r"(?<=\d)x\b")

#: `formula-ascii-subscript` (`lint_notation` only -- see module
#: docstring "canon v3" for why this is never auto-rewritten): a closed,
#: case-sensitive periodic-table symbol list, so a match requires a real
#: element spelling, not any letter. `(?<![-A-Za-z])` before the symbol
#: keeps it from matching mid-acronym (the `S` in `ZSM-5` is preceded by
#: `Z`, so it's excluded even though `S` alone is a real symbol);
#: `(?![+-])` after the digits excludes the one *unambiguous* false-
#: positive family found in the dry run -- ASCII ionic-charge digits
#: (`Mg2+`, `Gd3+`), which want a superscript, not a subscript, so
#: flagging them under this code would misdirect a fix.
_ELEMENT_SYMBOLS: tuple[str, ...] = (
    "H", "He", "Li", "Be", "B", "C", "N", "O", "F", "Ne", "Na", "Mg", "Al", "Si", "P",
    "S", "Cl", "Ar", "K", "Ca", "Sc", "Ti", "V", "Cr", "Mn", "Fe", "Co", "Ni", "Cu",
    "Zn", "Ga", "Ge", "As", "Se", "Br", "Kr", "Rb", "Sr", "Y", "Zr", "Nb", "Mo", "Tc",
    "Ru", "Rh", "Pd", "Ag", "Cd", "In", "Sn", "Sb", "Te", "I", "Xe", "Cs", "Ba", "La",
    "Ce", "Pr", "Nd", "Pm", "Sm", "Eu", "Gd", "Tb", "Dy", "Ho", "Er", "Tm", "Yb", "Lu",
    "Hf", "Ta", "W", "Re", "Os", "Ir", "Pt", "Au", "Hg", "Tl", "Pb", "Bi",
)  # fmt: skip
_ELEMENT_ALT = "|".join(
    re.escape(e) for e in sorted(_ELEMENT_SYMBOLS, key=len, reverse=True)
)
_FORMULA_ASCII_SUBSCRIPT_RE = re.compile(
    rf"(?<![-A-Za-z])(?:{_ELEMENT_ALT})\d+(?![+-])"
)


_SUPERSCRIPT_DIGIT_MAP = str.maketrans("0123456789", _SUPERSCRIPT_DIGITS)

#: UTF-8 subscript digits -- used only by `normalize_notation`'s simple-TeX
#: rewrite (`$_{60}$` -> `₆₀`); `lint_notation` has no subscript rule (a
#: subscript-digit fragment like `C$_{60}$` is caught by `tex-residue`
#: instead, since the raw `$`/`\` presence is the load-bearing signal).
_SUBSCRIPT_DIGITS = "₀₁₂₃₄₅₆₇₈₉"
_SUBSCRIPT_DIGIT_MAP = str.maketrans("0123456789", _SUBSCRIPT_DIGITS)


def _format_superscript_exponent(exp: str) -> str:
    """Render an ASCII exponent (``'-6'``, ``'+3'``, ``'3'``) as UTF-8
    superscript digits, e.g. ``'-6'`` -> ``'⁻⁶'``."""
    sign = ""
    if exp and exp[0] in "+-":
        if exp[0] == "-":
            sign = "⁻"
        exp = exp[1:]
    return sign + exp.translate(_SUPERSCRIPT_DIGIT_MAP)


def _check_approx_spacing(sentence: str) -> str | None:
    """Return one ``approx-spacing`` warning for the first ``≈`` whose
    spacing doesn't match its context, or ``None``.

    Quantity context (nothing, or a non-symbol word, to the left) wants no
    space before the number: ``≈1 Å``. Relation context (a symbol -- single
    letter or ``K_d``-style subscript identifier -- immediately to the
    left) wants a space on both sides: ``n ≈ 10²²``.
    """
    for i, ch in enumerate(sentence):
        if ch != "≈":
            continue
        before = sentence[:i]
        after = sentence[i + 1 :]
        left_attached = bool(before) and not before[-1].isspace()

        if left_attached:
            tm = _APPROX_TOKEN_RE.search(before)
            token = tm.group(0) if tm else ""
            if _APPROX_SYMBOL_RE.match(token):
                return (
                    f"approx-spacing: {token}≈… found -- a binary relation "
                    "with a symbol on the left wants a space on both sides "
                    "of '≈' (e.g. 'n≈10²²' -> 'n ≈ 10²²')."
                )
            continue

        stripped_before = before.rstrip()
        prev_token = ""
        if stripped_before:
            tm = _APPROX_TOKEN_RE.search(stripped_before)
            prev_token = tm.group(0) if tm else ""
        if prev_token and _APPROX_SYMBOL_RE.match(prev_token):
            continue  # 'n ≈ 10²²' -- correct relation form

        after_stripped = after.lstrip()
        if after != after_stripped and after_stripped[:1].isdigit():
            return (
                "approx-spacing: '≈ …' found -- '≈' modifying a bare "
                "quantity takes no space (e.g. '≈ 1 Å' -> '≈1 Å'); a space "
                "on both sides is only for a binary relation with a symbol "
                "on the left ('n ≈ 10²²')."
            )
    return None


def lint_notation(sentence: str) -> list[str]:
    """Return human-readable warnings about ``sentence``'s notation.

    Advisory only: never raises (any input, including ``""``, returns a
    list -- possibly empty), never rewrites ``sentence``. Each warning
    names the offending substring so a caller can fix it inline before
    mint/reword.
    """
    if not sentence:
        return []

    warnings: list[str] = []

    m = _CARET_EXPONENT_RE.search(sentence)
    if m:
        warnings.append(
            "caret-exponent: '^' found -- use a UTF-8 superscript "
            "(e.g. 'cm^2' -> 'cm²'), never a caret; a caret followed by an "
            "uppercase letter (e.g. '2^N') is a letter superscript, which "
            "canon keeps ASCII, and is exempt."
        )

    tex_hits = [ch for ch in ("$", "\\") if ch in sentence]
    if tex_hits:
        warnings.append(
            f"tex-residue: {'/'.join(tex_hits)!r} found -- claim sentences are "
            "plain text, never TeX fragments (e.g. 'C$_{60}$' -> 'C₆₀')."
        )

    m = _DIGIT_GROUPING_RE.search(sentence)
    if m:
        warnings.append(
            f"digit-grouping: {m.group(0)!r} found -- no grouping separators "
            "('4,600' -> '4600'); grouping is the single largest hash-splitter."
        )

    m = _ASCII_MULT_RE.search(sentence)
    if m:
        warnings.append(
            f"ascii-multiplication: {m.group(0)!r} found -- use '×' U+00D7, "
            "never 'x' (e.g. '4.6 x 10^-6' -> '4.6×10⁻⁶')."
        )

    m = _TILDE_APPROX_RE.search(sentence)
    if m:
        warnings.append(
            f"tilde-approximation: {m.group(0)!r} found -- use '≈', not '~' "
            "(ambiguous between 'about' and 'of order')."
        )

    m = _ASCII_MINUS_EXP_RE.search(sentence)
    if m:
        warnings.append(
            f"ascii-minus-exponent: {m.group(0)!r} found -- use '⁻' U+207B "
            "for a negative exponent (e.g. 's-1' -> 's⁻¹'); a nomenclature "
            "hyphen (e.g. '5-8-5', 'Fe-ZSM-5', 'MOF-74') is unaffected -- "
            "this only fires when a standalone accepted unit symbol "
            "immediately precedes the hyphen."
        )

    for dm in _DENOMINATOR_RUN_RE.finditer(sentence):
        run = dm.group(1)
        if len(run) < 2:
            continue
        stripped = _SUPERSCRIPT_TAIL_RE.sub("", run)
        if stripped in _ACCEPTED_DENOMINATORS:
            continue
        if _DIFFERENTIAL_DENOMINATOR_RE.fullmatch(stripped):
            continue
        warnings.append(
            f"two-denominator-solidus: '/{run}' found -- a solidus is fine "
            "for a single recognised unit ('mA/µm', 'S/cm') but ambiguous "
            "with two denominators ('cm²/Vs' -> 'cm² V⁻¹ s⁻¹')."
        )

    # ── canon v2: ASCII -> UTF-8 symbol respellings ─────────────────────

    m = _PLUSMINUS_RE.search(sentence)
    if m:
        warnings.append(
            f"ascii-plusminus: {m.group(0)!r} found -- use '±' U+00B1, "
            "never '+/-' or '+-' (e.g. '25 +/- 2 K' -> '25 ± 2 K')."
        )

    m = _UG_UNIT_RE.search(sentence) or _MICRO_PREFIX_RE.search(sentence)
    if m:
        warnings.append(
            f"ascii-micro: {m.group(0)!r} found -- use 'µ' U+00B5, never "
            "'ug' or spelled-out 'micro' as a prefix (e.g. '50 ug' -> "
            "'50 µg', 'micromolar' -> 'µM'); this is a symbol respelling, "
            "not a unit conversion."
        )

    m = _DEGREES_C_RE.search(sentence)
    if m:
        warnings.append(
            f"ascii-degrees: {m.group(0)!r} found -- use ' °C' (space + "
            "'°' U+00B0 + 'C'), never 'degrees C'/'deg C'/'degC' "
            "(e.g. '25 degrees C' -> '25 °C')."
        )

    m = _OHM_RE.search(sentence)
    if m:
        prefix = m.group(1)
        suggestion = f"{prefix}Ω" if prefix else "Ω"
        warnings.append(
            f"ascii-ohm: {m.group(0)!r} found -- use {suggestion!r} "
            "('Ω' U+03A9), never spelled-out 'Ohm'/'ohm' "
            "(e.g. 'kOhm' -> 'kΩ')."
        )

    m = _ANGSTROM_RE.search(sentence)
    if m:
        warnings.append(
            f"ascii-angstrom: {m.group(0)!r} found -- use 'Å' U+00C5, "
            "never spelled out 'Angstrom'/'Ångström'."
        )

    m = _MICROMETRE_WORD_RE.search(sentence) or _UM_UNIT_RE.search(sentence)
    if m:
        warnings.append(
            f"ascii-micrometre: {m.group(0)!r} found -- use 'µm', never "
            "'micrometer'/'micrometre'/'micron' spelled out, or 'um' as a "
            "unit (e.g. '500 um' -> '500 µm')."
        )

    m = _E_NOTATION_RE.search(sentence)
    if m:
        suggestion = f"{m.group(1)}×10{_format_superscript_exponent(m.group(2))}"
        warnings.append(
            f"e-notation: {m.group(0)!r} found -- use '×10' + a UTF-8 "
            "superscript, never scientific E-notation "
            f"(e.g. {m.group(0)!r} -> {suggestion!r})."
        )

    approx_warning = _check_approx_spacing(sentence)
    if approx_warning:
        warnings.append(approx_warning)

    # ── canon v3 ─────────────────────────────────────────────────────────

    m = _HYPHEN_NUMERIC_RANGE_RE.search(sentence)
    if m:
        warnings.append(
            f"hyphen-numeric-range: {m.group(0)!r} found -- use '–' U+2013 "
            "(en dash), unspaced, for a numeric range before a unit "
            "(e.g. '300-800 mg/g' -> '300–800 mg/g'); a nomenclature "
            "hyphen (e.g. 'Fe-ZSM-5', 'UiO-66') is unaffected -- this only "
            "fires when a digit sits on both sides of the hyphen."
        )

    m = _ASCII_X_MULT_RE.search(sentence)
    if m:
        warnings.append(
            f"ascii-x-multiplier: {m.group(0)!r} found -- use '×' U+00D7 "
            "for a multiplier directly after a digit, never 'x' "
            "(e.g. '4-8x acceleration' -> '4-8× acceleration')."
        )

    m = _FORMULA_ASCII_SUBSCRIPT_RE.search(sentence)
    if m:
        warnings.append(
            f"formula-ascii-subscript: {m.group(0)!r} found -- an element "
            "symbol immediately followed by ASCII digits may want a "
            "Unicode subscript (e.g. 'C60' -> 'C₆₀', 'NH3' -> 'NH₃'); "
            "check by hand -- this also matches non-stoichiometric digits "
            "(a chain-length label like 'C10-DNTT', a named method like "
            "'B3LYP', a crown-ether abbreviation like 'DB18C6'), which "
            "must NOT be subscripted, so this code is advisory only and "
            "`normalize_notation` never rewrites it."
        )

    return warnings


# ── normalize_notation: deterministic fixes, mechanically-unambiguous only ─
#
# Separate, narrower regexes from the lint ones above -- a lint pattern only
# has to prove *presence*, a normalize pattern has to capture exactly the
# span to rewrite without touching a neighbouring digit/word. Reused where
# the lint regex's groups already suffice (`_OHM_RE`, `_ANGSTROM_RE`,
# `_MICRO_PREFIX_RE`, `_DEGREES_C_RE`, `_MICROMETRE_WORD_RE`,
# `_E_NOTATION_RE`, `_PLUSMINUS_RE`, `_ASCII_MINUS_EXP_RE` -- the latter
# reused as-is, not just its groups, so the detector and the rewriter can
# never drift on which hyphens count as nomenclature); new ones where the
# lint version only proves presence (`caret-exponent`) or could consume a
# neighbouring digit it must leave alone (`ug`/`um` as a unit).

#: `ug`/`um` as a unit -- lookaround instead of `_UG_UNIT_RE`'s consuming
#: `\d`, so the match is exactly the letters to rewrite and never eats the
#: preceding digit.
_UG_UNIT_SUB_RE = re.compile(r"(?<=\d)\s?ug\b|\bug(?=/)")
_UM_UNIT_SUB_RE = re.compile(r"(?<=\d)\s?um\b|\bum(?=/)")

#: Full grouped-number span (`4,600`, `1,234,567`), not just the lint
#: existence-check's rightmost triplet.
_DIGIT_GROUPING_FULL_RE = re.compile(r"\d{1,3}(?:,\d{3})+")

#: Full mantissa before `x 10`, not just the lint check's single leading
#: digit -- `4.6 x 10` must rewrite to `4.6×10`, not lose the `4.`.
_ASCII_MULT_SUB_RE = re.compile(r"(\d+(?:\.\d+)?)\s*x\s*(10)")

#: A caret followed by a signed digit run -- letter superscripts (`2^N`)
#: never match `\d`, so canon's "letters stay ASCII" rule holds for free.
_CARET_DIGIT_SUB_RE = re.compile(r"\^([+-]?\d+)")

#: Simple TeX sub/superscript fragments only (`$_{60}$`, `$^{2}$`) -- a
#: `\mu_B$`-style fragment has no closed rewrite and is left for a human;
#: it still surfaces via `tex-residue` in `lint_notation`.
_TEX_SUBSCRIPT_SIMPLE_RE = re.compile(r"\$_\{(\d+)\}\$")
_TEX_SUPERSCRIPT_SIMPLE_RE = re.compile(r"\$\^\{(\d+)\}\$")

#: `micro<unit>` -> `µ<abbreviation>`. Keys are the same alternatives as
#: `_MICRO_PREFIX_RE`'s capture group, lowercased; extend both together.
_MICRO_UNIT_ABBREV: dict[str, str] = {
    "molar": "M",
    "watt": "W",
    "watts": "W",
    "ampere": "A",
    "amperes": "A",
    "amp": "A",
    "amps": "A",
    "volt": "V",
    "volts": "V",
    "gram": "g",
    "grams": "g",
    "liter": "L",
    "liters": "L",
    "litre": "L",
    "litres": "L",
    "second": "s",
    "seconds": "s",
    "farad": "F",
    "farads": "F",
    "henry": "H",
    "henries": "H",
    "siemens": "S",
    "newton": "N",
    "newtons": "N",
    "pascal": "Pa",
    "pascals": "Pa",
    "joule": "J",
    "joules": "J",
}


def _num_unit(m: re.Match[str], symbol: str) -> str:
    """Re-emit a `_NUMERAL_LEFT`-guarded match as value + separator +
    unit symbol.

    An empty separator becomes a space (canon separates a value from a
    unit symbol), while an explicit hyphen is preserved -- `1-µm colloids`
    is a compound adjective, not a spacing error.
    """
    sep = m.group("sep") or " "
    return f"{m.group('num')}{sep}{symbol}"


def _sub_micro_prefix(m: re.Match[str]) -> str:
    """`4 micromolar` -> `4 µM`, etc. -- unmapped unit words (never happens
    given `_MICRO_PREFIX_RE`'s closed alternation, but kept defensive)
    pass through unchanged rather than losing text."""
    abbrev = _MICRO_UNIT_ABBREV.get(m.group("unit").lower(), "")
    return _num_unit(m, f"µ{abbrev}") if abbrev else m.group(0)


def _apply(
    sentence: str,
    pattern: re.Pattern[str],
    repl: str | Callable[[re.Match[str]], str],
    code: str,
    applied: list[str],
) -> str:
    """Run one ``pattern.subn`` step; record ``code`` in ``applied`` iff it
    changed something. Shared plumbing for every rule in
    :func:`normalize_notation`."""
    new_sentence, n = pattern.subn(repl, sentence)
    if n:
        applied.append(code)
    return new_sentence


def normalize_notation(sentence: str) -> tuple[str, list[str]]:
    """Deterministically rewrite ``sentence`` to canon v2 notation, for the
    mechanically-unambiguous rule classes only.

    Returns ``(normalized_sentence, applied_codes)`` -- ``applied_codes``
    lists (in application order) which `lint_notation` codes this call
    actually fixed, e.g. ``["digit-grouping", "ascii-ohm"]``; empty if
    nothing changed. Pure and DB-free -- safe to run over a CSV dump
    outside any DB session.

    Applies: `digit-grouping`, `tex-residue` (simple ``$_{60}$``/``$^{2}$``
    forms only), `ascii-multiplication`, `e-notation`, `caret-exponent`
    (digit exponents only -- a letter superscript like ``2^N`` is left
    alone per canon), `ascii-minus-exponent`, `ascii-plusminus`,
    `ascii-micro`, `ascii-degrees`, `ascii-ohm`, `ascii-angstrom`,
    `ascii-micrometre`, `hyphen-numeric-range`, `ascii-x-multiplier`,
    `no-terminal-period`.

    Never touches `two-denominator-solidus` (which factor moves under the
    exponent is a judgment call), `over-long`, `multi-assertion`, or
    anything in `sentence_lint` -- those stay advisory-only. Letter
    sub/superscripts (`K_d`, `E_g`, `2^N`) are left ASCII, per canon v2.
    `formula-ascii-subscript` (canon v3) is *never* applied here even
    though `lint_notation` detects it -- see the module docstring's
    "canon v3" paragraph for why a naive element+digits rewrite corrupts
    nomenclature (crown ethers, chain-length labels, ionic charges) too
    often to be mechanically safe.
    """
    if not sentence:
        return "", []

    applied: list[str] = []
    s = sentence

    s = _apply(
        s,
        _DIGIT_GROUPING_FULL_RE,
        lambda m: m.group(0).replace(",", ""),
        "digit-grouping",
        applied,
    )
    s = _apply(
        s,
        _TEX_SUBSCRIPT_SIMPLE_RE,
        lambda m: m.group(1).translate(_SUBSCRIPT_DIGIT_MAP),
        "tex-residue",
        applied,
    )
    s = _apply(
        s,
        _TEX_SUPERSCRIPT_SIMPLE_RE,
        lambda m: m.group(1).translate(_SUPERSCRIPT_DIGIT_MAP),
        "tex-residue",
        applied,
    )
    s = _apply(s, _ASCII_MULT_SUB_RE, r"\1×\2", "ascii-multiplication", applied)
    s = _apply(
        s,
        _E_NOTATION_RE,
        lambda m: f"{m.group(1)}×10{_format_superscript_exponent(m.group(2))}",
        "e-notation",
        applied,
    )
    s = _apply(
        s,
        _CARET_DIGIT_SUB_RE,
        lambda m: _format_superscript_exponent(m.group(1)),
        "caret-exponent",
        applied,
    )
    s = _apply(
        s,
        _ASCII_MINUS_EXP_RE,
        lambda m: m.group(1) + _format_superscript_exponent("-" + m.group(2)),
        "ascii-minus-exponent",
        applied,
    )
    s = _apply(s, _PLUSMINUS_RE, r"\1±\2", "ascii-plusminus", applied)

    micro_applied: list[str] = []
    s = _apply(
        s,
        _UG_UNIT_SUB_RE,
        lambda m: m.group(0).replace("ug", "µg"),
        "ascii-micro",
        micro_applied,
    )
    s = _apply(s, _MICRO_PREFIX_RE, _sub_micro_prefix, "ascii-micro", micro_applied)
    if micro_applied:
        applied.append("ascii-micro")

    s = _apply(s, _DEGREES_C_RE, lambda m: _num_unit(m, "°C"), "ascii-degrees", applied)
    s = _apply(
        s,
        _OHM_RE,
        lambda m: f"{m.group(1)}Ω",
        "ascii-ohm",
        applied,
    )
    s = _apply(s, _ANGSTROM_RE, "Å", "ascii-angstrom", applied)

    micrometre_applied: list[str] = []
    s = _apply(
        s,
        _MICROMETRE_WORD_RE,
        lambda m: _num_unit(m, "µm"),
        "ascii-micrometre",
        micrometre_applied,
    )
    s = _apply(
        s,
        _UM_UNIT_SUB_RE,
        lambda m: m.group(0).replace("um", "µm"),
        "ascii-micrometre",
        micrometre_applied,
    )
    if micrometre_applied:
        applied.append("ascii-micrometre")

    s = _apply(
        s,
        _HYPHEN_NUMERIC_RANGE_RE,
        lambda m: f"{m.group(1)}–{m.group(2)}",
        "hyphen-numeric-range",
        applied,
    )
    s = _apply(s, _ASCII_X_MULT_RE, "×", "ascii-x-multiplier", applied)

    stripped = s.rstrip()
    if stripped and stripped[-1] not in ".?!:":
        s = stripped + "."
        applied.append("no-terminal-period")

    return s, applied
