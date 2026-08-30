"""Universal search-result merge primitive.

Two distinct search shapes share the same "rank, dedupe, label, render"
pipeline:

1. **Multi-source for one kind.** Patents merge a local DB leg
   (``store.search_chunks_fused``) with a remote OPS leg
   (``ops.search``). Each stream produces hits; local takes priority,
   remote augments, slugs already seen locally are dropped.

2. **Multi-kind, one source each.** Cross-kind search
   (``kind='paper,memory'`` or ``kind='*'``) fans out to each kind's
   ``search_hits`` method, then fuses the streams by rank.

This module factors that common shape out:

- :class:`SearchHit` — the small typed record every contributor
  emits.  Free of rendering concerns; carries enough metadata that
  the renderer can compose a uniform per-hit block.
- :func:`merge_and_render` — takes ``list[list[SearchHit]]`` and
  produces a ``Response``.  Two modes:

  - ``priority``: preserve incoming stream order; later streams
    only contribute hits whose ``dedupe_key`` is unseen.  Patent's
    local+remote behaviour.
  - ``rrf``: reciprocal rank fusion — each stream contributes
    ``1/(60+rank)`` to a per-document score, summed across
    streams.  Standard cross-source fusion; the right default for
    cross-kind merge where streams are equally trustworthy.

The renderer follows the format already in use for paper / patent
search so single-kind callers can drop in the primitive without
changing their UX.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal

from precis.response import Response
from precis.utils import handle_registry
from precis.utils.search_header import format_search_headline

# Reciprocal-rank-fusion constant.  60 is the value Cormack et al.
# settled on as a robust default across IR benchmarks; precis uses
# the same constant for the lex+sem fusion inside
# ``store.search_chunks_fused`` so it's also the value agents
# implicitly already pay for.
_RRF_K: int = 60

# Multiplicative penalty applied to a document's fused RRF total,
# keyed by ``refs.retraction_status``. See docs/backlog/retraction-
# status-downstream.md item 1 for the policy this encodes:
#
# - ``retracted`` is HARD: sink it toward the bottom of the page, but
#   never exclude it — a retracted paper is frequently exactly what a
#   search is looking for (checking whether something was retracted),
#   and silent exclusion is indistinguishable from a broken index.
# - ``corrected`` / ``expression_of_concern`` are SOFT: the underlying
#   science is usually still sound, so only a mild nudge.
# - anything else (``None`` — the vast majority of the sparsely-
#   checked corpus) gets factor 1.0, i.e. no change at all. This is
#   the regression-risk case: an unchecked paper must rank exactly as
#   it did before this feature existed.
#
# Multiplicative, not additive: an RRF total is a sum of
# ``1/(60+rank)`` contributions, so its scale shrinks as rank worsens
# and grows with the number of streams a document appears in
# (roughly 0.008..0.016 per contributing stream, more streams ⇒
# bigger raw number). A fixed additive penalty would have to be
# calibrated against that shifting scale — too small and a retracted
# top-of-every-stream hit barely moves, too big and it starts
# clobbering the *relative* ordering of unrelated, non-retracted
# hits whose totals happen to sit near the penalty's magnitude.
# Scaling the total proportionally sidesteps that: the penalty is
# always "N% of this document's own fused relevance", so it demotes
# consistently regardless of how many streams voted for it.
_RETRACTION_SCORE_FACTOR: dict[str, float] = {
    "retracted": 0.02,
    "corrected": 0.85,
    "expression_of_concern": 0.85,
}

# Human-facing one-line labels, keyed by ``refs.retraction_status``.
# Mirrors ``handlers/paper.py::_RETRACTION_LABELS`` (that module isn't
# imported here to avoid a handlers→utils dependency inversion; the
# two small dicts are cheap enough to keep in sync by hand).
_RETRACTION_LABELS: dict[str, str] = {
    "retracted": "RETRACTED",
    "corrected": "CORRECTED",
    "expression_of_concern": "EXPRESSION OF CONCERN",
}


def _retraction_note(status: str | None) -> str | None:
    """One-line ``⚠ LABEL`` annotation for a hit's retraction status.

    Returns ``None`` for the unset/unknown case so callers can `if
    note:` without a second lookup. Deliberately terse (no reason/date
    — that detail lives on the paper record itself) since this renders
    inline in a ranked list, not a dedicated banner.
    """
    if not status:
        return None
    label = _RETRACTION_LABELS.get(status.strip().lower())
    return f"⚠ {label}" if label else None


_MergeMode = Literal["priority", "rrf"]


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One ranked search result, kind-agnostic.

    Producers (handlers, OPS clients, future MCP federations) emit
    these; the merge primitive renders them.  Free of any rendering
    concerns — strings the renderer cares about (header, preview,
    extra bib lines) are passed through verbatim, scores are pure
    numbers.

    Attributes:
        score: Raw relevance from the producer.  Higher = better.
            For lexical/RRF results this is the original tsrank or
            fused score.  Used for *intra-stream* ordering only —
            cross-stream fusion uses the rank position, not this
            number, so producers don't need to normalise.
        kind: Owning kind (``"paper"``, ``"patent"``, ``"memory"``).
            Surfaced as a per-hit label in cross-kind merges.
        slug: Citation handle for the underlying ref. ``None`` for
            kinds whose refs are numeric (``todo``, ``memory``, …);
            in that case the renderer falls back to ``"#{ref_id}"``.
        title: Display title — ref title or chunk heading.
        preview: One- or two-line excerpt; renderer trusts the
            producer to have already truncated and stripped.
        pos: Block position within the ref, when the hit is block-
            level.  ``None`` for ref-level hits (numeric kinds, the
            patent-handler list view, etc.).  Renders as
            ``slug~pos`` when set.
        reach: Optional source-stream label (``"local"``,
            ``"ops"``).  When set it appears in brackets after the
            handle; useful when one kind has multiple sources.  In
            cross-kind merges where ``reach`` is unset the kind
            itself is the label. (Vocabulary-compaction Stage D: named
            ``reach`` — the leg a hit came from — to keep the word
            ``source`` for provenance, "the artifact a datum comes
            from"; only patent/edgar's local-vs-remote search legs set
            this today.)
        extra_lines: Bib metadata that should appear between the
            title and the preview.  Used by patents (applicants,
            publication date) and papers (DOI, year) — kept as a
            tuple so each line is rendered verbatim and the
            primitive doesn't care about formatting.
        ref_id: Optional ref id, surfaced for numeric kinds whose
            refs have no slug (``todo``, ``memory``).  When set
            and ``slug`` is None the renderer prints
            ``#{ref_id}`` as the citation handle.
        dedupe_key: When set, hits sharing this key collapse into
            one in ``priority`` mode (later streams' duplicates
            drop) and contribute as one document in ``rrf`` mode
            (their RRF scores sum).  ``None`` disables dedup for
            this hit; useful when the producer can't compute a
            stable cross-source identifier.
    """

    score: float
    kind: str
    title: str
    preview: str
    slug: str | None = None
    pos: int | None = None
    reach: str | None = None
    extra_lines: tuple[str, ...] = ()
    ref_id: int | None = None
    dedupe_key: str | None = None
    # Optional fields for the cross-kind TOON shape. Producers populate
    # when they have a meaningful per-hit summary distinct from
    # ``title`` (e.g. a paper hit's block excerpt vs. its paper title);
    # the renderer falls back to deriving from ``title`` when unset.
    # See ``_render_toon_table``.
    summary: str | None = None
    remaining_words: int | None = None
    # Per-hit keyword list (canonical lower-case forms from
    # ``chunks.keywords``). Block-level handlers populate via
    # ``block_hits_to_search_hits`` reading ``block.keywords``; ref-level
    # handlers can populate from their own keyword surface. Empty tuple
    # means "no keywords for this hit" — surfaced as an empty cell in
    # ``view='keywords'``. See :func:`_render_keywords_table`.
    keywords: tuple[str, ...] = ()
    # the computed universal handle for this hit's chunk (or
    # ref) — ``<type-code><chunk_id|ref_id>`` (e.g. ``pc40``, ``me5``).
    # Preferred over the legacy ``slug~pos`` / ``#ref_id`` form by the
    # renderers. None for a kind with no handle code.
    uhandle: str | None = None
    # ``refs.retraction_status`` verbatim (``"retracted"`` /
    # ``"corrected"`` / ``"expression_of_concern"`` / ``None``).
    # Populated from the Ref producers already have on hand — no new
    # store query. Drives two things downstream: :func:`_merge_rrf`
    # applies a severity-scaled multiplicative penalty
    # (``_RETRACTION_SCORE_FACTOR``) so a retracted hit sinks without
    # being excluded, and :func:`_render_hit` /
    # :func:`_render_toon_table` surface a ``⚠`` annotation so the
    # downrank is never mistaken for a missing/broken result. ``None``
    # (the overwhelming majority — checking is opt-in and sparse) is a
    # complete no-op on both paths.
    retraction_status: str | None = None

    @property
    def handle(self) -> str:
        """Citation handle for the renderer.

        ``slug~pos`` when both are present; ``slug`` for ref-level
        hits with a slug; ``#{ref_id}`` for numeric refs; ``"?"``
        as the last-resort fallback so the renderer never crashes
        on a malformed hit.
        """
        if self.slug:
            return f"{self.slug}~{self.pos}" if self.pos is not None else self.slug
        if self.ref_id is not None:
            return f"#{self.ref_id}"
        return "?"


_OutputShape = Literal["markdown", "toon", "keywords"]


def merge_and_render(
    streams: list[list[SearchHit]],
    *,
    page_size: int,
    query: str | None = None,
    header_noun: str = "match",
    mode: _MergeMode = "priority",
    show_label: bool = True,
    empty_body: str | None = None,
    output_shape: _OutputShape = "markdown",
    link_summary: dict[int, dict[tuple[str, str], int]] | None = None,
) -> Response:
    """Merge ranked ``SearchHit`` streams into a single rendered Response.

    Args:
        streams: One list of hits per producer.  Each inner list is
            assumed already sorted best-first.  Empty streams are
            allowed and skipped.
        page_size: Cap on rendered hits.  Applied after merge + dedup.
        query: Optional query string echoed in the header
            (``# N matches for 'foo'``).
        header_noun: Singular form of what's being counted.
            ``"match"`` for cross-kind merges; ``"patent hit"`` for
            patents; etc.  Pluralised by
            :func:`precis.utils.search_header.format_search_headline`.
        mode: How streams combine.

            - ``"priority"`` (default): hits emitted in stream
              order; for each stream we drop hits whose
              ``dedupe_key`` was already seen.  Reproduces patent's
              "local first, then remote, dedupe by docdb id"
              behaviour.
            - ``"rrf"``: reciprocal-rank fusion. Each hit gets
              ``1/(60+rank_in_stream)``; duplicate ``dedupe_key``s
              across streams sum.  Hits without a ``dedupe_key``
              are treated as singletons.  Result sorted by total
              RRF score desc, ties broken by best raw ``score``.
        show_label: Whether to emit the ``[reach-or-kind]`` label
            after the citation handle.  ``False`` for single-kind
            single-source merges that don't need the marker.
        empty_body: Override message when every stream is empty
            (or every hit got dedup'd away).  Defaults to
            ``"no <header_noun> matches"``.
        link_summary: Batch ``{ref_id: {(direction, relation):
            count}}`` map (see ``Store.link_rel_summary_for_refs``),
            used only by the ``output_shape="toon"`` renderer to
            populate the ``links`` column.  Ignored by the markdown
            and keywords shapes.

    Returns:
        ``Response`` whose body is the rendered merge.  Cost is
        always ``None`` — cost-tracking is the producers' job, not
        the renderer's.
    """
    merged: list[SearchHit]
    if mode == "priority":
        merged = _merge_priority(streams)
    elif mode == "rrf":
        merged = _merge_rrf(streams)
    else:  # pragma: no cover — Literal type guards this
        raise ValueError(f"unknown merge mode: {mode!r}")

    if not merged:
        body = empty_body or _empty_body(header_noun, query)
        return Response(body=body)

    rendered = merged[:page_size]
    total_pre_cap = len(merged)

    # ``format_search_headline`` requires a concrete query string.
    # ``query=None`` is supported on the empty-body path only — when
    # we reach the renderer we always have hits, and the empty
    # string is a defensible fallback for the rare wildcard /
    # browse-all caller that omitted the query.
    lines = [
        format_search_headline(
            n_returned=len(rendered),
            total=total_pre_cap,
            noun=header_noun,
            query=query or "",
        )
    ]
    if output_shape == "toon":
        lines.append(_render_toon_table(rendered, link_summary=link_summary))
    elif output_shape == "keywords":
        lines.append(_render_keywords_table(rendered))
    else:
        for i, hit in enumerate(rendered, 1):
            lines.append(_render_hit(i, hit, show_label=show_label))
    return Response(body="\n".join(lines))


# ---------------------------------------------------------------------------
# Merge strategies
# ---------------------------------------------------------------------------


def _merge_priority(streams: list[list[SearchHit]]) -> list[SearchHit]:
    """Concatenate streams in order; drop ``dedupe_key`` collisions across
    streams while preserving every hit within a single stream.

    Cross-stream dedup is the point — patent's local stream emits N
    block-level hits per ref (all sharing the docdb-slug
    ``dedupe_key`` when the producer asks for ref-level dedupe),
    and the remote OPS stream contributes one hit per ref keyed on
    the same docdb id.  We want every local block to render, but
    the remote duplicate to drop — so dedupe is applied at the
    **stream boundary**, not within a single stream.
    """
    seen: set[str] = set()
    out: list[SearchHit] = []
    for stream in streams:
        local_keys_this_stream: set[str] = set()
        for hit in stream:
            key = hit.dedupe_key
            if key is not None and key in seen:
                continue
            out.append(hit)
            if key is not None:
                local_keys_this_stream.add(key)
        seen.update(local_keys_this_stream)
    return out


def _merge_rrf(streams: list[list[SearchHit]]) -> list[SearchHit]:
    """Reciprocal-rank fusion across streams.

    Each hit contributes ``1/(60+rank)`` to its document's total.
    Documents are identified by ``dedupe_key`` when set, else by
    object identity (every such hit is its own document).

    Returns hits sorted by total RRF score desc, ties broken by
    raw ``score`` desc.  We return one ``SearchHit`` per document
    — the first one encountered when iterating streams in order
    — so consumers see consistent metadata.

    Retracted / corrected / EoC documents get their *total*
    multiplied by :data:`_RETRACTION_SCORE_FACTOR` after fusion (see
    that constant's docstring for why multiplicative) — applied once
    per document using the representative hit's
    ``retraction_status``, not per-stream-contribution, so a paper
    that surfaces across multiple streams is penalised exactly once.
    """
    # group_id is either the dedupe_key string or a synthetic
    # ``"_:{stream}:{idx}"`` token for hits without a key. Using a
    # tuple as the dict key would work too, but strings keep the
    # ordering stable across Python versions.
    totals: dict[str, float] = {}
    raw_max: dict[str, float] = {}
    representatives: dict[str, SearchHit] = {}
    insertion_order: list[str] = []

    for stream_idx, stream in enumerate(streams):
        for rank, hit in enumerate(stream, 1):
            key = hit.dedupe_key or f"_:{stream_idx}:{rank}"
            contribution = 1.0 / (_RRF_K + rank)
            totals[key] = totals.get(key, 0.0) + contribution
            raw_max[key] = max(raw_max.get(key, -math.inf), hit.score)
            if key not in representatives:
                representatives[key] = hit
                insertion_order.append(key)

    for key, hit in representatives.items():
        status = (hit.retraction_status or "").strip().lower()
        factor = _RETRACTION_SCORE_FACTOR.get(status)
        if factor is not None:
            totals[key] *= factor

    # Sort by RRF total desc, break ties by raw score desc, then
    # by insertion order for determinism.
    def _sort_key(k: str) -> tuple[float, float, int]:
        return (-totals[k], -raw_max[k], insertion_order.index(k))

    return [representatives[k] for k in sorted(totals, key=_sort_key)]


# ---------------------------------------------------------------------------
# Per-hit rendering
# ---------------------------------------------------------------------------


_TOON_SUMMARY_MAX_CHARS = 180
_TOON_SENTENCE_TERMINATORS = (". ", "! ", "? ")


def salient_link_label(
    rel_counts: dict[tuple[str, str], int],
) -> tuple[int, str | None]:
    """Pick the (total link count, salient relation label) pair for a hit.

    ``rel_counts`` is one ref's slice of
    ``Store.link_rel_summary_for_refs`` — ``{(direction, relation):
    count}``. ``total`` is the sum across every direction/relation.

    Label choice: an inbound ``cites`` edge (someone cites this ref) is
    always the headline — "cited" — regardless of what else is linked,
    since it's the strongest cross-kind signal a search result can
    carry. Otherwise pick the highest-count relation that isn't the
    generic ``related-to`` catch-all (ties broken by ``(-count, dir,
    relation)`` for determinism) and render it: outbound uses the
    relation slug as-is, inbound uses the passive/inverse short form
    (``_INVERSE_REL``, falling back to ``<-<relation>`` when the
    inverse isn't known). ``None`` when every entry is ``related-to``
    (or ``rel_counts`` is empty) — the cell just shows the count.
    """
    total = sum(rel_counts.values())
    if rel_counts.get(("in", "cites"), 0) > 0:
        return total, "cited"

    candidates = [
        (direction, relation, count)
        for (direction, relation), count in rel_counts.items()
        if relation != "related-to"
    ]
    if not candidates:
        return total, None

    candidates.sort(key=lambda c: (-c[2], c[0], c[1]))
    direction, relation, _count = candidates[0]
    if direction == "out":
        label = relation
    else:
        from precis.handlers._links_render import _INVERSE_REL

        label = _INVERSE_REL.get(relation, f"<-{relation}")
    return total, label


def _derive_toon_summary(hit: SearchHit) -> tuple[str, int]:
    """Pick the (summary, remaining_words) cell pair for a hit.

    Explicit ``hit.summary`` / ``hit.remaining_words`` win when set.
    Otherwise derive from ``title`` the same way the per-kind TOON
    list view does: first sentence within budget, hard-cut + ellipsis
    as last resort, remaining_words counts the trimmed tail.
    """
    if hit.summary is not None:
        rw = hit.remaining_words if hit.remaining_words is not None else 0
        return hit.summary, rw

    body = hit.title or ""
    if not body:
        return "", 0
    first_line, sep, rest = body.partition("\n")
    first_line = first_line.rstrip()
    if sep and len(first_line) <= _TOON_SUMMARY_MAX_CHARS:
        return first_line, len(rest.split())
    head = body[:_TOON_SUMMARY_MAX_CHARS]
    cut = -1
    for term in _TOON_SENTENCE_TERMINATORS:
        idx = head.rfind(term)
        if idx > cut:
            cut = idx
    if cut > 0:
        summary = body[: cut + 1].rstrip()
        tail = body[cut + 1 :]
        return summary, len(tail.split())
    if len(body) > _TOON_SUMMARY_MAX_CHARS:
        summary = body[:_TOON_SUMMARY_MAX_CHARS].rstrip() + "…"
        return summary, len(body[_TOON_SUMMARY_MAX_CHARS:].split())
    return body.rstrip(), 0


def _render_toon_table(
    hits: list[SearchHit],
    *,
    link_summary: dict[int, dict[tuple[str, str], int]] | None = None,
) -> str:
    """Render cross-kind hits as one TOON table.

    Columns: ``id | summary | remaining_words | links``. No ``kind``
    column — ``id`` carries it: the universal handle when the hit was
    backfilled with one, else a kind-qualified fallback
    (``kind:slug~pos`` / ``kind:slug`` / ``kind:ref_id``) so the kind
    is never lost even without a handle. (The per-kind hit-count
    breakdown line the caller prepends already says which kinds ran —
    dropping the column from every row loses no information.)

    ``links`` is populated from ``link_summary`` — a batch
    ``{ref_id: {(direction, relation): count}}`` map, one query per
    page (see ``Store.link_rel_summary_for_refs``) — rendered via
    :func:`salient_link_label` as ``"{total} · {label}"`` (or bare
    ``total`` with no salient relation, or empty when zero/unknown).
    Per-hit age still isn't populated here — that's a per-kind list-
    view concern, not this cross-kind table's.
    """
    from precis.format import render_agent_table

    rows: list[dict[str, str]] = []
    for hit in hits:
        summary, remaining_words = _derive_toon_summary(hit)
        note = _retraction_note(hit.retraction_status)
        if note:
            # Prefix rather than a dedicated column: the flag is rare
            # (most refs are unchecked) so a column would render an
            # empty cell on ~every row — pure token waste for the
            # common case. The handle/id cell stays untouched
            # (agents paste it verbatim into get()); the summary cell
            # is prose already, so a prefix reads naturally there.
            summary = f"{note} — {summary}" if summary else note
        if hit.uhandle:
            ident = hit.uhandle  # universal handle when backfilled
        elif hit.slug:
            ident = (
                f"{hit.kind}:{hit.slug}~{hit.pos}"
                if hit.pos is not None
                else f"{hit.kind}:{hit.slug}"
            )
        elif hit.ref_id is not None:
            ident = f"{hit.kind}:{hit.ref_id}"
        else:
            ident = "?"
        links_cell = ""
        if link_summary is not None and hit.ref_id is not None:
            rel_counts = link_summary.get(hit.ref_id)
            if rel_counts is not None:
                total, label = salient_link_label(rel_counts)
                if total:
                    links_cell = f"{total} · {label}" if label else str(total)
        rows.append(
            {
                "id": ident,
                "summary": summary,
                "remaining_words": str(remaining_words),
                "links": links_cell,
            }
        )
    schema = ["id", "summary", "remaining_words", "links"]
    return render_agent_table(rows, schema=schema)


def _render_keywords_table(hits: list[SearchHit]) -> str:
    """Render hits as a compact ``id | kind | keywords`` TOON table.

    Designed for the ``view='keywords'`` cross-kind discovery shape —
    one row per hit, no preview text. Keywords come from
    ``SearchHit.keywords`` (populated by ``block_hits_to_search_hits``
    from each block's ``chunks.keywords`` array). Hits with no
    keywords still appear so the agent sees the ref exists; the
    ``keywords`` cell is empty for those rows.

    Token economy: ~6 chars per keyword + the row scaffolding. A
    50-hit page comes in at well under a quarter of the equivalent
    markdown-preview shape. Built for "what topics span the corpus"
    LLM scans where the bodies don't matter yet.
    """
    from precis.format import render_agent_table

    rows: list[dict[str, str]] = []
    for hit in hits:
        if hit.uhandle:
            ident = hit.uhandle  # universal handle when backfilled
        elif hit.slug:
            ident = f"{hit.slug}~{hit.pos}" if hit.pos is not None else hit.slug
        elif hit.ref_id is not None:
            ident = str(hit.ref_id)
        else:
            ident = "?"
        rows.append(
            {
                "id": ident,
                "kind": hit.kind,
                "keywords": ", ".join(hit.keywords) if hit.keywords else "",
            }
        )
    schema = ["id", "kind", "keywords"]
    return render_agent_table(rows, schema=schema)


def _render_hit(rank: int, hit: SearchHit, *, show_label: bool) -> str:
    label = ""
    if show_label:
        marker = hit.reach or hit.kind
        if marker:
            label = f"  [{marker}]"
    # emit the universal handle when known, else the legacy
    # slug~pos / ref_id form (still resolves on input; un-backfilled rows).
    ident = hit.uhandle or hit.handle
    parts = [f"\n## {rank}. {ident}{label}"]
    if hit.title:
        parts.append(f"_{hit.title}_")
    note = _retraction_note(hit.retraction_status)
    if note:
        parts.append(note)
    parts.extend(hit.extra_lines)
    if hit.preview:
        parts.append(hit.preview)
    return "\n".join(parts)


def _empty_body(header_noun: str, query: str | None) -> str:
    if query:
        return f"no {header_noun} matches for {query!r}"
    return f"no {header_noun} matches"


# ---------------------------------------------------------------------------
# Convenience adapters used by handlers
# ---------------------------------------------------------------------------


def block_hits_to_search_hits(
    triples: list[tuple[Any, Any, float]],
    *,
    kind: str,
    reach: str | None = None,
    excerpt: int = 200,
    extra_lines_for: Any = None,
    dedupe_by_handle: bool = True,
    ref_level_dedupe: bool = False,
) -> list[SearchHit]:
    """Adapt ``(block, ref, score)`` rows from the store into ``SearchHit``s.

    Used by paper / patent / oracle / markdown / conv block-level
    searches where the store returns ``list[tuple[Block, Ref, float]]``.

    Args:
        triples: Output of ``store.search_chunks_fused`` /
            ``store.search_chunks_lexical`` /
            ``store.search_chunks_semantic``.
        kind: Owning kind, used as the per-hit label fallback.
        reach: Optional override for the per-hit ``reach`` field
            — set this when the producer is one of multiple
            sources within a kind (e.g. patent's ``"local"`` /
            ``"ops"`` legs).
        excerpt: Max characters of block text to surface as the
            preview.  Truncation appends an ellipsis; matches the
            existing per-handler behaviour.
        extra_lines_for: Optional ``(block, ref) -> tuple[str, ...]``
            callable that emits per-hit bib metadata.  ``None``
            means "no extras".  Kept as a callable so producers
            can pull from ``ref.meta`` without this module having
            to know the schema.
        dedupe_by_handle: When ``True`` (default) the per-block
            handle (``kind:slug~pos``) becomes the ``dedupe_key``
            so the same block doesn't appear twice across streams.
            Disable when handles aren't stable across streams.
        ref_level_dedupe: When ``True`` the ``dedupe_key`` collapses
            to ``kind:slug`` (no ``pos``), so multiple block-level
            hits from the same ref share one identity.  Used when
            this stream merges with a stream that only knows the
            ref (e.g. patent's local block hits merging with OPS
            remote hits keyed on DOCDB id) — without this collapse
            the per-ref dedup the merge needs can't fire because
            every local block has a different ``slug~pos`` key.
    """
    out: list[SearchHit] = []
    for block, ref, score in triples:
        text = (getattr(block, "text", None) or "").strip()
        preview = text if len(text) <= excerpt else text[: excerpt - 1].rstrip() + "…"
        slug = getattr(ref, "slug", None)
        pos = getattr(block, "ord", None)
        extras: tuple[str, ...] = ()
        if extra_lines_for is not None:
            try:
                extras = tuple(extra_lines_for(block, ref))
            except Exception:
                extras = ()
        dedupe = None
        if dedupe_by_handle and slug is not None:
            if ref_level_dedupe or pos is None:
                dedupe = f"{kind}:{slug}"
            else:
                dedupe = f"{kind}:{slug}~{pos}"
        kw = getattr(block, "keywords", None) or ()
        out.append(
            SearchHit(
                score=float(score),
                kind=kind,
                title=getattr(ref, "title", "") or "",
                preview=preview,
                slug=slug,
                pos=pos,
                reach=reach,
                extra_lines=extras,
                ref_id=getattr(ref, "id", None),
                dedupe_key=dedupe,
                keywords=tuple(kw) if kw else (),
                # the chunk handle is computed from
                # ``(kind, chunk_id)`` — ``block.id`` is the chunk_id.
                uhandle=handle_registry.try_format(
                    kind, getattr(block, "id", None), chunk=True
                ),
                # ``getattr`` — most producers pass a real ``Ref``
                # (always carries the column, ``None`` when unchecked),
                # but the block-level fakes used in some tests don't.
                retraction_status=getattr(ref, "retraction_status", None),
            )
        )
    return out


def ref_hits_to_search_hits(
    pairs: list[tuple[Any, float]],
    *,
    kind: str,
    reach: str | None = None,
    preview_for: Any = None,
    excerpt: int = 140,
) -> list[SearchHit]:
    """Adapt ``(ref, rank)`` rows into ``SearchHit``s.

    For ref-level lexical search (``store.search_refs_lexical``)
    used by numeric kinds (todo/memory/gripe/anki) and the oracle
    title-only search.

    Args:
        pairs: Output of ``store.search_refs_lexical``.
        kind: Owning kind.
        reach: Optional source-stream label.
        preview_for: Optional ``(ref) -> str`` callable for the
            preview text.  Defaults to a truncated ref title.
        excerpt: Truncation cap for the default title-based preview.
    """
    out: list[SearchHit] = []
    for ref, rank in pairs:
        title = getattr(ref, "title", "") or ""
        if preview_for is not None:
            try:
                preview = preview_for(ref) or ""
            except Exception:
                preview = title
        else:
            preview = (
                title if len(title) <= excerpt else title[: excerpt - 1].rstrip() + "…"
            )
        slug = getattr(ref, "slug", None)
        ref_id = getattr(ref, "id", None)
        # Dedupe keyed on the canonical handle: "kind:slug" or "kind:#id"
        # so cross-stream merges of the same ref collapse.
        if slug is not None:
            dedupe = f"{kind}:{slug}"
        elif ref_id is not None:
            dedupe = f"{kind}:#{ref_id}"
        else:
            dedupe = None
        out.append(
            SearchHit(
                score=float(rank),
                kind=kind,
                title=title,
                preview=preview,
                slug=slug,
                pos=None,
                reach=reach,
                ref_id=ref_id,
                dedupe_key=dedupe,
                # the record handle is computed from
                # ``(kind, ref_id)``.
                uhandle=handle_registry.try_format(kind, ref_id, chunk=False),
                retraction_status=getattr(ref, "retraction_status", None),
            )
        )
    return out
