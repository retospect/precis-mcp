"""Author + citation + JATS formatters for ``PaperHandler``.

Split out of ``handlers/paper.py`` 2026-06-05 — the handler module
crossed 2K LOC because ~20 standalone formatters had piled up next
to the actual handler class. These are pure functions over ``Ref``
metadata; they take no store, do no I/O, and never raise. Tested
directly by ``tests/test_mcp_critic_regressions.py`` (citation
rendering, JATS scrubbing, author normalisation).

Public-ish surface (used by ``paper.py`` and a few critic tests):

* :func:`_format_authors` — flat "A; B; C" or "A et al."
* :func:`_render_citation` — BibTeX / RIS / EndNote renderer, now
  type-aware over ``meta.entry_type`` (see :data:`ENTRY_TYPE_CHOICES` /
  :data:`_EXPORT_TYPE_MAP`, docs/backlog/paper-meta-surfacing.md).
* :func:`_clean_inline_text` — html.unescape + tag-whitelist strip
* :func:`_latex_escape` — backslash-escape BibTeX special chars
* :func:`_strip_jats` — drop ``<jats:*>`` namespace tags

``ENTRY_TYPE_CHOICES`` / ``ENTRY_TYPE_LABELS`` are also imported by
``precis_web.routes.papers`` for the Meta tab's document-type
``<select>`` and display label — the vocabulary is defined once here
so the edit form and the exporter never drift apart.
"""

from __future__ import annotations

import html
import re
from typing import Any

from precis.store import Ref
from precis.utils.authors import author_names as _shared_author_names
from precis.utils.record_cite import record_type_of
from precis.utils.title_views import title_md, title_plain

# Strip a small whitelist of inline HTML / JATS tags that publishers
# leak into title/journal metadata: ``<sub>``, ``<sup>``, ``<i>``,
# ``<b>``, ``<em>``, ``<strong>`` plus any ``<jats:*>`` namespace tag.
# The tag *contents* are kept; only the markers go.
_INLINE_TAG_RE = re.compile(
    r"</?(?:sub|sup|i|b|em|strong|jats:[a-zA-Z0-9_-]+)\b[^>]*>",
    re.IGNORECASE,
)

# Characters that need backslash-escaping for LaTeX. The list is the
# minimum set that breaks BibTeX / biber compilation when present in
# field values; ``$``, ``{``, ``}``, ``\`` are not common in
# bibliographic metadata and would need a richer escape.
_LATEX_ESCAPES: dict[str, str] = {
    "&": r"\&",
    "%": r"\%",
    "_": r"\_",
    "#": r"\#",
}

# JATS-XML namespace tags leak through some publishers' abstract
# metadata. We strip the simple ``<jats:tag>...</jats:tag>`` form
# rather than running a full parser — abstracts are short and the
# tag set is constrained.
#
# ``<jats:title>Abstract</jats:title>`` immediately followed by body
# text was rendering as ``AbstractMetal-organic frameworks…``
# (heading word glued to the next sentence) — the MCP critic's
# MINOR m1. Drop the ``<jats:title>Abstract</jats:title>`` block
# specifically because the view name itself ('abstract') already
# names the section, and the label is never anything else worth
# keeping.
_JATS_ABSTRACT_TITLE_RE = re.compile(
    r"<jats:title>\s*Abstract\s*</jats:title>", re.IGNORECASE
)

# Crossref document-type vocabulary this repo actually stores in
# ``meta.entry_type`` (paper_meta_enrich writes the Crossref ``type``
# field verbatim; this is the subset the Meta tab edit form offers as a
# ``<select>`` — anything else Crossref might send stays representable
# via ``meta.entry_type`` but has no dedicated select option, matching
# the "other" escape). Order is display order in the form.
ENTRY_TYPE_CHOICES: tuple[tuple[str, str], ...] = (
    ("journal-article", "Journal article"),
    ("proceedings-article", "Conference paper"),
    ("book-chapter", "Book chapter"),
    ("posted-content", "Preprint / posted content"),
    ("dissertation", "Dissertation"),
    ("book", "Book"),
    ("monograph", "Monograph"),
    ("report", "Report"),
    ("reference-entry", "Reference entry"),
    ("other", "Other"),
)

#: ``{entry_type: label}`` — the display-only lookup for the Meta tab
#: (an entry_type value that predates/exceeds ``ENTRY_TYPE_CHOICES``,
#: e.g. a raw Crossref type we don't offer in the select, still shows
#: its verbatim value via ``dict.get(..., entry_type)``).
ENTRY_TYPE_LABELS: dict[str, str] = dict(ENTRY_TYPE_CHOICES)

#: ``meta.entry_type`` → per-format citation type. Covers the full
#: Crossref document-type vocabulary (api.crossref.org/types,
#: gr515478); ``journal-article`` is the one type left unlisted — the
#: journal-article default (``@article`` / ``TY  - JOUR`` /
#: ``%0 Journal Article``) already produces it, and an absent or
#: unrecognised ``entry_type`` still falls through to that default
#: unchanged (an un-enriched paper's export is byte-identical to
#: before this map existed).
_EXPORT_TYPE_MAP: dict[str, dict[str, str]] = {
    "proceedings-article": {
        "bibtex": "inproceedings",
        "ris": "CONF",
        "endnote": "Conference Paper",
    },
    "proceedings": {
        "bibtex": "proceedings",
        "ris": "CONF",
        "endnote": "Conference Proceedings",
    },
    "proceedings-series": {
        "bibtex": "proceedings",
        "ris": "SER",
        "endnote": "Conference Proceedings",
    },
    "book-chapter": {
        "bibtex": "incollection",
        "ris": "CHAP",
        "endnote": "Book Section",
    },
    "book-part": {
        "bibtex": "inbook",
        "ris": "CHAP",
        "endnote": "Book Section",
    },
    "book-section": {
        "bibtex": "incollection",
        "ris": "CHAP",
        "endnote": "Book Section",
    },
    "book-track": {
        "bibtex": "inbook",
        "ris": "CHAP",
        "endnote": "Book Section",
    },
    "posted-content": {
        "bibtex": "misc",
        "ris": "UNPB",
        "endnote": "Unpublished Work",
    },
    "dissertation": {
        "bibtex": "phdthesis",
        "ris": "THES",
        "endnote": "Thesis",
    },
    "book": {"bibtex": "book", "ris": "BOOK", "endnote": "Book"},
    "book-series": {"bibtex": "book", "ris": "BOOK", "endnote": "Book"},
    "book-set": {"bibtex": "book", "ris": "BOOK", "endnote": "Book"},
    "monograph": {"bibtex": "book", "ris": "BOOK", "endnote": "Book"},
    "reference-book": {"bibtex": "book", "ris": "BOOK", "endnote": "Book"},
    "edited-book": {"bibtex": "book", "ris": "BOOK", "endnote": "Book"},
    "reference-entry": {
        "bibtex": "inreference",
        "ris": "ENCYC",
        "endnote": "Encyclopedia",
    },
    "report": {
        "bibtex": "techreport",
        "ris": "RPRT",
        "endnote": "Report",
    },
    "report-component": {
        "bibtex": "misc",
        "ris": "GEN",
        "endnote": "Generic",
    },
    "report-series": {
        "bibtex": "techreport",
        "ris": "RPRT",
        "endnote": "Report",
    },
    "standard": {
        "bibtex": "misc",
        "ris": "STAND",
        "endnote": "Standard",
    },
    "standard-series": {
        "bibtex": "book",
        "ris": "SER",
        "endnote": "Standard",
    },
    "journal": {
        "bibtex": "periodical",
        "ris": "JFULL",
        "endnote": "Generic",
    },
    "journal-issue": {
        "bibtex": "periodical",
        "ris": "JFULL",
        "endnote": "Generic",
    },
    "journal-volume": {
        "bibtex": "periodical",
        "ris": "JFULL",
        "endnote": "Generic",
    },
    "dataset": {
        "bibtex": "dataset",
        "ris": "DATA",
        "endnote": "Dataset",
    },
    # Whole-record cite type (gr514384): DataCite ``Software`` registers
    # with ``entry_type='software'`` alongside ``record_type``.
    "software": {
        "bibtex": "software",
        "ris": "COMP",
        "endnote": "Computer Program",
    },
    "database": {
        "bibtex": "misc",
        "ris": "DBASE",
        "endnote": "Online Database",
    },
    "component": {"bibtex": "misc", "ris": "GEN", "endnote": "Generic"},
    "peer-review": {"bibtex": "misc", "ris": "GEN", "endnote": "Generic"},
    "grant": {"bibtex": "misc", "ris": "GRANT", "endnote": "Grant"},
    "other": {"bibtex": "misc", "ris": "GEN", "endnote": "Generic"},
}

#: The whole Crossref vocabulary an ``entry_type=`` value may take —
#: the export map's keys plus the ``journal-article`` default that
#: stays unlisted there. ``PaperHandler.edit`` validates against this
#: (``ENTRY_TYPE_CHOICES`` is only the web form's shortlist).
ENTRY_TYPE_VOCAB: frozenset[str] = frozenset(_EXPORT_TYPE_MAP) | {"journal-article"}


def _author_names(raw: Any, *, order: str = "natural") -> list[str]:
    """Flat list of display-form name strings.

    Tolerates every stored ``refs.authors`` shape — ``{name}``,
    ``{family, given}``, bare strings, semicolon-packed byline — via
    the canonical :mod:`precis.utils.authors` reader. ``order='natural'``
    (the default) is the display convention everywhere;
    ``order='sortable'`` is used only by citation renderers (BibTeX)
    that need "Family, Given". Pure; never raises.
    """
    return _shared_author_names(raw, order=order)


def _format_authors(raw: Any) -> str:
    names = [_clean_inline_text(n) for n in _author_names(raw)]
    names = [n for n in names if n]
    if not names:
        return ""
    if len(names) <= 3:
        return "; ".join(names)
    return f"{names[0]} et al."


def _render_citation(ref: Ref, *, style: str, doi: str | None = None) -> str:
    """Render a citation in BibTeX / RIS / EndNote.

    All scalar metadata fields are run through :func:`_clean_inline_text`
    to strip JATS / HTML markup and unescape entities (``&amp;`` →
    ``&``); BibTeX additionally LaTeX-escapes ``& % _ #`` so the output
    compiles cleanly. (MCP critic MINOR — BibTeX leaks ``&amp;`` and
    paper list leaks ``<sub>``.)

    F15: ``authors`` and ``year`` now read from the top-level
    ``Ref.authors`` / ``Ref.year`` columns (v2 schema location).
    ``doi`` is fetched from ``ref_identifiers`` by the caller and
    passed in. ``journal`` is still in ``meta`` because it's the
    only one of the four that v2 chose to keep there.

    ``meta.entry_type`` (Crossref vocabulary, see
    :data:`_EXPORT_TYPE_MAP`) picks the citation type — ``@article`` /
    ``TY  - JOUR`` / ``%0 Journal Article`` when absent or unrecognised,
    matching the export's behaviour before ``entry_type`` existed.
    """
    meta = ref.meta or {}
    slug = ref.slug or "???"
    # gr515227: stored titles are raw — BibTeX takes the md view (math
    # islands are already valid LaTeX), RIS/EndNote the plain view.
    title_bibtex = bibtex_title(ref.title)
    title = title_plain(ref.title)
    # Citation formats (BibTeX / RIS / EndNote) use the sortable
    # "Family, Given" convention — inverted order is *derived* here,
    # never the stored display string (see utils/authors.py).
    authors = [
        _clean_inline_text(a) for a in _author_names(ref.authors, order="sortable")
    ]
    authors = [a for a in authors if a]
    journal = _clean_inline_text(str(meta.get("journal") or ""))
    year = ref.year
    doi_clean = _clean_inline_text(doi or "")
    # Bibliographic locators (gr515469) — the meta keys the enricher and
    # paper edit write: ``issue`` is stored as ``number``.
    volume = _clean_inline_text(str(meta.get("volume") or ""))
    number = _clean_inline_text(str(meta.get("number") or ""))
    pages = _clean_inline_text(str(meta.get("pages") or ""))
    url = _clean_inline_text(str(meta.get("url") or ""))
    entry_type = str(meta.get("entry_type") or "").strip().lower()
    # Whole-record cite (gr514384): ``meta.record_type`` wins over a
    # stale/absent ``entry_type`` — a record typed by hand carries only
    # ``record_type``, and a registered one carries both. A record has
    # no venue: ``publisher`` is its own field, ``journal`` suppressed.
    record_rt = record_type_of(meta)
    version = _clean_inline_text(str(meta.get("version") or ""))
    publisher = _clean_inline_text(str(meta.get("publisher") or ""))
    export_type = _EXPORT_TYPE_MAP.get(record_rt or entry_type)
    # No matched type and no venue: ``article``/``JOUR`` would claim a
    # journal that isn't there. Match export.latex.build_bib's @misc.
    venueless = export_type is None and not journal

    if style == "bibtex":
        # LaTeX-escape every scalar field that might carry a special
        # char. ``and``/``year``/``doi`` rarely do but we run them
        # through anyway for symmetry; a stray ``&`` in the title was
        # the actual MCP-critic finding. ``bibtex_title`` already
        # escaped the non-math spans and converted md emphasis.
        bx_title = title_bibtex
        bx_authors = " and ".join(_latex_escape(a) for a in authors)
        bx_journal = _latex_escape(journal)
        bx_doi = _latex_escape(doi_clean)
        bx_type = (
            export_type["bibtex"] if export_type else "misc" if venueless else "article"
        )
        lines = [f"@{bx_type}{{{slug},"]
        if bx_title:
            lines.append(f"  title = {{{bx_title}}},")
        if bx_authors:
            lines.append(f"  author = {{{bx_authors}}},")
        if year:
            lines.append(f"  year = {{{year}}},")
        if bx_journal and not record_rt:
            lines.append(f"  journal = {{{bx_journal}}},")
        for bib_key, value in (
            ("volume", volume),
            ("number", number),
            ("pages", pages),
            ("url", url),
            *((("version", version), ("publisher", publisher)) if record_rt else ()),
        ):
            if value:
                # ``url`` feeds ``\url{}``/hyperref, which wants the raw
                # string — ``\_``/``\%`` escapes break the link. Braces
                # are balanced, not escaped: BibTeX counts ``\{`` as a
                # brace anyway, so escaping can't keep the field closed.
                rendered = (
                    _balance_braces(value) if bib_key == "url" else _latex_escape(value)
                )
                lines.append(f"  {bib_key} = {{{rendered}}},")
        if bx_doi:
            lines.append(f"  doi = {{{bx_doi}}},")
        if entry_type == "posted-content":
            lines.append("  note = {Preprint},")
        lines.append("}")
        return "\n".join(lines) + "\n"

    if style == "ris":
        ris_type = export_type["ris"] if export_type else "GEN" if venueless else "JOUR"
        out = [f"TY  - {ris_type}"]
        if title:
            out.append(f"TI  - {title}")
        for a in authors:
            out.append(f"AU  - {a}")
        if year:
            out.append(f"PY  - {year}")
        if journal and not record_rt:
            out.append(f"JO  - {journal}")
        if record_rt and publisher:
            out.append(f"PB  - {publisher}")
        if doi_clean:
            out.append(f"DO  - {doi_clean}")
        out.append("ER  - ")
        return "\n".join(out)

    # endnote (subset)
    endnote_type = (
        export_type["endnote"]
        if export_type
        else "Generic"
        if venueless
        else "Journal Article"
    )
    out = [f"%0 {endnote_type}"]
    if title:
        out.append(f"%T {title}")
    for a in authors:
        out.append(f"%A {a}")
    if year:
        out.append(f"%D {year}")
    if journal and not record_rt:
        out.append(f"%J {journal}")
    if record_rt and publisher:
        out.append(f"%I {publisher}")
    if doi_clean:
        out.append(f"%R {doi_clean}")
    return "\n".join(out)


def _clean_inline_text(text: str) -> str:
    """Run the metadata-cleanup pipeline used by every renderer.

    Steps (idempotent):

    1. ``html.unescape`` to flip ``&amp;`` → ``&``, ``&lt;`` → ``<``,
       and any double-encoded ``&amp;lt;`` shapes back to literal text.
       Run twice so the double-encoded shape lands as ``<``.
    2. Strip a small whitelist of inline HTML/JATS tags.
    3. Collapse whitespace runs.

    Pure — never raises.
    """
    if not text:
        return ""
    cleaned = html.unescape(html.unescape(text))
    cleaned = _INLINE_TAG_RE.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _latex_escape(text: str) -> str:
    """Backslash-escape the LaTeX special chars BibTeX trips on."""
    if not text:
        return ""
    out = text
    for ch, esc in _LATEX_ESCAPES.items():
        out = out.replace(ch, esc)
    return out


#: Spans of a ``title_md`` view that are already valid LaTeX: ``$…$`` /
#: ``$$…$$`` math islands and backslash glyph escapes (``\$`` ``\%``
#: ``\_`` …) — the text-mode escape must not reach inside them (``\_``
#: would corrupt a math subscript; ``\&`` inside ``$…$`` is equally
#: wrong). The escape alternative takes a backslash plus a NON-LETTER
#: only: ``\`` + letter starts a real macro, which is not "safe" — it
#: must fall into the text segment where :func:`bibtex_title`
#: neutralises it (``\input{/etc/passwd}`` outside math used to reach
#: the .bib verbatim through the old ``\\.`` catch-all).
#: NOTE the capture group: ``re.split`` drops non-captured matches, so
#: without it the math spans would vanish from the output entirely.
_LATEX_SAFE_SPAN_RE = re.compile(r"(\\[^a-zA-Z]|\$\$.*?\$\$|\$[^$]*\$)", re.DOTALL)


def _latex_escape_math(text: str) -> str:
    """:func:`_latex_escape` applied only to the non-math spans of a
    :func:`~precis.utils.title_views.title_md` string."""
    if not text:
        return ""
    return "".join(
        seg if _LATEX_SAFE_SPAN_RE.fullmatch(seg) else _latex_escape(seg)
        for seg in _LATEX_SAFE_SPAN_RE.split(text)
        if seg
    )


#: md emphasis spans inside a ``title_md`` string — same shapes
#: ``precis_web.linkify``'s ``_MD_BOLD``/``_MD_ITALIC`` honour — converted
#: to ``\textbf``/``\emph`` for BibTeX. Applied only to non-math segments
#: (a ``*`` inside ``$…$`` is an operator, not emphasis).
_BIBTEX_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_BIBTEX_ITALIC_RE = re.compile(r"(?<![*\w])\*(?!\s)([^*]+?)(?<!\s)\*(?!\w)")

#: LaTeX commands allowed inside a ``$…$``/``$$…$$`` math island when a
#: title is exported to BibTeX (gr515709). The criterion is *typesetting
#: only*: anything that can touch the filesystem or the engine state —
#: ``\input``, ``\include``, ``\def``/``\newcommand``, ``\write``/
#: ``\openout``/``\read``, ``\catcode``/``\csname``, ``\verb``,
#: ``\usepackage``, ``\includegraphics``, ``\special``, ``\immediate``,
#: ``\label``/``\ref``/``\cite`` — is NOT here and gets its backslash
#: neutralised by :func:`_sanitize_math_span`, so a hostile title like
#: ``$\input{/etc/passwd}$`` reaches the .bib as harmless literal text
#: instead of a macro that executes at compile time.
_SAFE_MATH_MACROS: frozenset[str] = frozenset(
    {
        # fonts + text-in-math
        "mathrm",
        "mathit",
        "mathbf",
        "mathsf",
        "mathtt",
        "mathcal",
        "mathbb",
        "mathfrak",
        "mathscr",
        "mathnormal",
        "boldsymbol",
        "bm",
        "pmb",
        "mathop",
        "mathrel",
        "mathbin",
        "mathord",
        "mathopen",
        "mathclose",
        "mathpunct",
        "mathinner",
        "operatorname",
        "text",
        "textbf",
        "textit",
        "textrm",
        "textsf",
        "texttt",
        "emph",
        "textnormal",
        "textup",
        "textmd",
        "textsc",
        "textsl",
        "mbox",
        # Greek
        "alpha",
        "beta",
        "gamma",
        "delta",
        "epsilon",
        "varepsilon",
        "zeta",
        "eta",
        "theta",
        "vartheta",
        "iota",
        "kappa",
        "lambda",
        "mu",
        "nu",
        "xi",
        "pi",
        "varpi",
        "rho",
        "varrho",
        "sigma",
        "varsigma",
        "tau",
        "upsilon",
        "phi",
        "varphi",
        "chi",
        "psi",
        "omega",
        "Gamma",
        "Delta",
        "Theta",
        "Lambda",
        "Xi",
        "Pi",
        "Sigma",
        "Upsilon",
        "Phi",
        "Psi",
        "Omega",
        # binary operators + relations
        "pm",
        "mp",
        "times",
        "div",
        "cdot",
        "ast",
        "star",
        "circ",
        "bullet",
        "oplus",
        "ominus",
        "otimes",
        "oslash",
        "odot",
        "cap",
        "cup",
        "sqcap",
        "sqcup",
        "vee",
        "wedge",
        "setminus",
        "amalg",
        "le",
        "leq",
        "ge",
        "geq",
        "neq",
        "ne",
        "equiv",
        "sim",
        "simeq",
        "approx",
        "cong",
        "propto",
        "prec",
        "preceq",
        "succ",
        "succeq",
        "ll",
        "gg",
        "subset",
        "supset",
        "subseteq",
        "supseteq",
        "in",
        "ni",
        "notin",
        "perp",
        "parallel",
        "mid",
        "asymp",
        "models",
        "vdash",
        "dashv",
        "doteq",
        "sqsubseteq",
        "sqsupseteq",
        "not",
        # arrows
        "to",
        "rightarrow",
        "leftarrow",
        "Rightarrow",
        "Leftarrow",
        "leftrightarrow",
        "Leftrightarrow",
        "mapsto",
        "longrightarrow",
        "longleftarrow",
        "Longrightarrow",
        "Longleftarrow",
        "longleftrightarrow",
        "Longleftrightarrow",
        "uparrow",
        "downarrow",
        "updownarrow",
        "nearrow",
        "searrow",
        "swarrow",
        "nwarrow",
        "hookrightarrow",
        "hookleftarrow",
        # big operators + limits
        "sum",
        "prod",
        "coprod",
        "int",
        "iint",
        "iiint",
        "oint",
        "bigcup",
        "bigcap",
        "bigoplus",
        "bigotimes",
        "bigodot",
        "bigvee",
        "bigwedge",
        "bigsqcup",
        "biguplus",
        "limits",
        "nolimits",
        # named functions
        "sin",
        "cos",
        "tan",
        "sec",
        "csc",
        "cot",
        "sinh",
        "cosh",
        "tanh",
        "log",
        "ln",
        "lg",
        "exp",
        "min",
        "max",
        "sup",
        "inf",
        "lim",
        "limsup",
        "liminf",
        "det",
        "dim",
        "gcd",
        "deg",
        "arg",
        "ker",
        "Pr",
        "hom",
        "bmod",
        "pmod",
        # delimiters + sizes
        "left",
        "right",
        "big",
        "Big",
        "bigg",
        "Bigg",
        "bigl",
        "bigr",
        "Bigl",
        "Bigr",
        "biggl",
        "biggr",
        "Biggl",
        "Biggr",
        "langle",
        "rangle",
        "lfloor",
        "rfloor",
        "lceil",
        "rceil",
        "lvert",
        "rvert",
        "lVert",
        "rVert",
        # accents + over/under
        "hat",
        "bar",
        "vec",
        "dot",
        "ddot",
        "tilde",
        "widehat",
        "widetilde",
        "overline",
        "underline",
        "overrightarrow",
        "overleftarrow",
        "overbrace",
        "underbrace",
        "mathring",
        "check",
        "breve",
        "acute",
        "grave",
        # fractions + roots
        "frac",
        "dfrac",
        "tfrac",
        "cfrac",
        "binom",
        "dbinom",
        "tbinom",
        "sqrt",
        "over",
        "atop",
        "choose",
        # spacing + phantoms
        "quad",
        "qquad",
        "hspace",
        "vspace",
        "kern",
        "mkern",
        "mspace",
        "phantom",
        "hphantom",
        "vphantom",
        "smash",
        "llap",
        "rlap",
        "clap",
        "thinspace",
        "medspace",
        "thickspace",
        "negthinspace",
        "nobreak",
        "displaystyle",
        "textstyle",
        "scriptstyle",
        "scriptscriptstyle",
        "nonumber",
        "backslash",
        # symbols
        "infty",
        "partial",
        "nabla",
        "hbar",
        "ell",
        "Re",
        "Im",
        "aleph",
        "wp",
        "emptyset",
        "varnothing",
        "forall",
        "exists",
        "nexists",
        "neg",
        "lnot",
        "top",
        "bot",
        "angle",
        "measuredangle",
        "triangle",
        "square",
        "blacksquare",
        "diamond",
        "lozenge",
        "clubsuit",
        "diamondsuit",
        "heartsuit",
        "spadesuit",
        "prime",
        "backprime",
        "surd",
        "degree",
        "ldots",
        "cdots",
        "vdots",
        "ddots",
        "dots",
        "dotsm",
        "dotsc",
        "dotsb",
        "dotsi",
        "dotso",
        "ldotp",
        "cdotp",
        "AA",
        "aa",
        "O",
        "o",
        "OE",
        "oe",
        "AE",
        "ae",
        "L",
        "l",
        "ss",
        "dag",
        "ddag",
        "P",
        "S",
        "copyright",
        "circledR",
    }
)

#: ``\cmd`` inside a math span — a letter run, or a single non-letter
#: char (``\,`` ``\;`` ``\!`` ``\%`` ``\$`` ``\&`` ``\_`` ``\{`` ``\}``
#: ``\-`` and accent shorthands all escape to literal glyphs/spacing).
_MATH_CMD_RE = re.compile(r"\\([a-zA-Z]+|[^a-zA-Z])")

#: Single-char ``\x`` commands that are pure glyph escapes or spacing —
#: every non-letter following a backslash is a literal escape
#: (``\%`` → %, ``\,`` → thin space, ``\\`` → line break), never a macro.
_SAFE_MATH_ESCAPES: frozenset[str] = frozenset(
    {
        ",",
        ";",
        ":",
        "!",
        " ",
        "%",
        "$",
        "&",
        "_",
        "#",
        "{",
        "}",
        "-",
        "'",
        "`",
        "^",
        "~",
        "=",
        ".",
        '"',
        "|",
        "/",
        "(",
        ")",
        "[",
        "]",
        "<",
        ">",
        "\\",
    }
)


def _balance_braces(text: str) -> str:
    """Balance the braces of a .bib field value the way BibTeX counts
    them: EVERY ``{``/``}`` byte — ``\\{``/``\\}`` included. Backslash-
    escaping a stray brace does NOT protect the field (BibTeX still
    decrements its depth and closes ``title = {…}`` early — Opus
    re-review of gr515709), so instead: a ``}``/``\\}`` with nothing
    open is dropped, and opens still open at the end get their closer
    appended — ``\\}`` for a ``\\{`` so the LaTeX side keeps a literal
    pair rather than gaining a stray group-close. Runs on the WHOLE
    rendered title, not per segment — a ``{`` in one text span pairs
    with a ``}`` inside a math island for BibTeX purposes.
    """
    out: list[str] = []
    stack: list[bool] = []  # True → opened by \{ (close with \})
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            pair = text[i : i + 2]
            if pair == r"\{":
                stack.append(True)
            elif pair == r"\}":
                if not stack:
                    i += 2
                    continue
                stack.pop()
            out.append(pair)
            i += 2
            continue
        if ch == "{":
            stack.append(False)
        elif ch == "}":
            if not stack:
                i += 1
                continue
            stack.pop()
        out.append(ch)
        i += 1
    out.extend(r"\}" if esc else "}" for esc in reversed(stack))
    return "".join(out)


#: ``\cmd`` letter-runs that are pure typesetting in TEXT mode (Opus
#: re-review of gr515709): the letter-form text accents (``\u`` ``\v``
#: ``\H`` ``\c`` ``\d`` ``\b`` ``\t`` ``\r`` ``\k`` — the shorthand
#: accents ``\'`` ``\` `` ``\"`` ``\^`` ``\~`` ``\=`` ``\.`` are
#: ``\``+non-letter safe spans and never reach this scan), the dotless
#: and ligature letters (``\i`` ``\j`` ``\o`` ``\ae`` ``\ss`` family),
#: text font commands, glyph commands and the ``\LaTeX``/``\TeX``
#: logos. Anything else — ``\input`` ``\write`` ``\def`` … — is a live
#: macro and neutralised to ``\textbackslash{}name``: ``\backslash`` is
#: math-only, so in text it breaks the compile with "Missing $".
_SAFE_TEXT_MACROS: frozenset[str] = frozenset(
    {
        # letter-form text accents + dotless letters
        "u",
        "v",
        "H",
        "c",
        "d",
        "b",
        "t",
        "r",
        "k",
        "i",
        "j",
        # ligature / special letters
        "AA",
        "aa",
        "O",
        "o",
        "OE",
        "oe",
        "AE",
        "ae",
        "L",
        "l",
        "SS",
        "ss",
        # text symbols
        "dag",
        "ddag",
        "P",
        "S",
        "copyright",
        "circledR",
        "pounds",
        # font / typesetting commands
        "textit",
        "textbf",
        "emph",
        "textsc",
        "textsf",
        "texttt",
        "textrm",
        "textnormal",
        "textup",
        "textmd",
        "textsl",
        "textsuperscript",
        "textsubscript",
        "underline",
        "mbox",
        # literal glyphs
        "textbackslash",
        "textless",
        "textgreater",
        # logos
        "LaTeX",
        "LaTeXe",
        "TeX",
    }
)

#: A ``\cmd`` letter-run in text mode — passed through when allowlisted,
#: else neutralised to ``\textbackslash{}cmd``. ``\x`` with a non-letter
#: ``x`` is a glyph escape handled as a safe span, and a trailing lone
#: ``\`` becomes ``\textbackslash{}``.
_TEXT_CMD_RE = re.compile(r"\\([a-zA-Z]+)|\\$")


def _sanitize_math_span(span: str) -> str:
    """Neutralise non-allowlisted commands inside a ``$…$`` math island.

    An allowlisted ``\\cmd`` passes through; anything else becomes
    ``\\backslash{cmd}`` — rendered literally in the .bib, never
    executed (gr515709). The ``$``/``$$`` delimiters ride around the
    sanitised body unchanged. ``^^`` escapes never reach here —
    :func:`bibtex_title` neutralises them on the whole string before
    the safe-span split — and unbalanced braces are handled by
    :func:`_balance_braces` on the whole field afterwards.
    """
    n = 2 if span.startswith("$$") else 1
    inner = span[n : len(span) - n]

    def _repl(m: re.Match[str]) -> str:
        cmd = m.group(1)
        if cmd in _SAFE_MATH_MACROS or (len(cmd) == 1 and cmd in _SAFE_MATH_ESCAPES):
            return m.group(0)
        return rf"\backslash{{{cmd}}}"

    return span[:n] + _MATH_CMD_RE.sub(_repl, inner) + span[len(span) - n :]


def bibtex_title(raw: str) -> str:
    """The LaTeX-correct BibTeX title from the raw stored title: the md
    view with ``**x**`` → ``\\textbf{x}`` / ``*x*`` → ``\\emph{x}`` and
    ``_``/``&``/``%``/``#`` escaped — but only in the non-math spans.
    Math islands keep valid LaTeX but only for the
    :data:`_SAFE_MATH_MACROS` allowlist: a ``\\cmd`` that can act at
    compile time is neutralised by :func:`_sanitize_math_span`
    (gr515709); ``\\cmd`` letter-runs in text mode get the same
    treatment but against :data:`_SAFE_TEXT_MACROS`, ``^^`` escapes are
    defused on the whole string up front, and braces are balanced on
    the whole rendered title (Opus re-review of gr515709). Shared by
    ``_render_citation`` and the web References copy-as-BibTeX payload
    so both emit the same field."""
    md = title_md(raw)
    if not md:
        return ""
    # ``^^`` is TeX's input-level char escape (``^^5c`` IS a backslash,
    # ``^^69`` an ``i``). It must be defused BEFORE the safe-span split:
    # ``\^^69nput`` splits ``\^`` off as a "safe" glyph escape and the
    # leftover ``^69…`` segment no longer contains ``^^`` — the title
    # then compiles back into a live ``\input`` (Opus re-review
    # BLOCKER). ``\^{}\^{}`` renders two literal carets and is itself
    # safe in both text and math spans.
    md = md.replace("^^", r"\^{}\^{}")
    out: list[str] = []
    for seg in _LATEX_SAFE_SPAN_RE.split(md):
        if not seg:
            continue
        if _LATEX_SAFE_SPAN_RE.fullmatch(seg):
            out.append(_sanitize_math_span(seg) if seg.startswith("$") else seg)
            continue
        seg = _latex_escape(seg)
        # A ``\cmd`` in text mode is a live macro unless allowlisted —
        # neutralise it to ``\textbackslash{}cmd`` (``\backslash`` is
        # math-only). The escapes _latex_escape just wrote (``\&``
        # ``\_`` …) are ``\`` + non-letter and never match.
        seg = _TEXT_CMD_RE.sub(
            lambda m: (
                m.group(0)
                if m.group(1) in _SAFE_TEXT_MACROS
                else rf"\textbackslash{{}}{m.group(1) or ''}"
            ),
            seg,
        )
        seg = _BIBTEX_BOLD_RE.sub(r"\\textbf{\1}", seg)
        seg = _BIBTEX_ITALIC_RE.sub(r"\\emph{\1}", seg)
        out.append(seg)
    # Last, on the WHOLE title: BibTeX counts every ``{``/``}`` byte —
    # ``\{`` included — across segment boundaries, so balance the field
    # here rather than escaping per segment (which protects nothing).
    return _balance_braces("".join(out))


def _strip_jats(text: str) -> str:
    """Strip ``<jats:*>`` and ``</jats:*>`` namespace tags from text.

    Leaves tag *contents* intact, so ``<jats:p>Hi.</jats:p>`` becomes
    ``Hi.``. Idempotent. Whitespace around stripped tags is collapsed
    only where two newlines emerge — we keep paragraph structure.

    Block-level closing tags (``</jats:p>``, ``</jats:title>``) are
    replaced with a single space rather than nothing, so adjacent
    paragraphs/headings don't end up word-glued. The opening tags
    are still empty-substituted because the preceding character is
    typically already whitespace.
    """
    # Drop the redundant `<jats:title>Abstract</jats:title>` outright —
    # see comment on _JATS_ABSTRACT_TITLE_RE.
    cleaned = _JATS_ABSTRACT_TITLE_RE.sub("", text)
    # Closing tags get a space so we don't fuse "Hi.</jats:p><jats:p>Bye"
    # into "Hi.Bye". Opening tags get nothing.
    cleaned = re.sub(r"</jats:[a-zA-Z0-9_-]+\s*[^>]*>", " ", cleaned)
    cleaned = re.sub(r"<jats:[a-zA-Z0-9_-]+\s*[^>]*>", "", cleaned)
    # Collapse the spaces and newlines the strip can leave behind.
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


__all__ = [
    "ENTRY_TYPE_CHOICES",
    "ENTRY_TYPE_LABELS",
    "ENTRY_TYPE_VOCAB",
    "_author_names",
    "_clean_inline_text",
    "_format_authors",
    "_latex_escape",
    "_latex_escape_math",
    "_render_citation",
    "_strip_jats",
    "bibtex_title",
]
