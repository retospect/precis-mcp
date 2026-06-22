"""Draft → ``.docx`` export (python-docx). Sibling of ``export/latex.py``.

Synchronous and **toolchain-free** (python-docx + lxml, both already
deps) — this is the "just works" path: no latexmk, nothing to install,
nothing to compile. It mirrors the LaTeX exporter's chunk walk and inline
grammar so the two never drift, and — crucially — resolves citations
through the **same** paper-ref lookup the ``.bib`` path uses
(:func:`precis.export.latex.build_bib`). A docx and a PDF therefore cite
the *identical* resolved references: that shared resolver is the
citation-integrity guarantee.

Citation model (v1): each ``[§slug~n]`` / ``paper:slug~n`` becomes a
numbered marker ``[n]`` in the text, backed by a numbered **References**
section at the document end carrying the resolved bibliographic entry
(authors · year · title · DOI/arXiv). Real Word *endnote fields* and OMML
math are the next increments (see ``_render_math`` / module TODO); the
resolver and the numbering are already endnote-shaped, so swapping the
rendering surface doesn't touch the integrity path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from precis.export.latex import _COMBINED, _bibtex_authors
from precis.utils.draft_markup import DRAFT_CITE_PATTERN

#: chunk depth → Word heading level (1..4); deeper collapses to 4.
_MAX_HEADING_LEVEL = 4


@dataclass
class DocxResult:
    """Path written + diagnostics (parallel to the LaTeX ``ExportResult``)."""

    path: Path
    cited_slugs: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class _Ctx:
    store: Any
    known_handles: set[str]
    abbrevs: dict[str, str] = field(default_factory=dict)  # short → long
    cited: list[str] = field(default_factory=list)  # paper slug order = ref number
    warnings: list[str] = field(default_factory=list)
    seen_acr: set[str] = field(default_factory=set)  # already expanded once
    used_acr: set[str] = field(default_factory=set)  # for the acronyms list
    last_cite: str | None = None  # paper of the immediately-preceding mark

    def cite_number(self, slug: str) -> int:
        """1-based reference number for a slug (stable insertion order)."""
        if slug not in self.cited:
            self.cited.append(slug)
        return self.cited.index(slug) + 1

    _short_re: Any = None

    def short_pattern(self) -> Any:
        """A compiled regex matching any known short as a whole word
        (optional trailing ``s`` for plurals), longest-first so ``MOFs``
        wins over ``MOF``. ``None`` when the draft defines no abbreviations."""
        if self._short_re is None and self.abbrevs:
            shorts = sorted(self.abbrevs, key=len, reverse=True)
            self._short_re = re.compile(
                r"\b(" + "|".join(re.escape(s) for s in shorts) + r")(s?)\b"
            )
        return self._short_re


def export_docx(store: Any, ref: Any, *, target_path: Path) -> DocxResult:
    """Render a draft into ``target_path`` as a ``.docx``. Returns the
    path plus the cited slugs and any resolution warnings."""
    from docx import Document

    target_path = Path(target_path)
    chunks = store.reading_order(ref.id)
    handles = {c.handle for c in chunks}
    ctx = _Ctx(
        store=store,
        known_handles=handles,
        abbrevs=store.defined_abbrevs(ref.id),
    )

    doc = Document()
    terms = store.draft_terms(ref.id)  # handle → (short, long)

    # The first heading at depth 0 is the title — render it as the doc title.
    title_done = False
    for c in chunks:
        kind = c.chunk_kind
        if kind == "term":
            # Render in place (terms live under the draft's own Glossary
            # heading) as "SHORT — long", pulling the short from meta.
            short, long = terms.get(c.handle, ("", c.text))
            p = doc.add_paragraph()
            if short:
                p.add_run(short).bold = True
                p.add_run(f" — {long}")
            else:
                p.add_run(long)
            continue
        if kind == "heading":
            if not title_done and c.depth == 0:
                doc.add_heading(c.text, level=0)
                title_done = True
                continue
            level = min(max(c.depth, 1), _MAX_HEADING_LEVEL)
            doc.add_heading(c.text, level=level)
            continue
        if kind == "code":
            p = doc.add_paragraph()
            run = p.add_run(c.text)
            run.font.name = "Consolas"
            continue
        # paragraph (default)
        p = doc.add_paragraph()
        _render_inline(c.text, ctx, p)

    _append_acronyms(doc, ctx)
    _attach_endnotes(doc, ctx)

    target_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(target_path))
    return DocxResult(path=target_path, cited_slugs=list(ctx.cited), warnings=ctx.warnings)


# ── inline rendering ──────────────────────────────────────────────


def _render_inline(text: str, ctx: _Ctx, paragraph: Any) -> None:
    """Walk a chunk's text, adding runs to ``paragraph``. References go
    through :func:`_render_reference`; the prose gaps between them get
    markdown/sub-sup/math run formatting."""
    last = 0
    for m in _COMBINED.finditer(text):
        _render_gap(text[last : m.start()], ctx, paragraph)
        _render_reference(m, ctx, paragraph)
        last = m.end()
    _render_gap(text[last:], ctx, paragraph)


# Tokeniser for a non-reference gap: math / code / bold / italic / sub /
# sup become typed spans; everything else is plain text. Ordered so the
# verbatim spans (math, code) are carved out before emphasis.
_SPAN = re.compile(
    r"(?P<math>\$\$.+?\$\$|\$[^$]+\$)"
    r"|(?P<code>`[^`]+`)"
    r"|(?P<sub><sub>.+?</sub>)"
    r"|(?P<sup><sup>.+?</sup>)"
    r"|(?P<bold>\*\*.+?\*\*)"
    r"|(?P<italic>(?<![\*\w])\*(?!\s)[^*]+?(?<!\s)\*(?!\w))",
    re.DOTALL,
)


def _render_gap(text: str, ctx: _Ctx, paragraph: Any) -> None:
    if not text:
        return
    # Real prose between two citations breaks the consecutive-cite run;
    # whitespace-only (``[§a~3] [§a~9]``) does not, so those still collapse.
    if text.strip():
        ctx.last_cite = None
    last = 0
    for m in _SPAN.finditer(text):
        if m.start() > last:
            _emit_text(text[last : m.start()], ctx, paragraph)
        if m.group("math") is not None:
            _render_math(m.group("math"), paragraph)
        elif m.group("code") is not None:
            r = paragraph.add_run(m.group("code")[1:-1])
            r.font.name = "Consolas"
        elif m.group("sub") is not None:
            r = paragraph.add_run(m.group("sub")[5:-6])
            r.font.subscript = True
        elif m.group("sup") is not None:
            r = paragraph.add_run(m.group("sup")[5:-6])
            r.font.superscript = True
        elif m.group("bold") is not None:
            paragraph.add_run(m.group("bold")[2:-2]).bold = True
        elif m.group("italic") is not None:
            paragraph.add_run(m.group("italic")[1:-1]).italic = True
        last = m.end()
    if last < len(text):
        _emit_text(text[last:], ctx, paragraph)


def _emit_text(text: str, ctx: _Ctx, paragraph: Any) -> None:
    """Plain prose → runs, with **render-time first-use expansion** of
    known abbreviations: the first occurrence (in reading order) of a
    defined short becomes ``Long Form (SHORT)``; later ones stay ``SHORT``.
    No authoring markup — this mirrors what the LaTeX ``\\gls`` path does,
    and survives chunk reordering because it's computed here at export.
    A trailing ``s`` (plural) is preserved on the short."""
    pat = ctx.short_pattern()
    if pat is None:
        paragraph.add_run(text)
        return
    last = 0
    for m in pat.finditer(text):
        if m.start() > last:
            paragraph.add_run(text[last : m.start()])
        short, plural = m.group(1), m.group(2)
        ctx.used_acr.add(short)
        if short not in ctx.seen_acr:
            ctx.seen_acr.add(short)
            paragraph.add_run(f"{ctx.abbrevs[short]} ({short}{plural})")
        else:
            paragraph.add_run(f"{short}{plural}")
        last = m.end()
    if last < len(text):
        paragraph.add_run(text[last:])


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
        _render_target(m.group("bare"), None, ctx, paragraph)
        return
    if m.group("ref") is not None:
        if m.group("kind") == "paper":
            _cite(m.group("id"), ctx, paragraph)
        return  # bare memory:/think:/… — not citeable
    if m.group("bare_conv") is not None:
        return
    if m.group("bare_paper") is not None:
        _cite(m.group("bare_paper").split("~", 1)[0], ctx, paragraph)
        return


def _render_target(tgt: str, surface: str | None, ctx: _Ctx, paragraph: Any) -> None:
    if tgt.startswith("§"):  # a citation — keep the consecutive-cite run
        cm = DRAFT_CITE_PATTERN.fullmatch(tgt)
        if cm is not None:
            _cite(cm.group("slug"), ctx, paragraph)
        return
    # Any non-citation content breaks a run of consecutive citations.
    ctx.last_cite = None
    if tgt.startswith("¶"):
        handle = tgt[1:]
        if handle not in ctx.known_handles:
            ctx.warnings.append(f"cross-ref ¶{handle}: no such live chunk — downgraded")
        paragraph.add_run(surface or f"¶{handle}")
        return
    if tgt.startswith(("http://", "https://")):
        paragraph.add_run(surface or tgt)
        return


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
_CT_ENDNOTES = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.endnotes+xml"
)
_RT_ENDNOTES = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/endnotes"


def _cite(slug: str, ctx: _Ctx, paragraph: Any) -> None:
    """Emit a Word **endnote reference** at this point. The endnote body
    (the resolved reference) is attached to ``endnotes.xml`` at the end —
    see :func:`_attach_endnotes`. ``w:id`` matches the endnote's id.

    Citations key on the **paper**, not the chunk: ``a~3``, ``a~9``,
    ``a~23`` all reference paper ``a`` → one endnote. And **consecutive**
    marks for the same paper collapse to a single superscript (no ``¹¹¹``)."""
    from lxml import etree

    slug = slug.split("~", 1)[0]  # the paper, not the cited chunk
    n = ctx.cite_number(slug)  # registers the paper (idempotent)
    if ctx.last_cite == slug:
        return  # consecutive cite to the same paper — one mark for the run
    ctx.last_cite = slug
    r = etree.SubElement(paragraph._p, f"{{{_W}}}r")
    rpr = etree.SubElement(r, f"{{{_W}}}rPr")
    style = etree.SubElement(rpr, f"{{{_W}}}rStyle")
    style.set(f"{{{_W}}}val", "EndnoteReference")
    va = etree.SubElement(rpr, f"{{{_W}}}vertAlign")
    va.set(f"{{{_W}}}val", "superscript")
    ref = etree.SubElement(r, f"{{{_W}}}endnoteReference")
    ref.set(f"{{{_W}}}id", str(n))


# ── references + glossary sections ────────────────────────────────


def _format_reference(store: Any, slug: str, warnings: list[str]) -> str:
    """One reference line, resolved through the SAME paper lookup as the
    ``.bib`` path (citation-integrity parity with the PDF). A slug with no
    paper in the corpus degrades to a marked stub + a warning."""
    pref = store.get_ref(kind="paper", id=slug)
    if pref is None:
        warnings.append(f"cite {slug!r}: no paper in corpus — stub reference")
        return f"[missing paper {slug}] (cited slug not in corpus)"
    authors = _bibtex_authors(pref.authors).replace(" and ", "; ")
    # "Authors (year). Title." — robust plain-text assembly.
    head = " ".join(x for x in [authors, f"({pref.year})" if pref.year else ""] if x)
    line = (head + ". " if head else "") + (pref.title or slug) + "."
    try:
        alias = store.identifiers_for_refs([pref.id]).get(pref.id, {})
        if alias.get("doi"):
            line += f" doi:{alias['doi']}"
        elif alias.get("arxiv"):
            line += f" arXiv:{alias['arxiv']}"
    except Exception:
        pass
    return line


def _attach_endnotes(doc: Any, ctx: _Ctx) -> None:
    """Build ``word/endnotes.xml`` and relate it to the document so each
    citation's ``endnoteReference`` resolves to a real Word **endnote**
    carrying the resolved reference (authors · year · title · DOI). The
    endnote ids match the cite order. No-op when nothing was cited.

    Raw OPC surgery — python-docx has no endnote API: a separator pair
    (ids -1/0, required by Word) plus one content endnote per citation,
    registered via a part + relationship (the content-type override and
    serialisation are handled by the package on save)."""
    if not ctx.cited:
        return
    from docx.opc.packuri import PackURI
    from docx.opc.part import Part
    from lxml import etree

    root = etree.Element(f"{{{_W}}}endnotes", nsmap={"w": _W})
    for eid, etype, mark in (
        ("-1", "separator", "separator"),
        ("0", "continuationSeparator", "continuationSeparator"),
    ):
        en = etree.SubElement(root, f"{{{_W}}}endnote")
        en.set(f"{{{_W}}}type", etype)
        en.set(f"{{{_W}}}id", eid)
        p = etree.SubElement(en, f"{{{_W}}}p")
        r = etree.SubElement(p, f"{{{_W}}}r")
        etree.SubElement(r, f"{{{_W}}}{mark}")
    for i, slug in enumerate(ctx.cited, start=1):
        line = _format_reference(ctx.store, slug, ctx.warnings)
        en = etree.SubElement(root, f"{{{_W}}}endnote")
        en.set(f"{{{_W}}}id", str(i))
        p = etree.SubElement(en, f"{{{_W}}}p")
        r0 = etree.SubElement(p, f"{{{_W}}}r")
        rpr = etree.SubElement(r0, f"{{{_W}}}rPr")
        st = etree.SubElement(rpr, f"{{{_W}}}rStyle")
        st.set(f"{{{_W}}}val", "EndnoteReference")
        etree.SubElement(r0, f"{{{_W}}}endnoteRef")
        r1 = etree.SubElement(p, f"{{{_W}}}r")
        t = etree.SubElement(r1, f"{{{_W}}}t")
        t.set(_XML_SPACE, "preserve")
        t.text = f" {line}"
    blob = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    part = Part(PackURI("/word/endnotes.xml"), _CT_ENDNOTES, blob, doc.part.package)
    doc.part.relate_to(part, _RT_ENDNOTES)


def _append_acronyms(doc: Any, ctx: _Ctx) -> None:
    """An "Acronyms" list of every abbreviation actually used in the prose
    (auto-built, like the LaTeX glossaries acronym list) — SHORT → long."""
    used = sorted(s for s in ctx.used_acr if s in ctx.abbrevs)
    if not used:
        return
    doc.add_heading("Acronyms", level=1)
    for short in used:
        p = doc.add_paragraph()
        p.add_run(short).bold = True
        p.add_run(f" — {ctx.abbrevs[short]}")
