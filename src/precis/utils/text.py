"""Text-cleanup helpers shared across handlers.

Three near-identical ``_excerpt`` implementations used to live in
``handlers/paper.py``, ``handlers/markdown.py``, and
``handlers/patent.py``, each with a slightly different default limit
and tail-handling rule. They're consolidated here so adjustments
(elision character, whitespace policy, word-boundary handling) only
need to change in one place.

``markup_surface``/``markup_unit`` fold the markup papers print numbers
and units in — LaTeX (``\\pm``, ``\\text{}``, ``^{−2}``), HTML
``<sup>``/``<sub>``, minus-sign glyphs and ``|``-separated split table
cells — to the plain reading, so a check never compares markup against
plain text (gr511458).

Pure — no DB, no IO, no logging.
"""

from __future__ import annotations

import html
import re
from typing import Any
from xml.sax.saxutils import escape as _xml_escape

__all__ = [
    "clip",
    "clip_total",
    "esc",
    "esc_quoted",
    "excerpt",
    "fmt_num",
    "markup_surface",
    "markup_unit",
    "slugify",
]

_SLUG_RUN_RE = re.compile(r"[^a-z0-9]+")


def esc(value: Any, *, quote: bool = False) -> str:
    """XML/SVG-escape ``value`` (``&`` ``<`` ``>``); ``None`` -> ``""``.

    ``quote=True`` also escapes ``"`` as ``&quot;`` (for attribute values).
    """
    text = "" if value is None else str(value)
    return _xml_escape(text, {'"': "&quot;"} if quote else {})


def esc_quoted(value: Any) -> str:
    """Backslash-escape ``\\`` and ``"`` for a double-quoted DSN-style string."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def fmt_num(v: float) -> str:
    """A float without a trailing ``.0`` (``256.0`` -> ``256``)."""
    return str(int(v)) if v == int(v) else str(v)


def clip(text: str | None, n: int = 160) -> str:
    """Collapse whitespace, cut at ``n`` chars and append ``…`` if shortened.

    The result can be ``n + 1`` long (the ellipsis sits past the cut); use
    :func:`clip_total` when the ellipsis must fit inside the limit.
    """
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[:n].rstrip() + "…"


def clip_total(text: str, limit: int) -> str:
    """Strip ``text`` and trim it so the result, ellipsis included, is <= ``limit``."""
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def slugify(text: str | None, *, max_len: int | None = None, default: str = "") -> str:
    """Lowercase, runs of non-``[a-z0-9]`` to one hyphen, ends trimmed.

    No diacritic folding (``café`` -> ``caf``); use
    :func:`precis.utils.slug.slug_from_text` when folding is wanted.
    ``max_len`` truncates after slugging; ``default`` replaces an empty
    result.
    """
    s = _SLUG_RUN_RE.sub("-", (text or "").lower()).strip("-")
    if max_len is not None:
        s = s[:max_len]
    return s or default


def excerpt(
    text: str,
    *,
    limit: int = 240,
    ellipsis: str = "…",
    collapse_whitespace: bool = True,
) -> str:
    """Trim ``text`` to roughly ``limit`` chars, word-boundary-aware.

    Trimming snaps to the last whitespace boundary inside ``[:limit]``
    when one exists, which is friendlier for prose previews — search
    headlines, list rows, and TOC blurbs all benefit. When the slice
    has no internal whitespace (a long URL, a hash, a single token)
    we fall back to the hard-cut form to preserve the head of the
    string rather than collapse to a bare ellipsis.

    The ``ellipsis`` is appended only when the input was actually
    shortened. Returning an unchanged short string avoids the
    "every preview ends in …" smell that the older paper-side
    helper had.

    ``collapse_whitespace`` (default ``True``) flattens all runs of
    whitespace — including newlines — to a single space before
    trimming; the usual policy for a one-line preview. Pass ``False``
    to trim in place instead, preserving embedded newlines/paragraph
    breaks (a multi-paragraph abstract, say) — only the ``[:limit]``
    cut point still snaps to the last space.

    The function is idempotent on already-collapsed input and never
    raises — pass it whatever the upstream produced.
    """
    if not text:
        return ""
    collapsed = " ".join(text.split()) if collapse_whitespace else text
    if len(collapsed) <= limit:
        return collapsed
    head = collapsed[:limit]
    last_space = head.rfind(" ")
    if last_space > 0:
        head = head[:last_space]
    return f"{head.rstrip()}{ellipsis}"


# ── markup -> printed-reading folds (gr511458) ─────────────────────────
#
# Chunks carry the markup the source PDF/HTML was extracted through:
# LaTeX math ("$-0.7 \pm 0.1 \text{ W m}^{-2}$"), HTML super/subscripts
# ("W m<sup>−</sup><sup>2</sup>"), glyph variants for the minus sign, and
# markdown tables whose cells split a quantity across "|" and newlines.
# A reader emits the plain reading ("-0.7 ± 0.1", "W m-2") while a check
# saw raw markup, so the two never met. markup_surface is the matching
# form (exponents flattened); markup_unit keeps caret exponents for the
# pint boundary.

_TEX_ESCAPED = {
    "\\%": "%",
    "\\&": "&",
    "\\#": "#",
    "\\$": "$",
    "\\_": "_",
    "\\{": "{",
    "\\}": "}",
}

_TEX_SYMBOLS = {
    "pm": "±",
    "mp": "∓",
    "times": "×",
    "cdot": "·",
    "cdotp": "·",
    "div": "÷",
    "approx": "≈",
    "simeq": "≈",
    "propto": "∝",
    "sim": "~",
    "infty": "∞",
    "le": "≤",
    "leq": "≤",
    "leqslant": "≤",
    "ge": "≥",
    "geq": "≥",
    "geqslant": "≥",
    "ne": "≠",
    "neq": "≠",
    "circ": "°",
    "degree": "°",
    "ast": "*",
    "star": "*",
    "partial": "∂",
    "nabla": "∇",
    "langle": "⟨",
    "rangle": "⟩",
    "mu": "µ",
    "alpha": "α",
    "beta": "β",
    "gamma": "γ",
    "delta": "δ",
    "epsilon": "ε",
    "varepsilon": "ε",
    "zeta": "ζ",
    "eta": "η",
    "theta": "θ",
    "vartheta": "θ",
    "iota": "ι",
    "kappa": "κ",
    "lambda": "λ",
    "nu": "ν",
    "xi": "ξ",
    "pi": "π",
    "rho": "ρ",
    "sigma": "σ",
    "tau": "τ",
    "upsilon": "υ",
    "phi": "φ",
    "varphi": "φ",
    "chi": "χ",
    "psi": "ψ",
    "omega": "ω",
    "Gamma": "Γ",
    "Delta": "Δ",
    "Theta": "Θ",
    "Lambda": "Λ",
    "Xi": "Ξ",
    "Pi": "Π",
    "Sigma": "Σ",
    "Upsilon": "Υ",
    "Phi": "Φ",
    "Psi": "Ψ",
    "Omega": "Ω",
}
_TEX_SYMBOL_RE = re.compile(
    "\\\\(" + "|".join(sorted(_TEX_SYMBOLS, key=len, reverse=True)) + ")(?![A-Za-z])"
)
_TEX_WRAP_RE = re.compile(
    r"\\(?:text|mathrm|mathit|mathbf|mathsf|mathtt|mathcal|mathnormal|"
    r"textrm|textsf|texttt|textbf|textnormal|mbox|hbox|operatorname|ensuremath)"
    r"\s*\{([^{}]*)\}"
)
_TEX_FRAC_RE = re.compile(r"\\[dt]?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}")
# ``~`` is a TeX tie only between two glued tokens (``12~eV``); a leading
# or spaced ``~`` is the approximate-value prefix (``~12``) and survives.
_TEX_SPACE_RE = re.compile(
    r"\\[,;:]|\\ |\\(?:quad|qquad|enspace|thinspace)\b|(?<=[\w}$])~(?=[\w\\$])"
)
_TEX_DROP_RE = re.compile(
    r"\\(?:left|right|bigg?|Bigg?|big[lr]|Big[lr]|rm|bf|it|sf|tt|cal|"
    r"tiny|scriptsize|footnotesize|small|normalsize|large|Large|LARGE|"
    r"huge|Huge|displaystyle|limits|nolimits|noindent|hline|toprule|"
    r"midrule|bottomrule|quad|qquad|thinspace|enspace)(?![A-Za-z])"
)
_TEX_LEFTOVER_RE = re.compile(r"\\[A-Za-z]+")

#: Every dash print uses for a minus sign, folded to ``-``.
_MINUS_TRANS = str.maketrans({c: "-" for c in "‐‑‒–—―−﹣－"})
_SUP_TRANS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺", "0123456789-+")
_SUB_TRANS = str.maketrans("₀₁₂₃₄₅₆₇₈₉₊₋", "0123456789+-")

_SUP_TAG_RE = re.compile(r"<sup[^>]*>(.*?)</sup>", re.IGNORECASE | re.DOTALL)
_SUB_TAG_RE = re.compile(r"<sub[^>]*>(.*?)</sub>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")

#: ``a × 10^{n}`` / ``a·10^n`` / ``a × 10⁻³`` / sign-marked caret-less
#: ``a × 10-3`` -> ``a e n`` — the plain spelling a reader writes.
_SCI_X10_CARET_RE = re.compile(
    r"(?<![\w.])(\d+(?:\.\d+)?)\s*[×·*]\s*10\s*\^\s*\{?\s*([-+]?\d{1,4})\s*\}?"
)
_SCI_X10_GLYPH_RE = re.compile(
    r"(?<![\w.])(\d+(?:\.\d+)?)\s*[×·*]\s*10\s*([⁻⁺]?[⁰¹²³⁴⁵⁶⁷⁸⁹]+)"
)
_SCI_X10_SIGN_RE = re.compile(
    r"(?<![\w.])(\d+(?:\.\d+)?)\s*[×·*]\s*10\s*([-+]\d{1,4})(?![\d.])"
)
_SCI_10_CARET_RE = re.compile(r"(?<![\w.\d])10\s*\^\s*\{?\s*([-+]?\d{1,4})\s*\}?")
_SCI_10_GLYPH_RE = re.compile(r"(?<![\w.\d])10\s*([⁻⁺]?[⁰¹²³⁴⁵⁶⁷⁸⁹]+)")


def _markup_fold(text: str) -> str:
    """Entities, tags and TeX commands resolved to readable characters —
    ``<sup>``/``<sub>`` become ``^``/``_`` markers, ``\\pm`` becomes
    ``±``. Exponent marks are still caret-shaped; the callers decide
    whether to flatten or keep them."""
    s = html.unescape(text)
    s = _SUP_TAG_RE.sub(lambda m: "^" + m.group(1), s)
    s = _SUB_TAG_RE.sub(lambda m: "_" + m.group(1), s)
    s = _TAG_RE.sub("", s)
    for esc_, char in _TEX_ESCAPED.items():
        s = s.replace(esc_, char)
    s = _TEX_FRAC_RE.sub(r"\1/\2", s)
    for _ in range(3):  # unwrap \text{}/\mathrm{}… incl. shallow nesting
        unfolded = _TEX_WRAP_RE.sub(r"\1", s)
        if unfolded == s:
            break
        s = unfolded
    s = _TEX_SYMBOL_RE.sub(lambda m: _TEX_SYMBOLS[m.group(1)], s)
    s = _TEX_SPACE_RE.sub(" ", s)
    s = s.replace("\\!", "")
    s = _TEX_DROP_RE.sub("", s)
    return _TEX_LEFTOVER_RE.sub("", s)


def markup_surface(text: str) -> str:
    """The printed-reading surface of text that may carry LaTeX, HTML or
    markdown-table markup, for *matching* — never for display or storage.

    ``$-0.7 \\pm 0.1 \\text{ W m}^{-2}$`` reads ``-0.7 ± 0.1 W m-2``;
    ``<sup>−</sup>1.9`` reads ``-1.9``; ``2 \\times 10^{-3}`` reads
    ``2e-3``; ``| a | b |`` reads ``a  b`` (a cell boundary is a space).
    Both sides of a comparison fold identically, so a verbatim-markup
    literal still matches its markup span.
    """
    s = _markup_fold(text).translate(_MINUS_TRANS)
    s = s.replace("+/-", "±")
    s = _SCI_X10_CARET_RE.sub(r"\1e\2", s)
    s = _SCI_X10_GLYPH_RE.sub(
        lambda m: m.group(1) + "e" + m.group(2).translate(_SUP_TRANS), s
    )
    s = _SCI_X10_SIGN_RE.sub(r"\1e\2", s)
    s = _SCI_10_CARET_RE.sub(r"1e\1", s)
    s = _SCI_10_GLYPH_RE.sub(lambda m: "1e" + m.group(1).translate(_SUP_TRANS), s)
    s = s.translate(_SUP_TRANS).translate(_SUB_TRANS)
    s = re.sub(r"[$^_{}]", "", s)
    s = re.sub(r"([-+])\s*\|\s*(?=\d)", r"\1", s)  # "- | 1.93" cell
    s = s.replace("|", " ")
    return re.sub(r"\s+", " ", s.replace("μ", "µ")).strip()


def markup_unit(text: str) -> str:
    """``markup_surface`` for a unit going to pint: the same fold but
    caret exponents stay (``W m^{-2}`` -> ``W m^-2``,
    ``m<sup>−</sup><sup>2</sup>`` -> ``m^-2``, ``^{−2}`` -> ``^-2``) and
    ``_`` basis labels keep their underscore."""
    s = _markup_fold(text).translate(_MINUS_TRANS)
    s = s.translate(_SUP_TRANS).translate(_SUB_TRANS)
    s = re.sub(r"\^\s*\{([^{}]*)\}", r"^\1", s)
    s = re.sub(r"_\s*\{([^{}]*)\}", r"_\1", s)
    s = re.sub(r"[${}]", "", s)
    s = re.sub(r"\^([-+]?)\^", r"^\1", s)  # <sup>−</sup><sup>2</sup> -> ^-^2 -> ^-2
    return re.sub(r"\s+", " ", s.replace("μ", "µ")).strip()
