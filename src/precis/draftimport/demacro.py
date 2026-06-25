r"""Inline LaTeX -> draft-markdown cleanup (the de-macro pass).

Structure is handled elsewhere (``tex.build_tree`` / ``tex.plan_blocks``);
this turns the *inline* LaTeX inside a chunk's text into clean draft prose.

Two macro families get principled, not ad-hoc, handling:

* **value/constant macros** (``\boxelsize`` -> ``{\ensuremath{\sim}}7\,nm``):
  harvested from the preamble's ``\newcommand`` definitions and expanded
  inline (:func:`harvest_macros`). This is the general answer to "expand
  ``\boxelsize``" — every zero-arg constant is handled the same way.
* **acronyms** (``\gls{mof}`` -> "MOF"): the real abbreviation analog. We
  read the ``\newacronym`` table and expand to the short form here; the
  richer option (import each as a draft ``term`` and keep the glossary
  link) layers on top of the same table.

Cross-references and citations become *deferred* tokens the writing pass
resolves once chunks (and their handles) exist:

* ``\ref{lab}`` / ``\cref{lab}`` -> ``[¶@lab]``  (the chunk carrying
  ``\label{lab}`` — "a mention of the other chunk")
* ``\cite{k}`` / ``\mciteboxpE{k}{p}{q}`` -> ``[§slug]`` when a keymap is
  supplied (else ``[§k]``); the verbatim quote is carried separately for
  the ``~N`` chunk anchor.
"""

from __future__ import annotations

import re
from typing import Any

from precis.draftimport.tex import _balanced_groups, extract_cites

# --------------------------------------------------------------------------
# harvest custom definitions
# --------------------------------------------------------------------------

_NEWCMD = re.compile(
    r"\\(?:newcommand|renewcommand|providecommand)\*?\s*\{?\\([a-zA-Z]+)\}?\s*(\[\d+\])?\s*\{"
)
_ACRO = re.compile(
    r"\\newacronym\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}\s*\{([^}]+)\}\s*\{([^}]+)\}"
)


# Never treat these as expandable value macros even if the book
# \renewcommand's them — they are structure/citations handled upstream,
# and expanding them would corrupt the heading walk or the cite pass.
_NEVER_HARVEST = {
    "part",
    "chapter",
    "section",
    "subsection",
    "subsubsection",
    "paragraph",
    "subparagraph",
    "item",
    "caption",
    "label",
    "ref",
    "cite",
    "gls",
    "glspl",
}


def harvest_macros(preamble: str) -> dict[str, str]:
    """``{name: body}`` for every *zero-arg* ``\\newcommand`` (value macros).
    Macros taking arguments (``[n]``) — and structural/citation commands —
    are skipped; they aren't constants."""
    out: dict[str, str] = {}
    for m in _NEWCMD.finditer(preamble):
        name, nargs = m.group(1), m.group(2)
        if nargs or name in _NEVER_HARVEST or name.startswith(("cite", "mcitebox")):
            continue
        body, _ = _balanced_groups(preamble, m.end() - 1, 1)
        if body:
            out[name] = body[0]
    return out


def harvest_acronyms(glossary: str) -> dict[str, tuple[str, str]]:
    """``{label: (short, long)}`` from ``\\newacronym`` entries."""
    return {m.group(1): (m.group(2), m.group(3)) for m in _ACRO.finditer(glossary)}


#: bodies we can't expand by simple substitution (computed / stateful) —
#: those macros are handled explicitly or dropped, never param-expanded.
_COMPUTED = (
    "\\numexpr",
    "\\the",
    "\\csname",
    "\\ding",
    "\\value",
    "\\arabic",
    "\\ifthenelse",
    "\\protect",
    "\\index",
    "\\gls",
)
#: names handled explicitly elsewhere (dropped / special-cased) — never harvest.
_EXPLICIT = {
    "defsite",
    "cn",
    "mglsbox",
    "mrev",
    "mspecbox",
    "mtechq",
    "mxrefbox",
    "mpaper",
}


def harvest_param_macros(preamble: str) -> dict[str, tuple[int, str]]:
    """``{name: (nargs, body)}`` for custom *n-arg* macros (e.g.
    ``\\POR[1]{\\textbf{Plan of Record:} #1}``). Expanding these from their
    own definitions — rather than hard-coding book-specific names —
    generalises the ``\\boxelsize`` approach to content macros. Macros with
    computed/stateful bodies, citation/structural commands, and the
    explicitly-handled set are skipped."""
    out: dict[str, tuple[int, str]] = {}
    for m in _NEWCMD.finditer(preamble):
        name, nargs = m.group(1), m.group(2)
        if (
            not nargs
            or name in _NEVER_HARVEST
            or name in _EXPLICIT
            or name.startswith(("cite", "mcitebox"))
        ):
            continue
        body, _ = _balanced_groups(preamble, m.end() - 1, 1)
        if body and not any(tok in body[0] for tok in _COMPUTED):
            out[name] = (int(nargs[1:-1]), body[0])
    return out


def _expand_params(text: str, pmac: dict[str, tuple[int, str]], rounds: int = 3) -> str:
    """Expand harvested n-arg macros via ``#i`` substitution (bounded rounds
    for macros that reference macros)."""
    for _ in range(rounds):
        before = text
        for name, (n, body) in pmac.items():

            def _sub(a: list[str], body: str = body, n: int = n) -> str:
                out = body
                for i in range(n):
                    out = out.replace(f"#{i + 1}", a[i] if i < len(a) else "")
                return out

            text = _replace_cmd(text, name, n, _sub)
        if text == before:
            break
    return text


# --------------------------------------------------------------------------
# brace-aware command replacement
# --------------------------------------------------------------------------


def _replace_cmd(text: str, name: str, nargs: int, render) -> str:
    """Replace every ``\\name`` + ``nargs`` balanced groups via ``render(args)``.
    Leaves longer command names (``\\name...``) untouched."""
    pat = "\\" + name
    out: list[str] = []
    i = 0
    while True:
        j = text.find(pat, i)
        if j < 0:
            out.append(text[i:])
            break
        k = j + len(pat)
        if k < len(text) and text[k].isalpha():  # \namefoo — different command
            out.append(text[i:k])
            i = k
            continue
        out.append(text[i:j])
        args, k2 = _balanced_groups(text, k, nargs)
        if len(args) < nargs:
            out.append(text[j:k])
            i = k
            continue
        out.append(render(args))
        i = k2
    return "".join(out)


def strip_annotations(text: str) -> str:
    """Remove editorial annotation macros (``\\mrev``, ``\\mtechq``, …) from
    RAW text *before* structural splitting.

    Their arguments contain paragraph breaks, so the block splitter would
    otherwise cut one mid-argument — leaving unbalanced braces in two chunks
    and leaking the macro. Stripping them up front (balanced, paragraph-
    spanning) avoids that. They're dropped from the draft regardless."""
    for name, n in _DROP_MULTI:
        text = _replace_cmd(text, name, n, lambda _a: "")
    return text


#: annotation macros captured as in-context notes -> (note type, arity).
_NOTE_MACROS = {"mtechq": ("techq", 2), "mrev": ("review", 3)}


def extract_annotations(text: str) -> tuple[str, list[dict]]:
    """Capture ``\\mtechq`` / ``\\mrev`` as ``⟦note:N⟧`` sentinels and drop the
    other annotation macros, on RAW text *before* structural splitting.

    The sentinel is a single token, so a multi-paragraph note can't split a
    chunk, and it rides into the chunk where the macro sat — the writing pass
    then materialises an in-context ``note`` chunk there. Returns
    ``(text_with_sentinels, notes)`` where ``notes[N]`` is
    ``{type, code, sev?, text}`` (``text`` is the raw note body, de-macroed at
    chunk-creation time)."""
    notes: list[dict] = []

    def _capture(ntype: str):
        def _r(args: list[str]) -> str:
            i = len(notes)
            if ntype == "review":
                notes.append(
                    {
                        "type": "review",
                        "code": args[0].strip(),
                        "sev": args[1].strip().lower(),
                        "text": args[2].strip(),
                    }
                )
            else:
                notes.append(
                    {"type": "techq", "code": args[0].strip(), "text": args[1].strip()}
                )
            return f"⟦note:{i}⟧"

        return _r

    for name, (ntype, nargs) in _NOTE_MACROS.items():
        text = _replace_cmd(text, name, nargs, _capture(ntype))
    for name, n in (("mspecbox", 3), ("mpaper", 2), ("mxrefbox", 2)):
        text = _replace_cmd(text, name, n, lambda _a: "")
    return text, notes


def _expand_values(text: str, macros: dict[str, str], rounds: int = 4) -> str:
    """Expand zero-arg value macros (``\\boxelsize`` -> body), tolerating a
    trailing ``{}`` and macros that reference macros (bounded rounds)."""
    for _ in range(rounds):
        before = text
        for name, body in macros.items():
            text = re.sub(
                r"\\" + name + r"(?![a-zA-Z])(\{\})?",
                (lambda b: lambda _m: b)(body),
                text,
            )
        if text == before:
            break
    return text


# --------------------------------------------------------------------------
# the pass
# --------------------------------------------------------------------------

# glossaries-family command -> surface form to emit (case axis collapses;
# the short is already cased). 'short' is the default for any gls*/acr*.
_GLS_FORM = {
    "gls": "short",
    "Gls": "short",
    "GLS": "short",
    "glspl": "pl",
    "Glspl": "pl",
    "GLSpl": "pl",
    "glsshort": "short",
    "Glsshort": "short",
    "glsxtrshort": "short",
    "acrshort": "short",
    "glslong": "long",
    "Glslong": "long",
    "acrlong": "long",
    "acrfull": "full",
    "Acrfull": "full",
    "glsfirst": "full",
    "glsxtrfull": "full",
}

_DROP_ARG = ["index", "label", "glsadd", "defsite"]  # genuinely 1-arg, \cmd{..} dropped
# editorial annotation + layout macros — scaffolding, not prose.
# (name, arity) dropped whole. \renewcommand/\addcontentsline/\setcounter
# are front-matter/table plumbing that leaks at chunk edges.
_DROP_MULTI = [
    ("mrev", 3),
    ("mspecbox", 3),
    ("mtechq", 2),
    ("mxrefbox", 2),
    ("mpaper", 2),
    ("addcontentsline", 3),
    ("addtocontents", 2),
    ("renewcommand", 2),
    ("setcounter", 2),
]
_DROP_TOKEN = re.compile(
    r"\\(noindent|centering|raggedright|clearpage|newpage|bigskip|medskip|smallskip|"
    r"protect|scriptsize|footnotesize|tiny|small|normalsize|large|Large|LARGE|huge|"
    r"sffamily|ttfamily|rmfamily|bfseries|itshape|upshape|mdseries|hrule|hline|"
    r"toprule|midrule|bottomrule|endhead|linebreak|par|noalign|phantomsection|"
    r"FloatBarrier|maketitle|tableofcontents|listoffigures|listoftables|"
    r"frontmatter|mainmatter|backmatter|arraystretch|allowdisplaybreaks|"
    r"vfill|hfill|dotfill|normalfont|cleardoublepage|null|selectfont|"
    r"textheight|textwidth|columnwidth|linewidth|wordfont)\b"
)
#: zero-arg control-word symbols → their unicode character.
_CMD_SYMBOL = {
    "AA": "Å",
    "aa": "å",
    "AE": "Æ",
    "ae": "æ",
    "O": "Ø",
    "o": "ø",
    "ss": "ß",
    "S": "§",
    "P": "¶",
    "euro": "€",
    "pounds": "£",
    "copyright": "©",
    "checkmark": "✓",
    "dag": "†",
    "ddag": "‡",
    "textbullet": "•",
    "ldots": "…",
    "dots": "…",
    "textellipsis": "…",
    "textendash": "–",
    "textemdash": "—",
    "newline": " ",
    "quad": " ",
    "qquad": " ",
    "textdegree": "°",
    "degree": "°",
    "sim": "∼",
    "times": "×",
    "approx": "≈",
    "pm": "±",
    "leq": "≤",
    "geq": "≥",
    "neq": "≠",
    "cdot": "·",
    "to": "→",
    "alpha": "α",
    "beta": "β",
    "gamma": "γ",
    "delta": "δ",
    "mu": "µ",
    "Omega": "Ω",
    "allowbreak": "",
    "noeject": "",
    "relax": "",
    "LaTeX": "LaTeX",
    "TeX": "TeX",
    "textregistered": "®",
    "texttrademark": "™",
    "textmu": "µ",
    "AAngstrom": "Å",
    "circ": "°",
    "texteuro": "€",
    "DNA": "DNA",
    "textbar": "|",
    "textgreater": ">",
    "textless": "<",
    "textasciitilde": "~",
    "textquotedbl": '"',
    "enspace": " ",
    "space": " ",
    "thinspace": " ",
    "angstrom": "Å",
    "bond": "—",
}
#: accent command + letter → precomposed char (covers the book's names/words).
_ACCENT = {
    "'a": "á",
    "'e": "é",
    "'i": "í",
    "'o": "ó",
    "'u": "ú",
    "'y": "ý",
    "'n": "ń",
    "'c": "ć",
    "'s": "ś",
    "`a": "à",
    "`e": "è",
    "`i": "ì",
    "`o": "ò",
    "`u": "ù",
    '"a': "ä",
    '"e': "ë",
    '"i': "ï",
    '"o': "ö",
    '"u': "ü",
    "^a": "â",
    "^e": "ê",
    "^i": "î",
    "^o": "ô",
    "^u": "û",
    "~n": "ñ",
    "~a": "ã",
    "~o": "õ",
    "cc": "ç",
    "cC": "Ç",
    "=a": "ā",
    "=o": "ō",
    "=e": "ē",
}
# symbol-accents (\'e, \"o, …) may be unbraced; the cedilla \c is letter-led
# and would otherwise swallow \centi / \cdot, so it's handled braced-only.
_ACCENT_RE = re.compile(r"\\(['`\"^~=])\s*\{?([a-zA-Z])\}?")
_CEDILLA_RE = re.compile(r"\\c\{([a-zA-Z])\}")
# siunitx unit/prefix macros -> their symbol (a minimal but real subset).
_UNIT = {
    "siemens": "S",
    "ohm": "Ω",
    "meter": "m",
    "metre": "m",
    "gram": "g",
    "second": "s",
    "volt": "V",
    "ampere": "A",
    "kelvin": "K",
    "mole": "mol",
    "joule": "J",
    "watt": "W",
    "newton": "N",
    "pascal": "Pa",
    "hertz": "Hz",
    "liter": "L",
    "litre": "L",
    "bar": "bar",
    "per": "/",
    "centi": "c",
    "milli": "m",
    "micro": "µ",
    "nano": "n",
    "pico": "p",
    "kilo": "k",
    "mega": "M",
    "giga": "G",
    "cdot": "·",
    "squared": "²",
    "cubed": "³",
    "degree": "°",
    "percent": "%",
    "celsius": "°C",
}
_REF = re.compile(r"\\(?:c?ref|autoref|Cref|eqref|pageref)\s*\{([^}]+)\}")
_SYMBOLS = [
    (r"~", " "),
    (r"\\,", " "),
    (r"\\;", " "),
    (r"\\ ", " "),
    (r"\\@", ""),
    (r"\\%", "%"),
    (r"\\&", "&"),
    (r"\\_", "_"),
    (r"\\#", "#"),
    (r"\\\$", "$"),
    (r"\\textbackslash", "\\"),
    (r"---", "—"),
    (r"--", "–"),
    (r"``", "“"),
    (r"''", "”"),
]


def _cn_circled(a: list[str]) -> str:
    """``\\cn{2}`` -> circled number ② (a back-reference glyph)."""
    try:
        n = int(a[0])
        return chr(0x2460 + n - 1) if 1 <= n <= 20 else f"({n})"
    except (ValueError, IndexError):
        return a[0] if a else ""


def demacro(
    text: str,
    *,
    macros: dict[str, str] | None = None,
    param_macros: dict[str, tuple[int, str]] | None = None,
    acronyms: dict[str, tuple[str, str]] | None = None,
    keymap: dict[str, str] | None = None,
    cite_resolver: Any = None,
) -> str:
    """LaTeX inline -> clean draft prose. ``keymap`` maps cite key -> precis
    slug; absent keys fall through to ``[§key]``. ``cite_resolver(cite)`` — when
    given (the writing pass, with store access) — returns the rendered citation
    (``[pc<id>]`` for a quote-matched paragraph, ``[pa<id>]`` for the paper);
    without it, citations render as the dry-run ``[§slug]``."""
    macros = macros or {}
    param_macros = param_macros or {}
    acronyms = acronyms or {}
    keymap = keymap or {}

    # 0. normalise math + chemistry so later passes leave them alone:
    #    \(..\) inline math -> $..$ (so the commands inside aren't flagged/
    #    mangled); \ce{..}/\ch{..} (mhchem/chemformula) -> the bare formula.
    text = re.sub(r"\\\((.+?)\\\)", lambda m: f"${m.group(1)}$", text, flags=re.DOTALL)
    text = re.sub(r"\\c[eh]\*?\s*\{([^{}]*)\}", lambda m: m.group(1), text)

    # 0b. expand custom value/param macros FIRST — a macro body may inject a
    #     \cite or a \begin{quote} (epigraph macros do), so expanding before the
    #     cite pass means those injected cites get resolved, not leaked.
    text = _expand_values(text, macros)
    text = _expand_params(text, param_macros)
    text = _replace_cmd(text, "cn", 1, _cn_circled)  # \cn{2} -> ②
    text = _replace_cmd(text, "mglsbox", 2, lambda a: a[1])  # glossary box -> expansion

    # 1. citations (incl. \mciteboxp* quotes) -> [§slug]; do first so later
    #    brace-stripping never touches a cite key.
    for c in extract_cites(text):
        if cite_resolver is not None:
            repl = cite_resolver(c)
        else:
            repl = "".join(f"[§{keymap.get(k, k)}]" for k in c.keys)
        # rebuild the exact source span and swap it
        nargs = (
            3
            if c.macro.startswith("mcitebox")
            else {"citeq": 3, "citeqp": 4, "citeqm": 3}.get(c.macro, 1)
        )
        text = _replace_cmd(text, c.macro, nargs, (lambda r: lambda _a: r)(repl))

    # 2. glossaries family -> the plain surface text. This *is* the
    #    abbreviation mechanism: the exporter's _glsify auto-converts a
    #    plain short (and its plural) back to \gls/\glspl once the term is
    #    defined. The word — not a handle — is both how \gls actually fires
    #    and the lighter ask for an editor. (The explicit [surface](¶handle)
    #    form renders as a *hyperlink to the glossary entry*, a different
    #    thing, not an abbreviation use.) The writing pass still defines one
    #    `term` chunk per \newacronym entry so _glsify knows the short.
    #
    #    The package varies along three axes — number (pl), case (\Gls,
    #    \GLS), and form (short / long / full). We collapse to the surface
    #    text; the long-form first-letter capitalisation of \Gls on first
    #    use is the one nuance we drop (the short is already cased).
    for cmd, form in _GLS_FORM.items():

        def _g(a: list[str], form: str = form) -> str:
            short, long = acronyms.get(a[0].strip(), (a[0].strip().upper(), ""))
            if form == "pl":
                return short + "s"
            if form == "long":
                return long or short
            if form == "full":
                return f"{long} ({short})" if long else short
            return short  # 'short'

        text = _replace_cmd(text, cmd, 1, _g)
    text = _replace_cmd(text, "glsdisp", 2, lambda a: a[1])  # custom display text
    text = _replace_cmd(text, "glsadd", 1, lambda _a: "")  # index-only, prints nothing
    # any remaining gls*/acr* variant -> short, so none leaks raw into prose
    text = re.sub(
        r"\\(?:gls|Gls|GLS|acr|Acr)[A-Za-z]*\s*\{([^}]+)\}",
        lambda m: acronyms.get(m.group(1).strip(), (m.group(1).strip().upper(), ""))[0],
        text,
    )

    # 3. siunitx range + drop any env markers a macro body injected (a real
    #    \begin{..} was consumed during structural parsing, so a leftover one
    #    here is from macro expansion — strip the marker, keep the content).
    text = re.sub(
        r"\\SIrange\s*\{([^}]*)\}\s*\{([^}]*)\}\s*\{[^}]*\}",
        lambda m: f"{m.group(1)}–{m.group(2)}",
        text,
    )
    text = re.sub(r"\\(?:begin|end)\{[a-zA-Z*]+\}", "", text)

    # 4. cross-refs -> deferred [¶@label]
    text = _REF.sub(lambda m: f"[¶@{m.group(1).strip()}]", text)

    # 5. drop noise: multi-arg editorial annotations, then single-arg noise
    #    + bare font/rule tokens.
    for name, n in _DROP_MULTI:
        text = _replace_cmd(text, name, n, lambda _a: "")
    for name in _DROP_ARG:
        text = _replace_cmd(text, name, 1, lambda _a: "")
    text = _DROP_TOKEN.sub("", text)

    # 6. formatting -> markdown (bold dropped per draft style; emph -> *italic*)
    text = _replace_cmd(text, "textbf", 1, lambda a: a[0])
    text = _replace_cmd(text, "textit", 1, lambda a: f"*{a[0]}*")
    text = _replace_cmd(text, "emph", 1, lambda a: f"*{a[0]}*")
    text = _replace_cmd(text, "texttt", 1, lambda a: f"`{a[0]}`")
    text = _replace_cmd(text, "textsc", 1, lambda a: a[0])
    text = _replace_cmd(text, "textup", 1, lambda a: a[0])
    text = _replace_cmd(text, "underline", 1, lambda a: a[0])
    text = _replace_cmd(text, "textcolor", 2, lambda a: a[1])  # drop colour, keep text
    text = _replace_cmd(text, "texorpdfstring", 2, lambda a: a[0])  # keep the TeX form
    text = _replace_cmd(text, "textsuperscript", 1, lambda a: f"^{a[0]}")
    text = _replace_cmd(text, "textsubscript", 1, lambda a: a[0])
    text = _replace_cmd(text, "text", 1, lambda a: a[0])
    text = _replace_cmd(text, "ensuremath", 1, lambda a: f"${a[0]}$")
    text = _replace_cmd(text, "href", 2, lambda a: f"[{a[1]}]({a[0]})")
    text = _replace_cmd(text, "url", 1, lambda a: a[0])
    text = _replace_cmd(text, "footnote", 1, lambda a: f" ({a[0].strip()})")
    # spacing/rule commands that take an arg
    for name in (
        "vspace",
        "hspace",
        "vskip",
        "hskip",
        "raisebox",
        "setlength",
        "fontfamily",
        "thispagestyle",
        "pagestyle",
        "addvspace",
    ):
        text = _replace_cmd(text, name, 1, lambda _a: "")
    text = re.sub(r"\\[vh]space\*\s*\{[^}]*\}", "", text)  # starred \vspace*{..}
    text = _replace_cmd(text, "fontsize", 2, lambda _a: "")  # \fontsize{size}{skip}

    # 6b. siunitx -> symbols (\si{\siemens\per\centi\meter} -> "S/cm")
    def _units(inner: str) -> str:
        return re.sub(
            r"\\([a-zA-Z]+)", lambda m: _UNIT.get(m.group(1), m.group(1)), inner
        )

    text = _replace_cmd(text, "si", 1, lambda a: _units(a[0]))
    text = _replace_cmd(text, "unit", 1, lambda a: _units(a[0]))
    text = _replace_cmd(text, "num", 1, lambda a: a[0])
    text = _replace_cmd(text, "SI", 2, lambda a: f"{a[0]} {_units(a[1])}")
    text = _replace_cmd(text, "qty", 2, lambda a: f"{a[0]} {_units(a[1])}")

    # 7. accents (cedilla braced-only; accent-over dotless \i/\j for names like
    #    Mart\'{\i}nez) + control-word symbols + line breaks
    text = _CEDILLA_RE.sub(lambda m: _ACCENT.get("c" + m.group(1), m.group(1)), text)
    text = re.sub(
        r"\\(['`\"^~=])\{?\\([ij])\}?",
        lambda m: _ACCENT.get(m.group(1) + m.group(2), m.group(2)),
        text,
    )
    text = _ACCENT_RE.sub(
        lambda m: _ACCENT.get(m.group(1) + m.group(2), m.group(2)), text
    )
    text = re.sub(r"\\([ij])(?![a-zA-Z])", lambda m: m.group(1), text)  # bare \i/\j
    text = re.sub(
        r"\\([a-zA-Z]+)(?![a-zA-Z])\s?",
        lambda m: _CMD_SYMBOL[m.group(1)] if m.group(1) in _CMD_SYMBOL else m.group(0),
        text,
    )
    text = re.sub(r"\\\\(\[[^\]]*\])?", " ", text)  # row/line breaks -> space
    text = re.sub(r"\\-", "", text)  # discretionary hyphen

    # 8. symbols & spacing (literal replacement — rep is not a regex template)
    for pat, rep in _SYMBOLS:
        text = re.sub(pat, (lambda r: lambda _m: r)(rep), text)
    text = _strip_grouping_braces(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return text.strip()


# --------------------------------------------------------------------------
# deferred-reference resolution (writing pass, once handles exist)
# --------------------------------------------------------------------------

_LABEL_RE = re.compile(r"\\label\s*\{([^}]+)\}")
_DEFERRED_REF = re.compile(r"\[¶@([^\]]+)\]")


def labels_in(raw: str) -> list[str]:
    """Every ``\\label{..}`` in a raw block — captured before cleanup so the
    writing pass can map label -> the created chunk's handle."""
    return [m.group(1).strip() for m in _LABEL_RE.finditer(raw)]


def resolve_deferred(
    text: str,
    *,
    labels: dict[str, str] | None = None,
    unresolved: list[str] | None = None,
) -> str:
    """Rewrite deferred cross-references once chunk handles are known.

    ``[¶@label]`` (from ``\\ref``/``\\cref``) -> ``[<dc-handle>]`` — the ADR
    0036 single-bracket handle reference form (``[dc456]``; ``labels`` maps each
    ``\\label`` to the ``dc<chunk_id>`` of the chunk it sits in). A missing label
    (e.g. a ``\\label`` on a dropped figure) degrades to a parenthetical mention
    and is recorded in ``unresolved``. (Glossary terms are *not* deferred — they
    are the plain short word, auto-linked by the exporter's ``_glsify``.)
    """
    labels = labels or {}

    def _r(m: re.Match[str]) -> str:
        lab = m.group(1)
        h = labels.get(lab)
        if h:
            return f"[{h}]"
        if unresolved is not None:
            unresolved.append(lab)
        return f"({lab.split(':')[-1].replace('-', ' ')})"

    return _DEFERRED_REF.sub(_r, text)


def _strip_grouping_braces(text: str) -> str:
    """Drop leftover TeX grouping braces, but keep braces inside ``$...$``
    math (where they group sub/superscripts) and escaped ``\\{`` / ``\\}``."""
    text = text.replace(r"\{", "\x00").replace(r"\}", "\x01")
    out: list[str] = []
    in_math = False
    for ch in text:
        if ch == "$":
            in_math = not in_math
            out.append(ch)
        elif ch in "{}" and not in_math:
            continue
        else:
            out.append(ch)
    return "".join(out).replace("\x00", "{").replace("\x01", "}")
