"""Paper handler — read-only access to pre-ingested scientific papers.

Extends RefHandler with paper-specific views: /abstract, /cite, /fig, /page.
Requires the ``paper`` extra: ``pip install precis-mcp[paper]``.
"""

from __future__ import annotations

import html
import json
import logging
import re
from pathlib import Path
from typing import ClassVar

from acatome_meta.literature import first_author_surname

from precis.handlers._ref_base import RefHandler, _pluralise, _truncate
from precis.protocol import ErrorCode, PrecisError, extract_kwargs
from precis.uri import SEP

log = logging.getLogger(__name__)


# HTML/JATS tags embedded in titles by the CrossRef / JATS ingestion
# pipeline.  A plain strip leaves the inner text intact; we deliberately
# do not translate ``<i>`` → ``\textit{}`` because downstream templates
# vary (biblatex/natbib/plain bibtex all differ on the right spelling),
# and leaving the text unformatted is safer than guessing.
_HTML_TAG_RE = re.compile(r"<[^>]+>")

# JATS XML markers embedded in abstracts coming from CrossRef.  These
# are NOT stripped by the existing title sanitiser because we want to
# convert ``<jats:sub>2</jats:sub>`` to a Unicode subscript instead of
# just deleting it.  Without this, ``/abstract`` returns prose like
# ``reducing nitrate (NO<jats:sub>3</jats:sub><jats:sup>−</jats:sup>)``
# verbatim — unusable in any downstream prompt or paper draft.
# Review: 2026-04-25 mcp-critic finding B4.
_JATS_SUB_RE = re.compile(r"<jats:sub>(.*?)</jats:sub>", re.IGNORECASE | re.DOTALL)
_JATS_SUP_RE = re.compile(r"<jats:sup>(.*?)</jats:sup>", re.IGNORECASE | re.DOTALL)
_JATS_TAG_RE = re.compile(r"</?jats:[A-Za-z\-]+(?:\s[^>]*)?\s*/?>", re.IGNORECASE)

# Characters we can map verbatim to Unicode subscripts / superscripts.
# Anything outside this set falls back to a markdown ``_x_`` / ``^x^``
# wrapper so we never silently drop content.
_SUB_TRANSLATE = str.maketrans(
    "0123456789+-=()n\u2212",
    "\u2080\u2081\u2082\u2083\u2084\u2085\u2086\u2087\u2088\u2089\u208a\u208b\u208c\u208d\u208e\u2099\u208b",
)
_SUP_TRANSLATE = str.maketrans(
    "0123456789+-=()n\u2212",
    "\u2070\u00b9\u00b2\u00b3\u2074\u2075\u2076\u2077\u2078\u2079\u207a\u207b\u207c\u207d\u207e\u207f\u207b",
)
_SUB_OK = set("0123456789+-=()n\u2212")
_SUP_OK = set("0123456789+-=()n\u2212")

# Pattern to recognise an in-text figure / scheme / table caption when
# the figure-extractor missed it (i.e. caption lives in a regular text
# block instead of the figures table).  Matches ``Figure 3.``, ``Fig. 3``,
# ``Scheme 2``, ``Table 1.``, etc.
# Review: 2026-04-25 mcp-critic finding B7.
_CAPTION_PATTERNS = {
    "fig": re.compile(
        r"^\s*(?:Figure|Fig\.?|Scheme)\s+(\d+)\b", re.IGNORECASE
    ),
}

# Same pattern, anchored on the leading label only.  Used to strip the
# duplicate prefix when we re-emit a caption with our own bold marker.
# Without this, ``**Figure 1.** Figure 1. CO2 cycle...`` ships to the
# agent and gets copied verbatim into prose.  Review 2026-04-25 mcp-
# critic finding B7 (caption-label duplication).
_CAPTION_LEAD_RE = re.compile(
    r"^\s*(?:Figure|Fig\.?|Scheme)\s+\d+\.?\s*[:\-—]?\s*",
    re.IGNORECASE,
)


def _caption_body(text: str) -> str:
    """Return ``text`` with any leading ``Figure N.`` label stripped.

    The figure formatter adds its own ``**Figure N.**`` bold marker, so
    a caption that already starts with ``Figure 1. CO2 cycle...`` would
    otherwise render as ``**Figure 1.** Figure 1. CO2 cycle...`` —
    duplicate prefix the caller has to clean up by hand.
    """
    if not text:
        return text
    stripped = _CAPTION_LEAD_RE.sub("", text, count=1)
    # Preserve internal whitespace; only trim the head + tail.
    return stripped.strip()


def _convert_jats_sub(match: re.Match[str]) -> str:
    """Replace ``<jats:sub>x</jats:sub>`` with Unicode subscript or
    markdown ``_x_`` fallback.
    """
    inner = match.group(1).strip()
    if not inner:
        return ""
    if all(c in _SUB_OK for c in inner):
        return inner.translate(_SUB_TRANSLATE)
    return f"_{inner}_"


def _convert_jats_sup(match: re.Match[str]) -> str:
    """Replace ``<jats:sup>x</jats:sup>`` with Unicode superscript or
    markdown ``^x^`` fallback.
    """
    inner = match.group(1).strip()
    if not inner:
        return ""
    if all(c in _SUP_OK for c in inner):
        return inner.translate(_SUP_TRANSLATE)
    return f"^{inner}^"


def _clean_jats(text: str) -> str:
    """Strip JATS XML markup from CrossRef-style abstracts.

    Conversion rules (applied in order):

    1. ``<jats:sub>x</jats:sub>`` → Unicode subscript (e.g. ``₃⁴−``)
       when ``x`` is digits/operators only, else markdown ``_x_``.
    2. ``<jats:sup>x</jats:sup>`` → Unicode superscript, else ``^x^``.
    3. Any remaining ``<jats:foo>`` / ``</jats:foo>`` opening or closing
       tag (``<jats:title>``, ``<jats:p>``, ``<jats:italic>``, etc.)
       is dropped verbatim.
    4. HTML entities decoded.
    5. Outer whitespace stripped.  Internal whitespace is left alone
       so paragraph breaks survive.

    Review: 2026-04-25 mcp-critic finding B4 — ``/abstract`` was
    returning raw ``<jats:title>Abstract</jats:title>...`` markup.
    """
    if not text:
        return text
    text = _JATS_SUB_RE.sub(_convert_jats_sub, text)
    text = _JATS_SUP_RE.sub(_convert_jats_sup, text)
    text = _JATS_TAG_RE.sub("", text)
    text = html.unescape(text)
    return text.strip()

# BibTeX reserved characters that must be escaped with a backslash when
# they appear literally inside a braced field value.  ``~`` and ``^``
# require ``\textasciitilde{}`` / ``\textasciicircum{}`` which is more
# invasive than we want here — they're left alone and the bib file
# compiler will complain if they slip through.
_BIBTEX_ESCAPE = str.maketrans(
    {
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
    }
)

# Collapses any run of whitespace (incl. newlines, tabs) down to a
# single ASCII space.  Titles arrive multi-line from sources that pretty-
# print the source XML.
_WHITESPACE_RE = re.compile(r"\s+")


def _clean_title(raw: object) -> str:
    """Sanitise a title for inclusion in a citation field.

    Steps, in order:

    1. Coerce to ``str`` and strip outer whitespace.
    2. Decode HTML entities (``&amp;`` → ``&``, ``&mdash;`` → ``—``).
    3. Strip inline HTML/JATS tags verbatim.
    4. Collapse all internal whitespace runs to a single space.
    5. Escape BibTeX reserved characters (``&`` ``%`` ``$`` ``#`` ``_``).

    Safe on empty/``None`` input — returns ``""``.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    text = html.unescape(text)
    text = _HTML_TAG_RE.sub("", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text.translate(_BIBTEX_ESCAPE)


def _bibtex_escape(raw: object) -> str:
    """Minimal escaping for non-title string fields (journal, author…).

    Runs HTML-entity decode + whitespace collapse + BibTeX-special
    escape.  Does not strip HTML tags — none of the non-title fields
    have been seen to carry them, and stripping proactively would be a
    data-loss footgun on surname strings like ``<van> der Waals``.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    text = html.unescape(text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text.translate(_BIBTEX_ESCAPE)


def _format_next_block(calls: list[tuple[str, str]]) -> list[str]:
    """Render a "Next:" block of (call, description) pairs aligned.

    The trailers we emit at the end of figure / overview responses are
    visual tables: each row is a copy-pasteable ``get(...)`` call on
    the left and a one-line gloss on the right, separated by a
    long-dash.  When those rows were hand-aligned with literal
    spaces, three things broke:

    * Slugs vary in length (``ni2024atomic`` vs
      ``marquessilva1999grasp``) — hand-counted spaces only worked
      for the design-time fixture.
    * Figure numbers grow past one digit on some papers — by ``fig
      10`` the column was already off.
    * Adding or renaming a row required re-counting every line.

    This helper takes a list of ``(call, description)`` pairs and
    pads every call to the longest one before the dash, so the
    column stays straight regardless of input.
    """
    if not calls:
        return []
    width = max(len(call) for call, _ in calls)
    return [f"  {call:<{width}}  — {desc}" for call, desc in calls]


def _author_names(raw: object) -> list[str]:
    """Normalise the ``authors`` column into a flat list of name strings.

    Accepts the shapes that actually show up in the store:

    * ``list[dict]``    e.g. ``[{"name": "Smith, John"}, {"name": "Li, X."}]``
    * ``list[str]``     e.g. ``["Smith, John", "Li, X."]``
    * ``str`` (JSON)    e.g. ``'[{"name": "Smith, John"}, …]'`` — decoded
    * ``str`` (plain)   e.g. ``"Smith, John; Li, X."`` — split on ``;``
    * ``None`` / other — empty list

    Returns a list of cleaned display names with empty entries stripped.
    Pure — never raises.
    """
    # Decode JSON-encoded strings first so the downstream branching
    # only has to deal with the structured case.
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped.startswith("["):
            try:
                raw = json.loads(stripped)
            except (ValueError, TypeError):
                # Fall through: treat as plain semicolon string.
                pass
    if isinstance(raw, list):
        out: list[str] = []
        for item in raw:
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
            else:
                name = str(item).strip()
            if name:
                out.append(name)
        return out
    if isinstance(raw, str) and raw.strip():
        return [a.strip() for a in raw.split(";") if a.strip()]
    return []


class PaperHandler(RefHandler):
    """Handler for paper: scheme — read-only with notes.

    Extends RefHandler with paper-specific views:
      /abstract, /cite (bib/ris/acs), /fig, /page
    """

    scheme = "paper"
    writable = False
    corpus_id = "papers"
    # Paper has a real onboarding skill — wire it so ``paper:/help``
    # serves the skill body instead of returning VIEW_UNKNOWN.  The
    # skill already exists at ``src/precis/skills/paper-structural-
    # navigation/SKILL.md``; the only thing missing was this attribute.
    # Without the declaration, ``/help`` appeared in the
    # ``options=`` list of every paper-view error but every probe of
    # it 404'd \u2014 the recovery hint pointed at a dead view
    # (mcp-critic finding M4).
    onboarding_skill: ClassVar[str | None] = "paper-structural-navigation"
    views = {
        **RefHandler.views,
        "abstract": "_read_abstract_view",
        "cite": "_read_cite_view",
        "cites": "_read_cites_view",
        "cited-by": "_read_cited_by_view",
        "fig": "_read_fig_view",
        "page": "_read_page_view",
        # /notes — read back notes created via put(note='...').  The
        # write path lived in :func:`precis.tools._create_note` since
        # day one, but the read surface was missing entirely; agents
        # could create notes that no one could find again from the
        # MCP (mcp-critic finding N2).  Implementation pulls from
        # ``store.get_notes(ref_id=...)``; rendering keeps the same
        # shape as the create response so an agent's write\u2192read loop
        # round-trips cleanly.
        "notes": "_read_notes_view",
    }
    extensions: set[str] = set()

    _ref_noun = "paper"
    _ref_emoji = "📄"

    # ── View dispatchers ─────────────────────────────────────────────

    def _read_abstract_view(self, store, ref, selector, subview, **kwargs) -> str:
        extract_kwargs(kwargs, (), context="paper/abstract")
        return self._read_abstract(store, ref)

    def _read_cite_view(self, store, ref, selector, subview, **kwargs) -> str:
        extract_kwargs(kwargs, (), context="paper/cite")
        return self._read_citation(ref, subview or "bib")

    def _read_cites_view(self, store, ref, selector, subview, **kwargs) -> str:
        extract_kwargs(kwargs, (), context="paper/cites")
        limit, offset = self._parse_s2_pagination(subview, ref, "cites")
        return self._read_s2_graph(
            ref, direction="references", limit=limit, offset=offset
        )

    def _read_cited_by_view(self, store, ref, selector, subview, **kwargs) -> str:
        extract_kwargs(kwargs, (), context="paper/cited-by")
        limit, offset = self._parse_s2_pagination(subview, ref, "cited-by")
        return self._read_s2_graph(
            ref, direction="cited_by", limit=limit, offset=offset
        )

    def _read_fig_view(self, store, ref, selector, subview, **kwargs) -> str:
        extract_kwargs(kwargs, (), context="paper/fig")
        return self._read_figures(store, ref, subview)

    def _read_page_view(self, store, ref, selector, subview, **kwargs) -> str:
        extract_kwargs(kwargs, (), context="paper/page")
        return self._read_page(store, ref, selector)

    def _read_notes_view(self, store, ref, selector, subview, **kwargs) -> str:
        """List notes attached to this paper (ref- and block-level).

        Read-side counterpart to ``put(id='paper:<slug>',
        note='...')``.  Pulls every note for the ref via
        :meth:`store.get_notes` (ordered newest\u2192oldest), groups
        ref-level notes first, then per-block, and emits a structured
        listing with the create-time hint for round-trips.

        Empty case is handled by the ID_NOT_FOUND-shaped trailer (no
        notes is not an error \u2014 it's the default state for any new
        paper).  mcp-critic finding N2.
        """
        extract_kwargs(kwargs, (), context="paper/notes")
        slug = ref.get("slug") or "???"
        ref_id = ref.get("ref_id") or ref.get("id")
        notes = store.get_notes(ref_id=ref_id) if ref_id is not None else []
        if not notes:
            return (
                f"📄 {slug} \u2014 no notes yet.\n\n"
                "Create one:\n"
                f"  put(id='paper:{slug}', note='...')              \u2014 ref-level\n"
                f"  put(id='paper:{slug}{SEP}<block>', note='...')   \u2014 block-level"
            )

        ref_notes = [n for n in notes if not n.get("block_node_id")]
        block_notes = [n for n in notes if n.get("block_node_id")]

        lines = [f"📝 {slug} \u2014 {_pluralise(len(notes), 'note')}", ""]
        if ref_notes:
            lines.append(f"## Ref-level ({len(ref_notes)})")
            lines.append("")
            for n in ref_notes:
                title = n.get("title") or ""
                created = (n.get("created_at") or "")[:19]
                origin = n.get("origin") or ""
                head = f"#{n.get('id')}"
                if title:
                    head += f"  {title}"
                if created:
                    head += f"  ({created}"
                    if origin:
                        head += f", {origin}"
                    head += ")"
                lines.append(head)
                content = (n.get("content") or "").strip()
                if content:
                    for cl in content.splitlines():
                        lines.append(f"  {cl}")
                tags = n.get("tags")
                if tags:
                    lines.append(f"  tags: {', '.join(tags) if isinstance(tags, list) else tags}")
                lines.append("")

        if block_notes:
            lines.append(f"## Block-level ({len(block_notes)})")
            lines.append("")
            # Group by block_node_id so the agent can find related
            # notes alongside their target chunk.
            by_block: dict[str, list[dict]] = {}
            for n in block_notes:
                by_block.setdefault(n.get("block_node_id", ""), []).append(n)
            for node_id, group in by_block.items():
                lines.append(f"block {node_id}:")
                for n in group:
                    head = f"  #{n.get('id')}"
                    created = (n.get("created_at") or "")[:19]
                    if created:
                        head += f"  ({created})"
                    lines.append(head)
                    content = (n.get("content") or "").strip()
                    if content:
                        for cl in content.splitlines():
                            lines.append(f"    {cl}")
                lines.append("")

        lines.append("Next:")
        lines.append(
            f"  put(id='paper:{slug}', note='...')          "
            "\u2014 add ref-level note"
        )
        lines.append(
            f"  put(id='paper:{slug}{SEP}<block>', note='...') "
            "\u2014 add block-level note"
        )
        return "\n".join(lines)

    def _read_overview(self, store, ref: dict) -> str:
        slug = ref.get("slug") or "???"
        title = ref.get("title") or ""
        # BUG-D fix — route ``authors`` through ``_author_names`` so the
        # landing-page header stays in sync with the cite formatters.
        # Without this, papers with JSON-encoded author arrays (e.g.
        # ``marquessilva1999grasp``) render a literal
        # ``[{"name": "Marques-Silva, J.P."}, …]`` in the header.
        authors_list = _author_names(ref.get("authors"))
        authors = "; ".join(authors_list)
        year = ref.get("year") or ""
        journal = ref.get("journal") or ""
        doi = ref.get("doi") or ""

        abstract_blocks = store.get_blocks(slug, block_type="abstract")
        # Run abstract through _clean_jats so the overview's preview
        # paragraph doesn't leak ``<jats:title>...<jats:p>`` markup into
        # downstream prompts.  Review 2026-04-25 finding B4.
        abstract = (
            _clean_jats(abstract_blocks[0]["text"]) if abstract_blocks else ""
        )

        all_blocks = store.get_blocks(slug)
        n_blocks = len(all_blocks)
        page_count = max((b.get("page") or 0) for b in all_blocks) if all_blocks else 0

        lines = [f"📄 {slug}"]
        lines.append(f"  {title}")
        if authors:
            lines.append(f"  {authors}")
        if journal or year:
            lines.append(f"  {journal} ({year})" if journal else f"  ({year})")
        if doi:
            lines.append(f"  doi:{doi}")
        lines.append(f"  {n_blocks} blocks, {page_count} pages")
        if abstract:
            lines.append("")
            lines.append(abstract[:500])
        lines.append("")
        # Link count hint
        try:
            link_counts = store.get_link_count(slug)
            if link_counts:
                total = sum(link_counts.values())
                lines.append(f"  {total} links")
        except Exception:
            pass

        lines.append("")
        lines.append("Next:")
        lines.append(f"  get(id='{slug}/toc')  — structure")
        lines.append(f"  get(id='{slug}{SEP}0..10')  — first 10 chunks")
        lines.append(f"  get(id='{slug}/cite/bib')  — BibTeX citation")
        lines.append(f"  get(id='{slug}/summary')  — paper summary")
        lines.append(f"  get(id='{slug}/links')  — links graph")
        lines.append(f"  get(id='{slug}/cites')  — outgoing references (S2)")
        lines.append(f"  get(id='{slug}/cited-by')  — incoming citations (S2)")
        lines.append(f"  Cite in docs: [@{slug}]")
        return "\n".join(lines)

    def _read_meta(self, ref: dict) -> str:
        lines = []
        for key in (
            "slug",
            "title",
            "authors",
            "year",
            "journal",
            "doi",
            "volume",
            "pages",
            "issn",
        ):
            val = ref.get(key, "")
            if val:
                lines.append(f"  {key}: {val}")
        ref_id = ref.get("ref_id") or ref.get("id")
        if ref_id:
            lines.append(f"  ref_id: {ref_id}")
        retracted = ref.get("retracted", False)
        if retracted:
            lines.append(f"  ⚠ RETRACTED: {ref.get('retraction_note', '')}")
        return "\n".join(lines)

    def _list_header(self, count: int, grep: str = "") -> str:
        if grep:
            return f"📚 {count} papers matching '{grep}'"
        return f"📚 {count} papers in library"

    def _list_entry(self, ref: dict) -> str:
        # Every field coerced via ``or ""`` — partially-ingested refs
        # may carry None values even for declared keys.  See BUG-A
        # regression coverage in ``test_paper_handler.py``
        # (``TestListRendererTolerateNones``).
        slug = ref.get("slug") or "???"
        title = _truncate(ref.get("title") or "", 80)
        year = ref.get("year") or ""
        doi = ref.get("doi") or ""
        first_author = first_author_surname(ref.get("authors") or "")
        parts = [f"  {slug}  {year}"]
        if first_author:
            parts.append(first_author)
        parts.append(title)
        line = "  ".join(parts)
        if doi:
            line += f"  doi:{doi}"
        return line

    def _overview_hints(self, slug: str, ref: dict) -> list[str]:
        return [
            f"get(id='{slug}/cite/bib')  — BibTeX citation",
            f"Cite in docs: [@{slug}]",
        ]

    # ── Paper-specific views ─────────────────────────────────────────

    def _read_abstract(self, store, ref: dict) -> str:
        slug = ref.get("slug", "???")
        blocks = store.get_blocks(slug, block_type="abstract")
        if not blocks:
            return f"No abstract available for {slug}"
        # CrossRef abstracts ship with JATS XML markup (sub/sup tags,
        # <jats:title>, <jats:p>, etc.).  Strip / convert before handing
        # the text to the agent — otherwise paper drafts and prompts
        # built off ``/abstract`` carry the markup verbatim.  Review
        # 2026-04-25 finding B4.
        return _clean_jats(blocks[0].get("text", ""))

    def _read_citation(self, ref: dict, style: str) -> str:
        slug = ref.get("slug", "???")
        raw_title = ref.get("title", "")
        raw_authors = ref.get("authors", "")
        raw_journal = ref.get("journal", "")
        raw_doi = ref.get("doi", "")
        raw_year = ref.get("year", "")

        # Normalised author list for both BibTeX ("X and Y") and RIS
        # (one ``AU  -`` line per author).  Empty list if no authors.
        authors = _author_names(raw_authors)

        if style == "bib":
            # Apply the full title sanitiser (HTML strip + whitespace
            # collapse + BibTeX escape) and the lighter escaper to
            # every other string field so backslash-reserved chars don't
            # break the emitted ``.bib``.
            title = _clean_title(raw_title)
            journal = _bibtex_escape(raw_journal)
            doi = _bibtex_escape(raw_doi)
            year = _bibtex_escape(raw_year)
            bib_authors = " and ".join(_bibtex_escape(n) for n in authors)

            entry = f"@article{{{slug},\n"
            if title:
                entry += f"  title = {{{title}}},\n"
            if bib_authors:
                # BibTeX authors: joined with " and ".  Each name keeps
                # its original ``Last, First`` ordering (when present)
                # so BibTeX's name-parsing grammar recovers the
                # surname/givenname split unambiguously.
                entry += f"  author = {{{bib_authors}}},\n"
            if year:
                entry += f"  year = {{{year}}},\n"
            if journal:
                entry += f"  journal = {{{journal}}},\n"
            if doi:
                entry += f"  doi = {{{doi}}},\n"
            entry += "}\n"
            return entry
        elif style == "ris":
            # RIS fields are plain UTF-8 text with no backslash-escape
            # dialect — skip the BibTeX-specific translation but still
            # HTML-decode, tag-strip (for titles) and whitespace-collapse
            # to keep the output machine-parseable.
            def _ris_clean(raw: object, *, strip_tags: bool) -> str:
                text = str(raw or "").strip()
                if not text:
                    return ""
                text = html.unescape(text)
                if strip_tags:
                    text = _HTML_TAG_RE.sub("", text)
                return _WHITESPACE_RE.sub(" ", text).strip()

            lines = ["TY  - JOUR"]
            title = _ris_clean(raw_title, strip_tags=True)
            if title:
                lines.append(f"TI  - {title}")
            for name in authors:
                cleaned = _ris_clean(name, strip_tags=False)
                if cleaned:
                    lines.append(f"AU  - {cleaned}")
            if raw_year:
                lines.append(f"PY  - {raw_year}")
            journal = _ris_clean(raw_journal, strip_tags=False)
            if journal:
                lines.append(f"JO  - {journal}")
            if raw_doi:
                lines.append(f"DO  - {raw_doi}")
            lines.append("ER  - ")
            return "\n".join(lines)
        else:
            # ACS-style inline — whitespace-collapse the journal so a
            # multi-line source doesn't spill into the output.
            if authors and raw_year:
                first_author = first_author_surname(raw_authors)
                journal = _WHITESPACE_RE.sub(" ", str(raw_journal or "")).strip()
                return f"{first_author} et al., {journal} {raw_year}".strip()
            return slug

    # ── Figures ──────────────────────────────────────────────────────────

    _FIGURES_DIR = "figures"

    def _read_figures(self, store, ref: dict, subview: str | None = None) -> str:
        """Dispatch figure views.

        subview forms:
            None          → list all figures
            "3"           → overview of figure 3 (legend + hints)
            "3/legend"    → caption/legend text only
            "3/image"     → base64-encoded image
            "3/image/export" → export to ./figures/<slug>_fig<N>.<ext>
        """
        slug = ref.get("slug", "???")

        if not subview:
            return self._list_figures(store, slug)

        parts = subview.split("/")
        raw_num = parts[0]
        # Figure number must be a non-negative integer.  Both ``abc``
        # (non-numeric) and ``-1`` / ``0`` (out-of-range) used to take
        # different code paths (one structured error, two prose lines)
        # — review 2026-04-25 finding B5/D4.  All three now route
        # through the same ``_figure_not_found`` helper which produces
        # a consistent ``ERROR [<code>]`` envelope with the available
        # list folded into the ``next:`` hint.
        # ``_resolved_figs`` (B7) re-binds API ``fig_num`` to printed
        # figure numbers when the extractor missed the caption, so the
        # available-list hint matches what the caller will see in the
        # listing view.
        figs = self._resolved_figs(store, slug)
        try:
            fig_num = int(raw_num)
        except ValueError:
            raise self._figure_not_found(
                slug=slug,
                figs=figs,
                requested=raw_num,
                code=ErrorCode.ID_MALFORMED,
            ) from None
        if fig_num < 1:
            raise self._figure_not_found(
                slug=slug,
                figs=figs,
                requested=raw_num,
                code=ErrorCode.ID_NOT_FOUND,
            )

        aspect = "/".join(parts[1:]) if len(parts) > 1 else ""

        if aspect == "":
            return self._figure_overview(store, slug, fig_num, figs=figs)
        elif aspect == "legend":
            return self._figure_legend(store, slug, fig_num, figs=figs)
        elif aspect == "image":
            return self._figure_image(store, slug, fig_num, figs=figs)
        elif aspect == "image/export":
            return self._figure_export(store, slug, fig_num)
        else:
            raise PrecisError(
                ErrorCode.VIEW_UNKNOWN,
                cause=f"unknown figure aspect: {aspect!r}",
                options=[
                    "/fig/N",
                    "/fig/N/legend",
                    "/fig/N/image",
                    "/fig/N/image/export",
                ],
            )

    def _list_figures(self, store, slug: str) -> str:
        # Use _resolved_figs so any caption rescued from an adjacent
        # text block (B7) carries through to both the displayed caption
        # AND the API ``fig_num`` — without re-binding, ``/fig/3`` lists
        # a caption beginning ``Figure 4.`` and the citation key (3)
        # disagrees with the printed label (4).  Review 2026-04-25
        # mcp-critic finding B7 (figure-number mismatch).
        figs = self._resolved_figs(store, slug)
        if not figs:
            return f"No figures found for {slug}"
        lines = [f"📊 {slug} — {len(figs)} figure(s)", ""]
        for fig in figs:
            n = fig["fig_num"]
            page = fig.get("page", "")
            caption_body = _caption_body(fig.get("caption", ""))
            lines.append(f"  fig {n}  p{page}  {_truncate(caption_body, 100)}")
        lines.append("")
        lines.append("Next:")
        # Listing trailer can't promise a caption for an unknown figure
        # — paper figure-extractors miss captions for ~5–10 % of figs.
        # Hedge with "when present" so an agent doesn't trust a fixed
        # promise and fabricate a description (mcp-critic finding M6).
        # Build (call, description) tuples and pad programmatically so
        # the column alignment stays correct regardless of slug length.
        next_calls: list[tuple[str, str]] = [
            (f"get(id='{slug}/fig/1')",              "overview + caption"),
            (f"get(id='{slug}/fig/1/legend')",       "caption text"),
            (f"get(id='{slug}/fig/1/image')",        "encoded image (+ caption when present)"),
            (f"get(id='{slug}/fig/1/image/export')", "save to ./figures/"),
        ]
        lines.extend(_format_next_block(next_calls))
        return "\n".join(lines)

    def _figure_overview(
        self, store, slug: str, fig_num: int, *, figs: list | None = None
    ) -> str:
        if figs is None:
            figs = self._resolved_figs(store, slug)
        fig = next((f for f in figs if f["fig_num"] == fig_num), None)
        if not fig:
            raise self._figure_not_found(
                slug=slug, figs=figs, requested=str(fig_num),
                code=ErrorCode.ID_NOT_FOUND,
            )
        # Caption already resolved in ``_resolved_figs`` (B7).  Strip the
        # leading ``Figure N.`` label so it doesn't duplicate with the
        # bold marker we add below.
        caption = _caption_body(fig.get("caption", ""))
        page = fig.get("page", "")
        # Trailer reflects actual caption resolution so a downstream
        # agent reading only the trailer knows whether the /image view
        # will carry the legend or just the bytes.  Without this, the
        # static "+ caption" promise misled callers into fabricating
        # descriptions for caption-less figures (mcp-critic finding M6).
        image_desc = (
            "encoded image + caption" if caption else "encoded image (caption: missing)"
        )
        next_calls: list[tuple[str, str]] = [
            (f"get(id='{slug}/fig/{fig_num}/legend')",       "caption text"),
            (f"get(id='{slug}/fig/{fig_num}/image')",        image_desc),
            (f"get(id='{slug}/fig/{fig_num}/image/export')", "save to ./figures/"),
        ]
        lines = [
            f"📊 {slug} fig {fig_num}  (page {page})",
            "",
            f"**Figure {fig_num}.** {caption}" if caption else "[no caption]",
            "",
            "Next:",
        ]
        lines.extend(_format_next_block(next_calls))
        return "\n".join(lines)

    def _figure_legend(
        self, store, slug: str, fig_num: int, *, figs: list | None = None
    ) -> str:
        if figs is None:
            figs = self._resolved_figs(store, slug)
        fig = next((f for f in figs if f["fig_num"] == fig_num), None)
        if not fig:
            raise self._figure_not_found(
                slug=slug, figs=figs, requested=str(fig_num),
                code=ErrorCode.ID_NOT_FOUND,
            )
        # B7 — caption resolution happens in ``_resolved_figs`` so the
        # legend view is just a passthrough.  We keep the full label
        # (``Figure 3. (a) Electronic band structure...``) here because
        # the legend view is consumed standalone — the bold marker
        # only adds noise without an image alongside.
        caption = fig.get("caption", "")
        return caption if caption else f"[no caption for {slug} fig {fig_num}]"

    def _figure_image(
        self, store, slug: str, fig_num: int, *, figs: list | None = None
    ) -> str:
        # ``_resolved_figs`` may re-bind the API ``fig_num`` to the
        # printed figure number when the figure-extractor missed the
        # caption (B7 finding #2).  Look up the resolved metadata first
        # so the store-bundle lookup uses whatever number actually
        # carries the image bytes.
        if figs is None:
            figs = self._resolved_figs(store, slug)
        meta = next((f for f in figs if f["fig_num"] == fig_num), None)
        store_fig_num = meta.get("_orig_fig_num", fig_num) if meta else fig_num
        result = store.get_figure_image(slug, store_fig_num)
        if not result:
            return (
                f"No image data for {slug} fig {fig_num}.\n"
                f"The figure may not have an embedded image in the bundle.\n"
                f"Try: get(id='{slug}/fig') to list available figures."
            )
        import base64

        # B7 — caption hint above the base64 blob so an agent quoting
        # the image alone has the legend right there.  Resolved-figs
        # caption wins; fall back to whatever the bundle ships with.
        page_hint = meta.get("page") if meta else None
        caption = (meta.get("caption", "") if meta else "") or result.get("caption", "")
        if not caption:
            caption = self._rescue_caption(store, slug, fig_num, page_hint)
        caption = _caption_body(caption)
        caption_short = _truncate(caption, 200) if caption else ""

        b64 = base64.b64encode(result["image_bytes"]).decode("ascii")
        mime = "image/png" if result["image_ext"] == ".png" else "image/jpeg"
        # Annotate the header line with caption-presence so an agent
        # reading only the first line of the response still knows
        # whether the legend ships with the bytes or has to be fetched
        # separately (mcp-critic finding M6).
        cap_status = "+ caption" if caption else "no caption"
        lines: list[str] = [
            f"📊 {slug} fig {fig_num}  ({len(result['image_bytes'])} bytes, {mime}, {cap_status})",
        ]
        if caption_short:
            lines.append("")
            lines.append(f"**Figure {fig_num}.** {caption_short}")
        lines.append("")
        lines.append(f"data:{mime};base64,{b64}")
        lines.append("")
        lines.append(
            f"Next: get(id='{slug}/fig/{fig_num}/image/export') — save to file"
        )
        if not caption:
            lines.append(
                f"      get(id='{slug}/fig/{fig_num}/legend')       — caption text "
                "(may also be missing in the source PDF)"
            )
        return "\n".join(lines)

    def _figure_export(self, store, slug: str, fig_num: int) -> str:
        result = store.get_figure_image(slug, fig_num)
        if not result:
            return (
                f"No image data for {slug} fig {fig_num}.\n"
                f"Try: get(id='{slug}/fig') to list available figures."
            )
        out_dir = Path(self._FIGURES_DIR)
        out_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{slug}_fig{fig_num}{result['image_ext']}"
        out_path = out_dir / filename
        out_path.write_bytes(result["image_bytes"])
        return (
            f"✓ Exported {slug} fig {fig_num} → {out_path}\n"
            f"  {len(result['image_bytes'])} bytes, {result['image_ext']}\n"
            f"  Caption: {_truncate(result.get('caption', ''), 120)}"
        )

    @staticmethod
    def _rescue_caption(
        store, slug: str, fig_num: int, page: int | None
    ) -> str:
        """Find a ``Figure N. …`` text block when the metadata caption is empty.

        The figure extractor sometimes drops the legend (it lives in a
        plain text block instead of being paired into the figures
        table).  Walk the body blocks looking for one whose first line
        matches ``Figure <N>`` / ``Fig. <N>`` / ``Scheme <N>``.  If a
        ``page`` is known, prefer a hit on the same page; otherwise
        return the first match.  Returns ``""`` when nothing matches.

        Review: 2026-04-25 mcp-critic finding B7.
        """
        try:
            blocks = store.get_blocks(slug)
        except Exception:  # store can be flaky on partial ingests
            return ""
        candidates: list[dict] = []
        for block in blocks:
            text = (block.get("text") or "").lstrip()
            if not text:
                continue
            match = _CAPTION_PATTERNS["fig"].match(text)
            if not match:
                continue
            try:
                if int(match.group(1)) != fig_num:
                    continue
            except (ValueError, TypeError):
                continue
            candidates.append(block)
        if not candidates:
            return ""
        if page is not None:
            for block in candidates:
                if block.get("page") == page:
                    return (block.get("text") or "").strip()
        return (candidates[0].get("text") or "").strip()

    def _block_chunk_hint(self, store, slug: str, block: dict) -> str:
        """Override: hint at ``/fig/N`` for empty figure-type chunks.

        When a figure block carries no caption text, the chunk view
        used to render as ``>> slug ~38  p6  [figure]\\n\\n\\n`` —
        three lines, none of them actionable.  This method finds the
        figure's API number (rebound via ``_resolved_figs`` for B7
        compliance) and emits a single ``→ get(id='<slug>/fig/<N>')``
        line so the caller knows where the binary lives.

        Returns "" when the block isn't a figure or no fig_num maps
        back to its block_index.  Review 2026-04-25 mcp-critic
        finding B5.
        """
        if (block.get("block_type") or "text") != "figure":
            return ""
        block_idx = block.get("block_index")
        if block_idx is None:
            return ""
        try:
            figs = self._resolved_figs(store, slug)
        except Exception:
            return ""
        # Match by either current block_index (figure-type blocks) or
        # the original auto-numbered fig that rescued from this block.
        for fig in figs:
            fig_block_idx = fig.get("block_index")
            if fig_block_idx == block_idx:
                return f"→ get(id='{slug}/fig/{fig['fig_num']}')  — figure image + caption"
        return ""

    @classmethod
    def _resolved_figs(cls, store, slug: str) -> list[dict]:
        """Return figures with rescued captions and re-bound API numbers.

        ``store.get_figures`` parses the figure number from the figure
        block's own caption text (e.g. "Figure 4. ATR-SEIRAS …").
        When extraction lost the caption, the block has ``caption=""``
        and the store falls back to assigning sequential auto numbers
        starting from the smallest unused integer.

        That fallback is the source of the B7 figure-number mismatch:
        the API exposes ``/fig/3`` (auto-assigned) while the printed
        figure adjacent in body text is "Figure 4."  An agent that
        sees "Figure 4" in a body chunk and tries ``get(id='…/fig/4')``
        lands on the wrong (or no) figure.

        This helper post-processes the store's output: for every fig
        whose caption is empty, it scans the next 1..5 body blocks for
        a "Figure N. …" caption and, if found, re-binds ``fig_num`` to
        N and uses the body text as the caption.  The original number
        is preserved in ``_orig_fig_num`` so the bundle-side image
        lookup can still find the bytes.

        Duplicate-N collisions after re-binding fall back to the
        original auto number for the loser, keeping the API enum
        unique.  Review 2026-04-25 mcp-critic finding B7.
        """
        figs = list(store.get_figures(slug))
        if not figs:
            return figs
        # Figure blocks that already had a parseable caption: keep
        # both their fig_num and caption.  Only the caption-less ones
        # are candidates for re-binding.
        if all(f.get("caption") for f in figs):
            return figs
        try:
            blocks = list(store.get_blocks(slug))
        except Exception:
            return figs
        text_blocks = [
            b for b in blocks
            if (b.get("block_type") or "text") == "text"
            and b.get("block_index") is not None
            and (b.get("text") or "").strip()
        ]
        text_blocks.sort(key=lambda b: b["block_index"])
        # Pre-compute (block_index → printed_n, text) for blocks whose
        # first line begins with a "Figure N." label.
        labelled: list[tuple[int, int, str]] = []
        for b in text_blocks:
            text = (b.get("text") or "").lstrip()
            m = _CAPTION_PATTERNS["fig"].match(text)
            if not m:
                continue
            try:
                n = int(m.group(1))
            except (ValueError, TypeError):
                continue
            labelled.append((b["block_index"], n, (b.get("text") or "").strip()))
        if not labelled:
            return figs
        used_nums = {f["fig_num"] for f in figs if f.get("caption")}
        out: list[dict] = []
        for fig in figs:
            if fig.get("caption"):
                out.append(fig)
                continue
            fig_block_idx = fig.get("block_index")
            candidate: tuple[int, int, str] | None = None
            if fig_block_idx is not None:
                # Closest forward labelled block within 5 indices wins —
                # PDF extractors emit "Figure N. caption" right after
                # the figure block in 90 %+ of papers.
                for idx, n, txt in labelled:
                    delta = idx - fig_block_idx
                    if 0 < delta <= 5 and n not in used_nums:
                        candidate = (idx, n, txt)
                        break
            if candidate is None:
                # Fallback: same-numbered "Figure <fig_num>." anywhere
                # in body blocks, optionally on the same page.  This
                # is the legacy ``_rescue_caption`` path — keep it
                # alive so figures whose ``block_index`` is unknown
                # still get their caption paired.
                rescued = cls._rescue_caption(
                    store, slug, fig["fig_num"], fig.get("page")
                )
                if rescued:
                    new_fig = dict(fig)
                    new_fig["caption"] = rescued
                    out.append(new_fig)
                else:
                    out.append(fig)
                continue
            _, n, caption_text = candidate
            new_fig = dict(fig)
            new_fig["_orig_fig_num"] = fig.get("fig_num")
            new_fig["fig_num"] = n
            new_fig["caption"] = caption_text
            used_nums.add(n)
            out.append(new_fig)
        out.sort(key=lambda f: f.get("fig_num", 0))
        return out

    @staticmethod
    def _figure_not_found(
        *,
        slug: str,
        figs: list,
        requested: str,
        code: ErrorCode,
    ) -> PrecisError:
        """Build the unified figure-not-found / malformed envelope.

        Review 2026-04-25 finding B5/D4 — ``fig/0``, ``fig/-1`` and
        ``fig/abc`` used to produce three different error shapes (two
        prose lines plus one structured envelope).  All three now
        route through this helper which:

        - Folds the available-figure list into ``next:`` so the
          recovery hint survives in the structured envelope.
        - Picks ``ID_MALFORMED`` for non-numeric input and
          ``ID_NOT_FOUND`` for valid integers that don't match any
          figure.
        - Ensures the caller always sees ``ERROR [<code>]: … / next:
          …`` rather than free-form prose.
        """
        available = ", ".join(str(f["fig_num"]) for f in figs) or "none"
        if code is ErrorCode.ID_MALFORMED:
            cause = f"invalid figure number: {requested!r}"
        else:
            cause = f"figure {requested} not found for {slug}"
        next_hint = (
            f"available: {available} — "
            f"get(id='{slug}/fig') to list figures"
        )
        return PrecisError(code, cause=cause, next=next_hint)

    # ── Semantic Scholar graph ────────────────────────────────────

    #: Default cap on /cites and /cited-by responses.  Heavily-cited
    #: papers (>50 references) used to dump everything in one shot,
    #: blowing the agent's context window with 3 k+ tokens of metadata
    #: and offering no pagination knob.  Review 2026-04-25 mcp-critic
    #: finding B6.
    _S2_PAGE_DEFAULT = 20

    def _parse_s2_pagination(
        self, subview: str | None, ref: dict, view: str
    ) -> tuple[int | None, int]:
        """Parse the ``/cites`` / ``/cited-by`` subview into limit + offset.

        Accepted forms (``view`` is ``cites`` or ``cited-by``)::

            <view>          → limit=20, offset=0
            <view>/all      → limit=None (no cap), offset=0
            <view>/<N>      → limit=20, offset=N (pagination by offset)

        Returns ``(limit, offset)``.  ``limit=None`` means no cap.

        Invalid offsets raise ``PARAM_INVALID`` with a recovery hint.
        """
        if not subview:
            return self._S2_PAGE_DEFAULT, 0
        slug = ref.get("slug", "???")
        if subview == "all":
            return None, 0
        try:
            offset = int(subview)
        except ValueError:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause=(
                    f"unrecognised /{view} subview {subview!r} — expected an "
                    "integer offset or 'all'"
                ),
                next=(
                    f"get(id='{slug}/{view}')      — first {self._S2_PAGE_DEFAULT}\n"
                    f"  get(id='{slug}/{view}/{self._S2_PAGE_DEFAULT}')   — next page\n"
                    f"  get(id='{slug}/{view}/all') — full list"
                ),
            ) from None
        if offset < 0:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause=f"/{view}/{offset} — offset must be non-negative",
                next=f"get(id='{slug}/{view}/0') for the first page",
            )
        return self._S2_PAGE_DEFAULT, offset

    @staticmethod
    def _get_s2_identifier(ref: dict) -> str | None:
        """Return the best S2-compatible identifier for a ref."""
        doi = ref.get("doi")
        if doi:
            return f"DOI:{doi}"
        s2_id = ref.get("s2_id")
        if s2_id:
            return s2_id
        arxiv_id = ref.get("arxiv_id")
        if arxiv_id:
            return f"ARXIV:{arxiv_id}"
        return None

    def _read_s2_graph(
        self,
        ref: dict,
        direction: str,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> str:
        """Fetch cites or cited-by from Semantic Scholar on demand.

        ``limit=None`` returns every paper; an integer caps the
        response size.  Pagination is offset-based — the trailer
        emits the next-page URI when more results remain.  Review
        2026-04-25 mcp-critic finding B6.
        """
        slug = ref.get("slug", "???")
        view = "cites" if direction == "references" else "cited-by"
        s2_id = self._get_s2_identifier(ref)
        if not s2_id:
            return (
                f"No DOI, S2 ID, or arXiv ID for {slug} — cannot query Semantic Scholar.\n"
                f"Hint: get(id='{slug}/meta') to check available identifiers."
            )

        try:
            from acatome_meta.citations import citations
        except ImportError:
            return (
                "acatome-meta is not installed.\n"
                'Install with: pip install "precis-mcp[paper]"'
            )

        try:
            result = citations(s2_id)
        except Exception as e:
            return f"Semantic Scholar lookup failed for {slug}: {e}"

        all_papers = result.get(direction, [])
        label = "references" if direction == "references" else "citing papers"
        emoji = "📖" if direction == "references" else "📣"
        total = len(all_papers)

        if not all_papers:
            other = "cited-by" if direction == "references" else "cites"
            return (
                f"{emoji} {slug} — 0 {label} found on Semantic Scholar.\n"
                f"\nNext:\n"
                f"  get(id='{slug}/{other}')  — try the other direction"
            )

        if offset >= total:
            return (
                f"{emoji} {slug} — offset {offset} is past the end "
                f"({total} {label} total).\n"
                f"Next: get(id='{slug}/{view}/0') for the first page"
            )

        # Slice for pagination.  ``limit=None`` means /all → keep all.
        end = total if limit is None else min(offset + limit, total)
        papers = all_papers[offset:end]

        showing_clause = (
            f"{total} {label}"
            if limit is None or total <= limit
            else f"{label} {offset + 1}–{end} of {total}"
        )
        lines = [
            f"{emoji} {slug} — {showing_clause} (via Semantic Scholar)",
            "",
        ]
        for p in papers:
            title = _truncate(p.get("title", ""), 80)
            year = p.get("year", "")
            doi = p.get("doi", "")
            s2 = p.get("s2_id", "")
            id_str = f"doi:{doi}" if doi else (f"s2:{s2}" if s2 else "")
            lines.append(f"  {year or '?'}  {title}")
            if id_str:
                lines.append(f"        {id_str}")

        other = "cited-by" if direction == "references" else "cites"
        lines.append("")
        lines.append("Next:")
        # Pagination trailer — only when we actually capped.  Without
        # this the caller has no way to fetch the long tail of a
        # 50-citation paper; with it, every paginated response carries
        # the literal next URI to copy.
        if limit is not None and end < total:
            lines.append(
                f"  get(id='{slug}/{view}/{end}')  — next {min(limit, total - end)} of {total}"
            )
            lines.append(
                f"  get(id='{slug}/{view}/all')  — full list ({total} entries)"
            )
        lines.append(
            f"  get(id='{slug}/{other}')  — {('incoming citations' if direction == 'references' else 'outgoing references')}"
        )
        lines.append("  search(query='<keyword>')  — find related papers in library")
        return "\n".join(lines)

    def _read_page(self, store, ref: dict, selector: str | None) -> str:
        slug = ref.get("slug", "???")
        if not selector:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause="page number required",
                next=f"get(id='{slug}{SEP}3/page')",
            )
        try:
            page_num = int(selector)
        except ValueError as exc:
            raise PrecisError(
                ErrorCode.ID_MALFORMED,
                cause=f"invalid page number: {selector!r}",
            ) from exc
        all_blocks = store.get_blocks(slug)
        page_blocks = [b for b in all_blocks if b.get("page") == page_num]
        if not page_blocks:
            return f"No blocks on page {page_num} of {slug}"
        lines = [f"📄 {slug}  page {page_num}  ({len(page_blocks)} blocks)", ""]
        for block in page_blocks:
            idx = block.get("block_index", "?")
            kind = block.get("block_type", "text")
            text = block.get("text", "")
            lines.append(f">> {slug} {SEP}{idx}  [{kind}]")
            lines.append(text)
            lines.append("")
        return "\n".join(lines)
