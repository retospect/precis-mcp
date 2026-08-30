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

#: ``meta.entry_type`` → per-format citation type, for the entry types
#: whose export differs from the journal-article default
#: (``@article`` / ``TY  - JOUR`` / ``%0 Journal Article``). Absent or
#: unrecognised ``entry_type`` (including ``journal-article``,
#: ``reference-entry``, ``other``) falls through to that default
#: unchanged — an un-enriched paper's export is byte-identical to
#: before this map existed.
_EXPORT_TYPE_MAP: dict[str, dict[str, str]] = {
    "proceedings-article": {
        "bibtex": "inproceedings",
        "ris": "CONF",
        "endnote": "Conference Paper",
    },
    "book-chapter": {
        "bibtex": "incollection",
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
    "monograph": {"bibtex": "book", "ris": "BOOK", "endnote": "Book"},
    "report": {
        "bibtex": "techreport",
        "ris": "RPRT",
        "endnote": "Report",
    },
}


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
    title = _clean_inline_text(ref.title)
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
    entry_type = str(meta.get("entry_type") or "").strip()
    export_type = _EXPORT_TYPE_MAP.get(entry_type)

    if style == "bibtex":
        # LaTeX-escape every scalar field that might carry a special
        # char. ``and``/``year``/``doi`` rarely do but we run them
        # through anyway for symmetry; a stray ``&`` in the title was
        # the actual MCP-critic finding.
        bx_title = _latex_escape(title)
        bx_authors = " and ".join(_latex_escape(a) for a in authors)
        bx_journal = _latex_escape(journal)
        bx_doi = _latex_escape(doi_clean)
        bx_type = export_type["bibtex"] if export_type else "article"
        lines = [f"@{bx_type}{{{slug},"]
        if bx_title:
            lines.append(f"  title = {{{bx_title}}},")
        if bx_authors:
            lines.append(f"  author = {{{bx_authors}}},")
        if year:
            lines.append(f"  year = {{{year}}},")
        if bx_journal:
            lines.append(f"  journal = {{{bx_journal}}},")
        if bx_doi:
            lines.append(f"  doi = {{{bx_doi}}},")
        if entry_type == "posted-content":
            lines.append("  note = {Preprint},")
        lines.append("}")
        return "\n".join(lines) + "\n"

    if style == "ris":
        ris_type = export_type["ris"] if export_type else "JOUR"
        out = [f"TY  - {ris_type}"]
        if title:
            out.append(f"TI  - {title}")
        for a in authors:
            out.append(f"AU  - {a}")
        if year:
            out.append(f"PY  - {year}")
        if journal:
            out.append(f"JO  - {journal}")
        if doi_clean:
            out.append(f"DO  - {doi_clean}")
        out.append("ER  - ")
        return "\n".join(out)

    # endnote (subset)
    endnote_type = export_type["endnote"] if export_type else "Journal Article"
    out = [f"%0 {endnote_type}"]
    if title:
        out.append(f"%T {title}")
    for a in authors:
        out.append(f"%A {a}")
    if year:
        out.append(f"%D {year}")
    if journal:
        out.append(f"%J {journal}")
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
    "_author_names",
    "_clean_inline_text",
    "_format_authors",
    "_latex_escape",
    "_render_citation",
    "_strip_jats",
]
