"""``view='sentences'`` — the numbered-sentence read of one chunk
(gr716229).

The measure evidence contract asks for a sentence-span anchor
(``anchor={'chunk': 'pc<id>', 'anchor_scheme': 'sentence',
'span': 's3'}``) but the read side offered no way to see which text an
id like ``s3`` names — callers anchored blind and learned the mapping
only from the write side's ``anchor_matched`` / ``anchor_mismatch``
receipts (gr716229: ``get(kind='paper', id='pc6200173',
view='sentences')`` answered ``Unsupported``). This view renders the
selected chunk(s) split into sentence spans by the SAME splitter the
anchor machinery uses (:func:`precis.extract.anchors.sentence_spans`),
numbered ``s0``, ``s1``, … from each chunk's first span, so the span id
in a measure anchor can be checked against the exact text it names
before the run is written. Chunk selection reuses the ``~N`` / ``~A..B``
selector grammar — a ``pc<id>`` chunk handle from search rewrites to
``<slug>~<ord>`` and lands here, with or without ``kind=`` (dispatch
routes a chunk-handle view path to the owning kind).

The numbering is 0-based to match the anchor module's positional ids
(``"<chunk_id>.<n>"`` is the n-th sentence span, 0-based): the ``sN``
printed here is the span the measure anchor's ``span: 'sN'`` should
name. Raw-offset anchors (``[chunk, start, end]``) are the extraction
pipeline's own form and need no read side.

Runtime reserves its suffix before fitting at a sentence boundary.
A sentence larger than the page gets a marked excerpt, and the next
window starts at the following span. Numbering stays chunk-local.

The write side now resolves ``s<N>`` itself
(:func:`precis.store._measures_ops._span_range`, gr728422): the receipt's
``anchor_matched`` / ``anchor_mismatch`` checks the literal against the
sentence(s) the span names — a wrong ``s<N>`` flags ``anchor_mismatch``
and a span naming nothing in the chunk mismatches too, where before this
read was the ONLY span-level check (the write tested the whole chunk).
The numbering shown here is still the one to pick the ``s<N>`` by.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from precis._result_budget import body_bytes
from precis.errors import ResultBudgetExceeded
from precis.extract.anchors import sentence_spans
from precis.response import Response
from precis.utils import handle_registry
from precis.utils.next_block import render_next_section

if TYPE_CHECKING:
    from precis.store import Ref
    from precis.store.types import ChunkRow

_LEAD = (
    "sentences are numbered s0, s1, … by the anchor splitter "
    "(precis.extract.anchors) — anchor one in a measure as "
    "{'chunk': 'pc<id>', 'anchor_scheme': 'sentence', 'span': 'sN'}. "
    "Pick the sN here: the write receipt resolves sN against this "
    "numbering, so a wrong index reads anchor_mismatch. "
    "Recipe: get(kind='skill', id='precis-measure-help')"
)


def render_sentences_view(
    ref: Ref,
    blocks: list[ChunkRow],
    *,
    lo: int,
    hi: int,
    s_window: tuple[int, int | None] | None = None,
    banner: str | None = None,
    glyph_warnings: dict[int, list[str]] | None = None,
    correction_lines: dict[int, list[str]] | None = None,
) -> Response:
    """Numbered sentences of ``blocks`` (the ``~lo..hi`` selection).

    The ``s<N>`` printed per line is the ``span`` a measure sentence
    anchor names — same splitter, same 0-based numbering. The write
    receipt resolves the ``s<N>`` against this numbering
    (``anchor_mismatch`` when the literal is not in the named sentence),
    so the ``s<N>`` picked here is exactly what the receipt checks.

    ``s_window`` (the ``/sA..B`` path form) selects chunk-local span
    indices and KEEPS the original numbering — the resumable read the
    budget cut emits, never a renumbering. ``banner`` (retraction /
    glyph-health banner) prepends the title; ``glyph_warnings`` carries
    per-chunk source-caveat lines so a sentence quoted from this read
    keeps the warning it was read under (gr725288). ``correction_lines``
    (gr724751) carries per-chunk correction records, rendered beneath the
    chunk's sentences."""
    paper = handle_registry.format_handle(ref.kind, ref.id)
    selector = f"~{lo}" if lo == hi else f"~{lo}..{hi}"
    window_suffix = ""
    if s_window is not None:
        a, z = s_window
        window_suffix = f"/s{a}" if z == a else f"/s{a}..{z if z is not None else ''}"
        selector += window_suffix

    def chunk_call(ord_: int) -> str:
        rest = f"{paper}~{ord_}" if ord_ == hi else f"{paper}~{ord_}..{hi}"
        if window_suffix:
            return f"get(id='{rest}/sentences{window_suffix}')"
        return f"get(id='{rest}', view='sentences')"

    title = f"# {paper} — sentences {selector}"
    if banner:
        title = f"{banner}\n\n{title}"

    parts: list[str] = []
    continuations: list[str] = []
    total = 0
    for block_index, block in enumerate(blocks):
        spans = sentence_spans(block.text)
        label = handle_registry.chunk_head_label(
            ref.kind, block.id, block.ord, ref.slug
        )
        head = f"\n\n## {label}"
        for warning in (glyph_warnings or {}).get(block.id, []):
            head += f"\n{warning}"
        records = "".join(f"\n{r}" for r in (correction_lines or {}).get(block.id, []))
        start, stop = s_window if s_window is not None else (0, None)
        last = len(spans) - 1 if stop is None else min(stop, len(spans) - 1)

        if last < start:
            parts.append(f"{head}\n(no sentence spans in this chunk){records}")
            continuations.append(chunk_call(block.ord))
            continue
        total += last - start + 1
        for index in range(start, last + 1):
            left, right = spans[index]
            prefix = head if index == start else ""
            tail = records if index == last else ""
            parts.append(f"{prefix}\ns{index}: {block.text[left:right]}{tail}")
            if index == start:
                continuations.append(chunk_call(block.ord))
            else:
                call = f"get(id='{paper}~{block.ord}/sentences/s{index}..{last}')"
                if block_index + 1 < len(blocks):
                    # Finish this chunk before opening the later chunks.
                    call += f", then {chunk_call(blocks[block_index + 1].ord)}"
                continuations.append(call)

    first = blocks[0]
    pc = handle_registry.try_format(ref.kind, first.id, chunk=True)
    nav: list[tuple[str, str]] = []
    if pc is not None:
        nav.append((f"get(id='{pc}')", "read this chunk's raw text"))
    nav.append(
        (
            f"get(id='{paper}', view='toc')",
            "map the whole paper",
        )
    )
    nav.append(
        (
            f"get(id='{paper}', view='measures')",
            "see the measures already anchored to this paper",
        )
    )
    trailer = render_next_section(nav)

    body = f"{title}\n{_LEAD}" + "".join(parts) + trailer

    def fit(cap: int) -> str:
        if body_bytes(body) <= cap:
            return body
        # The fitted page keeps source caveats and spends its remaining bytes
        # on spans and recovery. The full read carries the recipe and navigation.
        used = body_bytes(title)
        keep = 0
        tails = [f"\n\n… sentence page cut\nrest: {call}" for call in continuations]
        tails.append("")
        for index, part in enumerate(parts):
            used += body_bytes(part)
            if used + body_bytes(tails[index + 1]) > cap:
                break
            keep = index + 1
        if keep:
            return title + "".join(parts[:keep]) + tails[keep]

        # Preserve the first sentence's label and source caveats, and advance
        # past the marked excerpt so an oversized sentence cannot stall a walk.
        prefix, sep, excerpt = parts[0].partition(f"\ns{(s_window or (0, None))[0]}: ")
        marker = "… [sentence excerpt]"
        available = cap - body_bytes(title + prefix + sep + marker + tails[1])
        if sep and available > 0:
            clipped = excerpt.encode("utf-8")[:available].decode(
                "utf-8", errors="ignore"
            )
            return title + prefix + sep + clipped.rstrip() + marker + tails[1]
        raise ResultBudgetExceeded(
            "the sentence label, source caveats and continuation cannot fit "
            f"under the {cap}-byte result budget",
            next="increase the result byte budget and retry the sentence read",
        )

    return Response(body=body, result_count=total, budget_fit=fit)
