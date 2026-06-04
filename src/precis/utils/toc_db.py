"""DB-backed TOC renderer.

Replaces :func:`precis.utils.toc.render_for_ref` (which recomputed
DP segmentation + KeyBERT at every request) with one SQL SELECT
against ``ref_segments`` + ``ref_segment_sentences``. The worker
(see :mod:`precis.workers.segment_toc`) pre-computes the artifacts
at ingest time; this module is the read side.

Output shape: TOON-tabular for the segment rows, indented prose
sub-lines for the per-segment excerpt. Format laid out in the
storage-v2 design discussion (2026-05-31):

::

    {handle\theading\tkeywords}
    foo~5..8\tResults\tCu-MOF, FTIR, CO2 adsorption, Faradaic efficiency
      - excerpt @ ~7: "We synthesized Cu-MOF nanocrystals..."
    foo~9..12\tDiscussion\tion transport, charge balance, voltage stability
      - excerpt @ ~10: "The cell sustained 500 cycles at 80% retention."

When the segments table is empty for the ref (worker hasn't run
yet), the renderer returns a deterministic "compute pending"
placeholder rather than falling back to on-demand recompute — the
storage-v2 contract says workers populate before reads.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from precis.format import render_agent_table
from precis.store._segments_ops import SegmentRow, SentenceRow

#: F5+D4: row cap for the per-chunk fallback TOC. Above this the
#: response is hard to skim and the agent should narrow the range or
#: use the segment-level view. Pagination is a stretch goal — for now
#: we render the first N and append a "more" hint.
_FALLBACK_ROW_CAP = 20

#: First-N chars of chunk text shown in the per-chunk fallback. The
#: full chunk lives one read away (``get(kind='paper', id='slug~N')``);
#: 60 chars gives the agent enough to decide whether to drill in.
_FALLBACK_PREVIEW_CHARS = 60

#: Number of TOC excerpt sub-lines per segment. Two gives the agent
#: enough context to triage without dominating the table row above.
_EXCERPT_LINES_PER_SEGMENT = 1


class _SegmentReader(Protocol):
    """Duck-type for whatever store-like object we read from.

    Lets the function accept a real :class:`precis.store.Store` or a
    test double without an import-time dependency.
    """

    def list_segments_for_ref(self, ref_id: int) -> list[SegmentRow]: ...

    def top_sentences_for_segment(
        self,
        segment_id: int,
        *,
        limit: int = ...,
        query_embedding: list[float] | None = ...,
    ) -> list[SentenceRow]: ...


def render_from_store(
    *,
    store: _SegmentReader,
    ref_id: int,
    slug: str,
    kind: str,
    scope: tuple[int, int] | None = None,
) -> str:
    """Render the TOC body for ``ref_id`` from persistent storage.

    ``scope`` restricts the view to segments fully inside the
    given absolute chunk-position range. Used by the recursive
    sub-TOC drill-in (``get(id='slug~5..14', view='toc')``).

    Returns a Markdown body. The first line is the kind-aware
    headline; the rest is the TOON table + per-segment excerpts.
    """
    segments = store.list_segments_for_ref(ref_id)
    if not segments:
        return _placeholder_body(slug=slug, kind=kind)

    if scope is not None:
        lo, hi = scope
        scoped = [
            s for s in segments
            if s.pos_lo >= lo and s.pos_hi <= hi
        ]
        # F5+D4: when the requested range yields zero or one segments,
        # the segment-level table is uninformative (the caller already
        # picked the range it cares about). Fall back to a per-chunk
        # preview so the agent sees what's actually inside the range.
        if len(scoped) <= 1:
            fallback = _render_per_chunk_fallback(
                store=store, ref_id=ref_id, slug=slug, scope=scope
            )
            if fallback is not None:
                return fallback
        segments = scoped
        if not segments:
            return f"# {slug} — no segments in scope ~{lo}..{hi}"

    use_h2 = all(s.mode == "h2" for s in segments)
    n_seg = len(segments)
    headline = _headline(slug=slug, kind=kind, n_seg=n_seg, use_h2=use_h2, scope=scope)

    rows: list[dict[str, str]] = []
    for seg in segments:
        handle = _handle_for(slug, seg.pos_lo, seg.pos_hi)
        keywords_str = _keywords_display(seg.keywords)
        if use_h2:
            heading = seg.heading or ""
            rows.append(
                {"handle": handle, "heading": heading, "keywords": keywords_str}
            )
        else:
            rows.append({"handle": handle, "keywords": keywords_str})

    # F4: TOC no longer emits per-segment ``excerpt @ ~N: "..."``
    # sub-lines. They roughly doubled the per-row token cost, the
    # central-sentence picker produced garbage on many segments
    # (single-token citation markers, bare headings), and the
    # keywords already do the segment-identification job. Excerpts
    # are still query-aligned and useful in *search* responses; the
    # TOC view is navigation, not query-result.
    table = render_agent_table(rows)
    return f"{headline}\n\n{table}"


# ── helpers ──────────────────────────────────────────────────────────


def _render_per_chunk_fallback(
    *,
    store: Any,
    ref_id: int,
    slug: str,
    scope: tuple[int, int],
) -> str | None:
    """F5+D4: per-chunk preview when the range collapses to ≤ 1 segment.

    Returns the rendered body, or ``None`` when the store doesn't
    expose chunk access (caller falls back to the segment table or
    placeholder). Uses :meth:`Store.list_blocks_for_ref` via duck-
    typing so test doubles only need the segment surface unless they
    exercise this path.
    """
    lo, hi = scope
    list_blocks = getattr(store, "list_blocks_for_ref", None)
    if list_blocks is None:
        return None
    blocks = list_blocks(ref_id, pos_range=(lo, hi))
    if not blocks:
        return None

    n_total = len(blocks)
    truncated = blocks[:_FALLBACK_ROW_CAP]
    rows: list[dict[str, str]] = []
    for block in truncated:
        text = (block.text or "").strip().replace("\n", " ")
        if len(text) > _FALLBACK_PREVIEW_CHARS:
            preview = text[:_FALLBACK_PREVIEW_CHARS].rstrip() + "…"
        else:
            preview = text
        rows.append(
            {
                "handle": f"{slug}~{block.pos}",
                "preview": preview,
            }
        )

    headline = (
        f"# {slug} sub-TOC ~{lo}..{hi} — {n_total} chunks (segment-level "
        f"view collapsed to one segment for this range)"
    )
    table = render_agent_table(rows, schema=["handle", "preview"])
    body = f"{headline}\n\n{table}"
    if n_total > _FALLBACK_ROW_CAP:
        body += (
            f"\n\n…{n_total - _FALLBACK_ROW_CAP} more chunks. "
            f"Narrow the range or read directly: "
            f"get(kind='paper', id='{slug}~{lo + _FALLBACK_ROW_CAP}..{hi}')"
        )
    return body


def _placeholder_body(*, slug: str, kind: str) -> str:
    """Returned when no segments are stored for the ref yet."""
    return (
        f"# {slug} — segments not yet computed\n\n"
        f"Run `precis worker` to populate the discovery layer. "
        f"Once the segment-toc worker drains, this view will "
        f"render from `ref_segments` automatically."
    )


def _headline(
    *,
    slug: str,
    kind: str,
    n_seg: int,
    use_h2: bool,
    scope: tuple[int, int] | None,
) -> str:
    if scope is not None:
        return f"# {slug} sub-TOC ~{scope[0]}..{scope[1]} — {n_seg} segments"
    mode_word = "H2 sections" if use_h2 else "embedding clustering"
    return f"# {slug} TOC — {n_seg} segments via {mode_word}"


def _handle_for(slug: str, lo: int, hi: int) -> str:
    """Canonical chunk handle. Single chunks render ``~N``, ranges ``~A..B``."""
    if lo == hi:
        return f"{slug}~{lo}"
    return f"{slug}~{lo}..{hi}"


def _keywords_display(keywords: Sequence[dict[str, Any]]) -> str:
    """Render the JSONB keyword list as a comma-separated string.

    Prefers ``short`` when present (compact display in TOC rows);
    falls back to ``long``. Order is preserved (matryoshka — most-
    distinctive first).
    """
    parts: list[str] = []
    for kw in keywords:
        short = kw.get("short")
        long = kw.get("long") or ""
        if not long:
            continue
        # Compact: if a short form exists, use it.
        parts.append(short or long)
    return ", ".join(parts)


def _format_excerpt(slug: str, sentences: Sequence[SentenceRow]) -> str | None:
    """Two-space-indented Markdown sub-line listing the top excerpts.

    Returns ``None`` when no sentences are available (empty segment
    or all sentences failed compute). Multi-line excerpts stay on a
    single line — long lines are cheaper than wrapped lines for
    LLM consumption (one `\\n` token saved per wrap).
    """
    if not sentences:
        return None
    out: list[str] = []
    for s in sentences:
        # Strip newlines from the stored text to keep the sub-line
        # one rendered line (LLM-friendly per the 2026-05-31 design
        # discussion). pysbd preserves intra-sentence whitespace; we
        # collapse it on render only.
        flat = " ".join(s.text.split())
        out.append(f'  - excerpt @ ~{s.chunk_pos}: "{flat}"')
    return "\n".join(out)


__all__ = ["render_from_store"]
