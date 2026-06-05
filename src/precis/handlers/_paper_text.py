"""Block-level text scrubbing for ``PaperHandler``.

Split out of ``handlers/paper.py`` 2026-06-05. These helpers handle
the parts of paper chunks that an LLM should *not* quote back to
the user:

* Image markers (``![](_page_19_Figure_1.jpeg)``) whose paths
  resolve to nothing the MCP serves — the LLM that quotes them is
  inventing a working link.
* Marker / acatome-extract page-anchor spans
  (``<span id="page-19-0"></span>``).
* Table grids that poison the RAKE keyword cell on search hits.

Pure functions; no store, no I/O. Tests live in
``tests/test_mcp_critic_regressions.py`` (the image-marker leakage
regression was one of the critic's recurring findings).
"""

from __future__ import annotations

import re

from precis.utils.rake import keyword_summary

# Markdown image markers like ``![](path/to.jpeg)``. The path
# component is captured so the placeholder can name the original
# asset (purely informational — the image isn't served).
_IMAGE_MARKER_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")

# Page-anchor span that acatome-extract emits before image blocks
# (``<span id="page-19-0"></span>``). Stripped along with the image
# so the placeholder body is just a one-line marker.
_PAGE_ANCHOR_RE = re.compile(r"<span\s+id=\"page-\d+-\d+\"\s*></span>")

# Caption blocks start with ``**Fig``, ``**Figure``, ``**Scheme``,
# or ``**Table`` — the bold-prefixed legend pattern emitted by
# Marker / acatome-extract. The check is applied to the *first
# non-empty line* of the candidate block.
_CAPTION_LEAD_RE = re.compile(
    r"^\*\*\s*(Fig(?:ure)?|Scheme|Table)\b",
    re.IGNORECASE,
)


def _is_image_only_block(text: str) -> bool:
    """True when the block consists solely of image markers + page anchors.

    A block that's just ``<span id="page-N-M"></span>![](_page_N_*.jpeg)``
    has no readable content for the agent — the relative path resolves
    to nothing the MCP serves, and there's no caption text to quote.
    """
    stripped = _PAGE_ANCHOR_RE.sub("", text)
    stripped = _IMAGE_MARKER_RE.sub("", stripped)
    return stripped.strip() == ""


def _looks_like_caption(text: str) -> bool:
    """True when the block opens with a Fig/Scheme/Table legend lead."""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        return bool(_CAPTION_LEAD_RE.match(line))
    return False


def _render_block_body(slug: str, pos: int, text: str) -> str:
    """Replace bare image markers with a structured placeholder.

    The relative URL ``![](_page_19_Figure_1.jpeg)`` resolves to
    nothing — quoting it back is a footgun for any LLM citing the
    figure. Replace each marker with a short ``[figure: slug~N —
    image not served; caption on adjacent block]`` placeholder, and
    strip the page-anchor spans around it.

    The asset path is **not** preserved.  An earlier cut kept it
    "for diagnostics" but a 7B caller reading ``asset: _page_3_
    Figure_3.jpeg`` still treats the string as a real file —
    that's the same footgun the substitution exists to close.
    The MCP critic's April 2026 re-probe pinned this regression.
    """
    if not _IMAGE_MARKER_RE.search(text):
        return text
    cleaned = _PAGE_ANCHOR_RE.sub("", text)

    def _replace(_m: re.Match[str]) -> str:
        return (
            f"[figure: {slug}~{pos} - image not served; "
            f"caption on adjacent block (~{pos + 1})]"
        )

    cleaned = _IMAGE_MARKER_RE.sub(_replace, cleaned)
    # Collapse whitespace-only artefacts left behind by the strip.
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def _chunk_keywords_or_caption(text: str) -> str:
    """Render the ``chunk_keywords`` cell for one search hit.

    Most chunks are prose — RAKE produces useful summary keywords.
    Tables (Marker emits them as markdown grids starting with ``|``)
    poison RAKE because empty cells render as ``"na"`` and the rake
    output collapses to ``"na na na na; na na na; ..."``. For those
    we skip RAKE and surface a one-line caption from the table's
    column header row instead, so the search hit still tells the
    agent *what kind of thing* this is without the noise.

    Heuristic: starts with ``|`` after lstrip, pipe density >5 %.
    Same heuristic as migration 0009's paragraph→table backfill,
    so this also handles legacy chunks that pre-date the
    chunk_kind=table classification.
    """
    stripped = text.lstrip()
    is_table = (
        stripped.startswith("|")
        and len(text) > 0
        and (text.count("|") / len(text)) > 0.05
    )
    if is_table:
        first_line = stripped.split("\n", 1)[0].strip()
        # Drop empty cells / trim long rows so the caption fits the
        # one-line cell budget. ``A, (A) | B, (B) | C, (C) | …`` is
        # plenty for an agent to recognise a data table.
        cells = [c.strip() for c in first_line.strip("|").split("|") if c.strip()]
        if cells:
            caption = " | ".join(cells[:6])
            if len(cells) > 6:
                caption += " | …"
            return f"[table] {caption}"
        return "[table]"
    return keyword_summary(text, top_k=5)


def _scrub_block_text(text: str) -> str:
    """Strip image markers + page anchors from arbitrary block text.

    Companion to :func:`_render_block_body` for code paths that
    don't have a slug/pos in scope (search previews, future digest
    views).  The output never carries a markdown image marker or
    a page-anchor span — anything that would lure an LLM into
    quoting a non-served asset is dropped, replaced by a brief
    ``[figure]`` sentinel.

    Idempotent: running twice yields the same result, because the
    regexes don't match their own replacements.

    The MCP critic's April 2026 re-probe flagged the search
    preview path leaking raw ``![](_page_3_Figure_3.jpeg)``
    markers because :func:`_render_block_body` was only wired
    into ``_render_chunks``.  Centralising the substitution in
    one helper keeps every excerpt path on the same contract.
    """
    if not text:
        return text
    cleaned = _PAGE_ANCHOR_RE.sub("", text)
    cleaned = _IMAGE_MARKER_RE.sub("[figure]", cleaned)
    return cleaned


__all__ = [
    "_chunk_keywords_or_caption",
    "_is_image_only_block",
    "_looks_like_caption",
    "_render_block_body",
    "_scrub_block_text",
]
