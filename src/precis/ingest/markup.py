"""Structured full-text producers — JATS / arXiv HTML / Elsevier XML / LaTeX.

Markup-first ingest (see ``docs/design/markup-first-ingest.md``): when a
publisher serves structured full text over an API, we chunk *that*
instead of OCR'ing the PDF. Marker is never invoked; the PDF is kept
only as the printable.

This module owns the format-specific parsing and is intentionally
**pure** — ``lxml`` + the text chunker, no Marker/torch — so it is
host-testable without the heavy extras. Each parser emits:

* bibliographic metadata (title / authors / year / abstract / ids), and
* a list of **Marker-shaped block dicts** (``{type, text, section_path,
  page}``),

which :func:`precis.ingest.pipeline.extract_paper_from_markup` then
assembles into a :class:`~precis.ingest.db_writer.PaperToWrite` by
reusing the exact downstream the PDF path uses (``_blocks_to_chunks`` →
``_retag_references`` → ``_build_cards`` → ``write_paper``).

Failure contract: any unrecoverable parse raises
:class:`MarkupParseError`. The caller treats that as a signal to fall
back to Marker OCR on the companion PDF — markup-first must never lose a
paper we could have OCR'd. Every failure logs at ERROR with the format
and cause so stuck papers are diagnosable from the worker log.
"""

from __future__ import annotations

import logging
import re
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from precis.ingest.text_chunker import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_TABLE_CHUNK_SIZE,
    enforce_hard_max,
    split_table,
    split_text,
)

log = logging.getLogger(__name__)

#: Formats this module can parse (mirrors the sidecar's non-``pdf``
#: ``SOURCE_FORMATS``).
MARKUP_FORMATS: frozenset[str] = frozenset(
    {"jats", "elsevier_xml", "arxiv_html", "latex"}
)

#: LaTeX macro-density gate. If, after stripping comments/preamble, the
#: fraction of whitespace-delimited tokens that begin with a backslash
#: exceeds this, the source is macro-soup and a naive flatten would
#: produce garbage — bail to OCR on the companion PDF instead. This is
#: the *only* tex→OCR fallback trigger; see the design doc §2.
_LATEX_MACRO_DENSITY_MAX = 0.30


class MarkupParseError(Exception):
    """A markup source could not be parsed into usable blocks.

    Signals the caller to fall back to Marker OCR on the companion PDF.
    Carries the source format for log triage.
    """

    def __init__(self, message: str, *, fmt: str = "") -> None:
        super().__init__(message)
        self.fmt = fmt


@dataclass
class MarkupExtraction:
    """Format-agnostic result of parsing one markup source.

    ``blocks`` are Marker-shaped dicts (``{type, text, section_path,
    page}``) ready for :func:`precis.ingest.pipeline._blocks_to_chunks`.
    """

    source_format: str
    title: str = ""
    authors: list[dict[str, Any]] = field(default_factory=list)
    year: int | None = None
    abstract: str = ""
    keywords: list[str] = field(default_factory=list)
    doi: str | None = None
    arxiv_id: str | None = None
    blocks: list[dict[str, Any]] = field(default_factory=list)
    #: Best-effort provenance URL if the caller supplied one.
    source_url: str | None = None


# ---------------------------------------------------------------------------
# Block emission — shared helper
# ---------------------------------------------------------------------------


def _norm_ws(text: str) -> str:
    """Collapse runs of whitespace to single spaces, strip ends."""
    return re.sub(r"\s+", " ", text or "").strip()


def _emit(
    blocks: list[dict[str, Any]],
    *,
    kind: str,
    text: str,
    section_path: list[str],
) -> None:
    """Append one or more size-bounded blocks of ``kind`` for ``text``.

    Prose is split with :func:`split_text`; tables with
    :func:`split_table`; everything is run through
    :func:`enforce_hard_max` so no block exceeds the embedder ceiling.
    Empty text is dropped. ``page`` is always ``None`` (markup has no
    pagination) — the pipeline tolerates that.
    """
    text = text.strip()
    if not text:
        return
    if kind == "table":
        pieces = split_table(text, DEFAULT_TABLE_CHUNK_SIZE)
    elif kind in ("figure", "caption", "equation", "heading"):
        # Structural units stay whole (only hard-capped) so a caption or
        # equation isn't fragmented across chunk boundaries.
        pieces = enforce_hard_max([text])
    else:
        pieces = enforce_hard_max(split_text(text, DEFAULT_CHUNK_SIZE))
    for piece in pieces:
        blocks.append(
            {
                "type": kind,
                "text": piece,
                "section_path": list(section_path),
                "page": None,
            }
        )


# ---------------------------------------------------------------------------
# XML profiles (JATS + Elsevier share a walker; only tag names differ)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _XmlProfile:
    """Localname sets that drive the generic XML body walker."""

    section: frozenset[str]
    title: frozenset[str]
    para: frozenset[str]
    table: frozenset[str]
    figure: frozenset[str]
    caption: frozenset[str]
    equation: frozenset[str]
    references: frozenset[str]
    skip: frozenset[str]


_JATS_PROFILE = _XmlProfile(
    section=frozenset({"sec"}),
    title=frozenset({"title"}),
    para=frozenset({"p", "list", "statement", "disp-quote"}),
    table=frozenset({"table-wrap", "table"}),
    figure=frozenset({"fig"}),
    caption=frozenset({"caption"}),
    equation=frozenset({"disp-formula"}),
    references=frozenset({"ref-list"}),
    skip=frozenset({"front", "back", "journal-meta", "article-meta"}),
)

# Elsevier's article DTD (ce: common element namespace). Localnames only.
_ELSEVIER_PROFILE = _XmlProfile(
    section=frozenset({"section"}),
    title=frozenset({"section-title"}),
    para=frozenset({"para", "list", "display"}),
    table=frozenset({"table"}),
    figure=frozenset({"figure"}),
    caption=frozenset({"caption", "simple-para"}),
    equation=frozenset({"formula", "display-formula"}),
    references=frozenset({"bibliography", "reference-list"}),
    skip=frozenset({"head", "author-group"}),
)


def _localname(el: Any) -> str:
    """Namespace-stripped local tag name, or ``""`` for comments/PIs."""
    tag = getattr(el, "tag", None)
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _text_of(el: Any) -> str:
    """All descendant text of ``el``, whitespace-normalized."""
    return _norm_ws("".join(el.itertext()))


def _walk_xml(
    el: Any,
    profile: _XmlProfile,
    section_path: list[str],
    blocks: list[dict[str, Any]],
) -> None:
    """Recursively emit blocks from an XML body subtree."""
    for child in el:
        name = _localname(child)
        if not name or name in profile.skip:
            continue
        if name in profile.references:
            # Whole subtree is the bibliography — emit as references and
            # do not recurse (each entry becomes a references chunk).
            _emit(
                blocks,
                kind="references",
                text=_text_of(child),
                section_path=section_path,
            )
            continue
        if name in profile.section:
            title = ""
            for sub in child:
                if _localname(sub) in profile.title:
                    title = _text_of(sub)
                    break
            new_path = section_path + [title] if title else list(section_path)
            _walk_xml(child, profile, new_path, blocks)
            continue
        if name in profile.title:
            # Section titles are consumed by their parent section; a
            # stray top-level title becomes a heading.
            continue
        if name in profile.table:
            _emit(blocks, kind="table", text=_text_of(child), section_path=section_path)
            continue
        if name in profile.figure:
            _emit(
                blocks, kind="figure", text=_text_of(child), section_path=section_path
            )
            continue
        if name in profile.caption:
            _emit(
                blocks, kind="caption", text=_text_of(child), section_path=section_path
            )
            continue
        if name in profile.equation:
            _emit(
                blocks, kind="equation", text=_text_of(child), section_path=section_path
            )
            continue
        if name in profile.para:
            _emit(
                blocks,
                kind="paragraph",
                text=_text_of(child),
                section_path=section_path,
            )
            continue
        # Unknown container — recurse to reach nested blocks.
        _walk_xml(child, profile, section_path, blocks)


def _find_first(root: Any, localnames: set[str]) -> Any | None:
    """Depth-first search for the first element with a matching localname."""
    for el in root.iter():
        if _localname(el) in localnames:
            return el
    return None


# ---------------------------------------------------------------------------
# JATS
# ---------------------------------------------------------------------------


def parse_jats(xml_bytes: bytes, *, source_url: str | None = None) -> MarkupExtraction:
    """Parse a JATS full-text XML document.

    Used for Europe PMC, PLOS, Springer OA, and any Crossref-TDM JATS.
    Raises :class:`MarkupParseError` when the document has no ``<body>``
    or yields no body blocks.
    """
    root = _parse_xml(xml_bytes, fmt="jats")
    ext = MarkupExtraction(source_format="jats", source_url=source_url)

    _jats_front(root, ext)

    body = _find_first(root, {"body"})
    if body is None:
        raise MarkupParseError("JATS: no <body> element", fmt="jats")
    _walk_xml(body, _JATS_PROFILE, [], ext.blocks)

    # Bibliography sometimes sits under <back>, not <body>.
    back = _find_first(root, {"back"})
    if back is not None:
        ref_list = _find_first(back, {"ref-list"})
        if ref_list is not None:
            _emit(
                ext.blocks,
                kind="references",
                text=_text_of(ref_list),
                section_path=[],
            )

    if not any(b["type"] not in ("references",) for b in ext.blocks):
        raise MarkupParseError(
            "JATS: body produced no non-reference blocks", fmt="jats"
        )
    log.info(
        "markup.parse_jats: title=%r doi=%s blocks=%d",
        ext.title[:60],
        ext.doi,
        len(ext.blocks),
    )
    return ext


def _jats_front(root: Any, ext: MarkupExtraction) -> None:
    """Fill title/authors/year/abstract/doi from the JATS <front>."""
    title_el = _find_first(root, {"article-title"})
    if title_el is not None:
        ext.title = _text_of(title_el)

    for contrib in root.iter():
        if _localname(contrib) != "contrib":
            continue
        if contrib.get("contrib-type") not in (None, "author"):
            continue
        surname = given = ""
        for sub in contrib.iter():
            ln = _localname(sub)
            if ln == "surname":
                surname = _text_of(sub)
            elif ln == "given-names":
                given = _text_of(sub)
        name = f"{surname}, {given}".strip(", ").strip()
        if name:
            ext.authors.append({"name": name})

    for aid in root.iter():
        if _localname(aid) == "article-id" and aid.get("pub-id-type") == "doi":
            ext.doi = _text_of(aid) or None
            break

    abstract_el = _find_first(root, {"abstract"})
    if abstract_el is not None:
        ext.abstract = _text_of(abstract_el)

    year = _find_first(root, {"year"})
    if year is not None:
        ext.year = _coerce_year(_text_of(year))

    for kwd in root.iter():
        if _localname(kwd) == "kwd":
            text = _text_of(kwd)
            if text:
                ext.keywords.append(text)


# ---------------------------------------------------------------------------
# Elsevier full-text XML
# ---------------------------------------------------------------------------


def parse_elsevier(
    xml_bytes: bytes, *, source_url: str | None = None
) -> MarkupExtraction:
    """Parse Elsevier's full-text XML (ce:/ja: DTD).

    Best-effort: Elsevier's schema is close enough to JATS structurally
    that the generic walker handles the body with an Elsevier tag
    profile. Metadata falls back to JATS-style probes (Elsevier embeds a
    ``<dc:title>`` / ``<prism:doi>`` header we read by localname).
    """
    root = _parse_xml(xml_bytes, fmt="elsevier_xml")
    ext = MarkupExtraction(source_format="elsevier_xml", source_url=source_url)

    title_el = _find_first(root, {"title", "dc:title"})
    if title_el is not None:
        ext.title = _text_of(title_el)
    doi_el = _find_first(root, {"doi"})
    if doi_el is not None:
        ext.doi = _text_of(doi_el) or None
    abstract_el = _find_first(root, {"abstract", "abstract-sec"})
    if abstract_el is not None:
        ext.abstract = _text_of(abstract_el)
    for author in root.iter():
        if _localname(author) != "author":
            continue
        surname = given = ""
        for sub in author.iter():
            ln = _localname(sub)
            if ln in ("surname", "last-name"):
                surname = _text_of(sub)
            elif ln in ("given-name", "first-name"):
                given = _text_of(sub)
        name = f"{surname}, {given}".strip(", ").strip()
        if name:
            ext.authors.append({"name": name})

    body = _find_first(root, {"body", "serial-item"})
    if body is None:
        raise MarkupParseError("Elsevier XML: no <body>", fmt="elsevier_xml")
    _walk_xml(body, _ELSEVIER_PROFILE, [], ext.blocks)
    if not ext.blocks:
        raise MarkupParseError("Elsevier XML: no body blocks", fmt="elsevier_xml")
    log.info(
        "markup.parse_elsevier: title=%r doi=%s blocks=%d",
        ext.title[:60],
        ext.doi,
        len(ext.blocks),
    )
    return ext


# ---------------------------------------------------------------------------
# arXiv HTML (LaTeXML output)
# ---------------------------------------------------------------------------


#: arXiv id in a source URL: modern ``2301.12345`` (optional version) or the
#: legacy ``hep-th/9901001`` scheme, under any ``/html|abs|pdf|e-print|src/``
#: path. The identity is what makes an arXiv markup ingest dedup against the
#: same paper from any other source — and, since arXiv markup is *only* reached
#: by fetching a known id, it is always recoverable here even when the document
#: body carries no DOI.
_ARXIV_URL_ID_RE = re.compile(
    r"arxiv\.org/(?:html|abs|pdf|e-print|src)/"
    r"(?P<id>(?:[a-z-]+(?:\.[A-Z]{2})?/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?)",
    re.IGNORECASE,
)


def _arxiv_id_from_url(url: str | None) -> str | None:
    """Best-effort arXiv id from a source URL (``None`` if absent/unmatched)."""
    if not url:
        return None
    m = _ARXIV_URL_ID_RE.search(url)
    return m.group("id") if m else None


def parse_arxiv_html(
    html_bytes: bytes, *, source_url: str | None = None
) -> MarkupExtraction:
    """Parse arXiv's official LaTeXML HTML (``arxiv.org/html/{id}``).

    LaTeXML emits a structured tree with ``ltx_*`` classes; we walk
    ``<section>``/``<p>``/``<figure>``/``<math>`` and the
    ``ltx_biblist`` bibliography. Structurally JATS-class.
    """
    try:
        from lxml import html as lxml_html
    except ImportError as exc:  # pragma: no cover - lxml is a dep
        raise MarkupParseError(f"lxml not available: {exc}", fmt="arxiv_html") from exc

    # Harden the parser the same way the XML path is (``_parse_xml``): the
    # bytes come off the network (arxiv.org/html/<id>), so refuse to fetch
    # any external DTD/entity (``no_network``) and cap tree growth
    # (``huge_tree=False``, the default, pinned here for intent) so a hostile
    # or malformed document can't drive an SSRF or an unbounded-tree blowup.
    parser = lxml_html.HTMLParser(no_network=True, huge_tree=False)
    try:
        root = lxml_html.fromstring(html_bytes, parser=parser)
    except Exception as exc:
        raise MarkupParseError(
            f"arXiv HTML: unparseable ({exc})", fmt="arxiv_html"
        ) from exc
    if root is None:
        raise MarkupParseError("arXiv HTML: empty document", fmt="arxiv_html")

    ext = MarkupExtraction(source_format="arxiv_html", source_url=source_url)
    ext.arxiv_id = _arxiv_id_from_url(source_url)

    title_el = _html_first(root, classes={"ltx_title_document", "ltx_title"})
    if title_el is not None:
        ext.title = _norm_ws(title_el.text_content())
    for person in _html_all(root, classes={"ltx_personname"}):
        name = _norm_ws(person.text_content())
        if name:
            ext.authors.append({"name": name})
    abstract_el = _html_first(root, classes={"ltx_abstract"})
    if abstract_el is not None:
        ext.abstract = _norm_ws(abstract_el.text_content())

    root_el = _html_first(root, tags={"article"}, classes={"ltx_document"})
    if root_el is None:
        root_el = root
    _walk_html(root_el, [], ext.blocks)

    for biblist in _html_all(root, classes={"ltx_biblist", "ltx_bibliography"}):
        _emit(
            ext.blocks,
            kind="references",
            text=_norm_ws(biblist.text_content()),
            section_path=[],
        )

    if not any(b["type"] != "references" for b in ext.blocks):
        raise MarkupParseError("arXiv HTML: no body blocks", fmt="arxiv_html")
    log.info(
        "markup.parse_arxiv_html: title=%r blocks=%d",
        ext.title[:60],
        len(ext.blocks),
    )
    return ext


_HTML_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


def _html_matches(el: Any, tags: set[str] | None, classes: set[str] | None) -> bool:
    """True iff ``el`` matches any of ``tags`` or any of ``classes``."""
    tag = getattr(el, "tag", None)
    if not isinstance(tag, str):
        return False
    if tags and tag.lower() in tags:
        return True
    if classes:
        el_classes = set((el.get("class") or "").split())
        if el_classes & classes:
            return True
    return False


def _html_first(
    root: Any, *, tags: set[str] | None = None, classes: set[str] | None = None
) -> Any | None:
    """First descendant (document order) matching ``tags``/``classes``."""
    for el in root.iter():
        if _html_matches(el, tags, classes):
            return el
    return None


def _html_all(
    root: Any, *, tags: set[str] | None = None, classes: set[str] | None = None
) -> list[Any]:
    """All descendants matching ``tags``/``classes`` (document order)."""
    return [el for el in root.iter() if _html_matches(el, tags, classes)]


def _walk_html(el: Any, section_path: list[str], blocks: list[dict[str, Any]]) -> None:
    """Walk a LaTeXML/HTML tree emitting Marker-shaped blocks."""
    for child in el:
        tag = child.tag
        if not isinstance(tag, str):
            continue
        tag = tag.lower()
        classes = set((child.get("class") or "").split())
        if "ltx_bibliography" in classes or "ltx_biblist" in classes:
            continue  # handled by the caller
        if "ltx_abstract" in classes or "ltx_authors" in classes:
            continue  # captured as metadata/cards, not a body chunk
        if tag == "section" or "ltx_section" in classes or "ltx_subsection" in classes:
            heading = _html_first(child, tags=_HTML_HEADINGS, classes={"ltx_title"})
            title = _norm_ws(heading.text_content()) if heading is not None else ""
            new_path = section_path + [title] if title else list(section_path)
            _walk_html(child, new_path, blocks)
            continue
        if tag in _HTML_HEADINGS:
            continue  # consumed as a section title
        if tag == "figure":
            _emit(
                blocks,
                kind="figure",
                text=_norm_ws(child.text_content()),
                section_path=section_path,
            )
            continue
        if tag == "table" or "ltx_tabular" in classes:
            _emit(
                blocks,
                kind="table",
                text=_norm_ws(child.text_content()),
                section_path=section_path,
            )
            continue
        if tag == "math" or "ltx_equation" in classes or "ltx_math" in classes:
            _emit(
                blocks,
                kind="equation",
                text=_norm_ws(child.text_content()),
                section_path=section_path,
            )
            continue
        if tag == "p" or "ltx_para" in classes or "ltx_p" in classes:
            _emit(
                blocks,
                kind="paragraph",
                text=_norm_ws(child.text_content()),
                section_path=section_path,
            )
            continue
        _walk_html(child, section_path, blocks)


# ---------------------------------------------------------------------------
# LaTeX (arXiv e-print tarball) — flatten-and-chunk
# ---------------------------------------------------------------------------

_TEX_COMMENT_RE = re.compile(r"(?<!\\)%.*")
_TEX_INPUT_RE = re.compile(r"\\(?:input|include)\s*\{([^}]+)\}")
_TEX_SECTION_RE = re.compile(r"\\(section|subsection|subsubsection)\*?\s*\{([^}]*)\}")
_TEX_DOCUMENTCLASS_RE = re.compile(r"\\documentclass")
_TEX_MACRO_TOKEN_RE = re.compile(r"\\[a-zA-Z@]+")


def parse_latex(path: Path, *, source_url: str | None = None) -> MarkupExtraction:
    """Flatten an arXiv LaTeX source (tarball, ``.tex``, or ``.tex.gz``).

    No structural parser: we find the main file, follow
    ``\\input``/``\\include`` from the tarball root, strip comments and
    preamble, segment on ``\\section`` for ``section_path``, and take
    references from the bundled ``.bbl`` (arXiv does not run BibTeX).
    ``anc/`` and build cruft are skipped. Raises
    :class:`MarkupParseError` on a macro-dense source (→ OCR fallback).
    """
    files, bbl_text = _read_latex_sources(path)
    if not files:
        raise MarkupParseError("LaTeX: no .tex sources found", fmt="latex")

    main = _pick_main_tex(files)
    if main is None:
        raise MarkupParseError("LaTeX: no \\documentclass file", fmt="latex")

    body = _assemble_latex_body(main, files)
    if not body.strip():
        raise MarkupParseError("LaTeX: empty document body", fmt="latex")

    _guard_macro_density(body)

    ext = MarkupExtraction(source_format="latex", source_url=source_url)
    ext.arxiv_id = _arxiv_id_from_url(source_url)
    ext.title = _latex_title(files)
    _segment_latex_body(body, ext.blocks)

    if bbl_text:
        _emit(ext.blocks, kind="references", text=_strip_tex(bbl_text), section_path=[])

    if not any(b["type"] != "references" for b in ext.blocks):
        raise MarkupParseError("LaTeX: no body blocks after flatten", fmt="latex")
    log.info(
        "markup.parse_latex: main=%s title=%r blocks=%d",
        main,
        ext.title[:60],
        len(ext.blocks),
    )
    return ext


#: Decompression-bomb rails for the LaTeX tarball reader. A real arXiv
#: source bundle is a handful of small ``.tex``/``.bbl`` files (a few MB
#: total); anything past these ceilings is a crafted bomb (or corrupt) and
#: is refused rather than expanded into memory. ``member.size`` is the
#: *declared* uncompressed size, so an oversized member is skipped before a
#: single byte is read.
_LATEX_MAX_MEMBER_BYTES = 16 * 1024 * 1024  # 16 MB per file
_LATEX_MAX_TOTAL_BYTES = 64 * 1024 * 1024  # 64 MB across all read files
_LATEX_MAX_MEMBERS = 4096  # member-count cap


def _read_latex_sources(path: Path) -> tuple[dict[str, str], str]:
    """Return ``({relpath: text}, bbl_text)`` from a tarball or bare file.

    Skips ``anc/`` (ancillary, not article text) and build cruft. The
    tarball reader is bounded by :data:`_LATEX_MAX_MEMBER_BYTES` /
    :data:`_LATEX_MAX_TOTAL_BYTES` / :data:`_LATEX_MAX_MEMBERS` so a
    decompression bomb can't OOM the fetch/watch worker (never extracts to
    disk, so no path traversal either).
    """
    files: dict[str, str] = {}
    bbl_text = ""
    suffix = path.suffix.lower()
    if suffix == ".tex":
        files[path.name] = _safe_read_text(path.read_bytes())
        bbl = path.with_suffix(".bbl")
        if bbl.is_file():
            bbl_text = _safe_read_text(bbl.read_bytes())
        return files, bbl_text

    total = 0
    seen_members = 0
    try:
        with tarfile.open(path, "r:*") as tar:
            for member in tar:
                seen_members += 1
                if seen_members > _LATEX_MAX_MEMBERS:
                    raise MarkupParseError(
                        f"LaTeX: tarball has > {_LATEX_MAX_MEMBERS} members "
                        "(decompression bomb?)",
                        fmt="latex",
                    )
                if not member.isfile():
                    continue
                name = member.name.lstrip("./")
                parts = name.split("/")
                if "anc" in parts:
                    continue  # arXiv ancillary dir — not article text
                low = name.lower()
                if not low.endswith((".tex", ".ltx", ".bbl")):
                    continue
                # Refuse an oversized member on its *declared* size — before
                # reading it — and cap the cumulative bytes we hold.
                if member.size > _LATEX_MAX_MEMBER_BYTES:
                    log.warning(
                        "markup.parse_latex: skipping oversized member %s "
                        "(%d bytes > %d cap)",
                        name,
                        member.size,
                        _LATEX_MAX_MEMBER_BYTES,
                    )
                    continue
                if total + member.size > _LATEX_MAX_TOTAL_BYTES:
                    raise MarkupParseError(
                        f"LaTeX: source bundle exceeds {_LATEX_MAX_TOTAL_BYTES} "
                        "bytes (decompression bomb?)",
                        fmt="latex",
                    )
                fh = tar.extractfile(member)
                if fh is None:
                    continue
                raw = fh.read(_LATEX_MAX_MEMBER_BYTES + 1)
                if len(raw) > _LATEX_MAX_MEMBER_BYTES:
                    # Declared size lied — the actual stream ran long. Drop it.
                    log.warning(
                        "markup.parse_latex: member %s exceeded %d bytes on "
                        "read; dropping",
                        name,
                        _LATEX_MAX_MEMBER_BYTES,
                    )
                    continue
                total += len(raw)
                if low.endswith(".bbl"):
                    bbl_text = _safe_read_text(raw)
                else:
                    files[name] = _safe_read_text(raw)
    except tarfile.TarError as exc:
        raise MarkupParseError(f"LaTeX: bad tarball ({exc})", fmt="latex") from exc
    return files, bbl_text


def _safe_read_text(raw: bytes) -> str:
    """Decode bytes as UTF-8, falling back to latin-1 (never raises)."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", errors="replace")


def _pick_main_tex(files: dict[str, str]) -> str | None:
    """Main file = the one(s) with ``\\documentclass`` (arXiv's rule).

    Multiple candidates concatenate alphanumerically per arXiv; we
    return the first and let ``\\input`` following pull the rest.
    """
    candidates = sorted(
        name for name, text in files.items() if _TEX_DOCUMENTCLASS_RE.search(text)
    )
    return candidates[0] if candidates else None


def _assemble_latex_body(main: str, files: dict[str, str]) -> str:
    """Follow ``\\input``/``\\include`` from ``main``; return body text.

    Body = everything between ``\\begin{document}`` and
    ``\\end{document}`` (preamble dropped). Includes are resolved
    relative to the tarball root, ``.tex`` appended when absent.
    """
    seen: set[str] = set()

    def resolve(name: str) -> str:
        text = _expand_includes(files.get(name, ""), files, seen)
        return text

    raw = resolve(main)
    seen.add(main)

    begin = raw.find(r"\begin{document}")
    end = raw.find(r"\end{document}")
    if begin != -1:
        raw = raw[begin + len(r"\begin{document}") :]
    if end != -1:
        raw = raw[: raw.find(r"\end{document}")] if end > begin else raw
    return raw


def _expand_includes(text: str, files: dict[str, str], seen: set[str]) -> str:
    """Recursively inline ``\\input``/``\\include`` targets."""

    def repl(match: re.Match[str]) -> str:
        target = match.group(1).strip()
        for cand in (target, f"{target}.tex", f"{target}.ltx"):
            if cand in files and cand not in seen:
                seen.add(cand)
                return _expand_includes(files[cand], files, seen)
        return ""  # missing include → drop silently (logged by caller if needed)

    return _TEX_INPUT_RE.sub(repl, text)


def _segment_latex_body(body: str, blocks: list[dict[str, Any]]) -> None:
    """Split the body on sectioning commands; emit one block per segment.

    Tracks ``\\section`` / ``\\subsection`` / ``\\subsubsection`` to
    build ``section_path``. Bibliography environments are emitted as
    references; everything else is a paragraph.
    """
    # Pull an inline thebibliography out first, if present.
    biblio = ""
    bib_match = re.search(
        r"\\begin\{thebibliography\}.*?\\end\{thebibliography\}", body, re.DOTALL
    )
    if bib_match:
        biblio = bib_match.group(0)
        body = body[: bib_match.start()] + body[bib_match.end() :]

    section_stack: list[str] = []
    depth_of = {"section": 0, "subsection": 1, "subsubsection": 2}
    pos = 0
    last_path: list[str] = []
    for match in _TEX_SECTION_RE.finditer(body):
        segment = body[pos : match.start()]
        _emit_latex_segment(segment, list(last_path), blocks)
        level = depth_of[match.group(1)]
        title = _strip_tex(match.group(2))
        section_stack = section_stack[:level]
        section_stack.append(title)
        last_path = list(section_stack)
        pos = match.end()
    _emit_latex_segment(body[pos:], list(last_path), blocks)

    if biblio:
        _emit(blocks, kind="references", text=_strip_tex(biblio), section_path=[])


def _emit_latex_segment(
    segment: str, section_path: list[str], blocks: list[dict[str, Any]]
) -> None:
    """Clean one inter-section LaTeX segment and emit paragraph blocks."""
    text = _strip_tex(segment)
    if text.strip():
        _emit(blocks, kind="paragraph", text=text, section_path=section_path)


def _strip_tex(text: str) -> str:
    """Light LaTeX cleanup: drop comments, common non-rendering commands.

    Deliberately minimal — native math and most inline commands stay as
    text (fine for embeddings). We only remove things that are pure
    noise for retrieval.
    """
    text = _TEX_COMMENT_RE.sub("", text)
    # Bare forced-linebreak macro (no brace argument, so the brace-unwrap loop
    # below never sees it) → a space, not a delete: dropping it outright would
    # glue the words on either side together (``Foo\\Bar`` → ``FooBar``).
    # Confirmed real artifact — prod todo titles like "Import & edit: \\ to
    # Ammonia System Manual" carried the macro verbatim into the title.
    text = re.sub(r"\\\\", " ", text)
    # Drop non-rendering housekeeping commands (keep their absence clean).
    text = re.sub(
        r"\\(label|ref|cite[a-z]*|usepackage|newcommand|renewcommand)"
        r"\s*(\[[^\]]*\])?\s*\{[^}]*\}",
        " ",
        text,
    )
    text = re.sub(r"\\(begin|end)\s*\{[^}]*\}", " ", text)
    # Unwrap \textbf{...}/\emph{...} etc → keep the inner text.
    for _ in range(3):
        text = re.sub(r"\\[a-zA-Z@]+\s*\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"[{}]", "", text)
    return _norm_ws(text)


def _guard_macro_density(body: str) -> None:
    """Raise if the source is macro-soup (unexpanded custom macros).

    Runs :func:`_strip_tex` first so that *standard* structural commands
    (``\\section``, ``\\cite``, ``\\begin``/``\\end``, ``\\textbf`` …) are
    already removed/unwrapped — what survives is unexpanded **custom**
    macros. When those dominate the remaining token stream (ratio over
    :data:`_LATEX_MACRO_DENSITY_MAX`) a flatten would embed garbage, so we
    bail to OCR on the companion PDF.
    """
    if not _norm_ws(_TEX_COMMENT_RE.sub("", body)):
        raise MarkupParseError("LaTeX: no tokens in body", fmt="latex")
    stripped = _strip_tex(body)
    tokens = stripped.split()
    if not tokens:
        # Everything stripped away (all commands, no prose) — treat as
        # macro-soup rather than an empty paper.
        raise MarkupParseError(
            "LaTeX: no prose survived cleanup (macro-soup; OCR fallback)",
            fmt="latex",
        )
    macros = len(_TEX_MACRO_TOKEN_RE.findall(stripped))
    density = macros / len(tokens)
    if density > _LATEX_MACRO_DENSITY_MAX:
        raise MarkupParseError(
            f"LaTeX: macro density {density:.2f} > {_LATEX_MACRO_DENSITY_MAX} "
            "(macro-soup; falling back to OCR)",
            fmt="latex",
        )


def _latex_title(files: dict[str, str]) -> str:
    """Best-effort ``\\title{...}`` extraction across the sources."""
    for text in files.values():
        m = re.search(r"\\title\s*\{", text)
        if m:
            # Balance braces from the opening one.
            start = m.end() - 1
            depth = 0
            for i in range(start, min(len(text), start + 2000)):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        return _strip_tex(text[start + 1 : i])
    return ""


# ---------------------------------------------------------------------------
# Shared XML entry
# ---------------------------------------------------------------------------


def _parse_xml(xml_bytes: bytes, *, fmt: str) -> Any:
    """Parse XML bytes into an lxml root, recovering from minor errors."""
    try:
        from lxml import etree
    except ImportError as exc:  # pragma: no cover - lxml is a dep
        raise MarkupParseError(f"lxml not available: {exc}", fmt=fmt) from exc
    try:
        parser = etree.XMLParser(recover=True, resolve_entities=False, no_network=True)
        root = etree.fromstring(xml_bytes, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise MarkupParseError(f"{fmt}: XML syntax error ({exc})", fmt=fmt) from exc
    if root is None:
        raise MarkupParseError(f"{fmt}: empty document", fmt=fmt)
    return root


def sniff_xml_format(xml_bytes: bytes) -> str | None:
    """Sniff a manually-dropped ``.xml`` as ``elsevier_xml`` or ``jats``.

    A fetched file carries an authoritative sidecar ``source_format``; a hand-
    dropped one has only its extension, and every ``.xml`` was being routed to
    the JATS profile — so a dropped Elsevier full-text XML parsed poorly
    (gripe 161850). Discriminate on the document element + namespaces: Elsevier
    wraps its full text in ``<full-text-retrieval-response>`` (the ScienceDirect
    API shape) and declares ``elsevier.com`` namespaces (``ce:``/``ja:``);
    JATS's document element is ``<article>`` with no Elsevier namespace.

    Returns ``"elsevier_xml"`` / ``"jats"``, or ``None`` when the bytes don't
    parse as XML at all (caller then falls back to the extension default).
    """
    try:
        root = _parse_xml(xml_bytes, fmt="jats")
    except MarkupParseError:
        return None
    if _localname(root) == "full-text-retrieval-response":
        return "elsevier_xml"
    ns_values = " ".join(
        v for v in getattr(root, "nsmap", {}).values() if isinstance(v, str)
    )
    if "elsevier.com" in ns_values.lower():
        return "elsevier_xml"
    return "jats"


def _coerce_year(text: str) -> int | None:
    """Parse a 4-digit year out of ``text``; ``None`` if absent."""
    m = re.search(r"\b(1[6-9]\d{2}|20\d{2})\b", text or "")
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def parse_markup(
    markup_path: Path,
    *,
    fmt: str,
    source_url: str | None = None,
) -> MarkupExtraction:
    """Parse ``markup_path`` according to ``fmt``.

    Raises :class:`MarkupParseError` for an unknown format or any parse
    failure. Logs at ERROR on failure with the format + cause so a stuck
    paper is diagnosable from the worker log.
    """
    if fmt not in MARKUP_FORMATS:
        raise MarkupParseError(f"unknown markup format {fmt!r}", fmt=fmt)
    try:
        if fmt == "latex":
            return parse_latex(markup_path, source_url=source_url)
        data = markup_path.read_bytes()
        if fmt == "jats":
            return parse_jats(data, source_url=source_url)
        if fmt == "elsevier_xml":
            return parse_elsevier(data, source_url=source_url)
        if fmt == "arxiv_html":
            return parse_arxiv_html(data, source_url=source_url)
        raise MarkupParseError(f"unhandled markup format {fmt!r}", fmt=fmt)
    except MarkupParseError as exc:
        log.error(
            "markup.parse_markup: %s parse failed for %s: %s",
            fmt,
            markup_path.name,
            exc,
        )
        raise
    except Exception as exc:
        log.error(
            "markup.parse_markup: %s unexpected error for %s: %s",
            fmt,
            markup_path.name,
            exc,
            exc_info=True,
        )
        raise MarkupParseError(f"{fmt}: unexpected error ({exc})", fmt=fmt) from exc


__all__ = [
    "MARKUP_FORMATS",
    "MarkupExtraction",
    "MarkupParseError",
    "parse_arxiv_html",
    "parse_elsevier",
    "parse_jats",
    "parse_latex",
    "parse_markup",
    "sniff_xml_format",
]
