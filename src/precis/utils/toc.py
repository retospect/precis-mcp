"""Generic TOC renderer — H2-first, embedding-segment fallback.

One implementation, every TOC-capable kind. Handler-side adapter
provides ``(chunks, embeddings, h2_boundaries)``; this module
handles the policy (H2 vs embedding clustering), per-segment RAKE
keyword extraction with abbreviation substitution, the
shared-phrases footer, and TOON rendering.

Public entry:

* :func:`render` — build the full TOC body for one ref.

The handler call site is just:

.. code-block:: python

    response_body = toc.render(
        chunks=adapter.chunks,
        embeddings=adapter.embeddings,
        h2_boundaries=adapter.h2_boundaries,
        slug=ref.slug,
        kind=spec.kind,
        scope=None,  # or "~A..B" for recursive sub-segment
    )

Adding a new TOC-capable kind = implementing the
``chunks_for_toc(ref)`` adapter on its handler. Every layout
decision (column shape, K bounds, abbreviation handling, shared
phrases) is fixed here.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from precis.format import render_agent_table
from precis.utils.abbreviations import find as find_abbreviations
from precis.utils.abbreviations import substitute as substitute_abbreviations
from precis.utils.boilerplate import ChunkClass, classify_chunks
from precis.utils.keybert import (
    extract_keywords_semantic,
    mean_embedding,
    privileged_candidates,
)
from precis.utils.rake import extract_keywords, keyword_summary
from precis.utils.segmentation import (
    K_MAX,
    K_MIN,
    SEGMENTATION_VERSION,
    segment_dp,
    segment_embeddings,
)


# ── tunables (operator-only, no env vars per maintainer 2026-05-31) ──

#: Minimum number of H2 sections to consider H2-driven TOC. Below
#: this the algorithm falls through to embedding-segmentation.
_H2_MIN_SECTIONS = 3

#: H2-mode requires H2 sections to cover at least this fraction of
#: the body — a paper with one H2 covering 5 chunks out of 100 is
#: not meaningfully sectioned.
_H2_COVERAGE_THRESHOLD = 0.8

#: How many keywords per segment row in the TOC table.
_KEYWORDS_PER_PAPER_SEGMENT = 5
_KEYWORDS_PER_SKILL_SECTION = 3
#: Keywords in the paper-wide top row.
_KEYWORDS_FOR_PAPER_ROW = 5

#: Target chunks per body segment when picking K for DP. ~20 chunks
#: per segment for the typical bge-m3 chunk size (~500 words);
#: produces 3-9 segments for papers in the 60-180 chunk range,
#: clamped at the K_MIN / K_MAX boundaries from segmentation.
_CHUNKS_PER_SEGMENT_TARGET = 20

#: How many RAKE candidates to feed into KeyBERT per segment. RAKE
#: runs in microseconds on any segment size; bge-m3 embedding scales
#: linearly. Capping at ~150 caps the embed cost while still
#: surfacing the vast majority of phrases an unbounded KeyBERT
#: would pick — the privileged-pattern union (UPPER-CASE acronyms,
#: abbreviation legend, Title Case multi-word phrases) catches the
#: rare-but-central terms RAKE's frequency-only score would
#: otherwise drop. Operator override:
#: ``PRECIS_TOC_CANDIDATE_CAP``.
_DEFAULT_CANDIDATE_CAP = 150

#: H2 headings that match this set are treated as "stupid" — too
#: generic to convey what's in the section. Triggers keyword
#: augmentation on the row.
_STUPID_H2_GENERICS: frozenset[str] = frozenset(
    {
        "introduction",
        "intro",
        "background",
        "methods",
        "method",
        "results",
        "result",
        "discussion",
        "conclusion",
        "conclusions",
        "summary",
        "abstract",
        "references",
        "appendix",
        "acknowledgements",
        "acknowledgments",
    }
)


# ── data shapes ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class TocSegment:
    """One rendered row in the TOC.

    ``handle`` is the agent-pasteable ``id=`` for the segment
    (``slug~A..B`` for paper segments, ``slug~N`` for skill H2s).
    ``heading`` is non-empty when the row came from an H2 section.
    """

    handle: str
    heading: str
    keywords: list[str]


@dataclass(frozen=True)
class ChunksForToc:
    """Per-ref input that the generic TOC renderer expects.

    Adapters on each TOC-capable handler return this. Embeddings is
    ``None`` for kinds without per-chunk vectors (skills with the
    embedded index disabled, for instance) — the renderer falls
    back to H2 structure or a flat listing in that case.

    ``positions`` is the canonical address that each chunk has on
    the kind's surface — for papers, ``block.pos``; for skills,
    just the 0-based list index. Used by the renderer to emit
    ``slug~N`` handles that resolve correctly through the kind's
    ``get(id=...)``. Defaults to ``0..N-1`` when omitted, which
    works for any kind whose chunk-list indexing is already
    contiguous from zero (most of them).

    ``embedder`` (optional): a duck-typed embedder with ``.embed
    (texts) -> list[list[float]]``. When provided, the renderer
    uses KeyBERT-style semantic keyword extraction — phrases
    scored by cosine similarity to the segment / paper centroid.
    When ``None``, falls back to RAKE. Both paths apply the
    abbreviation legend the same way.
    """

    chunks_text: tuple[str, ...]
    embeddings: tuple[tuple[float, ...], ...] | None
    h2_boundaries: tuple[tuple[int, int, str], ...]
    #: Per-chunk canonical position used in ``slug~N`` handles. None
    #: means "use list indices 0..N-1".
    positions: tuple[int, ...] | None = None
    #: Identifier for the chunker version that produced ``chunks_text``.
    #: Bumped when chunking strategy changes; surfaced in the cache key.
    chunker_version: str = "1.0"
    #: Identifier for the embedder that produced ``embeddings``.
    #: Distinguishes bge-m3 from mock from a future swap.
    embedder_name: str = "unknown"
    #: Live embedder for semantic keyword scoring. Not part of cache
    #: keys (the cache key uses ``embedder_name`` instead).
    embedder: Any = None


# ── in-memory LRU cache ──────────────────────────────────────────────


_CACHE_CAPACITY = 256
_cache: "OrderedDict[tuple[Any, ...], str]" = OrderedDict()


def cache_clear() -> None:
    """Drop every cached TOC rendering. Mostly for tests."""
    _cache.clear()


def _cache_key(
    *,
    ref_id: Any,
    kind: str,
    chunker_version: str,
    embedder_name: str,
    scope: tuple[int, int] | None,
) -> tuple[Any, ...]:
    return (
        ref_id,
        kind,
        chunker_version,
        embedder_name,
        SEGMENTATION_VERSION,
        scope,
    )


def _cache_get(key: tuple[Any, ...]) -> str | None:
    if key not in _cache:
        return None
    # Move to end → mark as most-recently-used.
    _cache.move_to_end(key)
    return _cache[key]


def _cache_put(key: tuple[Any, ...], body: str) -> None:
    _cache[key] = body
    _cache.move_to_end(key)
    while len(_cache) > _CACHE_CAPACITY:
        _cache.popitem(last=False)  # evict LRU


# ── public render ───────────────────────────────────────────────────


def render(
    *,
    slug: str,
    kind: str,
    chunks_text: Sequence[str],
    embeddings: Sequence[Sequence[float]] | None,
    h2_boundaries: Sequence[tuple[int, int, str]] | None,
    scope: tuple[int, int] | None = None,
    positions: Sequence[int] | None = None,
    embedder: Any = None,
) -> str:
    """Render a TOC body for ``slug`` of ``kind``.

    Pipeline:

    1. **Boilerplate classification**. Head (title/abstract/authors),
       tail (references/acknowledgements/contact) chunks get labelled
       and rendered as named rows. Only body chunks feed into
       segmentation — outliers stop dominating the distance signal.
    2. **DP-uniform-cost segmentation** of the body chunks into
       ``K = clamp(K_MIN, K_MAX, ceil(body_chunks / 20))`` contiguous
       segments. Balanced by construction; no more "one giant
       catch-all + tiny outliers".
    3. **KeyBERT keyword scoring** when ``embedder`` is provided:
       per-segment keywords scored by cosine to segment centroid,
       paper-wide keywords scored against the whole-paper centroid.
       Paper-wide row at the top; per-segment rows exclude paper-wide
       phrases (so segment rows show what's *unique* to each segment).
       Falls back to RAKE when no embedder.
    4. **H2-first override**: when the source has ≥3 H2 headings
       covering ≥80 % of the body, H2 mode replaces embedding
       clustering. Boilerplate detection still runs.

    Returns body string ready to wrap in a Response. No leading or
    trailing newlines.
    """
    # Apply scope first so everything downstream sees the sub-slice.
    base_offset = 0
    if scope is not None:
        base_offset = scope[0]
        chunks_text = chunks_text[scope[0] : scope[1] + 1]
        if embeddings is not None:
            embeddings = list(embeddings)[scope[0] : scope[1] + 1]
        if positions is not None:
            positions = list(positions)[scope[0] : scope[1] + 1]
        if h2_boundaries:
            h2_boundaries = [
                (s - base_offset, e - base_offset, h)
                for s, e, h in h2_boundaries
                if s >= scope[0] and e <= scope[1]
            ]

    n = len(chunks_text)
    if n == 0:
        return f"# {slug} — empty"

    # Build the per-paper abbreviation dict once across the entire
    # (scoped) body so a SHORT defined in chunk 0 substitutes in
    # chunk 47 too. Per-paper scoping keeps the dict's noun
    # collisions safe.
    full_text = "\n".join(chunks_text)
    abbrevs = find_abbreviations(full_text)

    # Step 1: boilerplate. Skip on scoped views (sub-range zoom) —
    # the classifier's head/tail heuristics are paper-level and a
    # sub-range generally has none of the head/tail content.
    if scope is None:
        classified = classify_chunks(chunks_text)
        boilerplate_segments = _boilerplate_segments(classified.classes)
        body_indices: list[int] = list(classified.body_indices)
    else:
        boilerplate_segments = []
        body_indices = list(range(n))

    # Step 4 (H2-first override). Only fires when there's enough
    # explicit structure in the body to override embedding clustering.
    body_n = len(body_indices)
    h2_body_segments = _h2_segments_within_body(h2_boundaries, body_indices)
    use_h2 = (
        len(h2_body_segments) >= _H2_MIN_SECTIONS
        and _h2_coverage_body(h2_body_segments, body_n) >= _H2_COVERAGE_THRESHOLD
    )

    body_segments: list[tuple[int, int, str]]
    if use_h2:
        body_segments = h2_body_segments
    elif body_n >= K_MIN and embeddings is not None:
        # Step 2: DP segmentation of body chunks only.
        body_embeddings = [embeddings[i] for i in body_indices]
        k = _target_k(body_n)
        distances = _adjacent_distances(body_embeddings)
        boundaries = segment_dp(distances, k=k)
        # boundaries are positions into body_indices; remap to
        # absolute chunk indices.
        body_segments = [
            (body_indices[s.start], body_indices[s.end], "")
            for s in boundaries
        ]
    elif body_n > 0:
        # No embeddings — flat list of body chunks.
        body_segments = [(i, i, "") for i in body_indices]
    else:
        body_segments = []

    # Step 3: KeyBERT or RAKE per segment. The paper-wide row goes
    # first; per-segment rows exclude paper-wide phrases.
    use_keybert = (
        embedder is not None and embeddings is not None and body_n > 0
    )
    keywords_per_segment = (
        _KEYWORDS_PER_SKILL_SECTION
        if kind == "skill"
        else _KEYWORDS_PER_PAPER_SEGMENT
    )
    paper_keywords: list[str] = []
    per_segment_keywords: list[list[str]] = []

    if use_keybert and body_segments:
        # Paper-wide centroid over body chunks only — back-matter
        # and front-matter shouldn't dilute the topical signal.
        import os

        cap = int(
            os.environ.get("PRECIS_TOC_CANDIDATE_CAP", _DEFAULT_CANDIDATE_CAP)
        )
        body_text = "\n".join(chunks_text[i] for i in body_indices)
        body_text_subst = (
            substitute_abbreviations(body_text, abbrevs) if abbrevs else body_text
        )
        body_centroid = mean_embedding([embeddings[i] for i in body_indices])

        # Pre-filter: RAKE top-N + privileged-pattern union. The
        # union covers rare-but-central phrases RAKE's frequency
        # score wouldn't reach (UPPER-CASE acronyms like "MOF" or
        # "FTIR", Title Case multi-word terms, and the per-paper
        # abbreviation legend itself).
        paper_candidates = _pre_filtered_candidates(
            body_text_subst,
            cap=cap,
            abbreviations=abbrevs,
        )
        paper_keywords = extract_keywords_semantic(
            body_text_subst,
            target_embedding=body_centroid,
            embedder=embedder,
            top_k=_KEYWORDS_FOR_PAPER_ROW,
            candidates=paper_candidates,
        )
        paper_exclude = {kw.lower() for kw in paper_keywords}

        for start, end, _h in body_segments:
            seg_indices = list(range(start, end + 1))
            seg_text = "\n".join(chunks_text[i] for i in seg_indices)
            seg_text_subst = (
                substitute_abbreviations(seg_text, abbrevs)
                if abbrevs
                else seg_text
            )
            seg_centroid = mean_embedding(
                [embeddings[i] for i in seg_indices]
            )
            seg_candidates = _pre_filtered_candidates(
                seg_text_subst,
                cap=cap,
                abbreviations=abbrevs,
            )
            per_segment_keywords.append(
                extract_keywords_semantic(
                    seg_text_subst,
                    target_embedding=seg_centroid,
                    embedder=embedder,
                    top_k=keywords_per_segment,
                    exclude=paper_exclude,
                    candidates=seg_candidates,
                )
            )
    else:
        # RAKE fallback. Build paper-wide phrases the same way: top-N
        # over the whole body text, exclude them from per-segment.
        body_text = "\n".join(chunks_text[i] for i in body_indices)
        if abbrevs:
            body_text = substitute_abbreviations(body_text, abbrevs)
        if body_text.strip():
            paper_keywords = extract_keywords(
                body_text, max_keywords=_KEYWORDS_FOR_PAPER_ROW
            )
        paper_exclude_lc = {kw.lower() for kw in paper_keywords}

        for start, end, _h in body_segments:
            seg_text = "\n".join(chunks_text[i] for i in range(start, end + 1))
            if abbrevs:
                seg_text = substitute_abbreviations(seg_text, abbrevs)
            raw = extract_keywords(
                seg_text, max_keywords=keywords_per_segment * 3
            )
            filtered = [k for k in raw if k.lower() not in paper_exclude_lc]
            per_segment_keywords.append(filtered[:keywords_per_segment])

    # Build the TOON rows in reading order: boilerplate head, body
    # segments interleaved with any boilerplate that lives inside the
    # body range, boilerplate tail.
    rows = _build_rows(
        slug=slug,
        boilerplate_segments=boilerplate_segments,
        body_segments=body_segments,
        per_segment_keywords=per_segment_keywords,
        positions=positions,
        base_offset=base_offset,
        use_h2=use_h2,
    )

    schema = ["handle", "heading", "keywords"] if use_h2 else ["handle", "keywords"]

    # Paper-wide row prepended above the per-segment rows (full-range
    # handle). Skip it when scope is set (the sub-range's "paper-wide"
    # is the parent's segment row already).
    if scope is None and paper_keywords and body_segments:
        first_pos = (
            positions[body_indices[0]]
            if positions is not None
            else body_indices[0]
        )
        last_pos = (
            positions[body_indices[-1]]
            if positions is not None
            else body_indices[-1]
        )
        paper_row: dict[str, str] = {
            "handle": _handle_for(slug, first_pos, last_pos),
            "keywords": ", ".join(paper_keywords),
        }
        if use_h2:
            paper_row = {
                "handle": paper_row["handle"],
                "heading": "[whole paper]",
                "keywords": paper_row["keywords"],
            }
        rows.insert(0, paper_row)

    # Headline.
    seg_count = len(body_segments) + len(boilerplate_segments)
    if scope is not None:
        scope_lo = scope[0]
        scope_hi = scope[1]
        head = (
            f"# {slug}~{scope_lo}..{scope_hi} — sub-TOC "
            f"({n} chunks, {seg_count} segments)"
        )
    else:
        mode = "H2 sections" if use_h2 else (
            "segments via embedding clustering"
            if embeddings is not None
            else "flat listing"
        )
        head = f"# {slug} — TOC ({n} chunks, {seg_count} {mode})"

    lines = [head, "", render_agent_table(rows, schema=schema)]

    # Abbreviation legend. The shared-phrases footer is gone —
    # superseded by the paper-wide row at the top.
    if abbrevs:
        legend = ", ".join(f"{short} ({long})" for short, long in abbrevs.items())
        lines.append("")
        lines.append(f"Abbrevs: {legend}")

    return "\n".join(lines)


# ── helpers ──────────────────────────────────────────────────────────


def _boilerplate_segments(
    classes: Sequence[ChunkClass],
) -> list[tuple[int, int, str]]:
    """Collapse runs of identical non-BODY classes into (start, end, label).

    Example: classes = [HEAD, HEAD, BODY, BODY, BODY, REFERENCES,
    REFERENCES, CONTACT] → segments = [(0, 1, "[front-matter]"),
    (5, 6, "[references]"), (7, 7, "[contact]")]. BODY runs are
    skipped — they're segmented separately by the body pipeline.
    """
    out: list[tuple[int, int, str]] = []
    i = 0
    n = len(classes)
    while i < n:
        c = classes[i]
        if c == ChunkClass.BODY:
            i += 1
            continue
        j = i
        while j + 1 < n and classes[j + 1] == c:
            j += 1
        out.append((i, j, _label_for_class(c)))
        i = j + 1
    return out


def _label_for_class(c: ChunkClass) -> str:
    return {
        ChunkClass.HEAD: "[front-matter]",
        ChunkClass.REFERENCES: "[references]",
        ChunkClass.ACKNOWLEDGEMENTS: "[acknowledgements]",
        ChunkClass.CONTACT: "[contact]",
    }.get(c, "")


def _h2_segments_within_body(
    h2_boundaries: Sequence[tuple[int, int, str]] | None,
    body_indices: Sequence[int],
) -> list[tuple[int, int, str]]:
    """Map H2 boundaries to body-only segments.

    Returns segments using *absolute* chunk indices but only for
    headings that fall inside the body (head/tail boilerplate is
    rendered separately). Gaps between H2s inside the body remain
    body chunks; H2 segments that don't cover a contiguous body
    region are dropped.
    """
    if not h2_boundaries or not body_indices:
        return []
    body_set = set(body_indices)
    sorted_bounds = sorted(h2_boundaries, key=lambda t: t[0])
    out: list[tuple[int, int, str]] = []
    cursor = body_indices[0]
    end_body = body_indices[-1]
    for start, end, heading in sorted_bounds:
        if start < body_indices[0] or end > end_body:
            continue  # crosses out of body — skip
        if not all(i in body_set for i in range(start, end + 1)):
            continue  # H2 range includes non-body chunks (boilerplate)
        if start > cursor:
            out.append((cursor, start - 1, ""))
        out.append((start, end, heading))
        cursor = end + 1
    if cursor <= end_body:
        out.append((cursor, end_body, ""))
    return out


def _h2_coverage_body(
    segments: list[tuple[int, int, str]], body_n: int
) -> float:
    if body_n == 0:
        return 0.0
    covered = sum(e - s + 1 for s, e, h in segments if h)
    return covered / body_n


def _target_k(body_n: int) -> int:
    """Pick K (segment count) for the body. ``ceil(body_n / 20)``
    clamped to ``[K_MIN, K_MAX]`` and to ``body_n`` itself."""
    import math

    raw = max(1, math.ceil(body_n / _CHUNKS_PER_SEGMENT_TARGET))
    return max(K_MIN, min(K_MAX, min(body_n, raw)))


def _adjacent_distances(
    embeddings: Sequence[Sequence[float]],
) -> list[float]:
    """``1 - cos(e[i], e[i+1])`` for the DP segmenter's cost function."""
    import math

    out: list[float] = []
    for i in range(len(embeddings) - 1):
        a = embeddings[i]
        b = embeddings[i + 1]
        dot = sum(x * y for x, y in zip(a, b, strict=True))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            out.append(0.0)
        else:
            out.append(1.0 - dot / (norm_a * norm_b))
    return out


def _pre_filtered_candidates(
    text: str,
    *,
    cap: int,
    abbreviations: dict[str, str] | None,
) -> list[str]:
    """Build the KeyBERT candidate list: RAKE top-N ∪ privileged patterns.

    KeyBERT embedding scales linearly with the candidate count.
    RAKE — which we run anyway as the fallback — orders candidates
    by statistical prominence in microseconds. Capping RAKE at
    ``cap`` and feeding only that subset into KeyBERT cuts embed
    work by 5-10× on big segments.

    The privileged-pattern union catches the rare-but-central
    phrases RAKE's frequency-only score might drop (a paper's
    central concept mentioned only twice). See
    :func:`precis.utils.keybert.privileged_candidates`.

    Returns a deduplicated list (case-insensitive). Order doesn't
    matter — KeyBERT scores all of them and picks top-K.
    """
    rake_top = extract_keywords(text, max_keywords=cap)
    abbrev_keys: Iterable[str] = (
        abbreviations.keys() if abbreviations else ()
    )
    privileged = privileged_candidates(text, abbreviations=abbrev_keys)
    seen: set[str] = set()
    out: list[str] = []
    for phrase in list(rake_top) + list(privileged):
        lc = phrase.strip().lower()
        if not lc or lc in seen:
            continue
        seen.add(lc)
        out.append(lc)
    return out


def _build_rows(
    *,
    slug: str,
    boilerplate_segments: list[tuple[int, int, str]],
    body_segments: list[tuple[int, int, str]],
    per_segment_keywords: list[list[str]],
    positions: Sequence[int] | None,
    base_offset: int,
    use_h2: bool,
) -> list[dict[str, str]]:
    """Interleave boilerplate + body segments in reading order, build
    one TOON row per segment."""
    # Combine all segments, sorted by start position. Pair body
    # segments with their per-segment keywords by index.
    all_segments: list[tuple[int, int, str, list[str] | None]] = []
    for (start, end, label), kws in zip(
        body_segments, per_segment_keywords, strict=True
    ):
        all_segments.append((start, end, label, kws))
    for start, end, label in boilerplate_segments:
        all_segments.append((start, end, label, None))
    all_segments.sort(key=lambda t: t[0])

    rows: list[dict[str, str]] = []
    for start, end, label, kws in all_segments:
        if positions is not None:
            handle_start = positions[start]
            handle_end = positions[end]
        else:
            handle_start = start + base_offset
            handle_end = end + base_offset
        handle = _handle_for(slug, handle_start, handle_end)
        kw_str = ", ".join(kws) if kws else ""
        if use_h2:
            # H2 mode: heading column carries either the H2 text or
            # the boilerplate label.
            heading = label
            row = {
                "handle": handle,
                "heading": heading,
                "keywords": kw_str if not heading or _is_stupid_h2(heading) else "",
            }
        else:
            # Embedding mode: no heading column; boilerplate label
            # prepended to keywords so it's visible inline.
            display = (
                f"{label} {kw_str}".strip() if label else kw_str
            )
            row = {"handle": handle, "keywords": display}
        rows.append(row)
    return rows


def _is_stupid_h2(heading: str) -> bool:
    """True if the H2 heading is too generic to convey content."""
    if not heading:
        return False  # missing heading is handled separately
    stripped = heading.strip().lower()
    if not stripped:
        return True
    # Digits-only or numbered section labels ("4.2", "Section 1").
    tokens = stripped.split()
    if all(_token_is_numericky(t) or t in _STUPID_H2_GENERICS for t in tokens):
        return True
    return False


def _token_is_numericky(token: str) -> bool:
    """True for tokens like ``4.2``, ``2``, ``a)``, ``i.`` — anything
    a section numbering scheme would produce."""
    stripped = token.strip("().[]")
    return all(c.isdigit() or c == "." for c in stripped) and any(c.isdigit() for c in stripped)


def _heading_for_row(h2: str, fallback_keywords: list[str]) -> str:
    """Pick the column value for a heading row. Use the H2 verbatim
    unless it's stupid, in which case fall back to the first 2 RAKE
    keywords."""
    if h2 and not _is_stupid_h2(h2):
        return h2
    if fallback_keywords:
        return ", ".join(fallback_keywords[:2])
    return h2  # whatever generic / empty value we had


def _handle_for(slug: str, start: int, end: int) -> str:
    """Render the agent-pasteable handle for a segment.

    Single-chunk segments get ``slug~N``; multi-chunk get ``slug~A..B``.
    """
    if start == end:
        return f"{slug}~{start}"
    return f"{slug}~{start}..{end}"


def render_for_ref(
    *,
    ref_id: Any,
    slug: str,
    kind: str,
    adapter: ChunksForToc,
    scope: tuple[int, int] | None = None,
) -> str:
    """Cached handler-facing entry — call this from handlers.

    Wraps :func:`render` with an LRU cache keyed on the ref's
    identity + chunker/embedder versions + scope. Subsequent calls
    on the same ref with the same scope (e.g. an agent doing the
    paper TOC after a search hit returns from cache; an agent
    re-drilling into the same sub-range gets the sub-TOC from
    cache too) return in microseconds.

    The handler-side adapter (``Handler.chunks_for_toc``) is what
    produces the :class:`ChunksForToc`; this function only handles
    the cache + delegation to :func:`render`.
    """
    key = _cache_key(
        ref_id=ref_id,
        kind=kind,
        chunker_version=adapter.chunker_version,
        embedder_name=adapter.embedder_name,
        scope=scope,
    )
    cached = _cache_get(key)
    if cached is not None:
        return cached

    body = render(
        slug=slug,
        kind=kind,
        chunks_text=adapter.chunks_text,
        embeddings=adapter.embeddings,
        h2_boundaries=adapter.h2_boundaries,
        scope=scope,
        positions=adapter.positions,
        embedder=adapter.embedder,
    )
    _cache_put(key, body)
    return body


def segments_for_ref(
    *,
    ref_id: Any,
    kind: str,
    adapter: ChunksForToc,
) -> list[tuple[int, int]]:
    """Cached segmentation: ``[(canonical_start, canonical_end), ...]``.

    Returns ranges in the adapter's canonical address space
    (``adapter.positions`` if provided, else 0..N-1). Used by the
    search-hit cluster trailer to find which segment contains a
    given hit position.

    Keyed identically to :func:`render_for_ref` (minus the scope
    dimension) so a TOC view + a search-hit lookup share the same
    underlying compute. After the first TOC view of a paper,
    finding any chunk's containing segment is microseconds.
    """
    key = ("segments", ref_id, kind, adapter.chunker_version, adapter.embedder_name)
    cached_str = _cache_get(key)
    if cached_str is not None:
        return _decode_segment_list(cached_str)

    if adapter.embeddings is None:
        # No embeddings: every chunk is its own segment in the
        # canonical address space.
        canonical: list[tuple[int, int]] = []
        for i in range(len(adapter.chunks_text)):
            pos = adapter.positions[i] if adapter.positions is not None else i
            canonical.append((pos, pos))
    else:
        boundaries = segment_embeddings(adapter.embeddings)
        canonical = []
        for s in boundaries:
            if adapter.positions is not None:
                canonical.append(
                    (adapter.positions[s.start], adapter.positions[s.end])
                )
            else:
                canonical.append((s.start, s.end))
    _cache_put(key, _encode_segment_list(canonical))
    return canonical


def _encode_segment_list(segments: list[tuple[int, int]]) -> str:
    """Stable string encoding so the LRU stores a single string per
    entry rather than mixed types. Cheap reversible format."""
    return ";".join(f"{a},{b}" for a, b in segments)


def _decode_segment_list(s: str) -> list[tuple[int, int]]:
    if not s:
        return []
    return [tuple(int(x) for x in pair.split(",")) for pair in s.split(";")]  # type: ignore[misc]


def segment_containing(
    position: int, segments: list[tuple[int, int]]
) -> tuple[int, int] | None:
    """Find the segment whose canonical range contains ``position``.

    Returns ``None`` if no segment contains it (shouldn't happen for
    a position that came from the same adapter's chunks list, but
    defensive against ref-update races).
    """
    for start, end in segments:
        if start <= position <= end:
            return (start, end)
    return None


__all__ = [
    "ChunksForToc",
    "TocSegment",
    "cache_clear",
    "render",
    "render_for_ref",
    "segment_containing",
    "segments_for_ref",
]
