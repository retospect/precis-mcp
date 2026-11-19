"""Draft → ``.docx`` export (python-docx). Sibling of ``export/latex.py``.

Synchronous and **toolchain-free** (python-docx + lxml, both already
deps) — this is the "just works" path: no latexmk, nothing to install,
nothing to compile. It mirrors the LaTeX exporter's chunk walk and inline
grammar so the two never drift, and — crucially — resolves citations
through the **same** paper-ref lookup the ``.bib`` path uses
(:func:`precis.export.latex.build_bib`). A docx and a PDF therefore cite
the *identical* resolved references: that shared resolver is the
citation-integrity guarantee.

Citation model: each ``[§slug~n]`` / ``paper:slug~n`` (and a verbatim
LaTeX ``\\cite{slug}``) becomes a superscript numbered marker ``[n]`` in
the text, backed by a numbered **References** section at the document end
carrying the resolved bibliographic entry (authors · year · title ·
DOI/arXiv). The marker is a plain run, so a paper cited many times reuses
its number at every site — deliberately *not* a native Word endnote field,
which must be referenced exactly once (reusing one makes Word declare the
file's content unreadable). Math is native OMML (see ``_render_math``).

A paragraph whose entire text is one ``$$…$$`` span (the SAME standalone-
equation shape ``export/latex.py`` detects) numbers "(N)" right-aligned
next to the equation; a bare ``[dc<id>]`` cross-ref to that chunk resolves
to "Eq. (N)". Unlike the PDF path's cleveref, the numbers are computed once
at export time from document order (:func:`_standalone_equation_numbers`)
and written as the cached result of Word fields: a ``SEQ`` field in each
figure / table caption and equation number, a bookmark ``_Ref_<dc>`` around
it (and around a heading's typed number), and a ``REF`` field in each
cross-reference, so Word renumbers on a field update (the exporter does not
set ``w:updateFields``: the file reads correctly as written).
"""

from __future__ import annotations

import html
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from precis.export import _abbrev, progress, walker
from precis.export._cite_ids import (
    merged_cite_numbers,
    merged_cite_numbers_si,
    paper_identifier,
)
from precis.export._data_package import (
    SECTION_TITLE as _DATA_PACKAGE_SECTION_TITLE,
)
from precis.export._data_package import (
    SIDECAR_NAME as _DATA_PACKAGE_SIDECAR_NAME,
)
from precis.export._data_package import (
    DataPackageFigure,
    collect_entry,
    header_lines,
    sidecar_json,
    table_lines,
    verify_snapshot,
)
from precis.export._nanopub_appendix import (
    SECTION_TITLE as _NANOPUB_SECTION_TITLE,
)
from precis.export._nanopub_appendix import (
    published_claim_entries,
)
from precis.export._patent_cite import (
    render_paper_inline_citation,
    render_patent_citation,
)
from precis.export._provenance_strip import (
    is_provenance_only,
    strip_provenance_handles,
)
from precis.export._provenance_strip import (
    short_title as _short_title,
)
from precis.export._ref_cache import RefLookupMemo
from precis.export._trust_marks import (
    UNSUPPORTED_MARK_TEXT,
    mark_text,
    record_override_event,
    unverified_claims_entries,
)
from precis.export.latex import (
    _CITE_ENTRY_TYPES,
    _MATH,
    _NAME_UNISPACE,
    _NAME_ZEROWIDTH,
    _PATENT_DOC_TYPE,
    _conjunct_cite_keys,
    _iter_refs,
    _math_braces_balanced,
    _math_plausible,
    _standalone_equation,
    datasheet_pub_label,
    paper_bib_type,
    preprocess_draft_inline,
    record_bib_type,
)
from precis.nanopub import pin_lint
from precis.utils import adsorbate_star, handle_registry
from precis.utils.authors import build_byline
from precis.utils.draft_markup import DRAFT_CITE_PATTERN
from precis.utils.mentions import (
    COMPUTED_EVIDENCE_KINDS,
    LINKIFY_KINDS,
    parse_pin_suffix,
)
from precis.utils.paper_links import doi_url, uol_url
from precis.utils.workspace import Workspace

if TYPE_CHECKING:
    from precis.store import Store
    from precis.store.protocols import RefLookupStore

#: chunk depth → Word heading level (1..4); deeper collapses to 4.
_MAX_HEADING_LEVEL = 4

#: A sober scientific-manuscript theme layered over python-docx's (bright,
#: blue-accented Calibri) default template. Times New Roman, all black, at
#: the sizes a journal submission expects. ``_apply_paper_theme`` writes
#: these onto the built-in named styles, so every paragraph/heading picks
#: them up without any per-run work. Tweak the constants here to re-tune.
_BODY_FONT = "Times New Roman"  # the journal / manuscript standard serif
_HEADING_FONT = "Times New Roman"
_BODY_PT = 12  # 12 pt body — standard for a double-spaced manuscript
_INK = "000000"  # everything black — no grey, no accent colour
#: hyperlink colour — black too (kept underlined so it still reads as a link).
_LINK_INK = "000000"
#: page margins, inches — the standard 1 in on all four sides.
_MARGIN_IN = 1.0
#: Standalone-equation numbering layout: the conventional manuscript tab
#: pattern — a CENTER tab (the equation sits mid-line) then a RIGHT tab at
#: the text-body's right edge (the "(N)" label), on an assumed default
#: 8.5in page width matching python-docx's built-in template.
_EQ_CENTER_TAB_IN = (8.5 - 2 * _MARGIN_IN) / 2
_EQ_NUMBER_TAB_IN = 8.5 - 2 * _MARGIN_IN
#: Caption formatting lives in Word styles, never in direct run formatting,
#: so an author restyles every caption by editing one style in Word:
#: ``Caption`` is Word's built-in caption paragraph style (the one *Insert
#: Caption* and *Table of Figures* use) and carries the figure-caption text;
#: ``Table Caption`` (based on it) the title above a table; ``Caption Label``
#: (character) the "Figure N:" / "Table N:" label; ``Figure`` the paragraph
#: that holds the picture.
_CAPTION_STYLE = "Caption"
_TABLE_CAPTION_STYLE = "Table Caption"
_CAPTION_LABEL_STYLE = "Caption Label"
_FIGURE_STYLE = "Figure"
#: The reference list and the acronym list are styled the same way.
#: ``Bibliography`` is Word's built-in name; in EndNote mode the list takes
#: ``EndNote Bibliography``, the name EndNote gives the list it regenerates,
#: so the author's changes to the style outlive an EndNote update.
#: ``Definition`` is one "SHORT: long form" entry, ``Definition Term``
#: (character) its "SHORT:" label.
_BIBLIOGRAPHY_STYLE = "Bibliography"
_ENDNOTE_BIBLIOGRAPHY_STYLE = "EndNote Bibliography"
_DEFINITION_STYLE = "Definition"
_DEFINITION_TERM_STYLE = "Definition Term"
#: One entry of the "Published claim artifacts" section: the claim sentence,
#: its source numbers, a linked word "link" (no URL printed) and, until the
#: artifact is public, " (under embargo)".
_PUBLISHED_CLAIM_STYLE = "Published Claim"
#: The abstract: the prose between the title block and the first heading.
#: ``Abstract Title`` is its one-word label (bold; a paragraph style, not a
#: heading, so it takes no section number and stays out of the outline),
#: ``Abstract`` the paragraphs themselves. A paragraph that starts with
#: "Keywords:" ends the abstract.
_ABSTRACT_TITLE_STYLE = "Abstract Title"
_ABSTRACT_STYLE = "Abstract"
_ABSTRACT_LABEL = "Abstract"
_KEYWORDS_START = re.compile(r"[\s*_]*keywords\s*:", re.IGNORECASE)
#: Running header and footer: Word's built-in ``Header`` / ``Footer`` styles
#: carry their alignment, so the author restyles them in Word. The running
#: title is cut at its first subtitle separator and, if still longer than
#: this, at a word boundary.
_HEADER_STYLE = "Header"
_FOOTER_STYLE = "Footer"
_RUNNING_TITLE_MAX = 60
_SUBTITLE_SEP = re.compile(r":|\s-\s|\s*[\u2013\u2014]\s*")
#: thin space between a byline's affiliation number and its ORCID mark.
_THIN_SPACE = "\u2009"
#: hanging indent of a reference entry, inches: every line but the first is
#: indented, and the tab after "[n]" lands the first line's text on it too.
_BIB_HANGING_IN = 0.5


def _apply_paper_theme(doc: Any) -> None:
    """Reface the built-in styles + page margins to a sober paper look.

    python-docx's default template ships bright Calibri body text, blue
    Calibri-Light headings, and Word's stock margins; we overwrite the
    named styles the walk actually uses (``Normal``, ``Title``, ``Heading
    1``–``4``) with black Times New Roman and set 1-inch margins. Guarded
    per style — a template missing one of these names just skips it."""
    from docx.shared import Inches, Pt, RGBColor

    def _restyle(name: str, *, font: str, size_pt: int | None = None) -> None:
        try:
            style = doc.styles[name]
        except KeyError:  # pragma: no cover - depends on the docx template
            return
        style.font.name = font
        style.font.color.rgb = RGBColor.from_string(_INK)
        if size_pt is not None:
            style.font.size = Pt(size_pt)

    _restyle("Normal", font=_BODY_FONT, size_pt=_BODY_PT)
    _restyle("Title", font=_HEADING_FONT)
    for lvl in range(1, _MAX_HEADING_LEVEL + 1):
        _restyle(f"Heading {lvl}", font=_HEADING_FONT)

    _ensure_caption_styles(doc)

    margin = Inches(_MARGIN_IN)
    for section in doc.sections:
        section.top_margin = margin
        section.bottom_margin = margin
        section.left_margin = margin
        section.right_margin = margin


def _ensure_caption_styles(doc: Any) -> None:
    """Define the four caption styles (idempotent; see ``_CAPTION_STYLE``).

    Figure captions are italic under a bold label, table titles bold; both
    are left-aligned. ``Figure`` and ``Table Caption`` keep with the next
    paragraph, so a picture stays on the page of its caption and a title on
    the page of its table."""
    from docx.enum.style import WD_STYLE_TYPE
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt, RGBColor

    styles = doc.styles
    if _CAPTION_LABEL_STYLE in styles:
        return

    def _get_or_add(name: str, style_type: Any) -> Any:
        return styles[name] if name in styles else styles.add_style(name, style_type)

    try:
        normal = styles["Normal"]
    except KeyError:  # pragma: no cover - depends on the docx template
        normal = None

    # python-docx's template ships ``Caption`` as 9 pt bold accent-blue.
    caption = _get_or_add(_CAPTION_STYLE, WD_STYLE_TYPE.PARAGRAPH)
    caption.base_style = normal
    caption.font.name = _BODY_FONT
    caption.font.size = Pt(_BODY_PT)
    caption.font.color.rgb = RGBColor.from_string(_INK)
    caption.font.bold = False
    caption.font.italic = True
    caption.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.LEFT

    table_caption = _get_or_add(_TABLE_CAPTION_STYLE, WD_STYLE_TYPE.PARAGRAPH)
    table_caption.base_style = caption
    table_caption.font.bold = True
    table_caption.font.italic = False
    table_caption.paragraph_format.keep_with_next = True

    figure = _get_or_add(_FIGURE_STYLE, WD_STYLE_TYPE.PARAGRAPH)
    figure.base_style = normal
    figure.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
    figure.paragraph_format.keep_with_next = True

    label = _get_or_add(_CAPTION_LABEL_STYLE, WD_STYLE_TYPE.CHARACTER)
    label.font.bold = True
    label.font.italic = False


def _bibliography_style(doc: Any, name: str) -> Any:
    """The reference-list paragraph style ``name``, defined on first use:
    based on ``Normal`` with a hanging indent (see ``_BIBLIOGRAPHY_STYLE``)."""
    from docx.enum.style import WD_STYLE_TYPE
    from docx.shared import Inches

    styles = doc.styles
    if name in styles:
        return styles[name]
    style = styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
    if "Normal" in styles:
        style.base_style = styles["Normal"]
    style.paragraph_format.left_indent = Inches(_BIB_HANGING_IN)
    style.paragraph_format.first_line_indent = Inches(-_BIB_HANGING_IN)
    return style


def _definition_styles(doc: Any) -> tuple[Any, Any]:
    """The ``Definition`` paragraph style and the ``Definition Term``
    character style, defined on first use (see ``_DEFINITION_STYLE``)."""
    from docx.enum.style import WD_STYLE_TYPE

    styles = doc.styles
    if _DEFINITION_STYLE in styles:
        para = styles[_DEFINITION_STYLE]
    else:
        para = styles.add_style(_DEFINITION_STYLE, WD_STYLE_TYPE.PARAGRAPH)
        if "Normal" in styles:
            para.base_style = styles["Normal"]
    if _DEFINITION_TERM_STYLE in styles:
        term = styles[_DEFINITION_TERM_STYLE]
    else:
        term = styles.add_style(_DEFINITION_TERM_STYLE, WD_STYLE_TYPE.CHARACTER)
        term.font.bold = True
    return para, term


def _published_claim_style(doc: Any) -> Any:
    """The ``Published Claim`` paragraph style, defined on first use:
    based on ``Normal``, nothing else (see ``_PUBLISHED_CLAIM_STYLE``)."""
    from docx.enum.style import WD_STYLE_TYPE

    styles = doc.styles
    if _PUBLISHED_CLAIM_STYLE in styles:
        return styles[_PUBLISHED_CLAIM_STYLE]
    style = styles.add_style(_PUBLISHED_CLAIM_STYLE, WD_STYLE_TYPE.PARAGRAPH)
    if "Normal" in styles:
        style.base_style = styles["Normal"]
    return style


def _abstract_styles(doc: Any) -> tuple[Any, Any]:
    """The ``Abstract Title`` and ``Abstract`` paragraph styles, defined on
    first use (see ``_ABSTRACT_TITLE_STYLE``)."""
    from docx.enum.style import WD_STYLE_TYPE

    styles = doc.styles
    out = []
    for name in (_ABSTRACT_TITLE_STYLE, _ABSTRACT_STYLE):
        if name in styles:
            out.append(styles[name])
            continue
        style = styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
        if "Normal" in styles:
            style.base_style = styles["Normal"]
        if name == _ABSTRACT_TITLE_STYLE:
            style.font.bold = True
            style.paragraph_format.keep_with_next = True
        out.append(style)
    return out[0], out[1]


def running_title(title: str | None) -> str:
    """The page-header title: the draft title as plain text, cut at the
    first ``:`` / `` - `` / en or em dash, then, if still longer than
    ``_RUNNING_TITLE_MAX``, at a word boundary with an ellipsis. A title that
    starts with a separator, so the cut leaves nothing, keeps its whole text."""
    from precis.utils.title_views import title_plain

    whole = title_plain(title).strip()
    text = _SUBTITLE_SEP.split(whole, maxsplit=1)[0].strip() or whole
    if len(text) <= _RUNNING_TITLE_MAX:
        return text
    head = text[:_RUNNING_TITLE_MAX]
    if text[_RUNNING_TITLE_MAX] != " " and " " in head:
        head = head.rsplit(" ", 1)[0]
    return head.rstrip(" ,;:") + "\u2026"


def running_authors(byline: dict[str, Any]) -> str:
    """The page-header author text from the byline: one author, the family
    name; two, ``A and B``; more, ``A et al.``; none, nothing. The family
    name is the last token of the byline's natural-order name (the heuristic
    :func:`precis.export.endnote._sortable_name` uses for a comma-less name)."""
    names = [a["name"].strip().rsplit(" ", 1)[-1] for a in byline.get("authors") or []]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{names[0]} et al."


def _add_field(
    paragraph: Any, instr: str, cached: str, *, style: str | None = None
) -> None:
    """Append a live Word field (``PAGE``, ``SEQ``, ``REF``) with its
    cached result, as the hand-written EndNote fields are
    (:mod:`.endnote`). ``style``: a character style every run of the field
    carries (the caption label's)."""
    from docx.enum.style import WD_STYLE_TYPE
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    from precis.export.endnote import _cached_run, _fld_char, _instr_text

    runs = [
        _fld_char("begin"),
        _instr_text(f" {instr} "),
        _fld_char("separate"),
        _cached_run(cached),
        _fld_char("end"),
    ]
    if style is not None:
        style_id = paragraph.part.get_style_id(style, WD_STYLE_TYPE.CHARACTER)
        for r in runs:
            rpr = r.find(qn("w:rPr"))
            if rpr is None:
                rpr = OxmlElement("w:rPr")
                r.insert(0, rpr)
            rstyle = OxmlElement("w:rStyle")
            rstyle.set(qn("w:val"), style_id)
            rpr.insert(0, rstyle)
    for r in runs:
        paragraph._p.append(r)


def _bookmark_name(dc: str) -> str:
    """The bookmark a number is cross-referenced through: ``_Ref_<dc>``
    (leading underscore: hidden in Word's bookmark list; letters, digits and
    underscore only, at most 40 characters)."""
    return ("_Ref_" + re.sub(r"\W", "_", dc))[:40]


def _bookmarked(paragraph: Any, ctx: _Ctx | None, dc: str | None, add: Any) -> None:
    """Call ``add()`` (it appends the number's runs to ``paragraph``) and wrap
    what it appended in the bookmark :func:`_bookmark_name` of ``dc``. No
    bookmark without a handle, or for a name already written."""
    name = _bookmark_name(dc) if dc and ctx is not None else None
    if name is None or ctx is None or name in ctx.bookmarks:
        add()
        return
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    ident = len(ctx.bookmarks) + 1
    ctx.bookmarks[name] = ident
    start = OxmlElement("w:bookmarkStart")
    start.set(qn("w:id"), str(ident))
    start.set(qn("w:name"), name)
    end = OxmlElement("w:bookmarkEnd")
    end.set(qn("w:id"), str(ident))
    paragraph._p.append(start)
    add()
    paragraph._p.append(end)


def _unlink_dangling_refs(doc: Any) -> None:
    """A ``REF`` field whose bookmark was never written (its target is not
    rendered: a withheld figure, say) keeps its cached number as plain
    text; Word would show "Error! Reference source not found." on update."""
    from docx.oxml.ns import qn

    body = doc.element.body
    names = {b.get(qn("w:name")) for b in body.iter(qn("w:bookmarkStart"))}
    for instr in list(body.iter(qn("w:instrText"))):
        parts = (instr.text or "").split()
        if len(parts) < 2 or parts[0] != "REF" or parts[1] in names:
            continue
        run = instr.getparent()
        begin = run.getprevious()
        separate = run.getnext()
        cached = separate.getnext() if separate is not None else None
        end = cached.getnext() if cached is not None else None
        for r in (begin, run, separate, end):
            if r is not None:
                r.getparent().remove(r)


def _header_footer(doc: Any, title: str | None, byline: dict[str, Any]) -> None:
    """Footer on every page ("Page N", a live ``PAGE`` field; no page count,
    so trailing pages can be left out of a printout unnoticed) and a
    running header on every page after the first (title at the left,
    ``Family et al.`` at the right margin). Both take Word's built-in
    ``Header`` / ``Footer`` styles, which carry the alignment: the footer
    is centred, the header has one right tab stop at the text edge."""
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
    from docx.shared import Inches

    styles = doc.styles
    footer_style = styles[_FOOTER_STYLE]
    footer_style.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
    header_style = styles[_HEADER_STYLE]
    tabs = header_style.paragraph_format.tab_stops
    tabs.clear_all()
    tabs.add_tab_stop(Inches(_EQ_NUMBER_TAB_IN), WD_TAB_ALIGNMENT.RIGHT)

    left, right = running_title(title), running_authors(byline)
    for section in doc.sections:
        section.different_first_page_header_footer = True
        for footer in (section.footer, section.first_page_footer):
            footer.is_linked_to_previous = False
            par = footer.paragraphs[0]
            par.style = footer_style
            par.add_run("Page ")
            _add_field(par, "PAGE", "1")
        # The first page keeps an explicitly empty header.
        first = section.first_page_header
        first.is_linked_to_previous = False
        first.paragraphs[0].style = header_style
        header = section.header
        header.is_linked_to_previous = False
        par = header.paragraphs[0]
        par.style = header_style
        par.add_run(f"{left}\t{right}" if right else left)


@dataclass
class DocxResult:
    """Path written + diagnostics (parallel to the LaTeX ``ExportResult``)."""

    path: Path
    cited_slugs: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: Set only when the draft rendered ≥1 figure carrying a data-package
    #: snapshot — the ``data-package.json`` sidecar written beside
    #: ``target_path`` (draft-pathway-figures-data-package.md). ``None``
    #: when no figure carried a snapshot.
    data_package_path: Path | None = None


@dataclass
class _XrefRun:
    """The cross-reference run last printed into ``paragraph``: its
    ``members`` and the paragraph children (``elements``) it wrote, so an
    adjacent reference can take them out and print the longer run."""

    paragraph: Any
    members: list[walker.Xref]
    elements: list[Any]


@dataclass
class _Ctx:
    # store stays Any: unit tests (test_export_docx_patent.py) construct
    # _Ctx directly with a hand-rolled fake (_Store: only get_ref) narrower
    # than the Store the export_docx() path always threads through here.
    store: Any
    known_handles: set[str]
    audience: Any = None
    abbrevs: dict[str, str] = field(default_factory=dict)  # short → long
    cited: list[str] = field(default_factory=list)  # paper slug order = ref number
    warnings: list[str] = field(default_factory=list)
    #: Read cache for the pin lint (:func:`precis.nanopub.pin_lint.warn_export`).
    pin_ctx: Any = None
    seen_acr: set[str] = field(default_factory=set)  # already expanded once
    used_acr: set[str] = field(default_factory=set)  # for the acronyms list
    #: Rendering a heading: abbreviations print bare and are not defined
    #: there (gr491599 M2, same as the PDF).
    in_heading: bool = False
    #: Buffered run of *directly adjacent* citations pending one merged mark /
    #: one EndNote field, plus the same-paper collapse state — the walker's
    #: docx grouping (:class:`walker.CiteRun`). A ref match with an EMPTY gap
    #: before it extends the run; anything else flushes it
    #: (:func:`_flush_cites`).
    cite_run: walker.CiteRun = field(default_factory=walker.CiteRun)
    #: Set by :func:`_finding_cite_keys_pinned` when a finding cite had no
    #: citable source; :func:`_render_inline` reads and clears it to pull the
    #: space before the dropped mark off the preceding run (mirrors
    #: ``export/latex.py``'s ``_Ctx.dropped_cite``).
    dropped_cite: bool = False
    endnote: bool = False  # emit EndNote CWYW fields instead of plain [n]
    resolved: dict[str, dict[str, Any]] = field(default_factory=dict)  # slug→record
    doc_type: str = ""  # meta.workspace.doc_type; "patent" → in-text, no refs
    #: Append the doi hyperlink run after each plain ``[n]`` citation
    #: marker — mirrors ``export/latex.py``'s ``_Ctx.doi_links``. No effect
    #: in patent/endnote mode (their own citation rendering).
    doi_links: bool = True
    #: Append the library-discovery-search hyperlink run (labelled
    #: ``config.library_label``) after each plain ``[n]`` marker — mirrors
    #: ``export/latex.py``'s ``_Ctx.library_links``. Independent of
    #: ``doi_links``.
    library_links: bool = True
    #: Link text for the library-search run (mirrors ``export/latex.py``'s
    #: ``_Ctx.library_label``) — read from config ONCE by
    #: :func:`export_docx` (not per cite site). An empty string (the
    #: dataclass default, used by tests that construct ``_Ctx`` directly)
    #: falls back to :func:`~precis.config.load_config` in
    #: ``__post_init__``.
    library_label: str = ""
    #: Search-URL template for the library-search run, mirrors
    #: ``export/latex.py``'s ``_Ctx.library_search_url`` — same
    #: once-per-export loading and test-fallback rule as
    #: :attr:`library_label`.
    library_search_url: str = ""
    #: ``{ref_id: identifier}`` memo, mirrors ``export/latex.py``'s
    #: ``_Ctx._identifier_cache``.
    _identifier_cache: dict[int, str] = field(default_factory=dict, repr=False)
    #: Memos for :func:`precis.store.si_links.cite_target_for` and the
    #: slug-level SI resolution in :func:`_resolve_si_slug`.
    _cite_target_cache: dict[int, tuple[Any, bool]] = field(
        default_factory=dict, repr=False
    )
    _si_slug_cache: dict[str, tuple[str, bool]] = field(
        default_factory=dict, repr=False
    )
    #: Memo for :func:`_bare_handle_text` (handle → display text).
    _bare_handle_cache: dict[str, str] = field(default_factory=dict, repr=False)
    #: Trust-mark bookkeeping (the trust-surfaces export marking), mirrors
    #: ``export/latex.py``'s ``_Ctx.trust`` — set in ``__post_init__``.
    trust: Any = None
    #: figures whose ``meta["figure"]["data_package"]`` snapshot was present,
    #: mirrors ``export/latex.py``'s ``_Ctx.data_package``.
    data_package: list[DataPackageFigure] = field(default_factory=list)
    #: dc handle → 1-based equation number, precomputed over the WHOLE
    #: draft before rendering starts (a cross-ref can precede its
    #: equation) by :func:`_standalone_equation_numbers`; it is the cached
    #: result of the equation's ``SEQ`` field and of the ``REF`` fields
    #: that point at it.
    eq_numbers: dict[str, int] = field(default_factory=dict)
    #: dc handle → ``(noun, number)`` for a heading (``("Section", "1.5.5")``)
    #: or figure (``("Figure", "3")``), precomputed over the whole draft by
    #: :func:`_xref_numbers` the same way LaTeX would count them, so a bare
    #: ``[dc<id>]`` cross-ref prints what ``\cref`` would.
    xref: dict[str, tuple[str, str]] = field(default_factory=dict)
    #: bookmark name → ``w:id`` of every bookmark written so far
    #: (:func:`_bookmarked`); a name is written once.
    bookmarks: dict[str, int] = field(default_factory=dict, repr=False)
    #: dc handles of headings that display a section number (depth <= 2).
    numbered_headings: set[str] = field(default_factory=set)
    #: reading-order index and enclosing numbered section per dc handle, and
    #: the chunk being rendered: a reference into the citing chunk's own
    #: section prints "above" / "below" (:func:`walker.relative_word`).
    xref_order: dict[str, int] = field(default_factory=dict)
    xref_section: dict[str, str] = field(default_factory=dict)
    #: dc handle → cleveref type of its label (:attr:`walker.XrefNumbers.types`),
    #: and the cross-reference run last printed into a paragraph
    #: (:class:`_XrefRun`), which an adjacent reference extends.
    xref_types: dict[str, str] = field(default_factory=dict)
    _xref_run: _XrefRun | None = field(default=None, repr=False)
    cursor: walker.XrefCursor = field(default_factory=walker.XrefCursor)
    #: every live ``dc`` handle (cross-ref liveness) and legacy ``¶`` handle →
    #: ``dc`` — mirrors ``export/latex.py``'s ``known_handles``/``legacy_to_dc``.
    dc_handles: set[str] = field(default_factory=set)
    legacy_to_dc: dict[str, str] = field(default_factory=dict)
    #: ``dc`` handles of uncleared third-party figures waived under
    #: ``placeholder_figures``: rendered as a bordered notice, never embedded.
    withheld_figures: frozenset[str] = frozenset()
    #: Memo for :func:`walker.dead_xref` — a dead cross-ref handle maps to
    #: ``(replacement dc | None, neutral text)`` (gr493181).
    _dead_xref_cache: dict[str, tuple[str | None, str]] = field(
        default_factory=dict, repr=False
    )
    #: Restore every repeated mark (:class:`walker.RepeatMarks`); the default
    #: prints a mark once per run of equal marks within a block.
    repeat_cites: bool = False
    #: Two records of one DOI share a reference number (set in ``__post_init__``).
    doi_aliases: walker.DoiAliases = field(init=False, repr=False)
    #: The block's repeated-mark state (fresh per :func:`_render_inline`), the
    #: EndNote field of the last printed mark (None in plain mode), a flag
    #: set while the mark right before a trust mark flushes (never dropped),
    #: and ``(paragraph, len(paragraph.runs))`` right after a dropped mark —
    #: the prose either side closes up (:func:`walker.close_up_dropped`).
    _marks: walker.RepeatMarks = field(default_factory=walker.RepeatMarks, repr=False)
    _kept_field: Any = field(default=None, repr=False)
    _force_keep: bool = field(default=False, repr=False)
    _dropped_mark: tuple[Any, int] | None = field(default=None, repr=False)
    #: The plain text before and after the reference being rendered
    #: (:func:`walker.mark_context`): what the repeat rule reads.
    _ref_context: tuple[str, str] = field(default=("", ""), repr=False)
    #: ``(paragraph, len(paragraph.runs))`` right after an ``(SI)``
    #: postnote mark — a directly adjacent following mark takes a
    #: separating space ("(SI) [4]", gr491600's nit); stale once other
    #: runs land.
    _si_mark: tuple[Any, int] | None = field(default=None, repr=False)
    #: finding ref id -> bibliography slugs its cite sites printed
    #: (registered in ``cited`` by :func:`_cite`; never added to here), in
    #: first-seen order.
    claim_cites: dict[int, list[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.doi_aliases = walker.DoiAliases(self.store, self.cited, self.warnings)
        if self.trust is None and self.store is not None:
            from precis.export._trust_marks import TrustTracker

            self.trust = TrustTracker(self.store)
        if not self.library_label or not self.library_search_url:
            from precis.config import load_config

            cfg = load_config()
            self.library_label = self.library_label or cfg.library_label
            self.library_search_url = self.library_search_url or cfg.library_search_url

    @property
    def patent_mode(self) -> bool:
        return self.doc_type == _PATENT_DOC_TYPE

    def cite_number(self, slug: str) -> int:
        """1-based reference number for a slug (stable insertion order)."""
        return walker.register(self.cited, slug)


#: The numbering pre-pass and the title / glossary structure are the
#: walker's (:mod:`precis.export.walker`); these names stay for existing
#: callers.
_NUMBERED_DEPTHS = walker.NUMBERED_DEPTHS
_is_glossary_heading = walker.is_glossary_heading
_title_heading_dc = walker.title_heading_dc
_depth_shift = walker.depth_shift


def _standalone_equation_numbers(chunks: list[Any]) -> dict[str, int]:
    """dc handle → 1-based equation number (the walker's pre-pass)."""
    return walker.number_xrefs(chunks, None, equation_of=_standalone_equation).equations


def _xref_numbers(
    chunks: list[Any], title: str | None = None
) -> tuple[dict[str, tuple[str, str]], set[str]]:
    """``({dc: (noun, number)}, {dc of headings that show a number})`` from
    the walker's pre-pass (:func:`walker.number_xrefs`)."""
    nums = walker.number_xrefs(chunks, title, equation_of=_standalone_equation)
    return nums.labels, nums.numbered_headings


def _add_hyperlink(
    paragraph: Any,
    url: str,
    text: str,
    *,
    size_pt: float | None = None,
    superscript: bool = False,
) -> None:
    """Append a real external hyperlink run to ``paragraph`` (python-docx
    has no native helper). Styled blue + underlined so it reads as a link.
    Used for a ROR affiliation id, the byline's ORCID ``iD`` mark
    (``superscript``), and (``size_pt`` set) the small doi/UL cite-link
    runs (:func:`_cite_link_group`) — ``w:sz`` is half-points."""
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.oxml.ns import qn
    from docx.oxml.shared import OxmlElement

    r_id = paragraph.part.relate_to(url, RT.HYPERLINK, is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), r_id)
    run = OxmlElement("w:r")
    rpr = OxmlElement("w:rPr")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), _LINK_INK)
    rpr.append(color)
    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    rpr.append(underline)
    if size_pt is not None:
        sz = OxmlElement("w:sz")
        sz.set(qn("w:val"), str(int(size_pt * 2)))
        rpr.append(sz)
    if superscript:
        va = OxmlElement("w:vertAlign")
        va.set(qn("w:val"), "superscript")
        rpr.append(va)
    run.append(rpr)
    t = OxmlElement("w:t")
    t.text = text
    run.append(t)
    link.append(run)
    paragraph._p.append(link)


def _set_core_properties(doc: Any, title: str | None, byline: dict[str, Any]) -> None:
    """Replace the python-docx template's document properties (gr725247):
    title = the draft title as plain text, author = the byline's names (empty
    when the draft has none), created / modified = now (UTC); description and
    last-modified-by empty."""
    from precis.utils.title_views import title_plain

    props = doc.core_properties
    now = datetime.now(UTC)
    props.title = title_plain(title)
    props.author = ", ".join(a["name"] for a in byline.get("authors") or [])
    props.comments = ""
    props.last_modified_by = ""
    props.created = now
    props.modified = now


def _render_date(doc: Any) -> None:
    """The title-page date line — LaTeX's ``\\date{\\today}``
    ("October 9, 2026"), taken at export time (UTC)."""
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    today = datetime.now(UTC)
    p = doc.add_paragraph(f"{today:%B} {today.day}, {today.year}")
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER


def _render_byline(doc: Any, byline: dict[str, Any]) -> None:
    """Emit the author byline + affiliation block under the title heading.

    Names on one line (superscript marks when >1 affiliation; an ORCID
    iD as a superscript ``iD`` hyperlinked to the orcid.org record), then
    one affiliation per line (``¹ Org``, org hyperlinked to its ROR id).
    A no-author draft renders nothing, matching the legacy title-only
    output."""
    authors = byline.get("authors") or []
    if not authors:
        return
    multi = byline.get("multi")
    names_p = doc.add_paragraph()
    for i, a in enumerate(authors):
        if i:
            names_p.add_run(", ")
        names_p.add_run(a["name"])
        has_sup = bool(multi and a.get("sup"))
        if has_sup:
            sup = names_p.add_run(a["sup"])
            sup.font.superscript = True
        if a.get("orcid"):
            # "Name¹ ⁱᴰ": a thin space keeps the iD link off the number.
            names_p.add_run(_THIN_SPACE if has_sup else " ")
            _add_hyperlink(
                names_p, f"https://orcid.org/{a['orcid']}", "iD", superscript=True
            )
    for aff in byline.get("affiliations") or []:
        p = doc.add_paragraph()
        if multi:
            mark = p.add_run(str(aff["index"]) + " ")
            mark.font.superscript = True
        org = aff.get("org") or aff.get("ror") or ""
        if aff.get("ror"):
            _add_hyperlink(p, aff["ror"], org)
        else:
            run = p.add_run(org)
            run.italic = True


def export_docx(
    store: Store,
    ref: Any,
    *,
    target_path: Path,
    citations: str = "plain",
    doc_type: str | None = None,
    doi_links: bool = True,
    library_links: bool = True,
    withheld_figures: frozenset[str] = frozenset(),
    claim_appendix: bool = True,
    record_override: bool = False,
    audience: str = "dev",
    repeat_cites: bool = False,
) -> DocxResult:
    """Render a draft into ``target_path`` as a ``.docx``. Returns the
    path plus the cited slugs and any resolution warnings.

    ``citations`` selects how in-text references render:

    * ``"plain"`` (default) — a superscript ``[n]`` marker + a plain
      numbered **References** section. Universal, no add-in needed.
    * ``"endnote"`` — native EndNote *Cite While You Write* fields
      (``ADDIN EN.CITE`` + ``EN.REFLIST``), so EndNote recognizes and can
      reformat / manage the citations (see :mod:`precis.export.endnote`).

    ``doi_links=False`` / ``library_links=False`` independently turn off
    the small doi / library-search hyperlink runs normally appended after
    each plain ``[n]`` marker (:func:`_cite_link_group`) — both default on;
    no effect in patent/endnote mode.

    ``claim_appendix=False`` omits the closing "Published claim artifacts"
    section (otherwise the last section, after References).

    ``repeat_cites=True`` prints every citation mark; by default a mark
    naming the same references as the previous printed one in its paragraph
    (list item, caption, table cell) is dropped (:class:`walker.RepeatMarks`).

    ``record_override=True`` writes the ``ref_events`` ``export_override``
    audit row when the export folded finding(s) via an author's override
    (gr491602 — the export itself is a read; only explicit write ops pass
    it).

    ``audience='dev'`` links record handles to the configured application.
    ``external`` emits descriptive Word footnotes, a Data Supplement table
    and ``supplement.zip`` beside the document; private prose URLs refuse."""
    from docx import Document

    from precis.export import guard_exportable, guard_no_secrets
    from precis.export.audience import for_export, guard_artifacts, guard_audience
    from precis.export.supplement import write_supplement

    progress.report("reading the draft")
    guard_exportable(ref)
    guard_no_secrets(store, ref)
    guard_audience(store, ref, audience)
    if audience == "external":
        library_links = False
    target_path = Path(target_path)
    ws = Workspace.from_meta(getattr(ref, "meta", None))
    if doc_type is None:
        doc_type = ws.doc_type if ws else ""
    chunks = store.drafts.reading_order(ref.id)
    handles = {c.handle for c in chunks}
    from precis.config import load_config

    cfg = load_config()
    nums = walker.number_xrefs(
        chunks, getattr(ref, "title", None), equation_of=_standalone_equation
    )
    ctx = _Ctx(
        store=RefLookupMemo(store),
        audience=for_export(store, audience, chunks=chunks),
        known_handles=handles,
        xref=nums.labels,
        numbered_headings=nums.numbered_headings,
        xref_order=nums.order,
        xref_section=nums.section,
        xref_types=nums.types,
        dc_handles={c.dc for c in chunks},
        legacy_to_dc=walker.legacy_map(chunks),
        abbrevs=store.drafts.defined_abbrevs(ref.id),
        endnote=(citations == "endnote"),
        doc_type=doc_type,
        eq_numbers=nums.equations,
        doi_links=doi_links,
        library_links=library_links,
        library_label=cfg.library_label,
        library_search_url=cfg.library_search_url,
        withheld_figures=withheld_figures,
        repeat_cites=repeat_cites,
    )
    # Export-level advisories (gr592892: unset PRECIS_WEB_BASE_URL →
    # every dev record-handle link is path-only).
    ctx.warnings.extend(ctx.audience.warnings)

    doc = Document()
    _apply_paper_theme(doc)
    byline = build_byline(getattr(ref, "authors", None))
    _set_core_properties(doc, getattr(ref, "title", None), byline)
    _header_footer(doc, getattr(ref, "title", None), byline)

    # The seeded title chunk renders as the doc title (``\maketitle``); a
    # draft that no longer opens with it (abstract moved above, chunk
    # retired) prints the ref title up front so the first real section is
    # not mistaken for it. The walk and its decisions are the walker's
    # (:func:`walker.walk_body`); :class:`DocxRenderer` formats.
    title_dc = _title_heading_dc(chunks, getattr(ref, "title", None))
    shift = _depth_shift(chunks, title_dc)
    # The opening prose is an abstract only when a section follows it
    # (:func:`walker.has_sections`), and a patent keeps its own sections.
    renderer = DocxRenderer(
        ctx,
        store=store,
        byline=byline,
        title=getattr(ref, "title", None),
        abstract=walker.has_sections(chunks, title_dc) and not ctx.patent_mode,
    )
    progress.report("rendering", 0, len(chunks))
    walker.walk_body(
        chunks,
        renderer,
        doc,
        title_dc=title_dc,
        depth_of=lambda c: max(int(c.depth) - shift, 0),
        back_matter=walker.back_matter_handles(chunks),
        equation_of=_standalone_equation,
        numbers=walker.XrefNumbers(ctx.xref, ctx.numbered_headings, ctx.eq_numbers),
        on_chunk=lambda n: progress.report("rendering", n, len(chunks)),
        cursor=ctx.cursor,
    )
    ctx.warnings.extend(ctx.doi_aliases.same_work_warnings())

    # Back matter in the one order both exporters share
    # (``export/_back_matter.py``); a block that does not apply is left out.
    # A patent specification cites prior art in-text: no References list.
    # Each _append_* is a no-op when its block is empty.
    progress.report("references and back matter")
    back = dict.fromkeys(_DOCX_BACK_MATTER, True)
    for name, block in walker.back_matter(
        back, patent=ctx.patent_mode, claim_appendix=claim_appendix
    ):
        renderer.back_matter(doc, name, block)
    if ctx.endnote and ctx.cited:
        from precis.export.endnote import endnote_style, install_document_vars
        from precis.export.latex import resolve_bib_style

        # EN.Libraries lists only the refs that actually resolved (an
        # unresolved slug prints its plain [n] stub and never lands a
        # record); record ids are corpus ref ids (M3), not cite order.
        rec_ids = [
            ctx.resolved[s]["rec_number"] for s in ctx.cited if s in ctx.resolved
        ]
        bib_style, style_warn = resolve_bib_style(ws.style if ws else "")
        if style_warn:
            ctx.warnings.append(style_warn)
        install_document_vars(doc, rec_ids, style=endnote_style(bib_style))

    # ``ref_events`` export record (the trust-surfaces override audit) — one row
    # naming every finding this export rendered clean only via an author's
    # override. Written only on an explicit record request (gr491602: the
    # export itself is a read); no-op when nothing was overridden.
    if record_override:
        record_override_event(store, ref, ctx.trust)

    _unlink_dangling_refs(doc)
    progress.report("saving .docx")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if audience == "external":
        write_supplement(ctx.audience.entries, target_path.parent / "supplement.zip")
    doc.save(str(target_path))

    # The JSON sidecar (data-package durability tier 3), written next to
    # the .docx — the same natural output directory the file itself lands
    # in. Only written when ≥1 figure carried a snapshot.
    data_package_path: Path | None = None
    if ctx.data_package:
        data_package_path = target_path.with_name(
            f"{target_path.stem}.{_DATA_PACKAGE_SIDECAR_NAME}"
        )
        data_package_path.write_text(sidecar_json(ctx.data_package), encoding="utf-8")

    if audience == "external":
        guard_artifacts(
            store,
            ref,
            [target_path, target_path.parent / "supplement.zip"],
            cited=list(ctx.cited),
            entries=ctx.audience.entries,
        )

    return DocxResult(
        path=target_path,
        cited_slugs=list(ctx.cited),
        warnings=ctx.warnings,
        data_package_path=data_package_path,
    )


# ── figure rendering ──────────────────────────────────────────────


def _border_paragraph(par: Any) -> None:
    """Draw a thin box around a paragraph (``w:pBdr``)."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    pbdr = OxmlElement("w:pBdr")
    for side in ("top", "left", "bottom", "right"):
        el = OxmlElement(f"w:{side}")
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), "6")
        el.set(qn("w:space"), "4")
        el.set(qn("w:color"), "000000")
        pbdr.append(el)
    par._p.get_or_add_pPr().append(pbdr)


#: Placed width of an embedded figure, and the print resolution below which
#: a raster figure earns a warning (gr725249).
_FIGURE_WIDTH_IN = 5.5
_MIN_FIGURE_DPI = 300
_ALT_TEXT_MAX = 250


def _alt_text(caption: str) -> str:
    """A caption as plain text for a picture's alt text: citation marks, math
    and inline markup stripped, cut at 250 characters on a word boundary."""
    from precis.utils.title_views import title_plain

    refs = [m.span() for m in _iter_refs(caption)]
    for lo, hi in reversed(refs):
        caption = caption[:lo] + caption[hi:]
    text = title_plain(caption)
    text = re.sub(r"\*\*|(?<![\w\\])\*(?=\S)|(?<=\S)\*(?!\w)|`", "", text)
    text = re.sub(r"\s+([,.;:!?)])", r"\1", re.sub(r"\s+", " ", text)).strip()
    if len(text) <= _ALT_TEXT_MAX:
        return text
    cut = text[:_ALT_TEXT_MAX]
    if not text[_ALT_TEXT_MAX].isspace() and " " in cut:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip()


def _warn_low_resolution(store: Store, chunk: Any, data: bytes, ctx: _Ctx) -> None:
    """Warn when a raster figure's pixel width over its placed width is below
    300 dpi. A vector source the exporter rasterised itself is exempt: it
    chose the width."""
    from docx.image.image import Image

    from precis.utils.figure_source import figure_asset_is_rasterised_vector

    width_px = Image.from_blob(data).px_width
    dpi = width_px / _FIGURE_WIDTH_IN
    if dpi >= _MIN_FIGURE_DPI or figure_asset_is_rasterised_vector(store, chunk):
        return
    ctx.warnings.append(
        f"figure {chunk.dc}: {int(dpi)} dpi at {_FIGURE_WIDTH_IN} in "
        f"({width_px} px wide); below {_MIN_FIGURE_DPI} dpi"
    )


def _repeat_header_row(table: Any) -> None:
    """Mark the table's first row as a header row (``w:tblHeader``) so Word
    repeats it at the top of each page the table spans."""
    from docx.oxml import OxmlElement

    tr_pr = table.rows[0]._tr.get_or_add_trPr()
    if not tr_pr.xpath("w:tblHeader"):
        tr_pr.append(OxmlElement("w:tblHeader"))


def _render_figure(doc: Any, store: Store, chunk: Any, ctx: _Ctx) -> None:
    """Embed a figure image + its caption. Raster blobs
    embed directly; an SVG (blob-SVG or a linked canvas) arrives pre-rasterised
    to PNG via ``figure_export_asset`` (docx can't consume SVG). An asset-less
    figure degrades to a caption-only paragraph + a warning."""
    from io import BytesIO

    from docx.shared import Inches

    from precis.render.figure import ensure_rendered
    from precis.utils.figure_source import figure_export_asset

    _ensure_caption_styles(doc)
    render_err = (
        None if chunk.dc in ctx.withheld_figures else ensure_rendered(store, chunk)
    )
    asset = (
        None
        if chunk.dc in ctx.withheld_figures or render_err
        else figure_export_asset(store, chunk)
    )
    if chunk.dc in ctx.withheld_figures:
        perm = ((chunk.meta or {}).get("figure") or {}).get("permission") or {}
        who = ", ".join(
            str(perm[k]) for k in ("publisher", "source_paper") if perm.get(k)
        )
        box = doc.add_paragraph(
            "Figure withheld pending permission" + (f": {who}" if who else ""),
            style=_FIGURE_STYLE,
        )
        _border_paragraph(box)
        ctx.warnings.append(
            f"figure {chunk.dc} withheld (third-party image, permission not granted)"
            " — placeholder used"
        )
    elif render_err:
        ctx.warnings.append(f"figure {chunk.dc}: {render_err} — caption only")
    elif asset is None:
        ctx.warnings.append(f"figure {chunk.dc} has no exportable image — caption only")
    else:
        data, _ext = asset
        try:
            shape = doc.add_picture(BytesIO(data), width=Inches(_FIGURE_WIDTH_IN))
            doc.paragraphs[-1].style = _FIGURE_STYLE
            alt = _alt_text(chunk.text or "")
            if alt:
                shape._inline.docPr.set("descr", alt)
            _warn_low_resolution(store, chunk, data, ctx)
        except Exception as exc:  # pragma: no cover — pillow/format edge
            ctx.warnings.append(f"figure {chunk.dc} could not embed: {exc}")
        snapshot = collect_entry(chunk)
        if snapshot is not None:
            for problem in verify_snapshot(snapshot):
                ctx.warnings.append(
                    f"figure {chunk.dc}: {problem} — the data package carries "
                    "a computed source the export cannot re-check"
                )
            ctx.data_package.append(
                DataPackageFigure(
                    label=chunk.dc, caption=chunk.text or "", snapshot=snapshot
                )
            )
    # Formatting comes from the ``Caption`` / ``Caption Label`` styles alone
    # (gr725239): no run here carries direct bold, italic or alignment.
    cap = doc.add_paragraph(style=_CAPTION_STYLE)
    # "Figure N:" — the same reading-order number a ``[dc<id>]`` cross-ref
    # prints as "Figure N", like LaTeX's ``\\caption`` label.
    number = ctx.xref.get(chunk.dc, ("", ""))[1]
    if number:
        _caption_label(cap, ctx, chunk.dc, "Figure", number, colon=True)
    _render_inline(chunk.text or "", ctx, cap)


def _caption_label(
    cap: Any, ctx: _Ctx, dc: str, word: str, number: str, *, colon: bool
) -> None:
    """The caption label (``Figure N: `` / ``Table N``) in the ``Caption
    Label`` style: the word, the number as a ``SEQ <word>`` field (cached result ``number``) inside the
    bookmark cross-references point at, and the colon."""
    cap.add_run(f"{word} ", style=_CAPTION_LABEL_STYLE)
    _bookmarked(
        cap,
        ctx,
        dc,
        lambda: _add_field(
            cap, f"SEQ {word} \\* ARABIC", number, style=_CAPTION_LABEL_STYLE
        ),
    )
    if colon:
        cap.add_run(": ", style=_CAPTION_LABEL_STYLE)


# ── table rendering ───────────────────────────────────────────────


def _render_table(doc: Any, chunk: Any, ctx: _Ctx) -> None:
    """Render a ``chunk_kind='table'`` chunk as a native Word table. The canonical ``meta.table`` is recovered via the shared
    :func:`precis.utils.table_data.table_payload`; cells go through the same
    inline grammar as prose so citations / math / abbreviations inside a
    cell resolve identically. Falls back to a plain paragraph when the table
    can't be recovered."""
    from precis.utils.table_data import table_payload

    payload = table_payload(getattr(chunk, "meta", None), chunk.text)
    number = ctx.xref.get(chunk.dc, ("", ""))[1]
    if payload is None:
        if number:
            # The walk still numbered it: keep its "Table N" (the ``SEQ``
            # field and bookmark), or a field update renumbers every later
            # table and strands the references to this one.
            _ensure_caption_styles(doc)
            cap = doc.add_paragraph(style=_TABLE_CAPTION_STYLE)
            _caption_label(cap, ctx, chunk.dc, "Table", number, colon=False)
        _render_inline(chunk.text, ctx, doc.add_paragraph())
        return
    header, rows, caption = payload["header"], payload["rows"], payload["caption"]
    # "Table N:" — the same reading-order number a ``[dc<id>]`` cross-ref
    # prints as "Table N", like the figure caption's "Figure N:" (gr592852).
    if number or caption:
        _ensure_caption_styles(doc)
        cap = doc.add_paragraph(style=_TABLE_CAPTION_STYLE)
        if number:
            _caption_label(cap, ctx, chunk.dc, "Table", number, colon=bool(caption))
        if caption:
            _render_inline(caption, ctx, cap)
    ncols = len(header)
    table = doc.add_table(rows=1, cols=ncols)
    try:  # built-in style; absent in some templates → fall back to no style
        table.style = "Table Grid"
    except KeyError:  # pragma: no cover - depends on the docx template
        pass
    _repeat_header_row(table)
    for cell, text in zip(table.rows[0].cells, header):
        para = cell.paragraphs[0]
        # Header cells use the same inline grammar as body cells — a
        # citation in a column header numbers like the PDF's.
        _render_inline(text, ctx, para)
        for run in para.runs:
            run.bold = True
    for row in rows:
        cells = table.add_row().cells
        for j in range(ncols):
            _render_inline(row[j] if j < len(row) else "", ctx, cells[j].paragraphs[0])


# ── inline rendering ──────────────────────────────────────────────


def _render_inline(text: str, ctx: _Ctx, paragraph: Any) -> None:
    """Walk a chunk's text, adding runs to ``paragraph``. References go
    through :func:`_render_reference`; the prose gaps between them get
    markdown/sub-sup/math run formatting."""
    text = _SCRIPT_ONLY_MATH_SPAN.sub(_script_only_to_html, text)
    text = preprocess_draft_inline(text)
    if ctx.audience is not None:
        from precis.export.audience import preserve_records

        text = preserve_records(text)
    text = strip_provenance_handles(text, title_for=lambda h: _bare_handle_text(h, ctx))
    pin_lint.warn_export(ctx, text)
    # A new chunk/caption/cell never continues the previous one's cite run:
    # without this reset, a paragraph opening with the same paper the last
    # one closed on lost its mark entirely.
    ctx.cite_run.interrupt()
    ctx._marks = walker.RepeatMarks(enabled=not ctx.repeat_cites)
    ctx._kept_field = ctx._dropped_mark = ctx._xref_run = None
    last = 0
    dropped = False  # the previous reference was a cite dropped for no source
    plain = ""  # source so far, a display link as its surface text
    for m in _iter_refs(text):
        gap = text[last : m.start()]
        if dropped and gap[:1] in _CLOSING_PUNCT:
            _rstrip_last_run(paragraph)
        _render_gap(gap, ctx, paragraph)
        plain += gap
        ctx._ref_context = walker.mark_context(plain, text[m.end() :])
        plain += m.group("disp") or m.group(0)
        ctx.dropped_cite = False
        _render_reference(m, ctx, paragraph)
        dropped, ctx.dropped_cite = ctx.dropped_cite, False
        last = m.end()
    tail = text[last:]
    if dropped and tail[:1] in _CLOSING_PUNCT:
        _rstrip_last_run(paragraph)
    _render_gap(tail, ctx, paragraph)
    _flush_cites(ctx, paragraph)
    _close_up_repeat(ctx, paragraph, "")


#: ``$_{zz}$`` / ``$^2$`` — a math span that is only a sub/superscript, the
#: chemistry-style ``NICS(1)$_{zz}$`` / ``Zr$_6$`` authoring. As OMML it has
#: an empty base (and the shared empty-base repair would pull the preceding
#: ``1)`` into the math, splitting the parenthesis); Word does better with a
#: plain sub/superscript run, which is also what the author meant.
_SCRIPT_ONLY_MATH = re.compile(r"\$\s*(?P<op>[_^])\s*(?P<arg>[^$]+?)\s*\$", re.DOTALL)
_SCRIPT_WRAPPER = re.compile(r"\\(?:mathrm|textrm|text|mathit)\{([^{}]*)\}")
_SCRIPT_PLAIN = re.compile(r"[^\s{}$\\_^]+")
_SCRIPT_ONLY_MATH_SPAN = _MATH


def _script_only_to_html(m: re.Match[str]) -> str:
    """``$_{zz}$`` → ``<sub>zz</sub>`` (``^`` → ``<sup>``) when the span is
    nothing but one plain-text script; any other math span is left alone."""
    sm = _SCRIPT_ONLY_MATH.fullmatch(m.group(0))
    if sm is None:
        return m.group(0)
    arg = sm.group("arg").strip()
    braced = arg.startswith("{") and arg.endswith("}")
    if braced:
        arg = arg[1:-1].strip()
    wm = _SCRIPT_WRAPPER.fullmatch(arg)
    if wm is not None:
        arg, braced = wm.group(1), True
    if not arg or not _SCRIPT_PLAIN.fullmatch(arg) or (len(arg) > 1 and not braced):
        return m.group(0)
    tag = "sub" if sm.group("op") == "_" else "sup"
    return f"<{tag}>{arg}</{tag}>"


#: A leading ASCII hyphen that is a minus sign in a sub/superscript: directly
#: before a digit (``-1``), or the whole script is a lone sign (``-``, ``+-``).
#: A hyphen inside a word-like script (``in-plane``) is a hyphen.
_SCRIPT_MINUS = re.compile(r"\A(\+?)-(?=\d|\Z)")
#: A superscript that ends in a hyphen right after a digit is a charge
#: (``SO$_4^{2-}$``): the hyphen is a minus too (gr725241).
_CHARGE_MINUS = re.compile(r"(?<=\d)-\Z")


def _script_minus(text: str, *, sup: bool = False) -> str:
    """The text of a ``<sub>``/``<sup>`` run with its minus sign typeset as
    U+2212, as the OMML path does for ``$10^{-53}$`` (gr725240). A superscript
    (``sup``) also takes the trailing charge sign (``2-``)."""
    text = _SCRIPT_MINUS.sub("\\1\u2212", text)
    return _CHARGE_MINUS.sub("\u2212", text) if sup else text


_CLOSING_PUNCT = walker.CLOSING_PUNCT


def _rstrip_last_run(paragraph: Any) -> None:
    """Trim the paragraph's last run before a dropped citation mark (its
    leading space, a stranded ``and``)."""
    runs = paragraph.runs
    if runs and not _is_field_run(runs[-1]):
        runs[-1].text = walker.trim_before_dropped(runs[-1].text)


def _is_field_run(run: Any) -> bool:
    """Is ``run`` a field's begin / separate / end mark? Setting its text
    would delete the mark."""
    from docx.oxml.ns import qn

    return run._r.find(qn("w:fldChar")) is not None


# Tokeniser for a non-reference gap: math / code / bold / italic / sub /
# sup become typed spans; everything else is plain text. Ordered so the
# verbatim spans (math, code) are carved out before emphasis. The math
# alternation is the LaTeX exporter's `_MATH` verbatim (an author-escaped
# ``\$`` is a literal dollar, never a delimiter) so the two exporters
# carve out the same spans and the write-path lint speaks for both. The
# italic arm is the LaTeX exporter's ``_MD_ITALIC`` verbatim too — an
# author-escaped ``\*`` and the adsorbate star (``*NO``, ``H*`` —
# ``utils/adsorbate_star``, gr674905) are literal in all three renderers.
_SPAN = re.compile(
    rf"(?P<math>{_MATH.pattern})"
    r"|(?P<code>`[^`]+`)"
    r"|(?P<sub><sub>.+?</sub>)"
    r"|(?P<sup><sup>.+?</sup>)"
    r"|(?P<bold>\*\*.+?\*\*)"
    rf"|(?P<italic>(?<![\*\w\\])\*(?!\s){adsorbate_star.STAR_NOT_FOLLOWED_BY_ELEMENT}"
    rf"[^*]+?(?<![\s\\]){adsorbate_star.STAR_NOT_PRECEDED_BY_ELEMENT}\*(?!\w))",
    re.DOTALL,
)


#: A math span that is exactly ``$\mu$`` / ``$\Omega$`` (gr725241).
_UNIT_SYMBOL_SPAN = re.compile(r"\$\s*\\(?P<name>mu|Omega)\s*\$")
#: What may directly precede a unit ``$\Omega$``: a digit, a digit plus one
#: space, or an SI prefix letter (``k``, ``M``, ``G``, ``m``, ``μ``) that is
#: itself at the start, or after a digit, whitespace or an opening bracket
#: (``system$\Omega$`` is not a unit).
_OHM_PREFIX = re.compile(
    r"(?:\d|\d[ \u00a0\u2009\u202f]"
    r"|(?:\A|(?<=[\d\s(\[{]))[kMGm\u03bc])\Z"
)


def _unit_symbol(span: str, before: str, after: str) -> str | None:
    """The literal character for a lone ``$\\mu$`` / ``$\\Omega$`` that is a
    unit symbol — ``V/$\\mu$m``, ``10 $\\mu$A`` (μ directly followed by a
    letter), ``150 $\\Omega$``, ``k$\\Omega$`` (Ω after a number or an SI
    prefix) — else ``None``. OMML would set these in math italic; the plain
    character prints upright in the body font. ``$\\mu_B$`` and a lone
    ``$\\mu$`` before a space stay math."""
    sm = _UNIT_SYMBOL_SPAN.fullmatch(span)
    if sm is None:
        return None
    if sm.group("name") == "mu":
        return "\u03bc" if after[:1].isalpha() else None
    return "\u03a9" if _OHM_PREFIX.search(before) else None


def _close_up_repeat(ctx: _Ctx, paragraph: Any, after: str) -> str:
    """Close the prose up around a repeated mark that was just not printed
    (:meth:`DocxRenderer.cite_group`): trims the paragraph's last run and
    returns the text that follows (``after``, "" at the block's end) without
    a doubled space. Nothing to do unless the dropped mark is the last thing
    the paragraph holds."""
    pending, ctx._dropped_mark = ctx._dropped_mark, None
    if pending is None or pending[0] is not paragraph:
        return after
    runs = paragraph.runs
    # Still the paragraph's last element: no run, math or link since.
    if len(runs) != pending[1] or not runs or paragraph._p[-1] is not runs[-1]._r:
        return after
    if _is_field_run(runs[-1]):  # a cross-reference number ends the prose
        return after
    while True:
        before, after = walker.close_up_dropped(runs[-1].text, after)
        runs[-1].text = before
        # A stranded "and" can leave a run empty: drop it and trim the one
        # before it, which now ends the prose.
        if before or len(runs) < 2 or runs[-1]._r.getprevious() is not runs[-2]._r:
            return after
        paragraph._p.remove(runs[-1]._r)
        runs = paragraph.runs


def _render_gap(text: str, ctx: _Ctx, paragraph: Any) -> None:
    if not text:
        return
    # Any gap content ends a run of adjacent citations (mirrors LaTeX's
    # ``walker.merge_adjacent``, which only merges separatorless marks).
    # Real prose between two citations breaks the consecutive-cite run;
    # whitespace-only (``[§a~3] [§a~9]``) does not, so those still collapse.
    _emit_cites(ctx, paragraph, ctx.cite_run.gap(text))
    text = _close_up_repeat(ctx, paragraph, text)
    if not text:
        return
    last = 0
    for m in _SPAN.finditer(text):
        if m.start() > last:
            _emit_text(text[last : m.start()], ctx, paragraph)
        if m.group("math") is not None:
            span = m.group("math")
            if _math_braces_balanced(span) and _math_plausible(span):
                unit = _unit_symbol(span, text[: m.start()], text[m.end() :])
                if unit is not None:
                    paragraph.add_run(unit)  # upright body-font run, not math
                else:
                    _render_math(span, paragraph)
            else:
                # Same demotion as the LaTeX exporter: a garbled or
                # money-dollar-mispaired span is prose, not math — emit
                # it literally instead of feeding broken OMML.
                _emit_text(span, ctx, paragraph)
        elif m.group("code") is not None:
            r = paragraph.add_run(m.group("code")[1:-1])
            r.font.name = "Consolas"
        elif m.group("sub") is not None:
            r = paragraph.add_run(_script_minus(m.group("sub")[5:-6]))
            r.font.subscript = True
        elif m.group("sup") is not None:
            r = paragraph.add_run(_script_minus(m.group("sup")[5:-6], sup=True))
            r.font.superscript = True
        elif m.group("bold") is not None:
            # Definitions inside bold resolve too — the PDF defines inside
            # the bold as well (L3); style the emitted runs after.
            n_runs = len(paragraph.runs)
            _emit_text(m.group("bold")[2:-2], ctx, paragraph)
            for r in paragraph.runs[n_runs:]:
                r.bold = True
        elif m.group("italic") is not None:
            n_runs = len(paragraph.runs)
            _emit_text(m.group("italic")[1:-1], ctx, paragraph)
            for r in paragraph.runs[n_runs:]:
                r.italic = True
        last = m.end()
    if last < len(text):
        _emit_text(text[last:], ctx, paragraph)


#: A bare (unbracketed) internal handle token in prose — two-letter kind code
#: + a 4+-digit id. Not inside a URL/DOI/path (no ``/``, ``.``, ``:``,
#: ``~``, ``-`` right before it) and 4+ digits so chemistry like ``co2`` / ``pt111``
#: never matches; :func:`_bare_handle_text` still checks the registry.
_BARE_HANDLE = re.compile(r"(?<![\w/.:~-])[a-z]{2}\d{4,}(?![\w~]|[-.]\d)")


def _bare_handle_text(token: str, ctx: _Ctx) -> str:
    """A bare handle token → reader text: the record's title when the store
    resolves it, else its kind name (``draft``, ``todo``) with a warning.
    A token the handle registry doesn't parse is prose and stays as is."""
    parsed = handle_registry.parse(token)
    if parsed is None:
        return token
    if token in ctx._bare_handle_cache:
        return ctx._bare_handle_cache[token]
    kind, is_chunk, pk = parsed
    title = ""
    fetch = getattr(ctx.store, "fetch_refs_by_ids", None)
    if not is_chunk and callable(fetch):
        try:
            ref = fetch([pk]).get(pk)
        except Exception:  # pragma: no cover — store hiccup → kind name
            ref = None
        title = _short_title(str(getattr(ref, "title", None) or "").strip())
    if not title:
        title = kind
        ctx.warnings.append(f"bare handle {token!r} in prose rendered as {kind!r}")
    ctx._bare_handle_cache[token] = title
    return title


def _emit_text(text: str, ctx: _Ctx, paragraph: Any) -> None:
    """Plain prose → runs, with **render-time first-use expansion** of
    known abbreviations: the first occurrence (in reading order) of a
    defined short becomes ``Long Form (SHORT)``; later ones stay ``SHORT``.
    The matching is :func:`walker.term_segments`, shared with the LaTeX
    ``\\gls`` path so the two formats define the same occurrence; it
    survives chunk reordering because it's computed here at export.

    An author-escaped ``\\$`` (the lint's advice for literal money
    dollars) renders as the bare ``$``; ``\\*`` (the lint's advice for a
    literal star) renders as ``*`` — Word has no escape syntax.

    A bare internal handle in prose (``dr173020's``, ``td459589/…``)
    renders as its record's title (gr493181), see :func:`_bare_handle_text`."""
    text = text.replace("\\$", "$").replace("\\*", "*")
    text = _BARE_HANDLE.sub(lambda m: _bare_handle_text(m.group(0), ctx), text)
    if not text:
        return
    segments = walker.term_segments(
        text,
        ctx.abbrevs,
        ctx.seen_acr,
        ctx.used_acr,
        preceding=paragraph.text,
        heading=ctx.in_heading,
    )
    renderer = DocxRenderer(ctx)
    for seg in segments:
        if isinstance(seg, str):
            paragraph.add_run(seg)
        else:
            renderer.term(paragraph, seg)


def _render_math(span: str, paragraph: Any) -> None:
    """Render ``$…$`` / ``$$…$$`` as native Word math (OMML), so equations
    typeset rather than appearing as literal source. Falls back to italic
    text when the LaTeX can't be converted (missing dep / exotic macro)."""
    inner = span.strip("$").strip()
    from precis.export.omml import latex_to_omml

    omath = latex_to_omml(inner)
    if omath is None:
        paragraph.add_run(inner).italic = True
        return
    # Append the <m:oMath> element directly into the paragraph's XML
    # (inline math sits among the runs).
    paragraph._p.append(omath)


def _render_equation(
    doc: Any,
    body: str,
    number: int | None,
    starred: bool,
    *,
    ctx: _Ctx | None = None,
    dc: str | None = None,
) -> None:
    """A standalone ``$$…$$`` paragraph → native OMML math on its own
    line, numbered ``(N)`` right-aligned (the conventional manuscript
    layout: a CENTER tab for the equation, a RIGHT tab at the margin for
    the number) — mirrors the LaTeX exporter's numbered ``equation``
    environment. The number is a ``SEQ Equation`` field between typed
    parentheses, its cached result the value computed once at export time
    from document order (:func:`_standalone_equation_numbers`) and
    bookmarked for ``REF`` fields. Unnumbered (``starred`` or ``number is
    None``, the escape hatch) renders the equation alone, no tab/number."""
    from docx.enum.text import WD_TAB_ALIGNMENT
    from docx.shared import Inches

    lines = _aligned_lines(body) or [body]
    for i, line in enumerate(lines):
        p = doc.add_paragraph()
        if starred or number is None:
            _render_math(f"$${line}$$", p)
            continue
        p.paragraph_format.tab_stops.add_tab_stop(
            Inches(_EQ_CENTER_TAB_IN), WD_TAB_ALIGNMENT.CENTER
        )
        p.paragraph_format.tab_stops.add_tab_stop(
            Inches(_EQ_NUMBER_TAB_IN), WD_TAB_ALIGNMENT.RIGHT
        )
        p.add_run("\t")
        _render_math(f"$${line}$$", p)
        if i == len(lines) - 1:  # one label for the whole block, on its last line
            p.add_run("\t(")
            _bookmarked(
                p,
                ctx,
                dc,
                lambda p=p: _add_field(p, "SEQ Equation \\* ARABIC", str(number)),
            )
            p.add_run(")")


#: A display body that is exactly one multi-line ``aligned``-family block.
_ALIGNED = re.compile(
    r"\A\s*\\begin\{(?P<env>aligned|align\*?|gathered|split)\}"
    r"(?P<inner>.*)\\end\{(?P=env)\}\s*\Z",
    re.DOTALL,
)
_ROW_BREAK = re.compile(r"\\\\(?:\[[^\]]*\])?")


def _aligned_lines(body: str) -> list[str]:
    """The rows of an ``\\begin{aligned}…\\end{aligned}`` display body, with
    the ``&`` alignment points dropped — Word's OMML converter has no
    ``aligned`` environment, so each row becomes its own equation line.
    ``[]`` when ``body`` is not such a block."""
    m = _ALIGNED.match(body)
    if m is None:
        return []
    rows = (r.replace("&", " ").strip() for r in _ROW_BREAK.split(m.group("inner")))
    return [r for r in rows if r]


def _render_reference(m: re.Match[str], ctx: _Ctx, paragraph: Any) -> None:
    """One matched inline reference. Citations → a numbered ``[n]``
    superscript marker (and register the slug). Cross-refs render their
    surface text; authoring links / bare thought mentions render nothing
    (provenance only) — mirrors the LaTeX exporter."""
    if m.group("auth") is not None:
        return  # [[…]] authoring link — provenance only
    if m.group("disp") is not None:
        _render_target(m.group("tgt"), m.group("disp"), ctx, paragraph)
        return
    if m.group("bare") is not None:
        tgt = m.group("bare") + (m.group("sel") or "")
        _render_target(tgt, None, ctx, paragraph, pin=m.group("pin"))
        return
    if m.group("ref") is not None:
        kind = m.group("kind")
        if kind == "paper":
            _cite(m.group("id"), ctx, paragraph)
        elif kind not in LINKIFY_KINDS and not handle_registry.is_known_kind(kind):
            # ``doi:10.1002/…`` etc. is prose, not a precis kind — keep it
            # (gr493193); a real kind (think:/plan:/…) stays dropped.
            ctx.cite_run.interrupt()
            _flush_cites(ctx, paragraph)
            paragraph.add_run(m.group(0))
        return  # bare memory:/think:/… — not citeable
    if m.group("bare_conv") is not None:
        return
    if m.group("bare_paper") is not None:
        _cite(m.group("bare_paper").split("~", 1)[0], ctx, paragraph)
        return
    if m.group("bcite") is not None:
        # A bare citable handle (``pa57151`` unbracketed in prose, gr606572):
        # cite it like the ``[pa<id>]`` form. An unresolved or non-live token
        # keeps today's prose fallback (the record-title splice).
        tgt = m.group("bcite")
        parsed = handle_registry.parse(tgt)
        if parsed is not None and (parsed[0] == "finding" or _handle_cite_si(tgt, ctx)):
            _render_target(tgt, None, ctx, paragraph)
        else:
            ctx.cite_run.interrupt()
            _flush_cites(ctx, paragraph)
            _emit_text(tgt, ctx, paragraph)
        return


def _handle_cite_key(tgt: str, ctx: _Ctx) -> tuple[str, int | None] | None:
    """A paper/patent handle (``pc<chunk_id>`` / ``pa<ref_id>``) → its
    ``(cite_key, chunk_id)``, via the one store resolver. ``chunk_id`` is set
    only for a chunk handle (``pc<id>``) — the specific cited passage, which
    the EndNote path embeds as a traveling note. ``None`` if it doesn't
    resolve to a live paper. Mirrors :func:`precis.export.latex._handle_cite_key`
    so the docx and PDF paths cite the identical resolved key."""
    hit = _handle_cite_si(tgt, ctx)
    return None if hit is None else (hit[0], hit[1])


def _handle_cite_si(tgt: str, ctx: _Ctx) -> tuple[str, int | None, bool] | None:
    """:func:`_handle_cite_key` plus whether the handle sits in a
    supplementary-information record (the key is then its parent's)."""
    return walker.resolve_cite_handle(ctx.store, tgt)


def _finding_cite_keys_pinned(tgt: str, pin: str | None, ctx: _Ctx) -> list[str]:
    """A finding handle (``fi<id>``) → its bibliographic cite_key(s), via
    the ONE shared resolver (:func:`precis.taproot.cite.finding_cite_keys`)
    ``precis resolve`` and :func:`precis.export.latex._render_finding_cite`
    also use. A plain finding resolves to its primary cite_key once the
    chase establishes it, else its ``pub_id`` stub. A Taproot claim hub
    resolves instead to its currently derived ``establishes`` originator(s)
    (falling back to corroborators) — a living citation. Empty when the
    target doesn't resolve to a live finding, or a hub has no resolvable
    evidence yet (in-flight).

    ``pin`` (Taproot slice A2, Phase 2) overrides/extends a hub's derived
    cite_keys through the ONE shared :func:`precis.taproot.cite.apply_pin`
    policy — mirrors :func:`precis.export.latex._render_finding_cite`'s
    pin handling so the docx and PDF paths agree. A pin on a non-hub
    finding is meaningless and is dropped with a warning."""
    if ctx.store is None:
        return []
    parsed = handle_registry.parse(tgt)
    if parsed is None:
        return []
    _kind, _is_chunk, pk = parsed
    from precis.taproot.cite import apply_pin, finding_cite_keys

    fc = finding_cite_keys(ctx.store, pk)
    keys = fc.cite_keys
    if not keys:
        # A composite (conjunction) hub carries no source of its own: it
        # cites the union of its conjunct atoms' sources — as LaTeX does.
        keys = _conjunct_cite_keys(ctx.store, pk)
    op, handles = parse_pin_suffix(pin)
    if op is not None:
        if fc.is_hub and fc.evidence is not None:
            result = apply_pin(
                ctx.store,
                label=tgt,
                op=op,
                handles=handles,
                derived_cite_keys=keys,
                evidence=fc.evidence,
            )
            keys = result.cite_keys
            ctx.warnings.extend(detail for _status, detail in result.warnings)
            if result.diverged and result.divergence:
                ctx.warnings.append(result.divergence)
        else:
            ctx.warnings.append(
                f"pin on {tgt} ignored — pins only apply to a Taproot claim hub cite"
            )
    if not keys and op is None:
        ctx.warnings.append(
            f"cite [{tgt}]: no citable source "
            "(conjunction hub without sourced conjuncts)"
        )
        ctx.dropped_cite = True
    return keys


#: colours for the two trust-mark runs — muted grey for "pending", loud red
#: for "contradicted" (mirrors ``export/latex.py``'s bold vs. plain marker).
_UNVERIFIED_INK = "666666"
_UNSUPPORTED_INK = "B00020"


def _render_trust_mark(ctx: _Ctx, finding_ref_id: int, paragraph: Any) -> None:
    """The inline trust mark appended after a finding-backed cite
    (the trust-surfaces export marking) — mirrors
    :func:`precis.export.latex._trust_mark_latex`. No-op for a clean
    citation (AC 6: identical content to today). Unverified gets a muted
    italic run; unsupported the louder bold red run.

    "Export always works, always marks" (the decided trust-surfaces
    policy — no refusing/strict mode): any resolution failure (malformed
    ``meta``, a store hiccup) degrades to no mark rather than aborting an
    export that previously worked."""
    if ctx.trust is None:
        return
    try:
        state = ctx.trust.resolve(finding_ref_id)
    except Exception:  # pragma: no cover — store hiccup / malformed meta
        return
    if state.label == "clean":
        return
    from docx.shared import RGBColor

    # The mark lands after the cite group; that group is never dropped as a
    # repeat, and the next mark is not compared with it.
    ctx._force_keep = True
    try:
        _flush_cites(ctx, paragraph)
    finally:
        ctx._force_keep = False
    ctx._marks = walker.RepeatMarks(enabled=not ctx.repeat_cites)
    ctx._kept_field = None
    if state.label == "unsupported":
        run = paragraph.add_run(f" {UNSUPPORTED_MARK_TEXT}")
        run.bold = True
        run.font.color.rgb = RGBColor.from_string(_UNSUPPORTED_INK)
        return
    # abstract-only / author-vouched / unverified — all calm italic marks;
    # the bracket lead word (mark_text) carries the distinction.
    run = paragraph.add_run(f" {mark_text(state)}")
    run.italic = True
    run.font.color.rgb = RGBColor.from_string(_UNVERIFIED_INK)


def _render_target(
    tgt: str, surface: str | None, ctx: _Ctx, paragraph: Any, *, pin: str | None = None
) -> None:
    if ctx.audience is not None:
        record = ctx.audience.reference(tgt, surface)
        if record is not None:
            ctx.cite_run.interrupt()
            _flush_cites(ctx, paragraph)
            if record.url:
                _add_hyperlink(paragraph, record.url, record.text)
            else:
                from precis.export._footnotes import add_footnote

                paragraph.add_run(record.text)
                add_footnote(paragraph, record.footnote)
            if not record.citation:
                return
    if tgt.startswith("§"):  # a citation — keep the consecutive-cite run
        cm = DRAFT_CITE_PATTERN.fullmatch(tgt)
        if cm is not None:
            _cite(cm.group("slug"), ctx, paragraph)
        return
    # universal handle: ``[pc10]`` / ``[pa5]`` (a paper) and
    # ``[pt7]`` (a patent) are citations; ``[fi3]`` a finding cite; ``[dc41]``
    # an intra-draft cross-ref; a computational-evidence handle
    # (:data:`precis.utils.mentions.COMPUTED_EVIDENCE_KINDS`, e.g. ``[st12]``)
    # renders as plain text (real grounding, not a bibliography entry); a
    # record handle for a thought (``[me5]``) is provenance-only. The LaTeX
    # exporter resolves these — the docx path used to drop them all, so
    # handle-cited drafts (e.g. everything imported from LaTeX) rendered
    # with no citations and no References section at all.
    parsed = handle_registry.parse(tgt)
    if parsed is not None and parsed[1]:
        # A chunk handle's ``~<table-sel>`` is row/cell detail the dispatch
        # paths don't read — dispatch on the chunk itself (mirrors
        # ``export/latex.py``'s ``_render_target``).
        base, sel = handle_registry.split_selector(tgt)
        if sel is not None:
            tgt = base
    elif parsed is None and "~" in tgt:
        # ``<record>~<sel>`` isn't a handle; when the base resolves the
        # selector is display/navigation only (the audience link above
        # already carried it) and the base dispatches as if written bare.
        base, sel = handle_registry.split_selector(tgt)
        if sel is not None:
            base_parsed = handle_registry.parse(base)
            if base_parsed is not None:
                parsed, tgt = base_parsed, base
    if parsed is not None:
        kind, is_chunk, pk = parsed
        if kind in ("paper", "patent"):
            if ctx.patent_mode:
                _inline_source_cite(tgt, kind, surface, ctx, paragraph)
                return
            hit = _handle_cite_si(tgt, ctx)
            if hit:
                slug, chunk_id, via_si = hit
                _cite(slug, ctx, paragraph, chunk_id=chunk_id, si=via_si)
            return
        if kind == "finding":
            slugs = _finding_cite_keys_pinned(tgt, pin, ctx)
            for slug in slugs:
                _cite(slug, ctx, paragraph)
            _record_claim_cite(ctx, pk, slugs)
            n_runs = len(paragraph.runs)
            _render_trust_mark(ctx, pk, paragraph)
            if len(paragraph.runs) > n_runs:
                ctx.dropped_cite = False  # a trust mark is still printed
            return
        if kind in COMPUTED_EVIDENCE_KINDS:
            # Computational evidence (a simulation structure, a calc/math
            # worksheet, a computed reaction pathway) — real grounding, just
            # not a bibliography entry, so write the authored display text
            # (or the literal handle) as plain text instead of dropping the
            # citation (mirrors export/latex.py's ``_render_target``).
            ctx.cite_run.interrupt()
            _flush_cites(ctx, paragraph)
            paragraph.add_run(surface or tgt)
            return
        # draft cross-ref / other record handle → not a citation.
        ctx.cite_run.interrupt()
        if not is_provenance_only(kind, is_chunk):
            _render_xref(tgt, surface, ctx, paragraph)
        elif surface:
            # ``[text](<record>)``: the record is provenance but the authored
            # display text is body prose — print it (the web links it).
            _render_gap(surface, ctx, paragraph)
        return
    # Any non-citation content breaks a run of consecutive citations.
    ctx.cite_run.interrupt()
    if tgt.startswith("¶"):
        handle = tgt[1:]
        dc = walker.resolve_legacy(
            handle,
            ctx.legacy_to_dc,
            accept_dc=True,
            known=ctx.known_handles,
            warnings=ctx.warnings,
        )
        if dc is not None:
            _render_xref(dc, surface, ctx, paragraph)
            return
        _flush_cites(ctx, paragraph)
        paragraph.add_run(surface or f"¶{handle}")
        return
    if tgt.startswith(("http://", "https://")):
        _flush_cites(ctx, paragraph)
        paragraph.add_run(surface or tgt)
        return
    if surface:
        # ``[text](<anything unresolvable>)`` — the target is dead but the
        # authored display text is body prose; print it rather than
        # dropping the words.
        _flush_cites(ctx, paragraph)
        _render_gap(surface, ctx, paragraph)


def _record_claim_cite(ctx: _Ctx, pk: int, slugs: list[str]) -> None:
    """Remember which bibliography slugs a finding cite site printed, for
    the published-claims section. Only slugs :func:`_cite`
    already registered in ``ctx.cited`` are kept: recording must never
    mint a citation (patent mode registers none, so it records none)."""
    walker.record_claim_keys(
        ctx.claim_cites.setdefault(pk, []),
        slugs,
        ctx.cited,
        lambda slug: _resolve_si_slug(slug.split("~", 1)[0], ctx)[0],
    )


def _section_heading(ctx: _Ctx, number: str) -> str | None:
    """The dc of the numbered heading that displays section ``number``
    (the bookmark every ``Section N`` reference points at)."""
    for dc in ctx.numbered_headings:
        if ctx.xref.get(dc, ("", ""))[1] == number:
            return dc
    return None


def _at_sentence_start(paragraph: Any) -> bool:
    """Is the next run the first word of a sentence (paragraph start, or
    right after ``. ``/``? ``/``! `` but not ``e.g. ``)? Drives
    first-letter capitalisation."""
    return _abbrev.at_sentence_start(paragraph.text)


def _plain_run_text(element: Any) -> str | None:
    """The text of a plain run (no formatting, nothing but text); ``None``
    for anything else (a field mark, a math span, a hyperlink, a styled
    run)."""
    from docx.oxml.ns import qn

    if element.tag != qn("w:r"):
        return None
    text = ""
    for child in element:
        if child.tag != qn("w:t"):
            return None
        text += child.text or ""
    return text


def _reopen_run(prior: _XrefRun, paragraph: Any, xref: walker.Xref) -> bool:
    """Does ``xref`` extend the cross-reference run ``prior`` printed into
    ``paragraph``, with only a connector since (whitespace, ``,``, ``and``,
    ``, and``)? Then take the run's elements and the connector out of the
    paragraph and say so: the
    caller prints the longer run in their place. The join is
    :func:`walker.merge_xref_runs`, the rule LaTeX merges ``\\cref`` marks by."""
    if prior.paragraph is not paragraph:
        return False
    p = paragraph._p
    if any(e.getparent() is not p for e in prior.elements):
        return False  # something took the run's elements out since
    tail = list(prior.elements[-1].itersiblings())
    gaps = [_plain_run_text(e) for e in tail]
    if any(g is None for g in gaps):
        return False
    items = walker.merge_xref_runs(
        [tuple(m.dc for m in prior.members), "".join(g or "" for g in gaps), (xref.dc,)]
    )
    if len(items) != 1:
        return False
    for element in [*prior.elements, *tail]:
        p.remove(element)
    return True


def _render_xref(dc: str, surface: str | None, ctx: _Ctx, paragraph: Any) -> None:
    """An intra-draft cross-ref to chunk ``dc``. An authored surface wins;
    otherwise print what LaTeX's ``\\cref`` would — ``Section 1.5.5``,
    ``Figure 2``, ``Eq. (3)`` (``Equation`` at a sentence start), the
    number a live Word ``REF`` field. A cross-ref to a chunk that isn't
    live in this draft is downgraded to its surface/handle with a
    warning, like LaTeX
    (:func:`walker.resolve_xref` decides; :meth:`DocxRenderer.xref` prints)."""
    xref = walker.resolve_xref(
        dc,
        surface,
        live=ctx.dc_handles or None,
        numbers=walker.XrefNumbers(
            ctx.xref,
            ctx.numbered_headings,
            ctx.eq_numbers,
            ctx.xref_order,
            ctx.xref_section,
            ctx.xref_types,
        ),
        warnings=ctx.warnings,
        dead=lambda d: walker.dead_xref(
            ctx.store, d, ctx.dc_handles, ctx._dead_xref_cache
        ),
        citing=ctx.cursor.dc,
    )
    DocxRenderer(ctx).xref(paragraph, xref)


def _inline_source_cite(
    tgt: str, kind: str, surface: str | None, ctx: _Ctx, paragraph: Any
) -> None:
    """Patent-spec mode: render a prior-art reference **in-text** (no ``[n]``
    marker / no References section). A display-link's authored text wins
    (WYSIWYG); otherwise a patent formats to its citation string and a paper
    to a light ``(Author, Year)``. See `me690333`."""
    ctx.cite_run.interrupt()
    _flush_cites(ctx, paragraph)
    if surface:
        paragraph.add_run(surface)
        return
    hit = _handle_cite_key(tgt, ctx)
    if not hit:
        return
    slug = hit[0]
    ref = ctx.store.get_ref(kind=kind, id=slug) if ctx.store is not None else None
    if ref is None:
        paragraph.add_run(slug)
        return
    text = (
        render_patent_citation(getattr(ref, "meta", None), slug)
        if kind == "patent"
        else render_paper_inline_citation(ref)
    )
    paragraph.add_run(text)


#: Font size (pt) for the doi/UL cite-link runs — small enough to read as
#: an annotation next to the ``[n]`` marker, not body text.
_CITE_LINK_PT = 7.0


def _paper_identifier(ref_id: int, ctx: _Ctx) -> str:
    """The one identifier a cite-link pair hangs off: a DOI, else an
    ``arxiv:``-prefixed arXiv id, else ``""`` (:func:`paper_identifier`,
    shared with ``export/latex.py`` — same ``identifiers_for_refs`` alias
    source as :func:`_resolve_source`'s ``url`` field, so the inline link
    can't disagree with the References entry). Memoized on ``ctx``."""
    if ref_id in ctx._identifier_cache:
        return ctx._identifier_cache[ref_id]
    fn = getattr(ctx.store, "identifiers_for_refs", None)
    identifier = paper_identifier(ref_id, fn)
    ctx._identifier_cache[ref_id] = identifier
    return identifier


def _cite_link_group(slug: str, ctx: _Ctx, paragraph: Any) -> None:
    """Append the small ``doi`` / library-search hyperlink run(s) right
    after a plain ``[n]`` citation marker — mirrors
    ``export/latex.py::_cite_urls``. ``ctx.doi_links`` and
    ``ctx.library_links`` are independent (either can run alone). No-op
    when both are off, there's no store, the slug doesn't resolve, or it
    carries neither a DOI nor an arXiv id (an arXiv-only paper reuses the
    ``doi``-labelled run for its arXiv abstract page, dropping the library
    run only if :func:`uol_url` can't build one)."""
    if not ctx.doi_links and not ctx.library_links:
        return
    if ctx.store is None:
        return
    get_ref = getattr(ctx.store, "get_ref", None)
    if not callable(get_ref):
        return
    pref = None
    for kind, _etype in _CITE_ENTRY_TYPES:
        pref = get_ref(kind=kind, id=slug)
        if pref is not None:
            break
    if pref is None:
        return
    identifier = _paper_identifier(pref.id, ctx)
    if ctx.doi_links:
        first = doi_url(identifier)
        if first:
            paragraph.add_run(" ")
            _add_hyperlink(paragraph, first, "doi", size_pt=_CITE_LINK_PT)
    if ctx.library_links:
        ul = uol_url(identifier, search_url_template=ctx.library_search_url)
        if ul:
            paragraph.add_run(" ")
            _add_hyperlink(paragraph, ul, ctx.library_label, size_pt=_CITE_LINK_PT)


def _resolve_si_slug(slug: str, ctx: _Ctx) -> tuple[str, bool]:
    """A bare cite slug -> ``(slug_to_cite, is_si)``; mirrors
    :func:`precis.export.latex._resolve_si_slug` (an SI record cites as its
    parent; with no live parent it keeps its own slug and warns), via the
    shared :func:`walker.resolve_si_slug`; a record whose DOI an already
    cited record has takes that record's slug (:class:`walker.DoiAliases`)."""
    key, is_si = walker.resolve_si_slug(
        slug,
        ctx.store,
        si_cache=ctx._si_slug_cache,
        target_cache=ctx._cite_target_cache,
        warnings=ctx.warnings,
    )
    return ctx.doi_aliases.canonical(key), is_si


def _cite(
    slug: str,
    ctx: _Ctx,
    paragraph: Any,
    chunk_id: int | None = None,
    si: bool = False,
) -> None:
    """Buffer a numbered citation marker — a superscript ``[n]`` keyed on
    the **paper** — into the current adjacent-citation run
    (:data:`_Ctx.cite_run`); :func:`_flush_cites` emits the run as ONE mark
    (``[1, 2]`` / ``[1–3]``) or ONE EndNote field. The numbered
    **References** section at the document end (:func:`_append_references`)
    carries the resolved entry, so entry ``n`` backs every in-text ``[n]``.

    Citations key on the paper, not the chunk: ``a~3``, ``a~9``, ``a~23``
    all reference paper ``a`` → mark ``[n]`` with the same ``n`` each time.
    A paper cited many times prints ``[n]`` at each site — a plain run, so
    it *repeats* freely (a real Word endnote field can't: endnotes are
    1:1 with their reference, and reusing one corrupts the document). And
    **consecutive** marks for the same paper collapse to a single mark.

    ``chunk_id`` (set for a ``pc<id>`` chunk citation) is the specific cited
    passage — carried into the EndNote field's traveling note."""
    slug = slug.split("~", 1)[0]  # the paper, not the cited chunk
    if ctx.patent_mode:
        # A patent specification has no References section — cite in-text.
        ctx.cite_run.interrupt()
        _flush_cites(ctx, paragraph)
        ref = (
            ctx.store.get_ref(kind="paper", id=slug) if ctx.store is not None else None
        )
        paragraph.add_run(
            render_paper_inline_citation(ref) if ref is not None else slug
        )
        return
    # A supplementary-information record cites as its parent paper; the
    # marker gains " (SI)" (an EndNote field carries it as <Suffix>).
    slug, resolved_si = _resolve_si_slug(slug, ctx)
    n = ctx.cite_number(slug)  # registers the paper (idempotent)
    # Consecutive cites to the same paper give one mark for the run. An
    # "(SI)" cite joins the run and keeps its postnote on its own number
    # (mirrors LaTeX's ``\\cites{a}[SI]{b}``); the same paper cited both plain
    # and SI back to back stays two marks (:class:`walker.CiteRun`).
    _emit_cites(
        ctx,
        paragraph,
        *ctx.cite_run.add(
            slug,
            n,
            si=si or resolved_si,
            chunk_id=chunk_id,
            context=ctx._ref_context,
        ),
    )


def _flush_cites(ctx: _Ctx, paragraph: Any) -> None:
    """Emit the buffered adjacent-citation run, then clear it — one merged
    superscript mark + one link group in plain mode, or ONE ``ADDIN
    EN.CITE`` field carrying several ``<Cite>`` elements in EndNote mode
    (both mirror LaTeX's ``walker.merge_adjacent``: directly adjacent cites
    collapse, a doubled slug dedupes, and a merged run carries the links of
    every paper in it). No-op on an empty run."""
    _emit_cites(ctx, paragraph, ctx.cite_run.flush())


def _emit_cites(ctx: _Ctx, paragraph: Any, *groups: walker.CiteGroup | None) -> None:
    renderer = DocxRenderer(ctx)
    for group in groups:
        if group is not None:
            renderer.cite_group(paragraph, group)


class DocxRenderer:
    """The docx :class:`walker.Renderer`: formats resolved values as
    python-docx runs (or EndNote fields) into a paragraph sink; the block
    methods (title … paragraph) take the Document as their sink."""

    #: docx has no code block for a ``listing`` and no aside box: both
    #: render as an ordinary paragraph (a known divergence from the PDF).
    code_kinds = frozenset({"code"})
    aside_kinds: frozenset[str] = frozenset()

    def __init__(
        self,
        ctx: _Ctx,
        *,
        store: Any = None,
        byline: dict[str, Any] | None = None,
        title: str | None = None,
        abstract: bool = False,
    ) -> None:
        self.ctx = ctx
        self.store = store if store is not None else ctx.store
        self.byline = byline or {"authors": [], "affiliations": []}
        self.doc_title = title
        #: whether the prose after the title block is an abstract at all
        #: (the caller knows if a section follows it).
        self._has_abstract = abstract
        #: "open" from the title block to the first heading (or a
        #: "Keywords:" paragraph); "labelled" once the label is printed.
        self._abstract = "closed"

    def title(self, sink: Any, chunk: Any | None) -> None:
        """The title page: the title (the seeded chunk's text, else the ref
        title), the byline and the date."""
        text = chunk.text if chunk is not None else (self.doc_title or "")
        from precis.export.audience import has_record_handles

        if self.ctx.audience is not None and has_record_handles(text, self.ctx.store):
            paragraph = sink.add_heading("", level=0)
            self.ctx.in_heading = True
            try:
                _render_inline(text, self.ctx, paragraph)
            finally:
                self.ctx.in_heading = False
        else:
            sink.add_heading(text, level=0)
        _render_byline(sink, self.byline)
        _render_date(sink)
        self._abstract = "open" if self._has_abstract else "closed"

    def anchor(self, sink: Any, chunk: Any) -> None:
        """Intentional no-op: docx has no phantom label. (The draft's own
        Glossary heading + term chunks are the source of the abbreviation
        registry, not body: like LaTeX, the "Acronyms" list is generated at
        the end from the shorts the prose actually used.)"""

    def list_open(self, sink: Any, chunk: Any, kind: str, level: int) -> None:
        """Intentional no-op: docx has no container element; each item is
        styled from its ``(kind, level)`` (:meth:`list_item`)."""

    def list_close(self, sink: Any, kind: str) -> None:
        """Intentional no-op: docx has no container element to close."""

    def list_item(self, sink: Any, chunk: Any, kind: str, level: int) -> None:
        """Word's built-in List Bullet / List Number style: the immediate
        container decides bullet vs number, the nesting level picks the
        style variant (1–3)."""
        base = "List Number" if kind == "olist" else "List Bullet"
        style = base if level <= 1 else f"{base} {min(level, 3)}"
        _render_inline(chunk.text, self.ctx, sink.add_paragraph(style=style))

    def heading(self, sink: Any, chunk: Any, info: walker.HeadingInfo) -> None:
        """section / subsection / subsubsection, like LaTeX's depth map.
        Heading text goes through the same inline grammar as prose —
        citations inside it number against the shared bibliography, like
        the PDF (they used to print literally and stay unnumbered, shifting
        numbering against the PDF)."""
        ctx = self.ctx
        level = min(info.depth + 1, _MAX_HEADING_LEVEL)
        self._abstract = "closed"
        p = sink.add_heading("", level=level)
        if info.number:
            # The typed number is bookmarked on its own (REF fields read
            # it); the space after it stays outside the bookmark.
            _bookmarked(p, ctx, chunk.dc, lambda: p.add_run(info.number))
            p.add_run(" ")
        ctx.in_heading = True
        try:
            _render_inline(chunk.text, ctx, p)
        finally:
            ctx.in_heading = False

    def figure(self, sink: Any, chunk: Any) -> None:
        _render_figure(sink, self.store, chunk, self.ctx)

    def table(self, sink: Any, chunk: Any) -> None:
        _render_table(sink, chunk, self.ctx)

    def code(self, sink: Any, chunk: Any) -> None:
        run = sink.add_paragraph().add_run(chunk.text)
        run.font.name = "Consolas"

    def aside(self, sink: Any, chunk: Any) -> None:
        """No aside box in docx: the walker never calls this
        (:attr:`aside_kinds` is empty); kept as an ordinary paragraph."""
        self.paragraph(sink, chunk)

    def equation(
        self, sink: Any, chunk: Any, body: str, number: int | None, starred: bool
    ) -> None:
        _render_equation(sink, body, number, starred, ctx=self.ctx, dc=chunk.dc)

    def paragraph(self, sink: Any, chunk: Any) -> None:
        """An ordinary paragraph. Those between the title block and the first
        heading are the abstract: one "Abstract" label (Word only; the PDF
        has none) and the ``Abstract`` style, until a "Keywords:" paragraph."""
        style = None
        if self._abstract != "closed":
            if _KEYWORDS_START.match(chunk.text or ""):
                self._abstract = "closed"
            else:
                label_style, style = _abstract_styles(sink)
                if self._abstract == "open":
                    sink.add_paragraph(_ABSTRACT_LABEL, style=label_style)
                    self._abstract = "labelled"
        _render_inline(chunk.text, self.ctx, sink.add_paragraph(style=style))

    def cite_group(self, sink: Any, group: walker.CiteGroup) -> None:
        """One superscript mark (``[n]``, ``[1, 2]``, ``[1–3]``; an SI mark
        ``[n] (SI)``) plus each paper's link runs, or ONE EndNote field. A
        mark naming the same references as the block's previous printed mark
        prints nothing (:class:`walker.RepeatMarks`); in EndNote mode its
        passage notes move to the kept field's records."""
        ctx, si = self.ctx, any(group.si)
        if not ctx._marks.admit(group, trusted=ctx._force_keep):
            if ctx._kept_field is not None:
                _merge_dropped_notes(ctx._kept_field, group, ctx)
            ctx._dropped_mark = (sink, len(sink.runs))
            return
        ctx._kept_field = None
        mixed = si and not all(group.si)  # "[9 (SI), 14]": the mark per number
        si_mark = ctx._si_mark
        ctx._si_mark = None
        if si_mark is not None and si_mark[0] is sink and len(sink.runs) == si_mark[1]:
            # A mark directly after "[n] (SI)" reads as one blob without a
            # separator ("(SI)[4]" — gr491600's nit).
            sink.add_run(" ")
        if ctx.endnote:
            entries = list(zip(group.keys, group.numbers, group.chunk_ids, strict=True))
            ctx._kept_field = _cite_endnote(
                entries,
                ctx,
                sink,
                suffix=" (SI)" if si and not mixed else "",
                suffixes=[" (SI)" if s else "" for s in group.si] if mixed else None,
            )
        elif mixed:
            sink.add_run(
                merged_cite_numbers_si(list(zip(group.numbers, group.si, strict=True)))
            ).font.superscript = True
            for slug in group.keys:
                _cite_link_group(slug, ctx, sink)
        else:
            # biblatex sorts the keys of a merged cite before printing it.
            sink.add_run(
                merged_cite_numbers(sorted(group.numbers))
            ).font.superscript = True
            if si:
                sink.add_run(" (SI)").font.superscript = True
            for slug in group.keys:
                _cite_link_group(slug, ctx, sink)
        if si and not mixed:
            ctx._si_mark = (sink, len(sink.runs))

    def term(self, sink: Any, use: _abbrev.Use) -> None:
        """``Long Form (SHORT)`` for a definition, the bare short otherwise
        (:func:`precis.export._abbrev.plain`)."""
        sink.add_run(_abbrev.plain(use, self.ctx.abbrevs))

    def xref(self, sink: Any, xref: walker.Xref) -> None:
        """The surface / fallback text, or what ``\\cref`` would print —
        ``Section 1.5.5``, ``Figure 2``, ``Table 1`` (always the capitalised
        word), ``Eq. (3)`` (``Equation`` at a sentence start); a back-matter
        target prints its name; a reference into the citing chunk's own
        section prints ``above`` / ``below`` (``Above``). Nothing when there
        is no number. References joined by whitespace, a comma or "and" print as one
        run (``Sections 1 and 2``; :meth:`_print_xref`)."""
        if not xref.live or xref.surface:
            text = xref.surface or xref.fallback or walker.DEAD_XREF_TEXT
        elif xref.relative is not None:
            # The target is the citing chunk's own section or an ancestor.
            text = xref.relative
            if _at_sentence_start(sink):
                text = text[0].upper() + text[1:]
        elif xref.kind is not None:
            self._print_xref(sink, xref)
            return
        elif xref.label is not None:
            noun, number = xref.label
            if not noun:
                text = number  # unnumbered back matter: its name, like \nameref
            else:
                # A target whose cleveref type is unknown (a list item, a
                # listing): the label word and number alone, never merged.
                _flush_cites(self.ctx, sink)
                sink.add_run(f"{noun} ")
                self._ref_field(sink, xref.dc, number, noun)
                return
        else:
            return  # front matter / glossary / before section 1: nothing to print
        _flush_cites(self.ctx, sink)
        sink.add_run(text)

    def _print_xref(self, sink: Any, xref: walker.Xref) -> None:
        """Print ``xref`` as ``\\cref`` would. A reference that follows the
        paragraph's last one across only a connector (whitespace, ``,``,
        ``and``; :func:`walker.merge_xref_runs`, the rule ``\\cref{a,b}`` follows)
        takes the earlier one's runs out and prints the whole run at once
        (:func:`walker.format_xref_run`: ``Sections 1 and 2``,
        ``Figures 1 to 3``); each number stays its own ``REF`` field."""
        ctx = self.ctx
        _flush_cites(ctx, sink)
        members = [xref]
        prior = ctx._xref_run
        if prior is not None and _reopen_run(prior, sink, xref):
            members = [*prior.members, xref]
        n_before = len(sink._p)
        for part in walker.format_xref_run(
            members, sentence_start=_at_sentence_start(sink)
        ):
            if part.dc:
                self._ref_field(sink, part.dc, part.number, part.noun or None)
            else:
                sink.add_run(part.text)
        ctx._xref_run = _XrefRun(sink, members, list(sink._p)[n_before:])

    def _ref_field(self, sink: Any, dc: str, number: str, noun: str | None) -> None:
        """The number of a cross-reference as a ``REF _Ref_<dc> \\h`` field
        (cached result ``number``) to the bookmark on the target's own
        number: the figure / table / equation itself, the heading itself
        or, for a paragraph or list item, the section it sits in. No
        bookmark to name: plain text. ``\\* MERGEFORMAT`` keeps the result
        in the citing text's own formatting on a field update; without it
        Word copies the target's (a bold caption label, a heading font)."""
        target = dc
        if noun == "Section":
            target = _section_heading(self.ctx, number) or ""
        if not target:
            sink.add_run(number)
            return
        _add_field(sink, f"REF {_bookmark_name(target)} \\h \\* MERGEFORMAT", number)

    def back_matter(self, sink: Any, name: str, block: Any) -> None:
        """Append one back-matter section to the document (each is a no-op
        when it has no content)."""
        ctx = self.ctx
        if name == "unverified":
            _append_unverified_claims(sink, ctx)
            from docx.shared import RGBColor

            from precis.export.retraction import (
                draft_retraction_report,
                retraction_coverage_entries,
            )

            entries = retraction_coverage_entries(
                draft_retraction_report(ctx.store, None, cited_slugs=ctx.cited), "none"
            )
            if entries:
                sink.add_heading("Retraction coverage", level=1)
                for text, red in entries:
                    run = sink.add_paragraph().add_run(text)
                    if red:
                        run.font.color.rgb = RGBColor(255, 0, 0)
        elif name == "data_package":
            _append_data_package(sink, ctx.data_package)
            if ctx.audience is not None and ctx.audience.name == "external":
                _append_supplement(sink, ctx.audience.entries)
        elif name == "references":
            _append_references(sink, ctx)
        elif name == "acronyms":
            _append_acronyms(sink, ctx)
        elif name == "published":
            _append_published_claims(sink, ctx)


#: The back-matter blocks docx can print (the PDF-only ``sources`` appendix
#: has no docx counterpart).
_DOCX_BACK_MATTER = (
    "unverified",
    "data_package",
    "references",
    "acronyms",
    "published",
)


def _resolve_source(store: RefLookupStore, slug: str) -> dict[str, Any] | None:
    """Resolve a cited slug to the structured record EndNote embeds — the
    SAME paper/patent/datasheet lookup the plain path and the ``.bib`` path
    use, so the docx, PDF and EndNote outputs cite the identical resolved
    source. ``rec_number`` is the corpus ref id (not the per-export citation
    number): a re-export keeps each paper's EndNote identity stable."""
    pref = (
        store.get_ref(kind="paper", id=slug)
        or store.get_ref(kind="patent", id=slug)
        or store.get_ref(kind="datasheet", id=slug)
    )
    if pref is None:
        return None
    doi = arxiv = None
    try:
        alias = store.identifiers_for_refs([pref.id]).get(pref.id, {})
        doi, arxiv = alias.get("doi"), alias.get("arxiv")
    except Exception:  # pragma: no cover — identifier lookup best-effort
        pass
    meta = pref.meta or {}
    url = (
        f"https://doi.org/{doi}"
        if doi
        else (f"https://arxiv.org/abs/{arxiv}" if arxiv else None)
    )
    return {
        "kind": pref.kind,
        "tag": slug,
        "rec_number": pref.id,
        "bib_type": (
            # A typed whole-record cite (gr514384) takes its genre entry
            # type — EndNote ref-type Computer Program / Dataset — over
            # the Crossref entry_type mapping.
            record_bib_type(meta) or paper_bib_type(meta)
            if pref.kind == "paper"
            else ""
        ),
        "authors": pref.authors,
        "title": pref.title or slug,
        "year": pref.year,
        "journal": meta.get("journal")
        or meta.get("venue")
        or meta.get("container_title"),
        "volume": meta.get("volume"),
        "number": meta.get("number"),
        "pages": meta.get("pages"),
        "doi": doi,
        "url": url,
    }


@dataclass
class _EndnoteField:
    """A written ``ADDIN EN.CITE`` field, kept while a later repeat of its
    mark may add passage notes to its records: the ``w:instrText`` element,
    the ``(source, notes, n)`` triple and cite key of each ``<Cite>``, the
    whole notes each record holds (``notes``, parallel to ``cites``; the
    triple's text is these joined by newlines) and the postnote arguments it
    was built with."""

    instr: Any
    cites: list[tuple[dict[str, Any], str | None, int]]
    keys: list[str]
    suffix: str
    suffixes: list[str] | None
    notes: list[list[str]]


def _chunk_note(ctx: _Ctx, chunk_id: int | None) -> str | None:
    """The cited passage's text (``pc<id>``), best effort."""
    if chunk_id is None or ctx.store is None:
        return None
    try:
        return ctx.store.chunks.chunk_text_by_id(chunk_id)
    except Exception:  # pragma: no cover — chunk fetch best-effort
        return None


def _merge_dropped_notes(
    kept: _EndnoteField, group: walker.CiteGroup, ctx: _Ctx
) -> None:
    """A repeated mark that was not written still pinned passages: append
    each one's text to the Research Notes of the kept field's record for
    that reference (once per passage) and rewrite the field's payload."""
    from precis.export.endnote import set_citation_payload

    changed = False
    for key, chunk_id in zip(group.keys, group.chunk_ids, strict=False):
        note = _chunk_note(ctx, chunk_id)
        if not note or key not in kept.keys:
            continue
        i = kept.keys.index(key)
        if note in kept.notes[i]:  # a whole note already there, not a substring
            continue
        kept.notes[i].append(note)
        source, _old, n = kept.cites[i]
        kept.cites[i] = (source, "\n".join(kept.notes[i]), n)
        changed = True
    if changed:
        set_citation_payload(kept.instr, kept.cites, kept.suffix, kept.suffixes)


def _cite_endnote(
    run: list[tuple[str, int, int | None]],
    ctx: _Ctx,
    paragraph: Any,
    suffix: str = "",
    suffixes: Sequence[str] | None = None,
) -> _EndnoteField | None:
    """Emit ONE native EndNote ``ADDIN EN.CITE`` field for a citation run —
    one ``<Cite>`` element per ``(slug, n, chunk_id)`` entry, as EndNote
    itself writes a grouped citation. An unresolved slug degrades to a
    plain superscript marker (nothing lost).

    A ``pc<id>`` chunk citation (``chunk_id`` set) embeds that chunk's text as
    the field's traveling note — so the exact cited passage rides along in
    EndNote. Per-citation, not per-paper: different cite sites of the same
    paper carry the passage each one actually pointed at. ``suffix`` is an
    all-SI run's ``" (SI)"`` postnote (gr491600 L5); ``suffixes`` (one per
    ``run`` entry) puts it on the SI ``<Cite>`` of a mixed run only.
    Returns the written field (None when no ``<Cite>`` resolved)."""
    from precis.export.endnote import add_citation_field

    cites: list[tuple[dict[str, Any], str | None, int]] = []
    cite_suffixes: list[str] = []
    keys: list[str] = []
    for i, (slug, n, chunk_id) in enumerate(run):
        source = ctx.resolved.get(slug)
        if source is None:
            source = _resolve_source(ctx.store, slug)
            if source is None:
                ctx.warnings.append(f"cite {slug!r}: no paper in corpus — plain marker")
                paragraph.add_run(
                    f"[{n}]{suffixes[i] if suffixes else suffix}"
                ).font.superscript = True
                continue
            if not source.get("year"):
                ctx.warnings.append(f"cite {slug!r}: no year — empty <Year/>")
            ctx.resolved[slug] = source
        cites.append((source, _chunk_note(ctx, chunk_id), n))
        cite_suffixes.append(suffixes[i] if suffixes else suffix)
        keys.append(slug)
    if not cites:
        return None
    sufs = cite_suffixes if suffixes else None
    instr = add_citation_field(paragraph, cites, suffix, sufs)
    notes = [[note] if note else [] for _source, note, _n in cites]
    return _EndnoteField(instr, cites, keys, suffix, sufs, notes)


# ── references + glossary sections ────────────────────────────────


#: One styled piece of a reference line: ``(text, styles)`` with styles drawn
#: from ``i`` italic, ``b`` bold, ``sub``, ``sup``, ``sc`` small caps.
_RefRun = tuple[str, frozenset[str]]

_BIB_TOKEN = re.compile(
    rf"(?P<math>{_MATH.pattern})|<(?P<close>/?)(?P<tag>[A-Za-z][A-Za-z0-9]*)\b[^>]*>",
    re.DOTALL,
)
_BIB_TAG_STYLE = {
    "sub": "sub",
    "sup": "sup",
    "i": "i",
    "em": "i",
    "scp": "sc",
    "b": "b",
    "strong": "b",
}
_TEX_TEXT_WRAPPER = re.compile(r"\\(?:mathrm|mathit|textrm|textit|text|rm|it)\b")


def _tex_script_runs(body: str, styles: frozenset[str]) -> list[_RefRun]:
    """The inside of a ``$…$`` span → styled runs: ``_x`` / ``^{xy}`` become
    sub/superscript runs, ``\\mathrm{…}``-style wrappers and braces vanish, any
    other macro is dropped. Enough for the chemistry in titles
    (``C$_{60}$``, ``Fe$^{3+}$``); not a general TeX renderer."""
    out: list[_RefRun] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            out.append(("".join(buf), styles))
            buf.clear()

    i, n = 0, len(body)
    while i < n:
        ch = body[i]
        if ch in "_^":
            flush()
            i += 1
            while i < n and body[i] == " ":
                i += 1
            if i < n and body[i] == "{":
                depth, j = 1, i + 1
                while j < n and depth:
                    depth += {"{": 1, "}": -1}.get(body[j], 0)
                    j += 1
                arg, i = body[i + 1 : j - 1], j
            elif i < n and body[i] == "\\":
                m = re.match(r"\\[A-Za-z]+", body[i:])
                arg = m.group(0) if m else body[i : i + 1]
                i += len(arg)
            else:
                arg, i = body[i : i + 1], i + 1
            out.extend(_tex_script_runs(arg, styles | {"sub" if ch == "_" else "sup"}))
        elif ch == "\\":
            m = re.match(r"\\[A-Za-z]+|\\.", body[i:])
            tok = m.group(0) if m else ch
            i += len(tok)
            if tok[1:].isalpha():
                continue  # a macro (``\mathrm`` wrapper or unknown): drop the name
            buf.append(tok[1:])  # an escaped char: ``\%`` -> ``%``
        elif ch in "{}":
            i += 1
        else:
            buf.append(ch)
            i += 1
    flush()
    return out


def _bib_markup_runs(text: str) -> list[_RefRun]:
    """A bibliography title / journal string → styled runs, the docx twin of
    ``export/latex.py::_bib_text``: Crossref-style inline HTML (``<sub>``,
    ``<sup>``, ``<i>``/``<em>``, ``<scp>``, ``<b>``) becomes real run
    formatting, other tags are stripped, entities are decoded, ``$…$`` math
    renders its sub/superscripts as runs, and BibTeX case-protection braces
    outside math are dropped."""
    out: list[_RefRun] = []
    active: list[str] = []
    pos = 0

    def plain(chunk: str) -> None:
        chunk = html.unescape(chunk).replace("{", "").replace("}", "")
        if chunk:
            out.append((chunk, frozenset(active)))

    for m in _BIB_TOKEN.finditer(text):
        plain(text[pos : m.start()])
        pos = m.end()
        if m.group("math") is not None:
            span = m.group("math")
            if _math_braces_balanced(span) and _math_plausible(span):
                out.extend(_tex_script_runs(span.strip("$"), frozenset(active)))
            else:
                plain(span)
            continue
        style = _BIB_TAG_STYLE.get(m.group("tag").lower())
        if style is None:
            continue  # unknown tag: stripped
        if m.group("close"):
            if style in active:
                active.remove(style)
        else:
            active.append(style)
    plain(text[pos:])
    return out


def _plain_authors(authors: list[dict[str, Any]] | None) -> str:
    """``A; B; C`` byline for a reference line (unescaped — unlike the BibTeX
    ``_bibtex_authors``, whose ``\\&`` escapes would print literally)."""
    names = []
    for a in authors or []:
        name = a.get("name") or " ".join(
            x for x in (a.get("given"), a.get("family")) if x
        )
        if name:
            names.append(_NAME_ZEROWIDTH.sub("", _NAME_UNISPACE.sub(" ", name)))
    return "; ".join(names)


def _runs_text(runs: list[_RefRun]) -> str:
    return "".join(t for t, _ in runs)


def _sentence(runs: list[_RefRun]) -> list[_RefRun]:
    """``runs`` closed with a full stop, unless it already ends in one
    (``fields.`` + ``.`` used to print ``fields..``)."""
    text = _runs_text(runs).rstrip()
    if not text or text[-1] in ".?!":
        return runs
    return [*runs, (".", frozenset())]


def _reference_runs(
    store: RefLookupStore, slug: str, warnings: list[str]
) -> list[_RefRun]:
    """One reference line as styled runs, resolved through the SAME
    paper/patent/datasheet lookup as the ``.bib`` path (citation-integrity
    parity with the PDF), carrying the fields ``build_bib`` emits: authors,
    year, title, journal / volume / number / pages (or the book / report
    equivalents) and the DOI / arXiv id. A slug with no matching source in
    the corpus degrades to a marked stub + a warning."""
    none: frozenset[str] = frozenset()
    pref = (
        store.get_ref(kind="paper", id=slug)
        or store.get_ref(kind="patent", id=slug)
        or store.get_ref(kind="datasheet", id=slug)
    )
    if pref is None:
        warnings.append(f"cite {slug!r}: no source in corpus — stub reference")
        return [(f"[missing source {slug}] (cited slug not in corpus)", none)]
    authors = _plain_authors(pref.authors)
    # A datasheet has no byline; its vendor stands in as the "author" org so the
    # reference reads "Espressif (2023). ESP32-C3. [Datasheet] Part C123."
    meta = pref.meta or {}
    kind = getattr(pref, "kind", "")
    if kind == "datasheet" and not authors:
        authors = str(meta.get("vendor") or "").strip()
    # "Authors (year). Title." — robust plain-text assembly.
    head = " ".join(x for x in [authors, f"({pref.year})" if pref.year else ""] if x)
    runs: list[_RefRun] = []
    if head:
        runs.append((head + ". ", none))
    runs += _sentence(_bib_markup_runs(pref.title or slug))
    if kind == "datasheet":
        runs.append((f" [{datasheet_pub_label(meta)}]", none))
        part = str(meta.get("part_lcsc") or "").strip()
        if part:
            runs.append((f" Part {part}.", none))
    elif kind == "paper":
        runs += _venue_runs(meta, record_bib_type(meta) or paper_bib_type(meta))
    try:
        alias = store.identifiers_for_refs([pref.id]).get(pref.id, {})
        if alias.get("doi"):
            runs.append((f" doi:{alias['doi']}", none))
        elif alias.get("arxiv"):
            runs.append((f" arXiv:{alias['arxiv']}", none))
    except Exception:
        pass
    return runs


def _venue_runs(meta: dict[str, Any], entry_type: str) -> list[_RefRun]:
    """The journal / container / publisher part of a paper's reference line,
    from the same ``meta`` fields (and precedence) ``build_bib`` reads."""
    none: frozenset[str] = frozenset()
    ital = frozenset({"i"})

    def field(key: str) -> str:
        return str(meta.get(key) or "").strip()

    out: list[_RefRun] = []
    if entry_type in ("software", "dataset"):
        # Whole-record cite (gr514384): no venue — the genre label,
        # publisher and version carry the line:
        # "… Title. [Software] Zenodo, version 1.2.3."
        out.append((f" [{entry_type.capitalize()}]", none))
        publisher = field("publisher")
        if publisher:
            out.append((f" {publisher}", none))
        version = field("version")
        if version:
            out.append((f"{', ' if publisher else ' '}version {version}", none))
        return _sentence(out) if out else out
    if entry_type == "article":
        journal = field("journal") or field("venue") or field("container_title")
        if journal:
            out.append((" ", none))
            out += [(t, st | ital) for t, st in _bib_markup_runs(journal)]
        vol, num, pages = field("volume"), field("number"), field("pages")
        tail = vol + (f"({num})" if num else "")
        if tail:
            out.append((f" {tail}", none))
        if pages:
            out.append((f"{', ' if journal or tail else ' '}{pages}", none))
        return _sentence(out) if out else out
    if entry_type in ("book", "incollection", "inproceedings"):
        booktitle = field("container_title") or field("journal")
        if entry_type != "book" and booktitle:
            out.append((" In: ", none))
            out += [(t, st | ital) for t, st in _bib_markup_runs(booktitle)]
        parts = [field("publisher")]
        if entry_type != "book":
            # A chapter in a numbered series volume ("In: IUTAM Bookseries,
            # vol. 13, 109-121.", gr725252).
            volume = field("volume")
            parts.insert(0, f"vol. {volume}" if volume else "")
            parts.append(field("pages"))
        extra = ", ".join(x for x in parts if x)
        if extra:
            out.append((f"{', ' if out else ' '}{extra}", none))
        return _sentence(out) if out else out
    inst = field("institution") or field("publisher")
    if inst:  # report / thesis / online
        out.append((f" {inst}", none))
    return _sentence(out) if out else out


def _format_reference(store: RefLookupStore, slug: str, warnings: list[str]) -> str:
    """One reference line as plain text (the EndNote cached list); the docx
    References section renders the styled :func:`_reference_runs` instead."""
    return _runs_text(_reference_runs(store, slug, warnings))


def _append_references(doc: Any, ctx: _Ctx) -> None:
    """A numbered **References** section: one entry per cited paper, in
    cite order, so entry ``n`` matches every in-text ``[n]`` mark. Each
    line is resolved through the SAME paper lookup as the ``.bib`` path
    (citation-integrity parity with the PDF). No-op when nothing was cited.

    A plain Word section (heading + numbered paragraphs), deliberately
    *not* native endnote fields: a paper cited many times must reuse one
    number, and a Word endnote can be referenced only once. Each entry is
    one paragraph in the bibliography style, "[n]", a tab, then the line."""
    if not ctx.cited:
        return
    doc.add_heading("References", level=1)
    if ctx.endnote:
        # EndNote regenerates this list on "Update Citations and Bibliography"
        # from the records embedded in the in-text fields; the plain lines are
        # cached placeholder text shown until then.
        from precis.export.endnote import add_reflist_field

        cached = [
            (f"[{i}]", _format_reference(ctx.store, slug, ctx.warnings))
            for i, slug in enumerate(ctx.cited, start=1)
        ]
        add_reflist_field(
            doc, cached, style=_bibliography_style(doc, _ENDNOTE_BIBLIOGRAPHY_STYLE)
        )
        return
    style = _bibliography_style(doc, _BIBLIOGRAPHY_STYLE)
    for i, slug in enumerate(ctx.cited, start=1):
        p = doc.add_paragraph(style=style)
        p.add_run(f"[{i}]").bold = True
        p.add_run("\t")
        for text, styles in _reference_runs(ctx.store, slug, ctx.warnings):
            run = p.add_run(text)
            if "i" in styles:
                run.italic = True
            if "b" in styles:
                run.bold = True
            if "sub" in styles:
                run.font.subscript = True
            if "sup" in styles:
                run.font.superscript = True
            if "sc" in styles:
                run.font.small_caps = True


def _append_acronyms(doc: Any, ctx: _Ctx) -> None:
    """An "Acronyms" list of every abbreviation actually used in the prose
    (auto-built, like the LaTeX glossaries acronym list), one "SHORT: long
    form" paragraph each. The look comes from the ``Definition`` and
    ``Definition Term`` styles alone, no run carries direct formatting."""
    used = walker.acronym_list(ctx.used_acr, ctx.abbrevs)
    if not used:
        return
    doc.add_heading("Acronyms", level=1)
    para_style, term_style = _definition_styles(doc)
    for short in used:
        p = doc.add_paragraph(style=para_style)
        p.add_run(f"{short}: ", style=term_style)
        p.add_run(ctx.abbrevs[short])


def _append_unverified_claims(doc: Any, ctx: _Ctx) -> None:
    """An "Unverified claims" section — one entry per finding that
    rendered with a trust mark (trust-surfaces export marking),
    mirroring :func:`precis.export.latex.build_unverified_claims_section`.
    No-op when nothing was marked. Entry wording is shared with the latex
    exporter via :func:`precis.export._trust_marks.unverified_claims_entries`.
    """
    entries = unverified_claims_entries(ctx.trust)
    if not entries:
        return
    doc.add_heading("Unverified claims", level=1)
    for title, tag, detail in entries:
        p = doc.add_paragraph()
        p.add_run(f"{title} ").bold = True
        p.add_run(f"({tag}){detail}")


def _append_published_claims(doc: Any, ctx: _Ctx) -> None:
    """A "Published claim artifacts" section — one paragraph per cited
    claim hub whose nanopub publish row is minted (sentence, source
    numbers, a linked "link", " (under embargo)" until public), mirroring
    :func:`precis.export.latex.build_published_claims_section`. No-op when
    nothing cited is minted. Entries (and the status wording) are shared
    via :func:`precis.export._nanopub_appendix.published_claim_entries`."""
    entries = published_claim_entries(ctx.store, ctx.trust)
    if not entries:
        return
    doc.add_heading(_NANOPUB_SECTION_TITLE, level=1)
    style = _published_claim_style(doc)
    for e in entries:
        # Numbers come from slugs the body already cited (never registers
        # one), so the bibliography is identical with or without this list.
        nums = walker.claim_source_numbers(ctx.claim_cites.get(e.fid, []), ctx.cited)
        p = doc.add_paragraph(style=style)
        p.add_run(e.sentence)
        if nums:
            p.add_run(f" {merged_cite_numbers(nums)}")
        p.add_run(" ")
        _add_hyperlink(p, e.trusty_uri, "link")
        if not e.public:
            p.add_run(" (under embargo)")


def _append_supplement(doc: Any, entries: list[Any]) -> None:
    """Format the numbered record table alongside the figure data package."""
    doc.add_heading("Data Supplement", level=1)
    table = doc.add_table(rows=1, cols=3)
    _repeat_header_row(table)
    for cell, text in zip(table.rows[0].cells, ("Entry", "Kind", "Description")):
        cell.text = text
    for entry in entries:
        for cell, text in zip(
            table.add_row().cells, (entry.number, entry.kind, entry.description)
        ):
            cell.text = text


def _append_data_package(doc: Any, entries: list[DataPackageFigure]) -> None:
    """A "Data package" section — one small-print (Courier New 7 pt)
    header + table per figure that carried a ``data_package`` snapshot,
    mirroring :func:`precis.export.latex.build_data_package_section`.
    No-op when no figure carried a snapshot. Header + table lines come
    from :func:`precis.export._data_package.header_lines` /
    :func:`~precis.export._data_package.table_lines`, shared with the
    LaTeX exporter so the two never print different numbers. The
    machine-readable JSON travels alongside as the ``data-package.json``
    sidecar written next to the ``.docx`` (see :func:`export_docx`) — a
    plain ``.docx`` has no attach-a-file mechanism the LaTeX ``embedfile``
    equivalent, so there's no embedded copy here."""
    from docx.shared import Pt

    if not entries:
        return
    doc.add_heading(_DATA_PACKAGE_SECTION_TITLE, level=1)
    for fig in entries:
        cap = doc.add_paragraph()
        cap.add_run(fig.caption).bold = True
        for line in [*header_lines(fig), "", *table_lines(fig)]:
            p = doc.add_paragraph()
            run = p.add_run(line)
            run.font.name = "Courier New"
            run.font.size = Pt(7)
