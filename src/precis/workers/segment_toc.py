"""Persistent segments + sentences worker.

Computes the discovery-layer artifacts for one ref and writes them
to ``ref_segments`` + ``ref_segment_sentences``. Idempotent — a
re-run for the same ref deletes the prior rows and overwrites.

Inputs (via :class:`precis.utils.toc.ChunksForToc` adapter):

* per-chunk body text
* per-chunk bge-m3 embeddings
* heading boundaries (for H2-mode segmentation)
* live embedder (for sentence-level embedding)

Outputs:

* one row per segment in ``ref_segments`` with the matryoshka-
  ordered keyword JSONB, denormalized ``forms`` array, and segment
  centroid vector.
* one row per body sentence in ``ref_segment_sentences`` with
  text + char offsets + per-sentence embedding + centroid score.

What's deliberately *not* in this MVP:

* per-keyword ``aliases[]`` enrichment via lemma + cosine collapse
  (current implementation emits the long form with an empty alias
  list — the GIN-indexed ``forms`` array still hits any surface
  form because we also add short/long pairs from the per-paper
  abbreviation legend).
* ``section_class`` population (column is nullable; future paper-
  specific classifier sets ``intro``/``methods``/``results``/…).
* status='failed' poison-pill handling (build_segments raises on
  failure; the runner catches and writes a status='failed' row).

Each follow-up can land without a migration.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from precis.utils.abbreviations import find as find_abbreviations
from precis.utils.boilerplate import classify_chunks
from precis.utils.keybert import (
    extract_keywords_semantic,
    mean_embedding,
    privileged_candidates,
)
from precis.utils.rake import extract_keywords
from precis.utils.segmentation import SEGMENTATION_VERSION, segment_dp
from precis.utils.sentences import SENTENCE_SPLITTER_VERSION, split_sentences
from precis.utils.toc import ChunksForToc


#: Bump when the worker's pipeline (rerank logic, sentence picking,
#: forms flattening) changes in a way that affects stored output.
#: Compared against ``ref_segments.extractor_version`` for lazy
#: invalidation.
EXTRACTOR_VERSION = "segment-toc-1"

#: Distinctiveness penalty weight. Applied as
#: ``score - λ * max(cos(phrase|sentence, sibling_centroid))``.
#: 0.3 = gentle penalty that demotes paper-wide-generic phrases
#: without overpowering the segment-centric base score.
_DISTINCTIVENESS_LAMBDA = 0.3

#: Per-segment keyword cap.
_KEYWORDS_PER_SEGMENT = 5

#: Per-segment central-sentence cap. Top-K by centroid score get
#: stored; full sentence set is also indexed for query-time rerank.
_CENTRAL_SENTENCES_PER_SEGMENT = 3

#: Candidate cap for KeyBERT — RAKE-top-N ∪ privileged.
_CANDIDATE_CAP = 150


# ── data shapes ─────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _KeywordRecord:
    """One keyword entry stored in ``ref_segments.keywords[]``."""

    long: str
    short: str | None
    aliases: list[str]
    score: float


@dataclass(frozen=True, slots=True)
class _SentenceRecord:
    """One row staged for ``ref_segment_sentences``."""

    sentence_idx: int
    text: str
    chunk_pos: int
    char_offset: int
    centroid_score: float
    embedding: list[float]


@dataclass(frozen=True, slots=True)
class _SegmentRecord:
    """One row staged for ``ref_segments`` with its sentences."""

    segment_idx: int
    pos_lo: int
    pos_hi: int
    heading: str | None
    mode: str  # 'h2' | 'embedding'
    centroid: list[float]
    keywords: list[_KeywordRecord]
    forms: list[str]
    sentences: list[_SentenceRecord]


# ── public entry ────────────────────────────────────────────────────


def build_segments(
    conn: Connection,
    *,
    ref_id: int,
    adapter: ChunksForToc,
) -> int:
    """Compute and persist segments + sentences for one ref.

    Returns the number of segments written. Raises if the adapter
    lacks the embedder / embeddings required for the semantic
    pipeline. Caller owns the transaction (and the failure-marker
    bookkeeping if any).
    """
    if adapter.embedder is None or adapter.embeddings is None:
        raise ValueError(
            "segment_toc worker requires both adapter.embedder and "
            "adapter.embeddings to be set"
        )

    chunks_text = list(adapter.chunks_text)
    embeddings = [list(v) for v in adapter.embeddings]
    h2_boundaries = list(adapter.h2_boundaries)
    embedder = adapter.embedder
    positions = (
        list(adapter.positions)
        if adapter.positions is not None
        else list(range(len(chunks_text)))
    )

    # 1. Boilerplate filter → body-only indices.
    classified = classify_chunks(chunks_text)
    body_indices = list(classified.body_indices)
    if not body_indices:
        # All boilerplate — wipe any prior rows and return.
        _delete_segments_for_ref(conn, ref_id)
        return 0

    body_text = [chunks_text[i] for i in body_indices]
    body_emb = [embeddings[i] for i in body_indices]

    # 2. Segmentation. H2 when coverage threshold met, else DP.
    segments_body, mode, headings = _pick_segments(
        body_indices=body_indices,
        body_text=body_text,
        body_emb=body_emb,
        h2_boundaries=h2_boundaries,
    )

    # 3. Per-paper abbreviation legend (Schwartz-Hearst).
    full_body_text = "\n\n".join(body_text)
    abbrevs = find_abbreviations(full_body_text)

    # 4. Centroids first — distinctiveness penalty needs all of them.
    seg_centroids = [
        mean_embedding([body_emb[i] for i in range(lo, hi + 1)])
        for (lo, hi) in segments_body
    ]
    paper_centroid = mean_embedding(body_emb)

    # 5. Paper-wide keywords. No exclude list — these define the
    # paper-wide row and feed per-segment exclusion to avoid dupes.
    paper_text = full_body_text
    paper_candidates = _candidates(paper_text, abbreviations=abbrevs)
    paper_keywords = _score_keywords(
        paper_text,
        target=paper_centroid,
        siblings=(),  # no penalty at paper level
        embedder=embedder,
        candidates=paper_candidates,
        top_k=_KEYWORDS_PER_SEGMENT,
        exclude=(),
        abbrevs=abbrevs,
    )

    # 6. Per-segment work: keywords (with distinctiveness penalty)
    # + central sentences (also with distinctiveness penalty against
    # sibling centroids).
    segment_records: list[_SegmentRecord] = []
    for seg_idx, ((lo, hi), centroid) in enumerate(
        zip(segments_body, seg_centroids, strict=True)
    ):
        seg_chunks_text = [body_text[i] for i in range(lo, hi + 1)]
        seg_chunks_emb = [body_emb[i] for i in range(lo, hi + 1)]
        # Map body-relative indices back to absolute chunk positions
        # (= ``ord`` on the chunks table = block.pos for papers).
        absolute_chunk_positions = [
            positions[body_indices[i]] for i in range(lo, hi + 1)
        ]
        seg_text = "\n\n".join(seg_chunks_text)

        # Keyword scoring against this segment's centroid.
        seg_candidates = _candidates(seg_text, abbreviations=abbrevs)
        sibling_centroids = tuple(
            c for j, c in enumerate(seg_centroids) if j != seg_idx
        )
        seg_keywords_raw = _score_keywords(
            seg_text,
            target=centroid,
            siblings=sibling_centroids,
            embedder=embedder,
            candidates=seg_candidates,
            top_k=_KEYWORDS_PER_SEGMENT,
            exclude=tuple(k.long for k in paper_keywords),
            abbrevs=abbrevs,
        )

        # Sentence extraction. Every body sentence gets stored; the
        # ``centroid_score`` column orders them so TOC-time picks
        # top-K and search-time picks query-aligned. Sentences embed
        # via the same bge-m3.
        sentences = _build_sentence_records(
            chunks_text=seg_chunks_text,
            chunk_positions=absolute_chunk_positions,
            centroid=centroid,
            sibling_centroids=sibling_centroids,
            embedder=embedder,
        )

        # Map back to absolute chunk positions for pos_lo/pos_hi.
        pos_lo = positions[body_indices[lo]]
        pos_hi = positions[body_indices[hi]]
        heading = headings[seg_idx]

        segment_records.append(
            _SegmentRecord(
                segment_idx=seg_idx,
                pos_lo=pos_lo,
                pos_hi=pos_hi,
                heading=heading,
                mode=mode,
                centroid=centroid,
                keywords=seg_keywords_raw,
                forms=_forms_from_keywords(seg_keywords_raw),
                sentences=sentences,
            )
        )

    # 7. Persist. DELETE-then-INSERT; cascade handles sentence rows.
    _delete_segments_for_ref(conn, ref_id)
    embedder_name = (
        adapter.embedder_name
        if adapter.embedder_name != "unknown"
        else getattr(embedder, "model", "unknown")
    )
    for record in segment_records:
        _insert_segment(
            conn,
            ref_id=ref_id,
            record=record,
            embedder_name=embedder_name,
        )
    return len(segment_records)


# ── helpers: segmentation ───────────────────────────────────────────


def _pick_segments(
    *,
    body_indices: list[int],
    body_text: list[str],
    body_emb: list[list[float]],
    h2_boundaries: Sequence[tuple[int, int, str]],
) -> tuple[list[tuple[int, int]], str, list[str | None]]:
    """Pick segments in body-relative index space.

    Returns ``(segments, mode, headings)`` where:

    * ``segments`` = list of ``(body_lo, body_hi)`` pairs (inclusive),
    * ``mode`` = ``'h2'`` or ``'embedding'``,
    * ``headings`` = same length as ``segments``; non-None when ``mode='h2'``.
    """
    # Project H2 boundaries (which live in absolute chunk-position
    # space) onto body-relative indices.
    body_idx_for_chunk: dict[int, int] = {
        chunk_i: body_i for body_i, chunk_i in enumerate(body_indices)
    }
    h2_in_body: list[tuple[int, int, str]] = []
    for (h_lo, h_hi, title) in h2_boundaries:
        body_lo = body_idx_for_chunk.get(h_lo)
        body_hi = body_idx_for_chunk.get(h_hi)
        if body_lo is None or body_hi is None:
            continue
        h2_in_body.append((body_lo, body_hi, title))

    # H2 mode requires enough sections covering enough of the body.
    body_n = len(body_text)
    if len(h2_in_body) >= 3 and body_n > 0:
        covered = sum((hi - lo + 1) for (lo, hi, _) in h2_in_body)
        if covered / body_n >= 0.8:
            return (
                [(lo, hi) for (lo, hi, _) in h2_in_body],
                "h2",
                [title for (_, _, title) in h2_in_body],
            )

    # Embedding mode: DP segmenter.
    if body_n == 0:
        return [], "embedding", []
    if body_n == 1:
        return [(0, 0)], "embedding", [None]
    K = _target_k(body_n)
    distances = _adjacent_distances(body_emb)
    dp_segments = segment_dp(distances, k=K)
    segments = [(s.start, s.end) for s in dp_segments]
    return segments, "embedding", [None] * len(segments)


def _target_k(body_n: int) -> int:
    """Choose K for DP segmentation. ~20 chunks per segment, clamped [3, 9].

    Additionally clamped to ``body_n`` so a tiny paper (≤2 body chunks)
    doesn't ask the DP segmenter for more segments than chunks (which
    raises ``ValueError``).
    """
    import math

    if body_n <= 0:
        return 0
    k = max(3, math.ceil(body_n / 20))
    return max(1, min(k, 9, body_n))


def _adjacent_distances(embeddings: list[list[float]]) -> list[float]:
    """Cosine distance between consecutive embeddings (1 - cos)."""
    if len(embeddings) < 2:
        return []
    out: list[float] = []
    for a, b in zip(embeddings, embeddings[1:]):
        out.append(1.0 - _cosine(a, b))
    return out


# ── helpers: keywords with distinctiveness ──────────────────────────


def _candidates(text: str, *, abbreviations: dict[str, str]) -> list[str]:
    """RAKE-top-N ∪ privileged patterns, capped at ``_CANDIDATE_CAP``."""
    rake_top = extract_keywords(text, max_keywords=_CANDIDATE_CAP)
    privileged = privileged_candidates(text, abbreviations=abbreviations.keys())
    seen: set[str] = set()
    out: list[str] = []
    for phrase in list(rake_top) + list(privileged):
        lc = phrase.strip().lower()
        if not lc or lc in seen:
            continue
        seen.add(lc)
        out.append(lc)
    return out


def _score_keywords(
    text: str,
    *,
    target: list[float],
    siblings: tuple[list[float], ...],
    embedder: Any,
    candidates: list[str],
    top_k: int,
    exclude: tuple[str, ...],
    abbrevs: dict[str, str],
) -> list[_KeywordRecord]:
    """Score candidates against ``target`` with optional distinctiveness penalty.

    ``siblings`` carries the centroids of other segments; non-empty
    triggers the ``score - λ * max(cos)`` re-ranking. Returns the
    top-``top_k`` enriched as :class:`_KeywordRecord`.
    """
    # First pass: pure-centroid top-K via the existing helper. Pull
    # 3× top_k here so the distinctiveness re-rank has headroom to
    # drop bad picks.
    plain = extract_keywords_semantic(
        text,
        target_embedding=target,
        embedder=embedder,
        top_k=max(top_k * 3, top_k),
        exclude=exclude,
        candidates=candidates,
    )
    if not plain:
        return []

    # Pull per-phrase embeddings so we can rerank against siblings
    # without a second embed call later. Single batch.
    phrase_embs = embedder.embed(plain)
    norm_target = _normalise(list(target))
    norm_siblings = [_normalise(list(s)) for s in siblings]

    scored: list[tuple[str, float, float]] = []
    for phrase, vec in zip(plain, phrase_embs, strict=True):
        vec_n = _normalise(list(vec))
        base = _dot(vec_n, norm_target)
        if norm_siblings:
            penalty = max(_dot(vec_n, s) for s in norm_siblings)
        else:
            penalty = 0.0
        adjusted = base - _DISTINCTIVENESS_LAMBDA * penalty
        scored.append((phrase, base, adjusted))

    scored.sort(key=lambda t: t[2], reverse=True)

    # Build _KeywordRecord with short-form enrichment from abbrev legend.
    long_to_short = {v: k for k, v in abbrevs.items()}
    short_to_long = abbrevs
    out: list[_KeywordRecord] = []
    for phrase, base, _adj in scored[:top_k]:
        # Recover display casing from the source.
        display = _recover_case(text, phrase)
        short = None
        # If the phrase IS a short form, use its long as canonical.
        if display in short_to_long or display.upper() in short_to_long:
            key = display if display in short_to_long else display.upper()
            short = key
            display = short_to_long[key]
        elif display in long_to_short:
            short = long_to_short[display]
        out.append(
            _KeywordRecord(
                long=display,
                short=short,
                aliases=[],  # MVP — see module docstring
                score=float(base),
            )
        )
    return out


def _forms_from_keywords(keywords: list[_KeywordRecord]) -> list[str]:
    """Flatten long + short + aliases into a deduplicated forms array."""
    seen: set[str] = set()
    out: list[str] = []
    for kw in keywords:
        for surface in (kw.long, kw.short, *kw.aliases):
            if not surface:
                continue
            if surface in seen:
                continue
            seen.add(surface)
            out.append(surface)
    return out


# ── helpers: sentences ──────────────────────────────────────────────


def _build_sentence_records(
    *,
    chunks_text: list[str],
    chunk_positions: list[int],
    centroid: list[float],
    sibling_centroids: tuple[list[float], ...],
    embedder: Any,
) -> list[_SentenceRecord]:
    """Split every chunk into sentences and rank by centroid score.

    Stores all sentences (not just top-K) so search-time query
    rerank works on the full set. Order in the returned list is
    by ``sentence_idx`` (= encounter order, stable within a
    segment); centroid_score sits on each record for downstream
    ``ORDER BY``.
    """
    # Collect (chunk_pos, char_offset_within_chunk, text).
    raw: list[tuple[int, int, str]] = []
    for chunk_text, chunk_pos in zip(chunks_text, chunk_positions, strict=True):
        for sent in split_sentences(chunk_text):
            text = sent.text.strip()
            if not text:
                continue
            raw.append((chunk_pos, sent.char_offset, text))
    if not raw:
        return []

    # Single batched embed call for all segment sentences.
    sentence_texts = [t for _, _, t in raw]
    sentence_embs = embedder.embed(sentence_texts)

    norm_centroid = _normalise(list(centroid))
    norm_siblings = [_normalise(list(s)) for s in sibling_centroids]
    records: list[_SentenceRecord] = []
    for idx, ((chunk_pos, char_offset, text), vec) in enumerate(
        zip(raw, sentence_embs, strict=True)
    ):
        vec_n = _normalise(list(vec))
        base = _dot(vec_n, norm_centroid)
        if norm_siblings:
            penalty = max(_dot(vec_n, s) for s in norm_siblings)
        else:
            penalty = 0.0
        score = base - _DISTINCTIVENESS_LAMBDA * penalty
        records.append(
            _SentenceRecord(
                sentence_idx=idx,
                text=text,
                chunk_pos=chunk_pos,
                char_offset=char_offset,
                centroid_score=float(score),
                embedding=list(vec),
            )
        )
    return records


# ── helpers: numerics ───────────────────────────────────────────────


def _normalise(vec: list[float]) -> list[float]:
    """L2-normalise; zero stays zero."""
    norm_sq = sum(x * x for x in vec)
    if norm_sq == 0.0:
        return vec
    inv = norm_sq**-0.5
    return [x * inv for x in vec]


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def _cosine(a: list[float], b: list[float]) -> float:
    return _dot(_normalise(list(a)), _normalise(list(b)))


def _recover_case(text: str, phrase_lc: str) -> str:
    """First-seen original casing for ``phrase_lc`` in ``text``."""
    idx = text.lower().find(phrase_lc)
    if idx == -1:
        return phrase_lc
    return text[idx : idx + len(phrase_lc)]


# ── helpers: DB writes ──────────────────────────────────────────────


def _delete_segments_for_ref(conn: Connection, ref_id: int) -> None:
    """Drop existing segment + sentence rows for this ref.

    Sentence rows cascade via the FK on segment_id.
    """
    conn.execute("DELETE FROM ref_segments WHERE ref_id = %s", (ref_id,))


def _insert_segment(
    conn: Connection,
    *,
    ref_id: int,
    record: _SegmentRecord,
    embedder_name: str,
) -> int:
    """Insert one segment + its sentences. Returns segment_id."""
    keywords_json = [
        {
            "long": kw.long,
            "short": kw.short,
            "aliases": list(kw.aliases),
            "score": kw.score,
        }
        for kw in record.keywords
    ]
    row = conn.execute(
        """
        INSERT INTO ref_segments
            (ref_id, segment_idx, pos_lo, pos_hi,
             heading, mode, section_class,
             segmentation_version, extractor_version, embedder_name,
             centroid, keywords, forms)
        VALUES
            (%s, %s, %s, %s,
             %s, %s, NULL,
             %s, %s, %s,
             %s, %s, %s)
        RETURNING segment_id
        """,
        (
            ref_id,
            record.segment_idx,
            record.pos_lo,
            record.pos_hi,
            record.heading,
            record.mode,
            SEGMENTATION_VERSION,
            EXTRACTOR_VERSION,
            embedder_name,
            record.centroid,
            Jsonb(keywords_json),
            list(record.forms),
        ),
    ).fetchone()
    assert row is not None
    segment_id = int(row[0])

    if record.sentences:
        # Batched insert via a managed cursor so it's closed cleanly
        # at the end of the with-block. ``executemany`` collapses the
        # round-trips for the ~50 sentences a typical segment carries.
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO ref_segment_sentences
                    (segment_id, sentence_idx, text, chunk_pos, char_offset,
                     centroid_score, embedding, sentence_splitter_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        segment_id,
                        s.sentence_idx,
                        s.text,
                        s.chunk_pos,
                        s.char_offset,
                        s.centroid_score,
                        s.embedding,
                        SENTENCE_SPLITTER_VERSION,
                    )
                    for s in record.sentences
                ],
            )
    return segment_id


def build_paper_adapter(
    store: Any,
    embedder: Any,
    ref_id: int,
) -> ChunksForToc:
    """Build a :class:`ChunksForToc` for a paper ref without a Hub.

    Mirrors :meth:`precis.handlers.paper.PaperHandler.chunks_for_toc`
    but takes a bare ``store`` + ``embedder`` so the runner can
    invoke it without instantiating the full handler graph.

    Block embeddings come from the LEFT JOIN on
    :class:`chunk_embeddings` performed by
    :meth:`list_blocks_for_ref`; H2 boundaries come from the same
    journal-template-filtered heading detector the handler uses.
    """
    from precis.handlers._paper_toc import detect_heading
    from precis.handlers.paper import _is_journal_template_heading
    from precis.ingest.text_chunker import CHUNKER_VERSION

    blocks = store.list_blocks_for_ref(ref_id, with_embedding=True)
    if not blocks:
        return ChunksForToc(
            chunks_text=(),
            embeddings=None,
            h2_boundaries=(),
        )
    blocks = sorted(blocks, key=lambda b: b.pos)
    chunks_text = tuple(b.text for b in blocks)
    positions = tuple(b.pos for b in blocks)

    embeddings: tuple[tuple[float, ...], ...] | None
    # Some chunk kinds (e.g. ``references``) aren't embedded by the
    # bge-m3 handler — citation lists don't need a semantic vector.
    # The segmenter only ever indexes into ``body_indices`` anyway, so
    # we substitute a zero-vector for the non-body holes to keep the
    # tuple aligned with ``chunks_text``. Refs with *zero* embedded
    # blocks (partial ingest still landing) still fall through to
    # ``None`` and get skipped by the runner.
    embedded = [b for b in blocks if b.embedding is not None]
    if not embedded:
        embeddings = None
    else:
        emb_dim = len(embedded[0].embedding)
        zero_vec: tuple[float, ...] = tuple([0.0] * emb_dim)
        embeddings = tuple(
            tuple(b.embedding) if b.embedding is not None else zero_vec
            for b in blocks
        )

    headings: list[tuple[int, str]] = []
    for b in blocks:
        h = detect_heading(b)
        if h is None or h.level not in (1, 2):
            continue
        if _is_journal_template_heading(h.title):
            continue
        headings.append((b.pos, h.title))

    h2_boundaries: list[tuple[int, int, str]] = []
    for i, (start, title) in enumerate(headings):
        end = headings[i + 1][0] - 1 if i + 1 < len(headings) else blocks[-1].pos
        h2_boundaries.append((start, end, title))

    return ChunksForToc(
        chunks_text=chunks_text,
        embeddings=embeddings,
        h2_boundaries=tuple(h2_boundaries),
        positions=positions,
        chunker_version=CHUNKER_VERSION,
        embedder_name=getattr(embedder, "model", "unknown"),
        embedder=embedder,
    )


def run_paper_segments_pass(
    store: Any,
    embedder: Any,
    *,
    limit: int = 32,
) -> dict[str, int]:
    """Process up to ``limit`` un-segmented paper refs.

    Returns ``{"claimed": N, "ok": K, "failed": F}`` for observability.
    Each ref runs in its own transaction so a single bad ref doesn't
    poison the batch. Failures land as a single warning per ref —
    poison-pill row support (``ref_segments.status='failed'``) is
    a follow-up; today's failure mode is "skip and try next pass."

    The runner only handles papers in v1; other kinds (skill,
    decision, …) get a no-op pass once they're persisted as refs
    and their handler exposes ``chunks_for_toc``.
    """
    import logging

    log = logging.getLogger(__name__)

    # One transaction per ref: claim_refs_without_segments takes
    # pg_try_advisory_xact_lock(ref_id) which releases on commit/rollback,
    # so processing must happen inside the same tx as the claim, and we
    # do one ref per tx to avoid pre-locking refs we won't get to. The
    # cost is ``limit`` round-trips; saves us a duplicate-key crash storm.
    ok = 0
    failed = 0
    total_claimed = 0
    for _ in range(limit):
        with store.pool.connection() as conn:
            rows = claim_refs_without_segments(conn, limit=1)
            if not rows:
                break
            ref_id = rows[0]
            total_claimed += 1
            try:
                adapter = build_paper_adapter(store, embedder, ref_id)
                if not adapter.chunks_text or adapter.embeddings is None:
                    # Nothing usable — either no body chunks, or partial
                    # ingest with missing embeddings. Skip without
                    # writing a poison-pill row.
                    continue
                build_segments(conn, ref_id=ref_id, adapter=adapter)
                conn.commit()
                ok += 1
            except Exception as exc:  # pragma: no cover — defensive
                conn.rollback()
                log.warning("segment_toc: ref_id=%s failed: %s", ref_id, exc)
                failed += 1
    return {"claimed": total_claimed, "ok": ok, "failed": failed}


def claim_refs_without_segments(
    conn: Connection,
    *,
    limit: int,
) -> list[int]:
    """Return up to ``limit`` ref_ids that lack any segment rows.

    Minimal runner-side helper — the same derived-queue idea the
    embedder uses (chunks LEFT JOIN chunk_embeddings), only at
    ref granularity. The caller is responsible for the per-ref
    setup (resolve the handler, build the
    :class:`~precis.utils.toc.ChunksForToc` adapter, call
    :func:`build_segments`).

    Only refs that have at least one body chunk are considered —
    ``ref_id``s with no chunks (metadata-only DOI registrations)
    have nothing to segment.

    No ``FOR UPDATE SKIP LOCKED`` here yet; v1 callers run
    single-process. Add advisory locking when a multi-worker setup
    materialises.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    # Advisory-xact-lock keyed on ref_id makes the claim race-safe across
    # workers: pg_try_advisory_xact_lock returns false (and the row is
    # filtered out) if another worker's claim transaction already holds
    # the lock for that ref. The lock auto-releases at end-of-tx, so the
    # caller MUST commit/rollback this conn before opening a new one to
    # process the rows — see run_paper_segments_pass for the dance.
    rows = conn.execute(
        """
        SELECT DISTINCT c.ref_id
          FROM chunks c
          LEFT JOIN ref_segments s ON s.ref_id = c.ref_id
         WHERE s.ref_id IS NULL
           AND c.ord >= 0                  -- body chunks only (cards are ord<0)
           AND c.chunk_kind <> 'references'
           AND pg_try_advisory_xact_lock(c.ref_id)
         ORDER BY c.ref_id
         LIMIT %s
        """,
        (limit,),
    ).fetchall()
    return [int(r[0]) for r in rows]


__all__ = [
    "EXTRACTOR_VERSION",
    "build_paper_adapter",
    "build_segments",
    "claim_refs_without_segments",
    "run_paper_segments_pass",
]
