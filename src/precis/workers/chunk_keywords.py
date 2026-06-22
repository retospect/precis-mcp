"""Per-chunk KeyBERT keywords with paper-scope abbreviation handling.

F20 (2026-06-05). Replaces the old ``segment_toc`` worker. Where the
old worker computed one cluster-of-chunks segmentation per paper and
stashed per-segment keybert in ``ref_segments``, this worker computes
per-CHUNK keybert keywords and stores them directly on ``chunks``:

* ``chunks.keywords TEXT[]``  — canonical lower-cased short / display
  form for GIN-indexed lexical filter + Jaccard-distance clustering
  at query time.
* ``chunks.keywords_meta JSONB`` — versioned rich envelope::

      {
        "version": "1.0",
        "embedder": "bge-m3",
        "keywords": [
          {"short": "MOF", "long": "metal-organic framework", "score": 0.82},
          {"short": null,  "long": "activation procedures",   "score": 0.61}
        ]
      }

Algorithm (per chunk):

1. Skip chunks below ``_MIN_CHUNK_CHARS`` (too short for stable
   KeyBERT — they fold into neighbours at TOC time via the
   empty-keyword Jaccard defence).
2. Skip chunks whose ``chunk_kind`` is on the non-content list
   (cards, tables, figures, equations, references). These would
   pollute the keyword set if included.
3. Load (or compute + lazy-stash) the paper's abbreviation dict from
   ``refs.meta['abbrevs']`` (Schwartz-Hearst).
4. Generate candidate phrases via RAKE.
5. For each candidate that matches a known short, embed its **long
   form** for scoring — gives bge-m3 the richer context (the
   embedding knows ``MOF ≈ metal-organic framework``).
6. Score by cosine vs. the chunk's pre-computed embedding.
7. Take top-K, dedupe abbrev pairs, store both forms in
   ``keywords_meta`` and the canonical short in ``keywords TEXT[]``.

Lazy upgrade: bump :data:`KEYWORDS_VERSION` to invalidate every
existing row's ``keywords_meta.version`` mismatch; the claim query
re-claims those chunks for re-extraction.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from precis.embedder import Embedder
from precis.utils.abbreviations import find as find_abbreviations
from precis.utils.rake import extract_keywords as _rake_phrases

log = logging.getLogger(__name__)

#: Bump on algorithm changes so existing rows get re-extracted.
KEYWORDS_VERSION = "1.0"

#: Below this length, KeyBERT doesn't have enough text for stable
#: scoring. We leave ``keywords`` NULL — the chunk folds into its
#: neighbour at TOC-clustering time (Jaccard defence: empty-keyword
#: chunks contribute zero distance to either side).
_MIN_CHUNK_CHARS = 150

#: Chunk kinds whose text is non-prose and would produce noise if
#: fed to KeyBERT. Cards (front matter), tables (cell salad),
#: equations (LaTeX), figures (terse captions), references (citation
#: lists). The set is small and stable; promote to ``chunk_kinds``
#: metadata if it ever grows.
_SKIP_KINDS: frozenset[str] = frozenset(
    {
        "card_authors",
        "card_combined",
        "card_title",
        "table",
        "equation",
        "figure",
        "references",
    }
)

#: Top-K KeyBERT keywords kept per chunk.
_TOP_K = 8

#: RAKE candidate pool size. Wider gives KeyBERT more to score and
#: rank from, at the cost of one extra batched embed call per chunk.
_RAKE_POOL = 40


def claim_chunks_without_keywords(
    conn: Connection, *, limit: int
) -> list[tuple[int, int, str, str | None]]:
    """Return up to ``limit`` chunks needing keyword extraction.

    A chunk is claimed when either its ``keywords`` array is NULL
    (never processed) or its ``keywords_meta->>'version'`` doesn't
    match :data:`KEYWORDS_VERSION` (algorithm changed). Filters out
    skip-kinds and too-short chunks at the SQL boundary so the
    worker doesn't pay for rows it would immediately discard.

    Returns rows of ``(chunk_id, ref_id, text, embedding)`` — the
    embedding is pulled from ``chunk_embeddings`` via JOIN so the
    worker can score candidates without re-embedding the chunk.
    ``embedding`` is ``None`` when no embedding exists yet (worker
    pass ordering: embed runs before keybert).
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    skip_list = list(_SKIP_KINDS)
    # Conv + draft blocks jump the queue. asa_bot's mid-range digest
    # tier renders ``chunks.keywords`` for each turn, and the draft
    # reader's view-slider (keywords / summary) needs them promptly on
    # the actively-edited write-up; without priority both wait behind
    # the ~1M-chunk paper backlog and render the text fallback for hours.
    # JOIN-ing refs adds one planner hop but the ref_id index makes it
    # nearly free.
    # ``meta->>'no_index' IS DISTINCT FROM 'true'`` filters out
    # ephemeral chunks (e.g. ``structure_draft`` annotation views
    # written by precis-dft's view_worker). NULL-safe: the vast
    # majority of chunks have no ``no_index`` key and still match.
    # Documented in ``docs/decisions/`` alongside PR 2 of the
    # plugin-substrate work; see the design doc at
    # ``docs/design/dft-phase-0-pr-2-substrate-hardening.md`` §2.3.
    sql = """
        SELECT c.chunk_id, c.ref_id, c.text, ce.vector::text
          FROM chunks c
          JOIN refs r ON r.ref_id = c.ref_id
          LEFT JOIN chunk_embeddings ce
            ON ce.chunk_id = c.chunk_id
           AND ce.embedder = %s
           AND ce.status = 'ok'
         WHERE c.chunk_kind <> ALL(%s)
           AND length(c.text) >= %s
           AND (c.meta->>'no_index') IS DISTINCT FROM 'true'
           AND (
                c.keywords IS NULL
             OR (c.keywords_meta->>'version') IS DISTINCT FROM %s
             -- re-derive when the chunk's text changed since the keywords
             -- were built (edited `draft` chunks, ADR 0033). Papers leave
             -- content_sha NULL → NULL-vs-NULL never fires.
             OR (c.keywords_meta->>'content_sha') IS DISTINCT FROM c.content_sha
           )
           AND ce.vector IS NOT NULL
           -- only once the embedding is current for this text — KeyBERT
           -- uses ce.vector, so wait for embed to refresh after an edit
           -- (else keywords from new text rank against a stale vector).
           AND ce.content_sha IS NOT DISTINCT FROM c.content_sha
         ORDER BY (CASE WHEN r.kind IN ('conv', 'draft') THEN 0 ELSE 1 END),
                  c.chunk_id
         LIMIT %s
           FOR UPDATE OF c SKIP LOCKED
    """
    rows = conn.execute(
        sql,
        ("bge-m3", skip_list, _MIN_CHUNK_CHARS, KEYWORDS_VERSION, limit),
    ).fetchall()
    out: list[tuple[int, int, str, str | None]] = []
    for r in rows:
        out.append((int(r[0]), int(r[1]), str(r[2]), r[3]))
    return out


def ensure_paper_abbrevs(conn: Connection, ref_id: int) -> dict[str, str]:
    """Return ``{SHORT: long}`` for the paper, populating it on first ask.

    First call for a ref runs Schwartz-Hearst over the ref's full
    body text and stashes the result on ``refs.meta['abbrevs']``.
    Subsequent calls read from JSONB directly.

    Empty dict when the ref has no detectable abbreviations.
    """
    row = conn.execute("SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)).fetchone()
    if row is None:
        return {}
    meta = row[0] or {}
    cached = meta.get("abbrevs")
    if cached is not None:
        # ``cached`` is the JSONB dict. Values may be plain strings
        # (legacy) or `{long, first_at}` envelopes; normalize to
        # ``{short: long}`` for the caller.
        out: dict[str, str] = {}
        for short, val in cached.items():
            if isinstance(val, str):
                out[short] = val
            elif isinstance(val, dict) and "long" in val:
                out[short] = str(val["long"])
        return out

    # Build the dict from the body text. We concatenate every body
    # chunk (ord >= 0, non-skip-kind) to give Schwartz-Hearst the
    # widest context — the parenthetical definition often lives in
    # the intro and the short form appears later.
    body_rows = conn.execute(
        """
        SELECT c.text
          FROM chunks c
         WHERE c.ref_id = %s
           AND c.ord >= 0
           AND c.chunk_kind <> ALL(%s)
         ORDER BY c.ord
        """,
        (ref_id, list(_SKIP_KINDS)),
    ).fetchall()
    full_text = "\n\n".join(r[0] or "" for r in body_rows)
    detected = find_abbreviations(full_text) if full_text else {}

    # Stash on meta. Use Jsonb adapter for the round-trip; meta
    # gets read back as a dict.
    new_meta = dict(meta)
    new_meta["abbrevs"] = dict(detected)
    conn.execute(
        "UPDATE refs SET meta = %s WHERE ref_id = %s",
        (Jsonb(new_meta), ref_id),
    )
    return detected


def extract_chunk_keywords(
    *,
    chunk_text: str,
    chunk_embedding: list[float],
    abbrevs: dict[str, str],
    embedder: Embedder,
) -> list[dict[str, Any]]:
    """Return the top-K KeyBERT keywords for one chunk.

    Pure compute (modulo embedder calls). Returns the rich form
    ``[{"short": str|None, "long": str, "score": float}, ...]``,
    descending by score.

    Algorithm:
      1. RAKE candidate generation (up to ``_RAKE_POOL`` phrases).
      2. For each candidate: if it matches a known short, embed its
         long form (richer context for bge-m3). Otherwise embed as-is.
      3. Cosine vs. chunk embedding; descending sort.
      4. Take top-K; merge abbrev pairs (``MOF`` + ``metal-organic
         framework`` collapse to one entry).
    """
    candidates = _rake_phrases(chunk_text, max_keywords=_RAKE_POOL)
    if not candidates:
        return []

    # Build the candidate→embedding_text mapping (long-form if known
    # short). We preserve the candidate-as-it-appeared for short-
    # form annotation downstream.
    short_to_long: dict[str, str] = {}
    long_to_short: dict[str, str] = {}
    for short, long_form in abbrevs.items():
        short_to_long[short.lower()] = long_form
        long_to_short[long_form.lower()] = short

    embed_texts: list[str] = []
    candidate_meta: list[tuple[str, str | None]] = []  # (long-form, short or None)
    for cand in candidates:
        cand_l = cand.lower()
        long_form = short_to_long.get(cand_l, cand)
        # If candidate is the long form of a known abbrev, attach the
        # short. If candidate IS a short, long is short_to_long[c].
        if cand_l in short_to_long:
            short = next(s for s in abbrevs if s.lower() == cand_l)
        elif cand_l in long_to_short:
            short = long_to_short[cand_l]
        else:
            short = None
        embed_texts.append(long_form)
        candidate_meta.append((long_form, short))

    # One batched embed call.
    cand_vecs = embedder.embed(embed_texts)

    # Score each candidate by cosine against the chunk embedding.
    chunk_norm = _l2_norm(chunk_embedding)
    scored: list[tuple[float, str, str | None]] = []
    for (long_form, short), vec in zip(candidate_meta, cand_vecs, strict=True):
        v_norm = _l2_norm(vec)
        if chunk_norm == 0.0 or v_norm == 0.0:
            score = 0.0
        else:
            score = sum(a * b for a, b in zip(chunk_embedding, vec, strict=True)) / (
                chunk_norm * v_norm
            )
        scored.append((float(score), long_form, short))

    scored.sort(key=lambda t: t[0], reverse=True)

    # Dedupe by abbrev pair: if the top-K contains both the short
    # ("MOF") and the long ("metal-organic framework") of the same
    # abbrev, keep just one entry (the higher-scoring). Walk top-down,
    # track seen abbrev-keys.
    seen_keys: set[str] = set()
    kept: list[dict[str, Any]] = []
    for score, long_form, short in scored:
        key = (short or long_form).lower()
        if key in seen_keys:
            continue
        seen_keys.add(key)
        kept.append({"short": short, "long": long_form, "score": score})
        if len(kept) >= _TOP_K:
            break
    return kept


def write_chunk_keywords(
    conn: Connection,
    chunk_id: int,
    *,
    keywords: list[dict[str, Any]],
    embedder_name: str,
) -> None:
    """Persist the keywords payload onto the chunk row.

    Writes both ``chunks.keywords`` (the canonical TEXT[] for GIN
    lookup + Jaccard) and ``chunks.keywords_meta`` (the rich JSONB
    envelope including version + embedder for the lazy-update path).
    """
    canonical = [
        (k.get("short") or k.get("long") or "").lower().strip() for k in keywords
    ]
    canonical = [c for c in canonical if c]
    meta = {
        "version": KEYWORDS_VERSION,
        "embedder": embedder_name,
        "keywords": keywords,
    }
    # Stamp the chunk's current content_sha into the envelope (column ref,
    # so it's the locked row's value — race-free) for the re-derive claim.
    # NB: use ``|| jsonb_build_object(...)`` rather than ``jsonb_set(...,
    # to_jsonb(content_sha), ...)``. When ``content_sha`` is SQL NULL (a
    # chunk that never had its hash stamped), ``to_jsonb(NULL)`` is SQL
    # NULL and ``jsonb_set`` then returns NULL — nulling the *entire*
    # envelope (version included), which makes the claim query
    # (``keywords_meta->>'version' IS DISTINCT FROM …``) re-claim the row
    # forever (a spin-loop). ``jsonb_build_object`` maps a NULL value to a
    # JSON ``null`` and the ``||`` merge preserves the rest of the envelope.
    conn.execute(
        """
        UPDATE chunks
           SET keywords = %s,
               keywords_meta =
                   %s::jsonb || jsonb_build_object('content_sha', content_sha)
         WHERE chunk_id = %s
        """,
        (canonical, Jsonb(meta), chunk_id),
    )


def run_chunk_keywords_pass(
    store: Any, embedder: Embedder, *, batch_size: int = 50
) -> dict[str, int]:
    """One pass over the chunk_keywords queue.

    Returns ``{"claimed": N, "ok": K, "failed": F}``. ``claimed`` is
    the number of chunks the claim query returned; ``ok``/``failed``
    sum to the same count. Pure pass — the caller decides whether to
    loop.
    """
    claimed = ok = failed = 0
    abbrev_cache: dict[int, dict[str, str]] = {}
    with store.pool.connection() as conn:
        rows = claim_chunks_without_keywords(conn, limit=batch_size)
        claimed = len(rows)
        for chunk_id, ref_id, text, vec_text in rows:
            try:
                if vec_text is None:
                    raise RuntimeError(
                        "chunk has no bge-m3 embedding yet — run `precis worker --only embed` first"
                    )
                # pgvector returns the vector as a string of the form
                # "[0.1, 0.2, ...]" when cast to text; parse it.
                chunk_vec = _parse_pgvector_text(vec_text)
                if ref_id not in abbrev_cache:
                    abbrev_cache[ref_id] = ensure_paper_abbrevs(conn, ref_id)
                abbrevs = abbrev_cache[ref_id]
                keywords = extract_chunk_keywords(
                    chunk_text=text,
                    chunk_embedding=chunk_vec,
                    abbrevs=abbrevs,
                    embedder=embedder,
                )
                write_chunk_keywords(
                    conn,
                    chunk_id,
                    keywords=keywords,
                    embedder_name=embedder.model,
                )
                ok += 1
            except Exception:
                log.exception("chunk_keywords: chunk_id=%s failed", chunk_id)
                failed += 1
    return {"claimed": claimed, "ok": ok, "failed": failed}


# ── helpers ─────────────────────────────────────────────────────────


def _l2_norm(vec: list[float]) -> float:
    """Pure-Python L2 norm. Returns 0.0 for the zero vector."""
    return math.sqrt(sum(x * x for x in vec))


def _parse_pgvector_text(s: str) -> list[float]:
    """Parse a pgvector text representation: ``"[0.1, 0.2, ...]"``."""
    s = s.strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    return [float(x) for x in s.split(",") if x.strip()]


__all__ = [
    "KEYWORDS_VERSION",
    "claim_chunks_without_keywords",
    "ensure_paper_abbrevs",
    "extract_chunk_keywords",
    "run_chunk_keywords_pass",
    "write_chunk_keywords",
]
