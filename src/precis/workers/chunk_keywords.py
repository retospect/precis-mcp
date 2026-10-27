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

1. Skip chunks below ``MIN_CHUNK_CHARS`` (too short for stable
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

**Claim shape (gr520236).** Three tiered ``FOR UPDATE OF c SKIP
LOCKED`` queries, newest-first within each: (1) ``conv``/``draft``
chunks — they jump the whole queue regardless of freshness; (2) the
fresh ``keywords IS NULL`` backlog, ``chunk_id DESC`` so the newest
ingests get labels first and the ``chunks_keywords_pending_idx``
partial index (migration 0261) yields only pending rows — the old
single ascending scan re-walked every already-done row between the
index's id-1 end and the live backlog on every claim; (3) stale
re-derives (version / ``content_sha`` mismatch with keywords already
set), also newest-first.

**Pooled candidate embeds.** The batch pools every claimed chunk's
RAKE candidates into shared ``embedder.embed`` calls
(:data:`_EMBED_TEXTS_PER_CALL` texts each) instead of one call per
chunk — on the remote embedder most of a call's cost is transport and
admission, so per-chunk calls were the bulk of pass wall-clock.

**Single-runner advisory lock.** ``chunk_keywords`` runs on every
system-profile host on the same ~2s worker tick, and every host's
claim scans the same queue — without a fleet-wide lock, every host
would re-claim and re-derive the *same* batch every cycle
(fleet-normal, not a rare edge case: the claim's row lock vanishes at
commit, well before the slow per-chunk embedder work runs). Guarded by a **transaction**-scoped
``pg_try_advisory_xact_lock`` (:func:`precis.store.advisory.try_xact_advisory_lock`)
held on one dedicated, non-pooled connection spanning claim + every
chunk's processing (gr463966). The earlier session-scoped
``pg_try_advisory_lock`` was broken behind prod's pgbouncer
``pool_mode = transaction``: lock and unlock landed on different server
backends, so there was no exclusion and the lock leaked. An open
transaction pins one pgbouncer server connection for the whole pass, so
the lock lives and dies on one backend, and it ends with the transaction
(rollback in a ``finally``), so a ``DISCARD ALL`` reset or backend reuse
cannot leak it. This pass has no ceiling on wall-clock length (a slow
embedder, or a big batch), so the helper issues ``SET LOCAL
idle_in_transaction_session_timeout = 0`` — prod's ``300s`` kill (same
``ALTER ROLE`` block that sets ``lock_timeout=5s``) would otherwise end
the idle lock-holding transaction mid-run, silently dropping the lock.
``SET LOCAL`` is transaction-scoped; a session ``SET`` through pgbouncer
would poison other clients. A lock-acquire miss (another host
owns this tick) is a graceful skip — same ``claimed=0`` convention as
the ``EmbedderUnavailable`` branch, not a failure.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Any

from psycopg import Connection
from psycopg.errors import DeadlockDetected, LockNotAvailable
from psycopg.types.json import Jsonb

from precis.embedder import Embedder, EmbedderUnavailable
from precis.store.advisory import try_xact_advisory_lock
from precis.utils.abbreviations import find as find_abbreviations
from precis.utils.db_retry import retry_locked
from precis.utils.rake import extract_keywords as _rake_phrases

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

#: Bump on algorithm changes so existing rows get re-extracted.
KEYWORDS_VERSION = "1.0"

#: Fixed signed-bigint key for the single-runner advisory lock (same idiom
#: as ``paper_reconcile``/``openalex_enrich``/``paper_meta_enrich`` — see
#: ``paper_reconcile``'s module docstring). Arbitrary constant, namespaced
#: away from their keys.
_LOCK_KEY = 0x63_68_6E_6B_5F_6B_77_00 - 2**63  # "chnk_kw\x00", mapped signed

#: Below this length, KeyBERT doesn't have enough text for stable
#: scoring. We leave ``keywords`` NULL — the chunk folds into its
#: neighbour at TOC-clustering time (Jaccard defence: empty-keyword
#: chunks contribute zero distance to either side).
MIN_CHUNK_CHARS = 150

#: Chunk kinds whose text is non-prose and would produce noise if
#: fed to KeyBERT. Cards (front matter), tables (cell salad),
#: equations (LaTeX), figures (terse captions), references (citation
#: lists). The set is small and stable; promote to ``chunk_kinds``
#: metadata if it ever grows.
SKIP_KINDS: frozenset[str] = frozenset(
    {
        "card_authors",
        "card_combined",
        "card_description",
        "card_title",
        "table",
        "equation",
        "figure",
        "references",
        # Paper notes (gr521094) sit at ord<0 so the claim queries
        # never reach them anyway — listed so a kind scan reads as
        # classified, not missed.
        "paper_note",
    }
)

#: Top-K KeyBERT keywords kept per chunk.
_TOP_K = 8

#: RAKE candidate pool size. Wider gives KeyBERT more to score and
#: rank from, at the cost of one extra batched embed call per chunk.
_RAKE_POOL = 40


def _eligibility_sql(prefix: str) -> str:
    """The claim's static eligibility rendered with a column ``prefix``
    (``"c."`` for the claim's aliased chunks table, ``""`` for the
    ``chunks_keywords_pending_idx`` partial-index predicate, migration
    0261) — the ONE source for the literals both carry. The claim
    inlines this text rather than binding params: the planner only
    picks the partial index when it can prove the query's WHERE
    implies the index predicate, which takes the same literal
    constants, not $n."""
    kinds = ", ".join(f"'{k}'" for k in sorted(SKIP_KINDS))
    return (
        f"{prefix}chunk_kind NOT IN ({kinds}) "
        f"AND length({prefix}text) >= {MIN_CHUNK_CHARS} "
        f"AND ({prefix}meta->>'no_index') IS DISTINCT FROM 'true'"
    )


#: The eligibility fragment as the claim's SELECT uses it (aliased ``c.``).
_ELIGIBILITY_SQL = _eligibility_sql("c.")

#: Run the tier-3 stale re-derive claim only every Nth pass — but only
#: while no stale backlog is draining. The claim seq-scans
#: ``chunk_embeddings`` (~4.7M rows) on every underfilled pass, and a
#: stale re-derive delayed N×tick costs nothing user-visible (gr520236
#: review). The exception: a ``KEYWORDS_VERSION`` bump re-derives the
#: whole corpus and that backlog drains ONLY through tier 3, so pacing
#: it to every Nth pass stretches a full re-derive N× — while the
#: previous tier-3 claim returned rows it runs on every pass
#: (:data:`_stale_draining`), falling back to the paced cadence after
#: a claim comes back empty.
_STALE_CLAIM_EVERY = 10

#: Passes this process has run (:func:`run_chunk_keywords_pass` bumps
#: it); the stale claim fires when it hits a multiple of
#: :data:`_STALE_CLAIM_EVERY`, or on every pass while
#: :data:`_stale_draining` holds.
_pass_count = 0

#: True while the previous tier-3 claim returned rows — a live stale
#: backlog (a ``KEYWORDS_VERSION`` re-derive) drains on every pass.
#: Per-process like :data:`_pass_count`: whichever host holds the
#: single-runner lock paces off its own last observation.
_stale_draining = False


def is_keyword_eligible(chunk: Any) -> bool:
    """The structural half of the claim predicate, in Python: a
    non-:data:`SKIP_KINDS` kind, ``len(text) >= MIN_CHUNK_CHARS``, and
    not ``meta.no_index`` — the same filters
    :func:`claim_chunks_without_keywords` and
    :func:`keyword_eligible_chunk_count` express in SQL, for callers
    that already hold chunk objects (``toc_db``'s pending-labels
    notice). ``getattr``-safe: a stub without ``text``/``chunk_kind``
    fields is ineligible by the length floor."""
    if getattr(chunk, "chunk_kind", "paragraph") in SKIP_KINDS:
        return False
    if len(getattr(chunk, "text", "") or "") < MIN_CHUNK_CHARS:
        return False
    meta = getattr(chunk, "meta", None) or {}
    return str(meta.get("no_index", "")).lower() != "true"


def claim_chunks_without_keywords(
    conn: Connection,
    *,
    limit: int,
    include_stale: bool = True,
    stale_claimed: list[int] | None = None,
) -> list[tuple[int, int, str, str | None]]:
    """Return up to ``limit`` chunks needing keyword extraction.

    A chunk is claimed when either its ``keywords`` array is NULL
    (never processed) or its ``keywords_meta->>'version'`` doesn't
    match :data:`KEYWORDS_VERSION` (algorithm changed). Filters out
    skip-kinds and too-short chunks at the SQL boundary so the
    worker doesn't pay for rows it would immediately discard.

    Three tiered claims, each ``ORDER BY c.chunk_id DESC`` (gr520236):

    1. ``conv``/``draft`` chunks, fresh or stale. They jump the whole
       queue: asa_bot's mid-range digest tier renders
       ``chunks.keywords`` for each turn, and the draft reader's
       view-slider (keywords / summary) needs them promptly on the
       actively-edited write-up; without priority both wait behind the
       paper backlog and render the text fallback for hours.
    2. Fresh backlog (``keywords IS NULL``) — newest-first so fresh
       ingests get labels promptly, and cheap at any backlog depth:
       ``chunks_keywords_pending_idx`` (partial on ``keywords IS
       NULL`` plus the same eligibility literals the claim inlines via
       :func:`_eligibility_sql`) lets the DESC walk skip every
       already-done row AND the never-eligible bulk, where the
       previous single ascending claim re-scanned the done prefix
       from id 1 on every batch.
    3. Stale re-derives — keywords exist but the version or
       ``content_sha`` drifted (edited ``draft`` chunks; papers leave
       ``content_sha`` NULL so NULL-vs-NULL never fires). Skipped when
       ``include_stale`` is false — the claim seq-scans
       ``chunk_embeddings``, so :func:`run_chunk_keywords_pass` runs
       it every pass only while the previous tier-3 claim returned
       rows, else every :data:`_STALE_CLAIM_EVERY` passes.

    Returns rows of ``(chunk_id, ref_id, text, embedding)`` — the
    embedding is pulled from ``chunk_embeddings`` via JOIN so the
    worker can score candidates without re-embedding the chunk.
    ``embedding`` is ``None`` when no embedding exists yet (worker
    pass ordering: embed runs before keybert).

    ``stale_claimed``: when a list is passed, the number of rows the
    tier-3 claim added is appended to it (nothing is appended when
    tier 3 did not run — ``include_stale`` false or the earlier tiers
    already filled ``limit``). :func:`run_chunk_keywords_pass` reads
    it to pace the seq-scan off the backlog it actually saw.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    # ``meta->>'no_index' IS DISTINCT FROM 'true'`` filters out
    # ephemeral chunks (e.g. ``structure_draft`` annotation views
    # written by precis-dft's view_worker). NULL-safe: the vast
    # majority of chunks have no ``no_index`` key and still match.
    # Documented in ``docs/decisions/`` alongside PR 2 of the
    # plugin-substrate work; see the design doc at
    # ``dft-phase-0-pr-2-substrate-hardening`` (git-only) §2.3.
    select = """
        SELECT c.chunk_id, c.ref_id, c.text, ce.vector::text
          FROM chunks c
          JOIN refs r ON r.ref_id = c.ref_id
          LEFT JOIN chunk_embeddings ce
            ON ce.chunk_id = c.chunk_id
           AND ce.embedder = %s
           AND ce.status = 'ok'
         WHERE {eligibility}
           AND ce.vector IS NOT NULL
           -- only once the embedding is current for this text — KeyBERT
           -- uses ce.vector, so wait for embed to refresh after an edit
           -- (else keywords from new text rank against a stale vector).
           AND ce.content_sha IS NOT DISTINCT FROM c.content_sha
           AND {where}
         ORDER BY c.chunk_id DESC
         LIMIT %s
           FOR UPDATE OF c SKIP LOCKED
    """
    # re-derive when the chunk's text changed since the keywords were
    # built (edited `draft` chunks), or the algorithm version moved.
    pending = """(
                c.keywords IS NULL
             OR (c.keywords_meta->>'version') IS DISTINCT FROM %s
             OR (c.keywords_meta->>'content_sha') IS DISTINCT FROM c.content_sha
           )"""
    out: list[tuple[int, int, str, str | None]] = []

    def _claim(where: str, params: tuple[Any, ...]) -> None:
        rows = conn.execute(
            select.format(where=where, eligibility=_ELIGIBILITY_SQL),
            ("bge-m3", *params, limit - len(out)),
        ).fetchall()
        for r in rows:
            out.append((int(r[0]), int(r[1]), str(r[2]), r[3]))

    _claim(f"r.kind IN ('conv', 'draft') AND {pending}", (KEYWORDS_VERSION,))
    if len(out) < limit:
        _claim("r.kind NOT IN ('conv', 'draft') AND c.keywords IS NULL", ())
    if include_stale and len(out) < limit:
        before = len(out)
        _claim(
            f"r.kind NOT IN ('conv', 'draft') AND c.keywords IS NOT NULL AND {pending}",
            (KEYWORDS_VERSION,),
        )
        if stale_claimed is not None:
            stale_claimed.append(len(out) - before)
    return out


def keyword_eligible_chunk_count(conn: Connection, *, ref_id: int) -> int:
    """Body chunks (``ord >= 0``) of ``ref_id`` eligible for keyword
    extraction — the denominator the toc readiness line pairs with
    :func:`unkeyworded_chunk_count` (``keyworded N/<eligible>``).

    The structural half of :func:`claim_chunks_without_keywords`'s
    predicate: not a :data:`SKIP_KINDS` kind, ``length(text) >=
    MIN_CHUNK_CHARS``, not ``meta.no_index``. The claim's other
    clauses (a current embedding to score against, keywords missing or
    stale) are *processing* preconditions, not eligibility, so they are
    not part of this denominator — same split as
    :func:`~precis.workers.llm_summarize.eligible_chunk_count`.
    """
    row = conn.execute(
        """
        SELECT count(*)
          FROM chunks c
         WHERE c.ref_id = %(ref_id)s
           AND c.ord >= 0
           AND c.chunk_kind <> ALL(%(skip_kinds)s)
           AND length(c.text) >= %(min_chars)s
           AND (c.meta->>'no_index') IS DISTINCT FROM 'true'
        """,
        {
            "ref_id": ref_id,
            "skip_kinds": list(SKIP_KINDS),
            "min_chars": MIN_CHUNK_CHARS,
        },
    ).fetchone()
    return int(row[0]) if row else 0


def unkeyworded_chunk_count(conn: Connection, *, ref_id: int) -> int:
    """Eligible body chunks of ``ref_id`` still carrying NULL
    ``keywords`` — the pending backlog the toc readiness line's
    ``— keywords pending for N chunks`` tail names.

    ``keywords IS NULL`` only: a version- or ``content_sha``-stale row
    re-queues in :func:`claim_chunks_without_keywords` but keeps its old
    labels meanwhile, so it is not "pending" the way a never-labelled
    chunk is."""
    row = conn.execute(
        """
        SELECT count(*)
          FROM chunks c
         WHERE c.ref_id = %(ref_id)s
           AND c.ord >= 0
           AND c.chunk_kind <> ALL(%(skip_kinds)s)
           AND length(c.text) >= %(min_chars)s
           AND (c.meta->>'no_index') IS DISTINCT FROM 'true'
           AND c.keywords IS NULL
        """,
        {
            "ref_id": ref_id,
            "skip_kinds": list(SKIP_KINDS),
            "min_chars": MIN_CHUNK_CHARS,
        },
    ).fetchone()
    return int(row[0]) if row else 0


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
        (ref_id, list(SKIP_KINDS)),
    ).fetchall()
    full_text = "\n\n".join(r[0] or "" for r in body_rows)
    detected = find_abbreviations(full_text) if full_text else {}

    # Stash on meta as a jsonb MERGE (`||`), not a full-``meta``
    # overwrite: this write can race a concurrent meta writer for the
    # same ref (another chunk_keywords worker on a sibling host, or an
    # unrelated pass), and a bare `SET meta = %s` built from a
    # possibly-stale `meta` read above would silently clobber whatever
    # that writer just landed. The payload carries ONLY the ``abbrevs``
    # key, so the merge can only ever touch that one key.
    conn.execute(
        "UPDATE refs SET meta = coalesce(meta, '{}'::jsonb) || %s::jsonb "
        "WHERE ref_id = %s",
        (Jsonb({"abbrevs": dict(detected)}), ref_id),
    )
    return detected


def _keyword_candidates(
    chunk_text: str, abbrevs: dict[str, str]
) -> tuple[list[str], list[tuple[str, str | None]]]:
    """RAKE candidates for one chunk → ``(embed_texts, candidate_meta)``.

    ``embed_texts`` is what the embedder scores (the candidate's long
    form when it names a known abbreviation — richer context for
    bge-m3); ``candidate_meta[i]`` is the matching
    ``(long_form, short-or-None)``. Split from the scoring half so the
    batch path can pool every chunk's texts into shared embed calls
    (gr520236).
    """
    candidates = _rake_phrases(chunk_text, max_keywords=_RAKE_POOL)

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
        cand_short: str | None
        if cand_l in short_to_long:
            cand_short = next(s for s in abbrevs if s.lower() == cand_l)
        elif cand_l in long_to_short:
            cand_short = long_to_short[cand_l]
        else:
            cand_short = None
        embed_texts.append(long_form)
        candidate_meta.append((long_form, cand_short))
    return embed_texts, candidate_meta


def _topk_keywords(
    chunk_embedding: list[float],
    candidate_meta: list[tuple[str, str | None]],
    cand_vecs: list[list[float]],
) -> list[dict[str, Any]]:
    """Score embedded candidates against the chunk vector → top-K.

    Cosine per candidate, descending sort, top-K with abbrev-pair
    dedupe (``MOF`` + ``metal-organic framework`` collapse to one
    entry — the higher-scoring wins).
    """
    chunk_norm = _l2_norm(chunk_embedding)
    scored: list[tuple[float, str, str | None]] = []
    for (long_form, cand_short), vec in zip(candidate_meta, cand_vecs, strict=True):
        v_norm = _l2_norm(vec)
        if chunk_norm == 0.0 or v_norm == 0.0:
            score = 0.0
        else:
            score = sum(a * b for a, b in zip(chunk_embedding, vec, strict=True)) / (
                chunk_norm * v_norm
            )
        scored.append((float(score), long_form, cand_short))

    scored.sort(key=lambda t: t[0], reverse=True)

    # Dedupe by abbrev pair: walk top-down, track seen abbrev-keys.
    seen_keys: set[str] = set()
    kept: list[dict[str, Any]] = []
    for score, long_form, cand_short in scored:
        key = (cand_short or long_form).lower()
        if key in seen_keys:
            continue
        seen_keys.add(key)
        kept.append({"short": cand_short, "long": long_form, "score": score})
        if len(kept) >= _TOP_K:
            break
    return kept


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
    descending by score. Single-chunk convenience wrapper over
    :func:`_keyword_candidates` + :func:`_topk_keywords` — the batch
    path pools candidates across chunks into shared embed calls.
    """
    embed_texts, candidate_meta = _keyword_candidates(chunk_text, abbrevs)
    if not embed_texts:
        return []
    return _topk_keywords(chunk_embedding, candidate_meta, embedder.embed(embed_texts))


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

    Guarded by the same predicate :func:`claim_chunks_without_keywords`
    claims on (``keywords IS NULL OR`` version/``content_sha`` stale): the
    claim's row lock is released well before this write runs (see
    :func:`run_chunk_keywords_pass`'s claim-commit-first phase). With the
    pass's single-runner advisory lock (also documented there), two hosts
    processing the same chunk is a genuinely rare edge case now (a
    lock-handoff window right at pass boundary, not the fleet-normal
    collision it would be without that lock) — belt-and-braces, not the
    primary defense. Without the guard, whichever write lands second would
    clobber the first with equivalent-but-not-identical data; with it, a
    redundant write is a cheap no-op (0 rows affected, no error) instead.
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
    # forever (a spin loop). ``jsonb_build_object`` maps a NULL value to a
    # JSON ``null`` and the ``||`` merge preserves the rest of the envelope.
    conn.execute(
        """
        UPDATE chunks
           SET keywords = %s,
               keywords_meta =
                   %s::jsonb || jsonb_build_object('content_sha', content_sha)
         WHERE chunk_id = %s
           AND (
                keywords IS NULL
             OR (keywords_meta->>'version') IS DISTINCT FROM %s
             OR (keywords_meta->>'content_sha') IS DISTINCT FROM content_sha
           )
        """,
        (canonical, Jsonb(meta), chunk_id, KEYWORDS_VERSION),
    )


def _ensure_paper_abbrevs_retrying(store: Store, ref_id: int) -> dict[str, str]:
    """:func:`ensure_paper_abbrevs`, in its own short transaction wrapped
    in :func:`retry_locked` (see :func:`run_chunk_keywords_pass`'s
    docstring for why: its ``UPDATE refs`` write can contend with a
    sibling host's own meta write for the same ref)."""

    def _do() -> dict[str, str]:
        with store.pool.connection() as conn:
            result = ensure_paper_abbrevs(conn, ref_id)
            conn.commit()
            return result

    return retry_locked(_do, label=f"chunk_keywords abbrevs ref_id={ref_id}")


#: Texts per pooled ``embedder.embed`` call. The batch gathers every
#: claimed chunk's RAKE candidates (~``_RAKE_POOL`` each) into shared
#: calls of at most this size — one HTTP round-trip per ~512 texts
#: instead of one per chunk (gr520236: on the remote embedder,
#: transport + admission was most of a per-chunk call's cost). The
#: service batches internally; this only bounds request size.
_EMBED_TEXTS_PER_CALL = 512


def _run_chunk_keywords_batch(
    store: Store,
    embedder: Embedder,
    *,
    batch_size: int,
    include_stale: bool,
    stale_claimed: list[int] | None = None,
) -> dict[str, int]:
    """The claim + per-chunk work. Runs while the caller
    (:func:`run_chunk_keywords_pass`) holds the single-runner advisory
    lock for the whole pass — every DB access in here still goes through
    ``store``'s own pool (short, independent transactions), never the
    caller's lock-holding connection.

    Four phases (mirrors ``runner.run_handler_once``'s claim-commit-
    first discipline, and gripe: prod's ``lock_timeout=5s`` — see
    ``precis.utils.db_retry``'s module docstring):

    1. **Claim** (short txn): :func:`claim_chunks_without_keywords`
       leases the batch's ``FOR UPDATE OF c SKIP LOCKED`` rows, then
       commits immediately — releasing the lock well before the slow
       per-chunk work below runs, not held for the whole batch as the
       pre-fix single-transaction version did. ``include_stale`` gates
       the seq-scanning tier-3 claim (see ``_STALE_CLAIM_EVERY``);
       ``stale_claimed`` is forwarded to the claim so the caller can
       see how much tier 3 contributed.
    2. **Prepare** (per chunk, no embedder): parse the stored vector,
       :func:`_ensure_paper_abbrevs_retrying` (per ref, cached — its
       own short retried transaction), then RAKE candidates via
       :func:`_keyword_candidates`.
    3. **Pooled embed** (no DB connection held): every prepared chunk's
       candidate texts in shared ``embedder.embed`` calls of
       :data:`_EMBED_TEXTS_PER_CALL` texts (gr520236). A non-retryable
       failure drops to per-chunk calls so one bad text cannot fail the
       batch.
    4. **Score + write** (short txn per chunk, wrapped in
       :func:`retry_locked`): :func:`_topk_keywords` then
       :func:`write_chunk_keywords`, guarded (see its docstring) so a
       double-claim landing in the post-commit window — genuinely rare
       now that the whole pass is single-runner, see
       :func:`run_chunk_keywords_pass` — is a cheap no-op instead of
       two workers racing the same row.

    Returns ``{"claimed": N, "ok": K, "failed": F}``. ``claimed`` is the
    number of chunks the claim query returned; ``ok``/``failed`` do NOT
    always sum to ``claimed`` — a chunk that hits residual lock
    contention after :func:`retry_locked`'s own retry budget is counted
    as neither (see the ``LockNotAvailable``/``DeadlockDetected`` branch
    below): it's still unwritten, so it re-claims cleanly next pass.
    """
    with store.pool.connection() as conn:
        rows = claim_chunks_without_keywords(
            conn,
            limit=batch_size,
            include_stale=include_stale,
            stale_claimed=stale_claimed,
        )
        conn.commit()
    claimed = len(rows)

    ok = failed = 0
    abbrev_cache: dict[int, dict[str, str]] = {}
    # (chunk_id, chunk_vec, candidate_meta) aligned with `spans`, whose
    # entries mark each chunk's [start, end) slice of `embed_texts`.
    prepared: list[tuple[int, list[float], list[tuple[str, str | None]]]] = []
    spans: list[tuple[int, int]] = []
    embed_texts: list[str] = []
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
                abbrev_cache[ref_id] = _ensure_paper_abbrevs_retrying(store, ref_id)

            texts, cand_meta = _keyword_candidates(text, abbrev_cache[ref_id])
            spans.append((len(embed_texts), len(embed_texts) + len(texts)))
            embed_texts.extend(texts)
            prepared.append((chunk_id, chunk_vec, cand_meta))
        except (LockNotAvailable, DeadlockDetected) as exc:
            # Residual lock contention that outlasted retry_locked's own
            # budget. Expected fleet contention under prod's
            # lock_timeout=5s, not a crash: log at warning (not
            # log.exception), and don't count the chunk ``failed`` — it
            # never got written, so it stays claimable and picks back up
            # next pass instead of being permanently marked bad.
            log.warning(
                "chunk_keywords: chunk_id=%s deferred after residual lock "
                "contention: %s",
                chunk_id,
                exc,
            )
        except Exception:
            log.exception("chunk_keywords: chunk_id=%s failed", chunk_id)
            failed += 1

    cand_vecs: list[list[float]] | None = None
    try:
        pooled: list[list[float]] = []
        for i in range(0, len(embed_texts), _EMBED_TEXTS_PER_CALL):
            pooled.extend(embedder.embed(embed_texts[i : i + _EMBED_TEXTS_PER_CALL]))
        cand_vecs = pooled
    except EmbedderUnavailable as exc:
        # The embedder is transiently down/busy — the next call would
        # just hit the same wall. Defer the batch: the unwritten chunks
        # stay unclaimed (keywords still NULL) and re-claim next pass.
        # Don't count them ``failed`` (the chunks are fine) and report
        # ``claimed=0`` so the run-loop backs off the down embedder
        # instead of hot-looping it.
        log.warning(
            "chunk_keywords: embedder unavailable (%s); "
            "deferring %d remaining chunks this pass",
            exc,
            claimed - ok,
        )
        return {"claimed": 0, "ok": 0, "failed": 0}
    except Exception:
        # A non-retryable pooled failure (wire skew, one bad text) must
        # not sink the batch: fall back to per-chunk calls below so the
        # fault stays scoped to its own chunk.
        log.exception(
            "chunk_keywords: pooled embed failed — per-chunk fallback for %d chunks",
            len(prepared),
        )

    for (chunk_id, chunk_vec, cand_meta), (lo, hi) in zip(prepared, spans, strict=True):
        try:
            if cand_vecs is not None:
                vecs = cand_vecs[lo:hi]
            elif hi > lo:
                vecs = embedder.embed(embed_texts[lo:hi])
            else:
                # No candidates (RAKE found nothing scoreable) — an
                # embed call over an empty slice is a wasted round-trip.
                vecs = []
            keywords = _topk_keywords(chunk_vec, cand_meta, vecs)

            def _write(
                chunk_id: int = chunk_id, keywords: list[dict[str, Any]] = keywords
            ) -> None:
                with store.pool.connection() as wconn:
                    write_chunk_keywords(
                        wconn,
                        chunk_id,
                        keywords=keywords,
                        embedder_name=embedder.model,
                    )
                    wconn.commit()

            retry_locked(_write, label=f"chunk_keywords chunk_id={chunk_id}")
            ok += 1
        except EmbedderUnavailable as exc:
            # Per-chunk fallback hit the same transient wall — defer
            # the rest, same convention as the pooled branch above.
            log.warning(
                "chunk_keywords: embedder unavailable (%s); "
                "deferring %d remaining chunks this pass",
                exc,
                claimed - ok,
            )
            return {"claimed": 0, "ok": 0, "failed": 0}
        except (LockNotAvailable, DeadlockDetected) as exc:
            log.warning(
                "chunk_keywords: chunk_id=%s deferred after residual lock "
                "contention: %s",
                chunk_id,
                exc,
            )
        except Exception:
            log.exception("chunk_keywords: chunk_id=%s failed", chunk_id)
            failed += 1
    return {"claimed": claimed, "ok": ok, "failed": failed}


def run_chunk_keywords_pass(
    store: Store, embedder: Embedder, *, batch_size: int = 128
) -> dict[str, int]:
    """One pass over the chunk_keywords queue, single-runner across the
    fleet.

    Returns ``{"claimed": N, "ok": K, "failed": F}`` — see
    :func:`_run_chunk_keywords_batch`'s docstring for the shape and the
    claim/extract/write phases it runs. Pure pass — the caller decides
    whether to loop.

    Single-runner advisory lock (see the module docstring): ``chunk_keywords``
    runs on every system-profile host on the same ~2s tick, all ordering
    candidates by ``chunk_id`` — without exclusivity, every host would
    re-claim and re-derive the same batch every cycle. A dedicated
    connection (:func:`precis.store.advisory.try_xact_advisory_lock`) holds
    a **transaction-scoped** ``pg_try_advisory_xact_lock`` for the WHOLE
    pass (this function's entire body, not just the claim step) and rolls
    it back on exit (gr463966: session-scoped locks are broken behind
    pgbouncer transaction pooling). The helper lifts
    ``idle_in_transaction_session_timeout`` for that transaction only, so
    prod's ``300s`` cap cannot kill a slow pass's lock; if the process dies
    outright, pgbouncer closes the orphaned server connection and the lock
    goes with it. That dedicated connection ONLY ever holds the lock;
    :func:`_run_chunk_keywords_batch` does every real read/write through
    ``store``'s own pool, on separate connections. A lock-acquire miss
    (another host owns this tick) is a graceful skip — same ``claimed=0``
    shape as the ``EmbedderUnavailable`` branch, not a failure.
    """
    idle: dict[str, int] = {"claimed": 0, "ok": 0, "failed": 0}
    if not store.dsn:
        return idle

    global _pass_count, _stale_draining
    with try_xact_advisory_lock(store.dsn, _LOCK_KEY) as got:
        if not got:
            return idle  # another node owns this pass this cycle
        _pass_count += 1
        stale: list[int] = []
        result = _run_chunk_keywords_batch(
            store,
            embedder,
            batch_size=batch_size,
            include_stale=_stale_draining or (_pass_count % _STALE_CLAIM_EVERY) == 0,
            stale_claimed=stale,
        )
        # The tier-3 claim only reports when it actually ran; a pass
        # filled by the earlier tiers leaves _stale_draining untouched.
        if stale:
            _stale_draining = stale[0] > 0
        return result


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
    "MIN_CHUNK_CHARS",
    "SKIP_KINDS",
    "claim_chunks_without_keywords",
    "ensure_paper_abbrevs",
    "extract_chunk_keywords",
    "is_keyword_eligible",
    "keyword_eligible_chunk_count",
    "run_chunk_keywords_pass",
    "unkeyworded_chunk_count",
    "write_chunk_keywords",
]
