"""Typeset a source's "reading edition" — a tablet-sized PDF assembled from
what the store already holds about *one* cited paper / patent / datasheet,
not a re-render of the original file.

For each source a draft cites, ``export_reading_edition`` writes a
compilable LaTeX project (mirroring ``precis.export.latex.export_draft``'s
project shape) with three parts, in order:

1. **Easy-read body** — the source's own body chunks
   (``store.chunks.list_chunks_for_ref``, ``ord >= 0``, reading order) at
   reMarkable page geometry, with section headings reconstructed from each
   chunk's ``meta['section_path']``.
2. **Claims appendix** — every Taproot claim hub grounded in this source
   (:func:`precis.taproot.lookup.hubs_grounded_by_paper`), with its publish
   state, so the reader sees what precis extracted next to what the authors
   wrote.
3. **The original PDF appended** (``pdfpages``), when this host holds a copy.

**The key insight this module exists to preserve**: body chunks and claims
come from the database, not the filesystem. A source whose PDF is *not* on
this host still gets a full reading edition — body + claims — just without
part 3 (a note says so in the document instead). Callers pass
``original_pdf=None`` for that case; :func:`precis.export.sources.
collect_cited_sources` already tells them, per source, whether a local copy
exists.

**Per-host caveat.** Same as ``remarkable_papers_send``: the corpus is a
per-host mount, so this build may run PDF-less even when the cluster holds
the source elsewhere. That's expected, not an error — only a source with
*zero* body chunks and no local PDF has nothing to typeset at all.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from precis.export.latex import (
    _template_text,
    _tex,
    assemble_document,
    build_author_block,
)
from precis.handlers._paper_text import _is_image_only_block, _scrub_block_text
from precis.utils import handle_registry

log = logging.getLogger(__name__)

#: chunk section_path depth (1-based) -> starred sectioning command. Starred
#: (unnumbered) — the paper's own section numbers already live in the text;
#: a depth beyond the table collapses to the deepest command.
_HEADING_CMDS = ("section*", "subsection*", "subsubsection*")


@dataclass
class ReadingEditionResult:
    """What got typeset — enough for the caller to decide whether the build
    was worth compiling and to report a summary."""

    chunk_count: int
    claim_count: int
    has_original: bool


def _heading_lines(path: list[str]) -> list[str]:
    """The sectioning command for a new ``section_path`` — level = the
    path's own depth, deepest element is the heading text."""
    depth = len(path) - 1
    cmd = _HEADING_CMDS[min(max(depth, 0), len(_HEADING_CMDS) - 1)]
    return [f"\\{cmd}{{{_tex(path[-1])}}}", ""]


def _render_body_tex(
    store: Any, source_ref: Any, *, has_original: bool
) -> tuple[str, int]:
    """The easy-read body: source body chunks in order, light formatting.

    Section headings emit on a ``section_path`` change; an image-only chunk
    (:func:`~precis.handlers._paper_text._is_image_only_block`) — a figure
    marker with no readable caption text — renders as a one-line
    placeholder instead of its unusable markdown image marker, since the
    figure itself isn't served here. Everything else is scrubbed
    (:func:`~precis.handlers._paper_text._scrub_block_text`, which strips
    page-anchor spans) then LaTeX-escaped."""
    blocks = store.chunks.list_chunks_for_ref(source_ref.id)
    lines: list[str] = []
    prev_path: list[str] | None = None
    for b in blocks:
        path = list((b.meta or {}).get("section_path") or [])
        if path and path != prev_path:
            lines.extend(_heading_lines(path))
        prev_path = path
        raw = b.text or ""
        if _is_image_only_block(raw):
            note = (
                "figure — see original PDF appended below"
                if has_original
                else "figure — not available on this host"
            )
            lines.append(f"\\emph{{[{note}]}}")
        else:
            lines.append(_tex(_scrub_block_text(raw)))
        lines.append("")
    return "\n".join(lines).strip() + "\n", len(blocks)


def _claims_section(store: Any, source_ref: Any) -> tuple[str, int]:
    """The "Claims grounded in this source" appendix — one item per Taproot
    claim hub with an evidence edge from this source, or ``("", 0)`` when
    there are none.

    A claims-query failure (malformed evidence graph, a store hiccup)
    degrades to no section rather than killing the whole reading edition —
    the body + original are still worth having without it."""
    try:
        from precis.nanopub.overview import hub_rows
        from precis.taproot.lookup import hubs_grounded_by_paper

        hubs = hubs_grounded_by_paper(store, source_ref.id, require_pub_id=False)
        if not hubs:
            return "", 0
        postures = {
            row.ref_id: row
            for row in hub_rows(store, ref_ids=[h["hub_ref_id"] for h in hubs])
        }
    except Exception:
        log.warning(
            "reading_edition: claims lookup failed for %s",
            getattr(source_ref, "slug", None),
            exc_info=True,
        )
        return "", 0

    lines = [
        "\\clearpage",
        "\\section*{Claims grounded in this source}",
        "",
        "\\begin{itemize}",
    ]
    for h in hubs:
        handle = handle_registry.format_handle("finding", h["hub_ref_id"])
        row = postures.get(h["hub_ref_id"])
        state = (row.state if row is not None else None) or "unminted"
        claim = _tex(str(h.get("claim") or ""))
        role = _tex(str(h.get("role") or ""))
        lines.append(f"\\item {claim} \\textit{{[{role}, {handle}, {_tex(state)}]}}")
    lines.append("\\end{itemize}")
    return "\n".join(lines), len(hubs)


def _original_section(has_original: bool) -> str:
    """The end-matter block for the original PDF: an ``\\includepdf``
    appendix when the host holds a copy, else a short note explaining why
    it isn't there (the per-host caveat, left as a trace in the document
    itself)."""
    if has_original:
        return "\n".join(
            [
                "\\clearpage",
                "\\phantomsection",
                "\\addcontentsline{toc}{section}{Original PDF}",
                "\\includepdf[pages=-]{sources/original.pdf}",
            ]
        )
    return "\\par\\noindent " + _tex(
        "Original PDF not available on this host — figures referenced "
        "above are in the source of record."
    )


def export_reading_edition(
    store: Any,
    source_ref: Any,
    target_dir: Path,
    *,
    original_pdf: Path | None,
) -> ReadingEditionResult:
    """Write a self-contained LaTeX project into ``target_dir`` for one
    source's reading edition — compilable via
    :func:`precis.export.compile.compile_pdf`.

    ``original_pdf`` is this host's local copy of the source (or ``None`` —
    the per-host-caveat case, see module docstring); when given it's staged
    at ``target_dir/sources/original.pdf`` and appended as a ``pdfpages``
    block. Writes ``main.tex`` + a copy of the checked-in ``preamble.tex`` +
    ``.latexmkrc`` + an (empty, since a reading edition cites nothing)
    ``refs.bib`` — the preamble unconditionally loads biblatex, so the bib
    file must exist even when it has no entries.
    """
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    has_original = original_pdf is not None
    if has_original:
        assert original_pdf is not None  # narrowed by has_original
        src_dir = target_dir / "sources"
        src_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original_pdf, src_dir / "original.pdf")

    body_tex, chunk_count = _render_body_tex(
        store, source_ref, has_original=has_original
    )
    claims_tex, claim_count = _claims_section(store, source_ref)
    appendix_parts = [p for p in (claims_tex, _original_section(has_original)) if p]
    appendix_tex = "\n\n".join(appendix_parts)

    title = str(
        getattr(source_ref, "title", None)
        or getattr(source_ref, "slug", None)
        or "Untitled"
    ).split("\n", 1)[0]
    fallback = str((getattr(source_ref, "meta", None) or {}).get("author") or "precis")
    author_block = build_author_block(
        getattr(source_ref, "authors", None), fallback=fallback
    )

    main_tex = assemble_document(
        title=title,
        author_block=author_block,
        body=body_tex,
        acronyms="",
        appendix=appendix_tex,
        remarkable=True,
    )

    (target_dir / "main.tex").write_text(main_tex, encoding="utf-8")
    (target_dir / "refs.bib").write_text("", encoding="utf-8")
    (target_dir / "preamble.tex").write_text(
        _template_text("preamble.tex"), encoding="utf-8"
    )
    (target_dir / ".latexmkrc").write_text(
        _template_text("latexmkrc"), encoding="utf-8"
    )

    return ReadingEditionResult(
        chunk_count=chunk_count,
        claim_count=claim_count,
        has_original=has_original,
    )


__all__ = ["ReadingEditionResult", "export_reading_edition"]
